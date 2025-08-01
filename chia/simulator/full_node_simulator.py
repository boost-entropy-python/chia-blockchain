from __future__ import annotations

import asyncio
import itertools
import time
from collections.abc import Collection
from typing import Any, Optional, Union

import anyio
from chia_rs import BlockRecord, FullBlock, SpendBundle
from chia_rs.sized_bytes import bytes32
from chia_rs.sized_ints import uint8, uint32, uint64, uint128

from chia.consensus.augmented_chain import AugmentedBlockchain
from chia.consensus.block_body_validation import ForkInfo
from chia.consensus.block_rewards import calculate_base_farmer_reward, calculate_pool_reward
from chia.consensus.blockchain import BlockchainMutexPriority
from chia.consensus.multiprocess_validation import PreValidationResult, pre_validate_block
from chia.full_node.full_node import FullNode
from chia.full_node.full_node_api import FullNodeAPI
from chia.protocols.outbound_message import NodeType
from chia.rpc.rpc_server import default_get_connections
from chia.simulator.add_blocks_in_batches import add_blocks_in_batches
from chia.simulator.block_tools import BlockTools
from chia.simulator.simulator_protocol import FarmNewBlockProtocol, GetAllCoinsProtocol, ReorgProtocol
from chia.types.blockchain_format.coin import Coin
from chia.types.coin_record import CoinRecord
from chia.types.validation_state import ValidationState
from chia.util.config import lock_and_load_config, save_config
from chia.util.timing import adjusted_timeout, backoff_times
from chia.wallet.conditions import CreateCoin
from chia.wallet.transaction_record import LightTransactionRecord, TransactionRecord
from chia.wallet.util.tx_config import DEFAULT_TX_CONFIG
from chia.wallet.wallet import Wallet
from chia.wallet.wallet_node import WalletNode
from chia.wallet.wallet_state_manager import WalletStateManager


class _Default:
    pass


default = _Default()

timeout_per_block = 5


async def wait_for_coins_in_wallet(coins: set[Coin], wallet: Wallet, timeout: Optional[float] = 5):
    """Wait until all of the specified coins are simultaneously reported as spendable
    by the wallet.

    Arguments:
        coins: The coins expected to be received.
        wallet: The wallet expected to receive the coins.
    """
    with anyio.fail_after(delay=adjusted_timeout(timeout)):
        for backoff in backoff_times():
            spendable_wallet_coin_records = await wallet.wallet_state_manager.get_spendable_coins_for_wallet(
                wallet_id=wallet.id()
            )
            spendable_wallet_coins = {record.coin for record in spendable_wallet_coin_records}

            if coins.issubset(spendable_wallet_coins):
                return

            await asyncio.sleep(backoff)


class FullNodeSimulator(FullNodeAPI):
    def __init__(self, full_node: FullNode, block_tools: BlockTools, config: dict) -> None:
        super().__init__(full_node)
        self.bt = block_tools
        self.full_node = full_node
        self.config = config
        self.time_per_block: Optional[float] = None
        self.full_node.simulator_transaction_callback = self.autofarm_transaction
        self.use_current_time: bool = self.config.get("simulator", {}).get("use_current_time", False)
        self.auto_farm: bool = self.config.get("simulator", {}).get("auto_farm", False)

    def get_connections(self, request_node_type: Optional[NodeType]) -> list[dict[str, Any]]:
        return default_get_connections(server=self.server, request_node_type=request_node_type)

    async def get_all_full_blocks(self) -> list[FullBlock]:
        peak: Optional[BlockRecord] = self.full_node.blockchain.get_peak()
        if peak is None:
            return []
        blocks = []
        peak_block = await self.full_node.blockchain.get_full_block(peak.header_hash)
        if peak_block is None:
            return []
        blocks.append(peak_block)
        current = peak_block
        while True:
            prev = await self.full_node.blockchain.get_full_block(current.prev_header_hash)
            if prev is not None:
                current = prev
                blocks.append(prev)
            else:
                break

        blocks.reverse()
        return blocks

    async def autofarm_transaction(self, spend_name: bytes32) -> None:
        if self.auto_farm:
            self.log.info(f"Autofarm triggered by tx-id: {spend_name.hex()}")
            new_block = FarmNewBlockProtocol(self.bt.farmer_ph)
            await self.farm_new_transaction_block(new_block, force_wait_for_timestamp=True)

    async def update_autofarm_config(self, enable_autofarm: bool) -> bool:
        if enable_autofarm == self.auto_farm:
            return self.auto_farm
        else:
            self.auto_farm = enable_autofarm
            with lock_and_load_config(self.bt.root_path, "config.yaml") as config:
                if "simulator" in config:
                    config["simulator"]["auto_farm"] = self.auto_farm
                save_config(self.bt.root_path, "config.yaml", config)
            self.config = config
            if self.auto_farm is True and self.full_node.mempool_manager.mempool.total_mempool_cost() > 0:
                # if mempool is not empty and auto farm was just enabled, farm a block
                await self.farm_new_transaction_block(FarmNewBlockProtocol(self.bt.farmer_ph))
            return self.auto_farm

    async def get_all_coins(self, request: GetAllCoinsProtocol) -> list[CoinRecord]:
        """
        Simulates fetching all coins by querying coins added at each block height.

        Args:
            request: An object containing the `include_spent_coins` flag.

        Returns:
            A combined list of CoinRecords (including spent coins if requested).
        """
        coin_records: list[CoinRecord] = []
        current_height = 0

        # `.get_peak_height` can return `None`. We use -1 in that case to exit early
        max_block_height = self.full_node.blockchain.get_peak_height() or -1

        while current_height <= max_block_height:
            # Fetch coins added at the current block height
            records_at_height = await self.full_node.coin_store.get_coins_added_at_height(uint32(current_height))

            if not request.include_spent_coins:
                # Filter out spent coins if not requested
                records_at_height = [record for record in records_at_height if not record.spent]

            coin_records.extend(records_at_height)
            current_height += 1

        return coin_records

    async def revert_block_height(self, new_height: uint32) -> None:
        """
        This completely deletes blocks from the blockchain.
        While reorgs are preferred, this is also an option
        Note: This does not broadcast the changes, and all wallets will need to be wiped.
        """
        async with self.full_node.blockchain.priority_mutex.acquire(priority=BlockchainMutexPriority.high):
            peak_height: Optional[uint32] = self.full_node.blockchain.get_peak_height()
            if peak_height is None:
                raise ValueError("We can't revert without any blocks.")
            elif peak_height - 1 < new_height:
                raise ValueError("Cannot revert to a height greater than the current peak height.")
            elif new_height < 1:
                raise ValueError("Cannot revert to a height less than 1.")
            block_record: BlockRecord = self.full_node.blockchain.height_to_block_record(new_height)
            # remove enough data to allow a bunch of blocks to be wiped.
            async with self.full_node.block_store.transaction():
                # set coinstore
                await self.full_node.coin_store.rollback_to_block(new_height)
                # set blockstore to new height
                await self.full_node.block_store.rollback(new_height)
                await self.full_node.block_store.set_peak(block_record.header_hash)
                self.full_node.blockchain._peak_height = new_height
        # reload mempool
        await self.full_node.mempool_manager.new_peak(block_record, None)

    async def get_all_puzzle_hashes(self) -> dict[bytes32, tuple[uint128, int]]:
        # puzzle_hash, (total_amount, num_transactions)
        ph_total_amount: dict[bytes32, tuple[uint128, int]] = {}
        all_non_spent_coins: list[CoinRecord] = await self.get_all_coins(GetAllCoinsProtocol(False))
        for cr in all_non_spent_coins:
            if cr.coin.puzzle_hash not in ph_total_amount:
                ph_total_amount[cr.coin.puzzle_hash] = (uint128(cr.coin.amount), 1)
            else:
                dict_value: tuple[uint128, int] = ph_total_amount[cr.coin.puzzle_hash]
                ph_total_amount[cr.coin.puzzle_hash] = (uint128(cr.coin.amount + dict_value[0]), dict_value[1] + 1)
        return ph_total_amount

    async def farm_new_transaction_block(
        self, request: FarmNewBlockProtocol, force_wait_for_timestamp: bool = False
    ) -> FullBlock:
        ssi = self.full_node.constants.SUB_SLOT_ITERS_STARTING
        diff = self.full_node.constants.DIFFICULTY_STARTING
        async with self.full_node.blockchain.priority_mutex.acquire(priority=BlockchainMutexPriority.high):
            self.log.info("Farming new block!")
            current_blocks = await self.get_all_full_blocks()
            if len(current_blocks) == 0:
                genesis = self.bt.get_consecutive_blocks(uint8(1))[0]
                future = await pre_validate_block(
                    self.full_node.blockchain.constants,
                    AugmentedBlockchain(self.full_node.blockchain),
                    genesis,
                    self.full_node.blockchain.pool,
                    None,
                    ValidationState(ssi, diff, None),
                )
                pre_validation_result: PreValidationResult = await future
                fork_info = ForkInfo(-1, -1, self.full_node.constants.GENESIS_CHALLENGE)
                await self.full_node.blockchain.add_block(
                    genesis,
                    pre_validation_result,
                    self.full_node.constants.SUB_SLOT_ITERS_STARTING,
                    fork_info,
                )

            peak = self.full_node.blockchain.get_peak()
            assert peak is not None
            curr: BlockRecord = peak
            while not curr.is_transaction_block:
                curr = self.full_node.blockchain.block_record(curr.prev_hash)
            current_time = self.use_current_time
            time_per_block = self.time_per_block
            assert curr.timestamp is not None
            if int(time.time()) <= int(curr.timestamp):
                if force_wait_for_timestamp:
                    await asyncio.sleep(1)
                else:
                    current_time = False
            mempool_bundle = self.full_node.mempool_manager.create_bundle_from_mempool(curr.header_hash)
            if mempool_bundle is None:
                spend_bundle = None
            else:
                spend_bundle = mempool_bundle[0]

            current_blocks = await self.get_all_full_blocks()
            target = request.puzzle_hash
            more = self.bt.get_consecutive_blocks(
                1,
                time_per_block=time_per_block,
                transaction_data=spend_bundle,
                farmer_reward_puzzle_hash=target,
                pool_reward_puzzle_hash=target,
                block_list_input=current_blocks,
                guarantee_transaction_block=True,
                current_time=current_time,
            )
        await self.full_node.add_block(more[-1])
        return more[-1]

    async def farm_new_block(self, request: FarmNewBlockProtocol, force_wait_for_timestamp: bool = False):
        ssi = self.full_node.constants.SUB_SLOT_ITERS_STARTING
        diff = self.full_node.constants.DIFFICULTY_STARTING
        async with self.full_node.blockchain.priority_mutex.acquire(priority=BlockchainMutexPriority.high):
            self.log.info("Farming new block!")
            current_blocks = await self.get_all_full_blocks()
            if len(current_blocks) == 0:
                genesis = self.bt.get_consecutive_blocks(uint8(1))[0]
                future = await pre_validate_block(
                    self.full_node.blockchain.constants,
                    AugmentedBlockchain(self.full_node.blockchain),
                    genesis,
                    self.full_node.blockchain.pool,
                    None,
                    ValidationState(ssi, diff, None),
                )
                pre_validation_result: PreValidationResult = await future
                fork_info = ForkInfo(-1, -1, self.full_node.constants.GENESIS_CHALLENGE)
                await self.full_node.blockchain.add_block(genesis, pre_validation_result, ssi, fork_info)
            peak = self.full_node.blockchain.get_peak()
            assert peak is not None
            curr: BlockRecord = peak
            while not curr.is_transaction_block:
                curr = self.full_node.blockchain.block_record(curr.prev_hash)
            current_time = self.use_current_time
            time_per_block = self.time_per_block
            assert curr.timestamp is not None
            if int(time.time()) <= int(curr.timestamp):
                if force_wait_for_timestamp:
                    await asyncio.sleep(1)
                else:
                    current_time = False
            mempool_bundle = self.full_node.mempool_manager.create_bundle_from_mempool(curr.header_hash)
            if mempool_bundle is None:
                spend_bundle = None
            else:
                spend_bundle = mempool_bundle[0]
            current_blocks = await self.get_all_full_blocks()
            target = request.puzzle_hash
            more = self.bt.get_consecutive_blocks(
                1,
                transaction_data=spend_bundle,
                farmer_reward_puzzle_hash=target,
                pool_reward_puzzle_hash=target,
                block_list_input=current_blocks,
                current_time=current_time,
                time_per_block=time_per_block,
            )
        await self.full_node.add_block(more[-1])

    async def reorg_from_index_to_new_index(self, request: ReorgProtocol):
        new_index = request.new_index
        old_index = request.old_index
        coinbase_ph = request.puzzle_hash
        seed = request.seed
        if seed is None:
            seed = bytes32(32 * b"1")

        current_blocks = await self.get_all_full_blocks()
        block_count = new_index - old_index

        more_blocks = self.bt.get_consecutive_blocks(
            block_count,
            farmer_reward_puzzle_hash=coinbase_ph,
            pool_reward_puzzle_hash=coinbase_ph,
            block_list_input=current_blocks[: old_index + 1],
            force_overflow=True,
            guarantee_transaction_block=True,
            seed=seed,
        )
        await add_blocks_in_batches(more_blocks[old_index + 1 :], self.full_node)

    async def farm_blocks_to_puzzlehash(
        self,
        count: int,
        farm_to: bytes32 = bytes32.zeros,
        guarantee_transaction_blocks: bool = False,
        timeout: Union[_Default, float, None] = default,
        _wait_for_synced: bool = True,
    ) -> int:
        """Process the requested number of blocks including farming to the passed puzzle
        hash. Note that the rewards for the last block will not have been processed.
        Consider `.farm_blocks_to_wallet()` or `.farm_rewards_to_wallet()` if the goal
        is to receive XCH at an address.

        Arguments:
            count: The number of blocks to process.
            farm_to: The puzzle hash to farm the block rewards to.

        Returns:
            The total number of reward mojos for the processed blocks.
        """
        if isinstance(timeout, _Default):
            timeout = count * timeout_per_block
            timeout += 1

        with anyio.fail_after(delay=adjusted_timeout(timeout)):
            rewards = 0

            if count == 0:
                return rewards

            for _ in range(count):
                if guarantee_transaction_blocks:
                    await self.farm_new_transaction_block(FarmNewBlockProtocol(farm_to))
                else:
                    await self.farm_new_block(FarmNewBlockProtocol(farm_to))
                height = self.full_node.blockchain.get_peak_height()
                assert height is not None

                rewards += calculate_pool_reward(height) + calculate_base_farmer_reward(height)

            if _wait_for_synced:
                await self.wait_for_self_synced(timeout=None)

            return rewards

    async def farm_blocks_to_wallet(
        self,
        count: int,
        wallet: Wallet,
        timeout: Union[_Default, float, None] = default,
        _wait_for_synced: bool = True,
    ) -> int:
        """Farm the requested number of blocks to the passed wallet. This will
        process additional blocks as needed to process the reward transactions
        and also wait for the rewards to be present in the wallet.

        Arguments:
            count: The number of blocks to farm.
            wallet: The wallet to farm the block rewards to.

        Returns:
            The total number of reward mojos farmed to the requested address.
        """
        if isinstance(timeout, _Default):
            timeout = (count + 1) * timeout_per_block
            timeout += 15

        with anyio.fail_after(delay=adjusted_timeout(timeout)):
            if count == 0:
                return 0

            async with wallet.wallet_state_manager.new_action_scope(DEFAULT_TX_CONFIG, push=True) as action_scope:
                target_puzzlehash = await action_scope.get_puzzle_hash(wallet.wallet_state_manager)
            rewards = 0

            block_reward_coins = set()
            expected_reward_coin_count = 2 * count

            original_peak_height = self.full_node.blockchain.get_peak_height()
            expected_peak_height = 0 if original_peak_height is None else original_peak_height
            extra_blocks = [[False, False]] if original_peak_height is None else []  # Farm genesis block first

            for to_wallet, tx_block in [*extra_blocks, *([[True, False]] * (count - 1)), [True, True], [False, True]]:
                # This complicated application of the last two blocks being transaction
                # blocks is due to the transaction blocks only including rewards from
                # blocks up until, and including, the previous transaction block.
                if to_wallet:
                    rewards += await self.farm_blocks_to_puzzlehash(
                        count=1,
                        farm_to=target_puzzlehash,
                        guarantee_transaction_blocks=tx_block,
                        timeout=None,
                        _wait_for_synced=False,
                    )
                else:
                    await self.farm_blocks_to_puzzlehash(
                        count=1, guarantee_transaction_blocks=tx_block, timeout=None, _wait_for_synced=False
                    )

                expected_peak_height += 1
                peak_height = self.full_node.blockchain.get_peak_height()
                assert peak_height == expected_peak_height

                coin_records = await self.full_node.coin_store.get_coins_added_at_height(height=peak_height)
                for record in coin_records:
                    if record.coin.puzzle_hash == target_puzzlehash and record.coinbase:
                        block_reward_coins.add(record.coin)

            if len(block_reward_coins) != expected_reward_coin_count:
                raise RuntimeError(
                    f"Expected {expected_reward_coin_count} reward coins, got: {len(block_reward_coins)}"
                )

            await wait_for_coins_in_wallet(coins=block_reward_coins, wallet=wallet, timeout=None)
            if _wait_for_synced:
                await self.wait_for_wallet_synced(wallet.wallet_state_manager.wallet_node, timeout=None)
            return rewards

    async def farm_rewards_to_wallet(
        self,
        amount: int,
        wallet: Wallet,
        timeout: Union[_Default, float, None] = default,
    ) -> int:
        """Farm at least the requested amount of mojos to the passed wallet. Extra
        mojos will be received based on the block rewards at the present block height.
        The rewards will be present in the wall before returning.

        Arguments:
            amount: The minimum number of mojos to farm.
            wallet: The wallet to farm the block rewards to.

        Returns:
            The total number of reward mojos farmed to the requested wallet.
        """
        rewards = 0

        if amount == 0:
            return rewards

        height_before: Optional[uint32] = self.full_node.blockchain.get_peak_height()
        if height_before is None:
            height_before = uint32(0)

        for count in itertools.count(1):
            height = uint32(height_before + count)
            rewards += calculate_pool_reward(height) + calculate_base_farmer_reward(height)

            if rewards >= amount:
                break
        else:
            raise Exception("internal error")

        if isinstance(timeout, _Default):
            timeout = (count + 1) * timeout_per_block

        with anyio.fail_after(delay=adjusted_timeout(timeout)):
            await self.farm_blocks_to_wallet(count=count, wallet=wallet, timeout=None)
            return rewards

    async def wait_transaction_records_entered_mempool(
        self,
        records: Collection[Union[TransactionRecord, LightTransactionRecord]],
        timeout: Union[float, None] = 5,
    ) -> None:
        """Wait until the transaction records have entered the mempool.  Transaction
        records with no spend bundle are ignored.

        Arguments:
            records: The transaction records to wait for.
        """
        with anyio.fail_after(delay=adjusted_timeout(timeout)):
            ids_to_check: set[bytes32] = set()
            for record in records:
                if record.spend_bundle is None:
                    continue

                ids_to_check.add(record.spend_bundle.name())

            for backoff in backoff_times():
                found = set()
                for spend_bundle_name in ids_to_check:
                    tx = self.full_node.mempool_manager.get_spendbundle(spend_bundle_name)
                    if tx is not None:
                        found.add(spend_bundle_name)
                ids_to_check = ids_to_check.difference(found)

                if len(ids_to_check) == 0:
                    return

                await asyncio.sleep(backoff)

    async def wait_bundle_ids_in_mempool(
        self,
        bundle_ids: Collection[bytes32],
        timeout: Union[float, None] = 5,
    ) -> None:
        """Wait until the ids of specific spend bundles have entered the mempool.

        Arguments:
            records: The bundle ids to wait for.
        """
        with anyio.fail_after(delay=adjusted_timeout(timeout)):
            ids_to_check: set[bytes32] = set(bundle_ids)

            for backoff in backoff_times():
                found = set()
                for spend_bundle_name in ids_to_check:
                    tx = self.full_node.mempool_manager.get_spendbundle(spend_bundle_name)
                    if tx is not None:
                        found.add(spend_bundle_name)
                ids_to_check = ids_to_check.difference(found)

                if len(ids_to_check) == 0:
                    return

                await asyncio.sleep(backoff)

    async def wait_transaction_records_marked_as_in_mempool(
        self,
        record_ids: Collection[bytes32],
        wallet_node: WalletNode,
        timeout: Union[float, None] = 10,
    ) -> None:
        """Wait until the transaction records have been marked that they have made it into the mempool.  Transaction
        records with no spend bundle are ignored.

        Arguments:
            records: The transaction records to wait for.
        """
        with anyio.fail_after(delay=adjusted_timeout(timeout)):
            ids_to_check: set[bytes32] = set(record_ids)

            for backoff in backoff_times():
                found = set()
                for txid in ids_to_check:
                    tx = await wallet_node.wallet_state_manager.tx_store.get_transaction_record(txid)
                    if tx is not None and (tx.is_in_mempool() or tx.spend_bundle is None):
                        found.add(txid)
                ids_to_check = ids_to_check.difference(found)

                if len(ids_to_check) == 0:
                    return

                await asyncio.sleep(backoff)

    async def process_transaction_records(
        self,
        records: Collection[TransactionRecord],
        timeout: Union[float, None] = (2 * timeout_per_block) + 5,
    ) -> None:
        """Process the specified transaction records and wait until they have been
        included in a block.

        Arguments:
            records: The transaction records to process.
        """
        with anyio.fail_after(delay=adjusted_timeout(timeout)):
            await self.wait_for_self_synced(timeout=None)

            coins_to_wait_for: set[Coin] = set()
            for record in records:
                if record.spend_bundle is None:
                    continue

                coins_to_wait_for.update(record.spend_bundle.additions())

            await self.wait_transaction_records_entered_mempool(records=records, timeout=None)

            return await self.process_coin_spends(coins=coins_to_wait_for, timeout=None)

    async def process_spend_bundles(
        self,
        bundles: Collection[SpendBundle],
        timeout: Union[float, None] = (2 * timeout_per_block) + 5,
    ) -> None:
        """Process the specified spend bundles and wait until they have been included
        in a block.

        Arguments:
            bundles: The spend bundles to process.
        """

        with anyio.fail_after(delay=adjusted_timeout(timeout)):
            coins_to_wait_for: set[Coin] = {addition for bundle in bundles for addition in bundle.additions()}
            return await self.process_coin_spends(coins=coins_to_wait_for, timeout=None)

    async def process_coin_spends(
        self,
        coins: Collection[Coin],
        timeout: Union[float, None] = (2 * timeout_per_block) + 5,
    ) -> None:
        """Process the specified coin names and wait until they have been created in a
        block.

        Arguments:
            coin_names: The coin names to process.
        """

        with anyio.fail_after(delay=adjusted_timeout(timeout)):
            coin_set = set(coins)
            coin_store = self.full_node.coin_store

            while True:
                await self.farm_blocks_to_puzzlehash(count=1, guarantee_transaction_blocks=True, timeout=None)

                found: set[Coin] = set()
                for coin in coin_set:
                    # TODO: is this the proper check?
                    if await coin_store.get_coin_record(coin.name()) is not None:
                        found.add(coin)

                coin_set = coin_set.difference(found)

                if len(coin_set) == 0:
                    return

    async def process_all_wallet_transactions(self, wallet: Wallet, timeout: Optional[float] = 5) -> None:
        # TODO: Maybe something could be done around waiting for the tx to enter the
        #       mempool.  Maybe not, might be too many races or such.
        wallet_state_manager: Optional[WalletStateManager] = wallet.wallet_state_manager
        assert wallet_state_manager is not None

        with anyio.fail_after(delay=adjusted_timeout(timeout)):
            for backoff in backoff_times():
                await self.farm_blocks_to_puzzlehash(count=1, guarantee_transaction_blocks=True, timeout=None)

                wallet_ids = wallet_state_manager.wallets.keys()
                for wallet_id in wallet_ids:
                    unconfirmed = await wallet_state_manager.tx_store.get_unconfirmed_for_wallet(wallet_id=wallet_id)
                    if len(unconfirmed) > 0:
                        break
                else:
                    # all wallets have zero unconfirmed transactions
                    break

                # at least one wallet has unconfirmed transactions
                await asyncio.sleep(backoff)

    async def check_transactions_confirmed(
        self,
        wallet_state_manager: WalletStateManager,
        transactions: Union[list[TransactionRecord], list[LightTransactionRecord]],
        timeout: Optional[float] = 5,
    ) -> None:
        transactions_left: set[bytes32] = {tx.name for tx in transactions}
        with anyio.fail_after(delay=adjusted_timeout(timeout)):
            for backoff in backoff_times():
                transactions_left &= {tx.name for tx in await wallet_state_manager.tx_store.get_all_unconfirmed()}
                if len(transactions_left) == 0:
                    break

                # at least one wallet has unconfirmed transactions
                await asyncio.sleep(backoff)  # pragma: no cover

    async def create_coins_with_amounts(
        self,
        amounts: list[uint64],
        wallet: Wallet,
        per_transaction_record_group: int = 50,
        timeout: Union[float, None] = 15,
    ) -> set[Coin]:
        """Create coins with the requested amount.  This is useful when you need a
        bunch of coins for a test and don't need to farm that many.

        Arguments:
            amounts: A list with entries of mojo amounts corresponding to each
                coin to create.
            wallet: The wallet to send the new coins to.
            per_transaction_record_group: The maximum number of coins to create in each
                transaction record.

        Returns:
            A set of the generated coins.  Note that this does not include any change
            coins that were created.
        """
        with anyio.fail_after(delay=adjusted_timeout(timeout)):
            invalid_amounts = [amount for amount in amounts if amount <= 0]
            if len(invalid_amounts) > 0:
                invalid_amounts_string = ", ".join(str(amount) for amount in invalid_amounts)
                raise Exception(f"Coins must have a positive value, request included: {invalid_amounts_string}")

            if len(amounts) == 0:
                return set()

            outputs: list[CreateCoin] = []
            amounts_seen: set[uint64] = set()
            for amount in amounts:
                # We need unique puzzle hash amount combos so we'll only generate a new puzzle hash when we've already
                # seen that amount sent to that puzzle hash
                async with wallet.wallet_state_manager.new_action_scope(DEFAULT_TX_CONFIG, push=True) as action_scope:
                    puzzle_hash = await action_scope.get_puzzle_hash(
                        wallet.wallet_state_manager, override_reuse_puzhash_with=amount not in amounts_seen
                    )
                outputs.append(CreateCoin(puzzle_hash, amount))
                amounts_seen.add(amount)

            transaction_records: list[TransactionRecord] = []
            outputs_iterator = iter(outputs)
            while True:
                # The outputs iterator must be second in the zip() call otherwise we lose
                # an element when reaching the end of the range object.
                outputs_group = [output for _, output in zip(range(per_transaction_record_group), outputs_iterator)]

                if len(outputs_group) > 0:
                    async with wallet.wallet_state_manager.new_action_scope(
                        DEFAULT_TX_CONFIG, push=True
                    ) as action_scope:
                        await wallet.generate_signed_transaction(
                            amounts=[output.amount for output in outputs_group],
                            puzzle_hashes=[output.puzzle_hash for output in outputs_group],
                            action_scope=action_scope,
                        )
                    transaction_records.extend(action_scope.side_effects.transactions)
                else:
                    break

            await self.wait_transaction_records_entered_mempool(transaction_records, timeout=None)
            await self.farm_blocks_to_puzzlehash(count=1, guarantee_transaction_blocks=True)

            output_coins = {coin for transaction_record in transaction_records for coin in transaction_record.additions}
            puzzle_hashes = {output.puzzle_hash for output in outputs}
            change_coins = {coin for coin in output_coins if coin.puzzle_hash not in puzzle_hashes}
            coins_to_receive = output_coins - change_coins
            await wait_for_coins_in_wallet(coins=coins_to_receive, wallet=wallet)

            return coins_to_receive

    def tx_id_in_mempool(self, tx_id: bytes32) -> bool:
        spendbundle = self.full_node.mempool_manager.get_spendbundle(bundle_hash=tx_id)
        return spendbundle is not None

    def txs_in_mempool(self, txs: list[TransactionRecord]) -> bool:
        return all(self.tx_id_in_mempool(tx_id=tx.spend_bundle.name()) for tx in txs if tx.spend_bundle is not None)

    async def self_is_synced(self) -> bool:
        return await self.full_node.synced()

    async def wallet_is_synced(self, wallet_node: WalletNode, peak_height: Optional[uint32] = None) -> bool:
        if not await self.self_is_synced():
            # Depending on races, may not be covered every time
            return False  # pragma: no cover
        if not await wallet_node.wallet_state_manager.synced():
            return False
        all_states_retried = await wallet_node.wallet_state_manager.retry_store.get_all_states_to_retry() == []
        wallet_height = await wallet_node.wallet_state_manager.blockchain.get_finished_sync_up_to()
        if peak_height is not None:
            return wallet_height >= peak_height and all_states_retried
        full_node_height = self.full_node.blockchain.get_peak_height()
        return wallet_height == full_node_height and all_states_retried

    async def wait_for_wallet_synced(
        self,
        wallet_node: WalletNode,
        timeout: Optional[float] = 5,
        peak_height: Optional[uint32] = None,
    ) -> None:
        with anyio.fail_after(delay=adjusted_timeout(timeout)):
            for backoff_time in backoff_times():
                if await self.wallet_is_synced(wallet_node=wallet_node, peak_height=peak_height):
                    break
                await asyncio.sleep(backoff_time)

    async def wallets_are_synced(self, wallet_nodes: list[WalletNode], peak_height: Optional[uint32] = None) -> bool:
        return all(
            [
                await self.wallet_is_synced(wallet_node=wallet_node, peak_height=peak_height)
                for wallet_node in wallet_nodes
            ]
        )

    async def wait_for_wallets_synced(
        self,
        wallet_nodes: list[WalletNode],
        timeout: Optional[float] = 5,
        peak_height: Optional[uint32] = None,
    ) -> None:
        with anyio.fail_after(delay=adjusted_timeout(timeout)):
            for backoff_time in backoff_times():
                if await self.wallets_are_synced(wallet_nodes=wallet_nodes, peak_height=peak_height):
                    break
                await asyncio.sleep(backoff_time)

    async def wait_for_self_synced(
        self,
        timeout: Optional[float] = 5,
    ) -> None:
        with anyio.fail_after(delay=adjusted_timeout(timeout)):
            for backoff_time in backoff_times():
                if await self.self_is_synced():
                    break
                # Depending on races, may not be covered every time
                await asyncio.sleep(backoff_time)  # pragma: no cover
