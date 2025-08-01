from __future__ import annotations

import asyncio
import logging
import time
import traceback
from collections.abc import Collection
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from typing import TYPE_CHECKING, ClassVar, Optional, cast

import anyio
from chia_rs import (
    AugSchemeMPL,
    BlockRecord,
    CoinState,
    EndOfSubSlotBundle,
    FoliageBlockData,
    FoliageTransactionBlock,
    FullBlock,
    G1Element,
    G2Element,
    MerkleSet,
    PoolTarget,
    RespondToPhUpdates,
    RewardChainBlockUnfinished,
    SpendBundle,
    SubEpochSummary,
    UnfinishedBlock,
    additions_and_removals,
    get_flags_for_height_and_constants,
)
from chia_rs import get_puzzle_and_solution_for_coin2 as get_puzzle_and_solution_for_coin
from chia_rs.sized_bytes import bytes32
from chia_rs.sized_ints import uint8, uint32, uint64, uint128
from chiabip158 import PyBIP158

from chia.consensus.block_creation import create_unfinished_block
from chia.consensus.blockchain import BlockchainMutexPriority
from chia.consensus.generator_tools import get_block_header
from chia.consensus.get_block_generator import get_block_generator
from chia.consensus.pot_iterations import calculate_ip_iters, calculate_iterations_quality, calculate_sp_iters
from chia.consensus.signage_point import SignagePoint
from chia.full_node.coin_store import CoinStore
from chia.full_node.fee_estimator_interface import FeeEstimatorInterface
from chia.full_node.full_block_utils import get_height_and_tx_status_from_block, header_block_from_block
from chia.full_node.tx_processing_queue import TransactionQueueEntry, TransactionQueueFull
from chia.protocols import farmer_protocol, full_node_protocol, introducer_protocol, timelord_protocol, wallet_protocol
from chia.protocols.fee_estimate import FeeEstimate, FeeEstimateGroup, fee_rate_v2_to_v1
from chia.protocols.full_node_protocol import RejectBlock, RejectBlocks
from chia.protocols.outbound_message import Message, make_msg
from chia.protocols.protocol_message_types import ProtocolMessageTypes
from chia.protocols.protocol_timing import RATE_LIMITER_BAN_SECONDS
from chia.protocols.shared_protocol import Capability
from chia.protocols.wallet_protocol import (
    PuzzleSolutionResponse,
    RejectBlockHeaders,
    RejectHeaderBlocks,
    RejectHeaderRequest,
    RespondFeeEstimates,
    RespondSESInfo,
)
from chia.server.api_protocol import ApiMetadata
from chia.server.server import ChiaServer
from chia.server.ws_connection import WSChiaConnection
from chia.types.block_protocol import BlockInfo
from chia.types.blockchain_format.coin import Coin, hash_coin_ids
from chia.types.blockchain_format.proof_of_space import verify_and_get_quality_string
from chia.types.coin_record import CoinRecord
from chia.types.generator_types import BlockGenerator, NewBlockGenerator
from chia.types.mempool_inclusion_status import MempoolInclusionStatus
from chia.types.peer_info import PeerInfo
from chia.util.batches import to_batches
from chia.util.db_wrapper import SQLITE_MAX_VARIABLE_NUMBER
from chia.util.hash import std_hash
from chia.util.limited_semaphore import LimitedSemaphoreFullError
from chia.util.task_referencer import create_referenced_task

if TYPE_CHECKING:
    from chia.full_node.full_node import FullNode
else:
    FullNode = object


class FullNodeAPI:
    if TYPE_CHECKING:
        from chia.server.api_protocol import ApiProtocol

        _protocol_check: ClassVar[ApiProtocol] = cast("FullNodeAPI", None)

    log: logging.Logger
    full_node: FullNode
    executor: ThreadPoolExecutor
    metadata: ClassVar[ApiMetadata] = ApiMetadata()

    def __init__(self, full_node: FullNode) -> None:
        self.log = logging.getLogger(__name__)
        self.full_node = full_node
        self.executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="node-api-")

    @property
    def server(self) -> ChiaServer:
        assert self.full_node.server is not None
        return self.full_node.server

    def ready(self) -> bool:
        return self.full_node.initialized

    @metadata.request(peer_required=True, reply_types=[ProtocolMessageTypes.respond_peers])
    async def request_peers(
        self, _request: full_node_protocol.RequestPeers, peer: WSChiaConnection
    ) -> Optional[Message]:
        if peer.peer_server_port is None:
            return None
        peer_info = PeerInfo(peer.peer_info.host, peer.peer_server_port)
        if self.full_node.full_node_peers is not None:
            msg = await self.full_node.full_node_peers.request_peers(peer_info)
            return msg
        return None

    @metadata.request(peer_required=True)
    async def respond_peers(
        self, request: full_node_protocol.RespondPeers, peer: WSChiaConnection
    ) -> Optional[Message]:
        self.log.debug(f"Received {len(request.peer_list)} peers")
        if self.full_node.full_node_peers is not None:
            await self.full_node.full_node_peers.add_peers(request.peer_list, peer.get_peer_info(), True)
        return None

    @metadata.request(peer_required=True)
    async def respond_peers_introducer(
        self, request: introducer_protocol.RespondPeersIntroducer, peer: WSChiaConnection
    ) -> Optional[Message]:
        self.log.debug(f"Received {len(request.peer_list)} peers from introducer")
        if self.full_node.full_node_peers is not None:
            await self.full_node.full_node_peers.add_peers(request.peer_list, peer.get_peer_info(), False)

        await peer.close()
        return None

    @metadata.request(peer_required=True, execute_task=True)
    async def new_peak(self, request: full_node_protocol.NewPeak, peer: WSChiaConnection) -> None:
        """
        A peer notifies us that they have added a new peak to their blockchain. If we don't have it,
        we can ask for it.
        """
        # this semaphore limits the number of tasks that can call new_peak() at
        # the same time, since it can be expensive
        try:
            async with self.full_node.new_peak_sem.acquire():
                await self.full_node.new_peak(request, peer)
        except LimitedSemaphoreFullError:
            self.log.debug("Ignoring NewPeak, limited semaphore full: %s %s", peer.get_peer_logging(), request)
            return None

        return None

    @metadata.request(peer_required=True)
    async def new_transaction(
        self, transaction: full_node_protocol.NewTransaction, peer: WSChiaConnection
    ) -> Optional[Message]:
        """
        A peer notifies us of a new transaction.
        Requests a full transaction if we haven't seen it previously, and if the fees are enough.
        """
        # Ignore if syncing
        if self.full_node.sync_store.get_sync_mode():
            return None
        if not (await self.full_node.synced()):
            return None

        # Ignore if already seen
        if self.full_node.mempool_manager.seen(transaction.transaction_id):
            return None

        if self.full_node.mempool_manager.is_fee_enough(transaction.fees, transaction.cost):
            # If there's current pending request just add this peer to the set of peers that have this tx
            if transaction.transaction_id in self.full_node.full_node_store.pending_tx_request:
                if transaction.transaction_id in self.full_node.full_node_store.peers_with_tx:
                    current_set = self.full_node.full_node_store.peers_with_tx[transaction.transaction_id]
                    if peer.peer_node_id in current_set:
                        return None
                    current_set.add(peer.peer_node_id)
                    return None
                else:
                    new_set = set()
                    new_set.add(peer.peer_node_id)
                    self.full_node.full_node_store.peers_with_tx[transaction.transaction_id] = new_set
                    return None

            self.full_node.full_node_store.pending_tx_request[transaction.transaction_id] = peer.peer_node_id
            new_set = set()
            new_set.add(peer.peer_node_id)
            self.full_node.full_node_store.peers_with_tx[transaction.transaction_id] = new_set

            async def tx_request_and_timeout(full_node: FullNode, transaction_id: bytes32, task_id: bytes32) -> None:
                counter = 0
                try:
                    while True:
                        # Limit to asking a few peers, it's possible that this tx got included on chain already
                        # Highly unlikely that the peers that advertised a tx don't respond to a request. Also, if we
                        # drop some transactions, we don't want to re-fetch too many times
                        if counter == 5:
                            break
                        if transaction_id not in full_node.full_node_store.peers_with_tx:
                            break
                        peers_with_tx: set[bytes32] = full_node.full_node_store.peers_with_tx[transaction_id]
                        if len(peers_with_tx) == 0:
                            break
                        peer_id = peers_with_tx.pop()
                        assert full_node.server is not None
                        if peer_id not in full_node.server.all_connections:
                            continue
                        random_peer = full_node.server.all_connections[peer_id]
                        request_tx = full_node_protocol.RequestTransaction(transaction.transaction_id)
                        msg = make_msg(ProtocolMessageTypes.request_transaction, request_tx)
                        await random_peer.send_message(msg)
                        await asyncio.sleep(5)
                        counter += 1
                        if full_node.mempool_manager.seen(transaction_id):
                            break
                except asyncio.CancelledError:
                    pass
                finally:
                    # Always Cleanup
                    if transaction_id in full_node.full_node_store.peers_with_tx:
                        full_node.full_node_store.peers_with_tx.pop(transaction_id)
                    if transaction_id in full_node.full_node_store.pending_tx_request:
                        full_node.full_node_store.pending_tx_request.pop(transaction_id)
                    if task_id in full_node.full_node_store.tx_fetch_tasks:
                        full_node.full_node_store.tx_fetch_tasks.pop(task_id)

            task_id: bytes32 = bytes32.secret()
            fetch_task = create_referenced_task(
                tx_request_and_timeout(self.full_node, transaction.transaction_id, task_id)
            )
            self.full_node.full_node_store.tx_fetch_tasks[task_id] = fetch_task
            return None
        return None

    @metadata.request(reply_types=[ProtocolMessageTypes.respond_transaction])
    async def request_transaction(self, request: full_node_protocol.RequestTransaction) -> Optional[Message]:
        """Peer has requested a full transaction from us."""
        # Ignore if syncing
        if self.full_node.sync_store.get_sync_mode():
            return None
        spend_bundle = self.full_node.mempool_manager.get_spendbundle(request.transaction_id)
        if spend_bundle is None:
            return None

        transaction = full_node_protocol.RespondTransaction(spend_bundle)

        msg = make_msg(ProtocolMessageTypes.respond_transaction, transaction)
        return msg

    @metadata.request(peer_required=True, bytes_required=True)
    async def respond_transaction(
        self,
        tx: full_node_protocol.RespondTransaction,
        peer: WSChiaConnection,
        tx_bytes: bytes = b"",
        test: bool = False,
    ) -> Optional[Message]:
        """
        Receives a full transaction from peer.
        If tx is added to mempool, send tx_id to others. (new_transaction)
        """
        assert tx_bytes != b""
        spend_name = std_hash(tx_bytes)
        if spend_name in self.full_node.full_node_store.pending_tx_request:
            self.full_node.full_node_store.pending_tx_request.pop(spend_name)
        if spend_name in self.full_node.full_node_store.peers_with_tx:
            self.full_node.full_node_store.peers_with_tx.pop(spend_name)

        # TODO: Use fee in priority calculation, to prioritize high fee TXs
        try:
            await self.full_node.transaction_queue.put(
                TransactionQueueEntry(tx.transaction, tx_bytes, spend_name, peer, test), peer.peer_node_id
            )
        except TransactionQueueFull:
            pass  # we can't do anything here, the tx will be dropped. We might do something in the future.
        return None

    @metadata.request(reply_types=[ProtocolMessageTypes.respond_proof_of_weight])
    async def request_proof_of_weight(self, request: full_node_protocol.RequestProofOfWeight) -> Optional[Message]:
        if self.full_node.weight_proof_handler is None:
            return None
        if self.full_node.blockchain.try_block_record(request.tip) is None:
            self.log.error(f"got weight proof request for unknown peak {request.tip}")
            return None
        if request.tip in self.full_node.pow_creation:
            event = self.full_node.pow_creation[request.tip]
            await event.wait()
            wp = await self.full_node.weight_proof_handler.get_proof_of_weight(request.tip)
        else:
            event = asyncio.Event()
            self.full_node.pow_creation[request.tip] = event
            wp = await self.full_node.weight_proof_handler.get_proof_of_weight(request.tip)
            event.set()
        tips = list(self.full_node.pow_creation.keys())

        if len(tips) > 4:
            # Remove old from cache
            for i in range(4):
                self.full_node.pow_creation.pop(tips[i])

        if wp is None:
            self.log.error(f"failed creating weight proof for peak {request.tip}")
            return None

        # Serialization of wp is slow
        if (
            self.full_node.full_node_store.serialized_wp_message_tip is not None
            and self.full_node.full_node_store.serialized_wp_message_tip == request.tip
        ):
            return self.full_node.full_node_store.serialized_wp_message
        message = make_msg(
            ProtocolMessageTypes.respond_proof_of_weight, full_node_protocol.RespondProofOfWeight(wp, request.tip)
        )
        self.full_node.full_node_store.serialized_wp_message_tip = request.tip
        self.full_node.full_node_store.serialized_wp_message = message
        return message

    @metadata.request()
    async def respond_proof_of_weight(self, request: full_node_protocol.RespondProofOfWeight) -> Optional[Message]:
        self.log.warning("Received proof of weight too late.")
        return None

    @metadata.request(reply_types=[ProtocolMessageTypes.respond_block, ProtocolMessageTypes.reject_block])
    async def request_block(self, request: full_node_protocol.RequestBlock) -> Optional[Message]:
        if not self.full_node.blockchain.contains_height(request.height):
            reject = RejectBlock(request.height)
            msg = make_msg(ProtocolMessageTypes.reject_block, reject)
            return msg
        header_hash: Optional[bytes32] = self.full_node.blockchain.height_to_hash(request.height)
        if header_hash is None:
            return make_msg(ProtocolMessageTypes.reject_block, RejectBlock(request.height))

        block: Optional[FullBlock] = await self.full_node.block_store.get_full_block(header_hash)
        if block is not None:
            if not request.include_transaction_block and block.transactions_generator is not None:
                block = block.replace(transactions_generator=None)
            return make_msg(ProtocolMessageTypes.respond_block, full_node_protocol.RespondBlock(block))
        return make_msg(ProtocolMessageTypes.reject_block, RejectBlock(request.height))

    @metadata.request(reply_types=[ProtocolMessageTypes.respond_blocks, ProtocolMessageTypes.reject_blocks])
    async def request_blocks(self, request: full_node_protocol.RequestBlocks) -> Optional[Message]:
        # note that we treat the request range as *inclusive*, but we check the
        # size before we bump end_height. So MAX_BLOCK_COUNT_PER_REQUESTS is off
        # by one
        if (
            request.end_height < request.start_height
            or request.end_height - request.start_height > self.full_node.constants.MAX_BLOCK_COUNT_PER_REQUESTS
        ):
            reject = RejectBlocks(request.start_height, request.end_height)
            msg: Message = make_msg(ProtocolMessageTypes.reject_blocks, reject)
            return msg
        for i in range(request.start_height, request.end_height + 1):
            if not self.full_node.blockchain.contains_height(uint32(i)):
                reject = RejectBlocks(request.start_height, request.end_height)
                msg = make_msg(ProtocolMessageTypes.reject_blocks, reject)
                return msg

        if not request.include_transaction_block:
            blocks: list[FullBlock] = []
            for i in range(request.start_height, request.end_height + 1):
                header_hash_i: Optional[bytes32] = self.full_node.blockchain.height_to_hash(uint32(i))
                if header_hash_i is None:
                    reject = RejectBlocks(request.start_height, request.end_height)
                    return make_msg(ProtocolMessageTypes.reject_blocks, reject)

                block: Optional[FullBlock] = await self.full_node.block_store.get_full_block(header_hash_i)
                if block is None:
                    reject = RejectBlocks(request.start_height, request.end_height)
                    return make_msg(ProtocolMessageTypes.reject_blocks, reject)
                block = block.replace(transactions_generator=None)
                blocks.append(block)
            msg = make_msg(
                ProtocolMessageTypes.respond_blocks,
                full_node_protocol.RespondBlocks(request.start_height, request.end_height, blocks),
            )
        else:
            blocks_bytes: list[bytes] = []
            for i in range(request.start_height, request.end_height + 1):
                header_hash_i = self.full_node.blockchain.height_to_hash(uint32(i))
                if header_hash_i is None:
                    reject = RejectBlocks(request.start_height, request.end_height)
                    return make_msg(ProtocolMessageTypes.reject_blocks, reject)
                block_bytes: Optional[bytes] = await self.full_node.block_store.get_full_block_bytes(header_hash_i)
                if block_bytes is None:
                    reject = RejectBlocks(request.start_height, request.end_height)
                    msg = make_msg(ProtocolMessageTypes.reject_blocks, reject)
                    return msg

                blocks_bytes.append(block_bytes)

            respond_blocks_manually_streamed: bytes = (
                uint32(request.start_height).stream_to_bytes()
                + uint32(request.end_height).stream_to_bytes()
                + uint32(len(blocks_bytes)).stream_to_bytes()
            )
            for block_bytes in blocks_bytes:
                respond_blocks_manually_streamed += block_bytes
            msg = make_msg(ProtocolMessageTypes.respond_blocks, respond_blocks_manually_streamed)

        return msg

    @metadata.request(peer_required=True)
    async def reject_block(
        self,
        request: full_node_protocol.RejectBlock,
        peer: WSChiaConnection,
    ) -> None:
        self.log.warning(f"unsolicited reject_block {request.height}")
        await peer.close(RATE_LIMITER_BAN_SECONDS)

    @metadata.request(peer_required=True)
    async def reject_blocks(
        self,
        request: full_node_protocol.RejectBlocks,
        peer: WSChiaConnection,
    ) -> None:
        self.log.warning(f"reject_blocks {request.start_height} {request.end_height}")
        await peer.close(RATE_LIMITER_BAN_SECONDS)

    @metadata.request(peer_required=True)
    async def respond_blocks(
        self,
        request: full_node_protocol.RespondBlocks,
        peer: WSChiaConnection,
    ) -> None:
        self.log.warning("Received unsolicited/late blocks")
        await peer.close(RATE_LIMITER_BAN_SECONDS)

    @metadata.request(peer_required=True)
    async def respond_block(
        self,
        respond_block: full_node_protocol.RespondBlock,
        peer: WSChiaConnection,
    ) -> Optional[Message]:
        self.log.warning(f"Received unsolicited/late block from peer {peer.get_peer_logging()}")
        await peer.close(RATE_LIMITER_BAN_SECONDS)
        return None

    @metadata.request()
    async def new_unfinished_block(
        self, new_unfinished_block: full_node_protocol.NewUnfinishedBlock
    ) -> Optional[Message]:
        # Ignore if syncing
        if self.full_node.sync_store.get_sync_mode():
            return None
        block_hash = new_unfinished_block.unfinished_reward_hash
        if self.full_node.full_node_store.get_unfinished_block(block_hash) is not None:
            return None

        # This prevents us from downloading the same block from many peers
        requesting, count = self.full_node.full_node_store.is_requesting_unfinished_block(block_hash, None)
        if requesting:
            self.log.debug(
                f"Already have or requesting {count} Unfinished Blocks with partial "
                f"hash {block_hash}. Ignoring this one"
            )
            return None

        msg = make_msg(
            ProtocolMessageTypes.request_unfinished_block,
            full_node_protocol.RequestUnfinishedBlock(block_hash),
        )
        self.full_node.full_node_store.mark_requesting_unfinished_block(block_hash, None)

        # However, we want to eventually download from other peers, if this peer does not respond
        # Todo: keep track of who it was
        async def eventually_clear() -> None:
            await asyncio.sleep(5)
            self.full_node.full_node_store.remove_requesting_unfinished_block(block_hash, None)

        create_referenced_task(eventually_clear(), known_unreferenced=True)

        return msg

    @metadata.request(reply_types=[ProtocolMessageTypes.respond_unfinished_block])
    async def request_unfinished_block(
        self, request_unfinished_block: full_node_protocol.RequestUnfinishedBlock
    ) -> Optional[Message]:
        unfinished_block: Optional[UnfinishedBlock] = self.full_node.full_node_store.get_unfinished_block(
            request_unfinished_block.unfinished_reward_hash
        )
        if unfinished_block is not None:
            msg = make_msg(
                ProtocolMessageTypes.respond_unfinished_block,
                full_node_protocol.RespondUnfinishedBlock(unfinished_block),
            )
            return msg
        return None

    @metadata.request()
    async def new_unfinished_block2(
        self, new_unfinished_block: full_node_protocol.NewUnfinishedBlock2
    ) -> Optional[Message]:
        # Ignore if syncing
        if self.full_node.sync_store.get_sync_mode():
            return None
        block_hash = new_unfinished_block.unfinished_reward_hash
        foliage_hash = new_unfinished_block.foliage_hash
        entry, count, have_better = self.full_node.full_node_store.get_unfinished_block2(block_hash, foliage_hash)

        if entry is not None:
            return None

        if have_better:
            self.log.info(
                f"Already have a better Unfinished Block with partial hash {block_hash.hex()} ignoring this one"
            )
            return None

        max_duplicate_unfinished_blocks = self.full_node.config.get("max_duplicate_unfinished_blocks", 3)
        if count > max_duplicate_unfinished_blocks:
            self.log.info(
                f"Already have {count} Unfinished Blocks with partial hash {block_hash.hex()} ignoring another one"
            )
            return None

        # This prevents us from downloading the same block from many peers
        requesting, count = self.full_node.full_node_store.is_requesting_unfinished_block(block_hash, foliage_hash)
        if requesting:
            return None
        if count >= max_duplicate_unfinished_blocks:
            self.log.info(
                f"Already requesting {count} Unfinished Blocks with partial hash {block_hash} ignoring another one"
            )
            return None

        msg = make_msg(
            ProtocolMessageTypes.request_unfinished_block2,
            full_node_protocol.RequestUnfinishedBlock2(block_hash, foliage_hash),
        )
        self.full_node.full_node_store.mark_requesting_unfinished_block(block_hash, foliage_hash)

        # However, we want to eventually download from other peers, if this peer does not respond
        # Todo: keep track of who it was
        async def eventually_clear() -> None:
            await asyncio.sleep(5)
            self.full_node.full_node_store.remove_requesting_unfinished_block(block_hash, foliage_hash)

        create_referenced_task(eventually_clear(), known_unreferenced=True)

        return msg

    @metadata.request(reply_types=[ProtocolMessageTypes.respond_unfinished_block])
    async def request_unfinished_block2(
        self, request_unfinished_block: full_node_protocol.RequestUnfinishedBlock2
    ) -> Optional[Message]:
        unfinished_block: Optional[UnfinishedBlock]
        unfinished_block, _, _ = self.full_node.full_node_store.get_unfinished_block2(
            request_unfinished_block.unfinished_reward_hash,
            request_unfinished_block.foliage_hash,
        )
        if unfinished_block is not None:
            msg = make_msg(
                ProtocolMessageTypes.respond_unfinished_block,
                full_node_protocol.RespondUnfinishedBlock(unfinished_block),
            )
            return msg
        return None

    @metadata.request(peer_required=True)
    async def respond_unfinished_block(
        self,
        respond_unfinished_block: full_node_protocol.RespondUnfinishedBlock,
        peer: WSChiaConnection,
    ) -> Optional[Message]:
        if self.full_node.sync_store.get_sync_mode():
            return None
        await self.full_node.add_unfinished_block(respond_unfinished_block.unfinished_block, peer)
        return None

    @metadata.request(peer_required=True)
    async def new_signage_point_or_end_of_sub_slot(
        self, new_sp: full_node_protocol.NewSignagePointOrEndOfSubSlot, peer: WSChiaConnection
    ) -> Optional[Message]:
        # Ignore if syncing
        if self.full_node.sync_store.get_sync_mode():
            return None
        if (
            self.full_node.full_node_store.get_signage_point_by_index(
                new_sp.challenge_hash,
                new_sp.index_from_challenge,
                new_sp.last_rc_infusion,
            )
            is not None
        ):
            return None
        if self.full_node.full_node_store.have_newer_signage_point(
            new_sp.challenge_hash, new_sp.index_from_challenge, new_sp.last_rc_infusion
        ):
            return None

        if new_sp.index_from_challenge == 0 and new_sp.prev_challenge_hash is not None:
            if self.full_node.full_node_store.get_sub_slot(new_sp.prev_challenge_hash) is None:
                collected_eos = []
                challenge_hash_to_request = new_sp.challenge_hash
                last_rc = new_sp.last_rc_infusion
                num_non_empty_sub_slots_seen = 0
                for _ in range(30):
                    if num_non_empty_sub_slots_seen >= 3:
                        self.log.debug("Diverged from peer. Don't have the same blocks")
                        return None
                    # If this is an end of sub slot, and we don't have the prev, request the prev instead
                    # We want to catch up to the latest slot so we can receive signage points
                    full_node_request = full_node_protocol.RequestSignagePointOrEndOfSubSlot(
                        challenge_hash_to_request, uint8(0), last_rc
                    )
                    response = await peer.call_api(
                        FullNodeAPI.request_signage_point_or_end_of_sub_slot, full_node_request, timeout=10
                    )
                    if not isinstance(response, full_node_protocol.RespondEndOfSubSlot):
                        self.full_node.log.debug(f"Invalid response for slot {response}")
                        return None
                    collected_eos.append(response)
                    if (
                        self.full_node.full_node_store.get_sub_slot(
                            response.end_of_slot_bundle.challenge_chain.challenge_chain_end_of_slot_vdf.challenge
                        )
                        is not None
                        or response.end_of_slot_bundle.challenge_chain.challenge_chain_end_of_slot_vdf.challenge
                        == self.full_node.constants.GENESIS_CHALLENGE
                    ):
                        for eos in reversed(collected_eos):
                            await self.respond_end_of_sub_slot(eos, peer)
                        return None
                    if (
                        response.end_of_slot_bundle.challenge_chain.challenge_chain_end_of_slot_vdf.number_of_iterations
                        != response.end_of_slot_bundle.reward_chain.end_of_slot_vdf.number_of_iterations
                    ):
                        num_non_empty_sub_slots_seen += 1
                    challenge_hash_to_request = (
                        response.end_of_slot_bundle.challenge_chain.challenge_chain_end_of_slot_vdf.challenge
                    )
                    last_rc = response.end_of_slot_bundle.reward_chain.end_of_slot_vdf.challenge
                self.full_node.log.warning("Failed to catch up in sub-slots")
                return None

        if new_sp.index_from_challenge > 0:
            if (
                new_sp.challenge_hash != self.full_node.constants.GENESIS_CHALLENGE
                and self.full_node.full_node_store.get_sub_slot(new_sp.challenge_hash) is None
            ):
                # If this is a normal signage point,, and we don't have the end of sub slot, request the end of sub slot
                full_node_request = full_node_protocol.RequestSignagePointOrEndOfSubSlot(
                    new_sp.challenge_hash, uint8(0), new_sp.last_rc_infusion
                )
                return make_msg(ProtocolMessageTypes.request_signage_point_or_end_of_sub_slot, full_node_request)

        # Otherwise (we have the prev or the end of sub slot), request it normally
        full_node_request = full_node_protocol.RequestSignagePointOrEndOfSubSlot(
            new_sp.challenge_hash, new_sp.index_from_challenge, new_sp.last_rc_infusion
        )

        return make_msg(ProtocolMessageTypes.request_signage_point_or_end_of_sub_slot, full_node_request)

    @metadata.request(
        reply_types=[ProtocolMessageTypes.respond_signage_point, ProtocolMessageTypes.respond_end_of_sub_slot]
    )
    async def request_signage_point_or_end_of_sub_slot(
        self, request: full_node_protocol.RequestSignagePointOrEndOfSubSlot
    ) -> Optional[Message]:
        if request.index_from_challenge == 0:
            sub_slot: Optional[tuple[EndOfSubSlotBundle, int, uint128]] = self.full_node.full_node_store.get_sub_slot(
                request.challenge_hash
            )
            if sub_slot is not None:
                return make_msg(
                    ProtocolMessageTypes.respond_end_of_sub_slot,
                    full_node_protocol.RespondEndOfSubSlot(sub_slot[0]),
                )
        else:
            if self.full_node.full_node_store.get_sub_slot(request.challenge_hash) is None:
                if request.challenge_hash != self.full_node.constants.GENESIS_CHALLENGE:
                    self.log.info(f"Don't have challenge hash {request.challenge_hash.hex()}")

            sp: Optional[SignagePoint] = self.full_node.full_node_store.get_signage_point_by_index(
                request.challenge_hash,
                request.index_from_challenge,
                request.last_rc_infusion,
            )
            if sp is not None:
                assert (
                    sp.cc_vdf is not None
                    and sp.cc_proof is not None
                    and sp.rc_vdf is not None
                    and sp.rc_proof is not None
                )
                full_node_response = full_node_protocol.RespondSignagePoint(
                    request.index_from_challenge,
                    sp.cc_vdf,
                    sp.cc_proof,
                    sp.rc_vdf,
                    sp.rc_proof,
                )
                return make_msg(ProtocolMessageTypes.respond_signage_point, full_node_response)
            else:
                self.log.info(f"Don't have signage point {request}")
        return None

    @metadata.request(peer_required=True)
    async def respond_signage_point(
        self, request: full_node_protocol.RespondSignagePoint, peer: WSChiaConnection
    ) -> Optional[Message]:
        if self.full_node.sync_store.get_sync_mode():
            return None
        async with self.full_node.timelord_lock:
            # Already have signage point

            if self.full_node.full_node_store.have_newer_signage_point(
                request.challenge_chain_vdf.challenge,
                request.index_from_challenge,
                request.reward_chain_vdf.challenge,
            ):
                return None
            existing_sp = self.full_node.full_node_store.get_signage_point_by_index_and_cc_output(
                request.challenge_chain_vdf.output.get_hash(),
                request.challenge_chain_vdf.challenge,
                request.index_from_challenge,
            )
            if existing_sp is not None and existing_sp.rc_vdf == request.reward_chain_vdf:
                return None
            peak = self.full_node.blockchain.get_peak()
            if peak is not None and peak.height > self.full_node.constants.MAX_SUB_SLOT_BLOCKS:
                next_sub_slot_iters = self.full_node.blockchain.get_next_sub_slot_iters_and_difficulty(
                    peak.header_hash, True
                )[0]
                sub_slots_for_peak = await self.full_node.blockchain.get_sp_and_ip_sub_slots(peak.header_hash)
                assert sub_slots_for_peak is not None
                ip_sub_slot: Optional[EndOfSubSlotBundle] = sub_slots_for_peak[1]
            else:
                sub_slot_iters = self.full_node.constants.SUB_SLOT_ITERS_STARTING
                next_sub_slot_iters = sub_slot_iters
                ip_sub_slot = None

            added = self.full_node.full_node_store.new_signage_point(
                request.index_from_challenge,
                self.full_node.blockchain,
                self.full_node.blockchain.get_peak(),
                next_sub_slot_iters,
                SignagePoint(
                    request.challenge_chain_vdf,
                    request.challenge_chain_proof,
                    request.reward_chain_vdf,
                    request.reward_chain_proof,
                ),
            )

            if added:
                await self.full_node.signage_point_post_processing(request, peer, ip_sub_slot)
            else:
                self.log.debug(
                    f"Signage point {request.index_from_challenge} not added, CC challenge: "
                    f"{request.challenge_chain_vdf.challenge.hex()}, "
                    f"RC challenge: {request.reward_chain_vdf.challenge.hex()}"
                )

            return None

    @metadata.request(peer_required=True)
    async def respond_end_of_sub_slot(
        self, request: full_node_protocol.RespondEndOfSubSlot, peer: WSChiaConnection
    ) -> Optional[Message]:
        if self.full_node.sync_store.get_sync_mode():
            return None
        msg, _ = await self.full_node.add_end_of_sub_slot(request.end_of_slot_bundle, peer)
        return msg

    @metadata.request(peer_required=True)
    async def request_mempool_transactions(
        self,
        request: full_node_protocol.RequestMempoolTransactions,
        peer: WSChiaConnection,
    ) -> Optional[Message]:
        received_filter = PyBIP158(bytearray(request.filter))

        items: list[SpendBundle] = self.full_node.mempool_manager.get_items_not_in_filter(received_filter)

        for item in items:
            transaction = full_node_protocol.RespondTransaction(item)
            msg = make_msg(ProtocolMessageTypes.respond_transaction, transaction)
            await peer.send_message(msg)
        return None

    # FARMER PROTOCOL
    @metadata.request(peer_required=True)
    async def declare_proof_of_space(
        self, request: farmer_protocol.DeclareProofOfSpace, peer: WSChiaConnection
    ) -> Optional[Message]:
        """
        Creates a block body and header, with the proof of space, coinbase, and fee targets provided
        by the farmer, and sends the hash of the header data back to the farmer.
        """
        if self.full_node.sync_store.get_sync_mode():
            return None

        async with self.full_node.timelord_lock:
            sp_vdfs: Optional[SignagePoint] = self.full_node.full_node_store.get_signage_point_by_index_and_cc_output(
                request.challenge_chain_sp, request.challenge_hash, request.signage_point_index
            )

            if sp_vdfs is None:
                self.log.warning(f"Received proof of space for an unknown signage point {request.challenge_chain_sp}")
                return None
            if request.signage_point_index > 0:
                assert sp_vdfs.rc_vdf is not None
                if sp_vdfs.rc_vdf.output.get_hash() != request.reward_chain_sp:
                    self.log.debug(
                        f"Received proof of space for a potentially old signage point {request.challenge_chain_sp}. "
                        f"Current sp: {sp_vdfs.rc_vdf.output.get_hash().hex()}"
                    )
                    return None

            if request.signage_point_index == 0:
                cc_challenge_hash: bytes32 = request.challenge_chain_sp
            else:
                assert sp_vdfs.cc_vdf is not None
                cc_challenge_hash = sp_vdfs.cc_vdf.challenge

            pos_sub_slot: Optional[tuple[EndOfSubSlotBundle, int, uint128]] = None
            if request.challenge_hash != self.full_node.constants.GENESIS_CHALLENGE:
                # Checks that the proof of space is a response to a recent challenge and valid SP
                pos_sub_slot = self.full_node.full_node_store.get_sub_slot(cc_challenge_hash)
                if pos_sub_slot is None:
                    self.log.warning(f"Received proof of space for an unknown sub slot: {request}")
                    return None
                total_iters_pos_slot: uint128 = pos_sub_slot[2]
            else:
                total_iters_pos_slot = uint128(0)
            assert cc_challenge_hash == request.challenge_hash

            # Now we know that the proof of space has a signage point either:
            # 1. In the previous sub-slot of the peak (overflow)
            # 2. In the same sub-slot as the peak
            # 3. In a future sub-slot that we already know of

            # Grab best transactions from Mempool for given tip target
            new_block_gen: Optional[NewBlockGenerator]
            async with self.full_node.blockchain.priority_mutex.acquire(priority=BlockchainMutexPriority.high):
                peak: Optional[BlockRecord] = self.full_node.blockchain.get_peak()

                # Checks that the proof of space is valid
                height: uint32
                if peak is None:
                    height = uint32(0)
                else:
                    height = peak.height
                quality_string: Optional[bytes32] = verify_and_get_quality_string(
                    request.proof_of_space,
                    self.full_node.constants,
                    cc_challenge_hash,
                    request.challenge_chain_sp,
                    height=height,
                )
                assert quality_string is not None and len(quality_string) == 32

                if peak is not None:
                    # Finds the last transaction block before this one
                    curr_l_tb: BlockRecord = peak
                    while not curr_l_tb.is_transaction_block:
                        curr_l_tb = self.full_node.blockchain.block_record(curr_l_tb.prev_hash)
                    try:
                        # TODO: once we're confident in the new block creation,
                        # make it default to 1
                        block_version = self.full_node.config.get("block_creation", 0)
                        block_timeout = self.full_node.config.get("block_creation_timeout", 2.0)
                        if block_version == 0:
                            create_block = self.full_node.mempool_manager.create_block_generator
                        elif block_version == 1:
                            create_block = self.full_node.mempool_manager.create_block_generator2
                        else:
                            self.log.warning(f"Unknown 'block_creation' config: {block_version}")
                            create_block = self.full_node.mempool_manager.create_block_generator

                        new_block_gen = create_block(curr_l_tb.header_hash, block_timeout)

                        if (
                            new_block_gen is not None and peak.height < self.full_node.constants.HARD_FORK_HEIGHT
                        ):  # pragma: no cover
                            self.log.error("Cannot farm blocks pre-hard fork")

                    except Exception as e:
                        self.log.error(f"Traceback: {traceback.format_exc()}")
                        self.full_node.log.error(f"Error making spend bundle {e} peak: {peak}")
                        new_block_gen = None
                else:
                    new_block_gen = None

            def get_plot_sig(to_sign: bytes32, _extra: G1Element) -> G2Element:
                if to_sign == request.challenge_chain_sp:
                    return request.challenge_chain_sp_signature
                elif to_sign == request.reward_chain_sp:
                    return request.reward_chain_sp_signature
                return G2Element()

            def get_pool_sig(_1: PoolTarget, _2: Optional[G1Element]) -> Optional[G2Element]:
                return request.pool_signature

            prev_b: Optional[BlockRecord] = peak

            # Finds the previous block from the signage point, ensuring that the reward chain VDF is correct
            if prev_b is not None:
                if request.signage_point_index == 0:
                    if pos_sub_slot is None:
                        self.log.warning("Pos sub slot is None")
                        return None
                    rc_challenge = pos_sub_slot[0].reward_chain.end_of_slot_vdf.challenge
                else:
                    assert sp_vdfs.rc_vdf is not None
                    rc_challenge = sp_vdfs.rc_vdf.challenge

                # Backtrack through empty sub-slots
                for eos, _, _ in reversed(self.full_node.full_node_store.finished_sub_slots):
                    if eos is not None and eos.reward_chain.get_hash() == rc_challenge:
                        rc_challenge = eos.reward_chain.end_of_slot_vdf.challenge

                found = False
                attempts = 0
                while prev_b is not None and attempts < 10:
                    if prev_b.reward_infusion_new_challenge == rc_challenge:
                        found = True
                        break
                    if prev_b.finished_reward_slot_hashes is not None and len(prev_b.finished_reward_slot_hashes) > 0:
                        if prev_b.finished_reward_slot_hashes[-1] == rc_challenge:
                            # This block includes a sub-slot which is where our SP vdf starts. Go back one more
                            # to find the prev block
                            prev_b = self.full_node.blockchain.try_block_record(prev_b.prev_hash)
                            found = True
                            break
                    prev_b = self.full_node.blockchain.try_block_record(prev_b.prev_hash)
                    attempts += 1
                if not found:
                    self.log.warning("Did not find a previous block with the correct reward chain hash")
                    return None

            try:
                finished_sub_slots: Optional[list[EndOfSubSlotBundle]] = (
                    self.full_node.full_node_store.get_finished_sub_slots(
                        self.full_node.blockchain, prev_b, cc_challenge_hash
                    )
                )
                if finished_sub_slots is None:
                    return None

                if (
                    len(finished_sub_slots) > 0
                    and pos_sub_slot is not None
                    and finished_sub_slots[-1] != pos_sub_slot[0]
                ):
                    self.log.error("Have different sub-slots than is required to farm this block")
                    return None
            except ValueError as e:
                self.log.warning(f"Value Error: {e}")
                return None
            if prev_b is None:
                pool_target = PoolTarget(
                    self.full_node.constants.GENESIS_PRE_FARM_POOL_PUZZLE_HASH,
                    uint32(0),
                )
                farmer_ph = self.full_node.constants.GENESIS_PRE_FARM_FARMER_PUZZLE_HASH
            else:
                farmer_ph = request.farmer_puzzle_hash
                if request.proof_of_space.pool_contract_puzzle_hash is not None:
                    pool_target = PoolTarget(request.proof_of_space.pool_contract_puzzle_hash, uint32(0))
                else:
                    assert request.pool_target is not None
                    pool_target = request.pool_target

            if peak is None or peak.height <= self.full_node.constants.MAX_SUB_SLOT_BLOCKS:
                difficulty = self.full_node.constants.DIFFICULTY_STARTING
                sub_slot_iters = self.full_node.constants.SUB_SLOT_ITERS_STARTING
            else:
                difficulty = uint64(peak.weight - self.full_node.blockchain.block_record(peak.prev_hash).weight)
                sub_slot_iters = peak.sub_slot_iters
                for sub_slot in finished_sub_slots:
                    if sub_slot.challenge_chain.new_difficulty is not None:
                        difficulty = sub_slot.challenge_chain.new_difficulty
                    if sub_slot.challenge_chain.new_sub_slot_iters is not None:
                        sub_slot_iters = sub_slot.challenge_chain.new_sub_slot_iters

            tx_peak = self.full_node.blockchain.get_tx_peak()
            required_iters: uint64 = calculate_iterations_quality(
                self.full_node.constants,
                quality_string,
                request.proof_of_space.size(),
                difficulty,
                request.challenge_chain_sp,
                sub_slot_iters,
                tx_peak.height if tx_peak is not None else uint32(0),
            )
            sp_iters: uint64 = calculate_sp_iters(self.full_node.constants, sub_slot_iters, request.signage_point_index)
            ip_iters: uint64 = calculate_ip_iters(
                self.full_node.constants,
                sub_slot_iters,
                request.signage_point_index,
                required_iters,
            )

            # The block's timestamp must be greater than the previous transaction block's timestamp
            timestamp = uint64(time.time())
            curr: Optional[BlockRecord] = prev_b
            while curr is not None and not curr.is_transaction_block and curr.height != 0:
                curr = self.full_node.blockchain.try_block_record(curr.prev_hash)
            if curr is not None:
                assert curr.timestamp is not None
                if timestamp <= curr.timestamp:
                    timestamp = uint64(curr.timestamp + 1)

            self.log.info("Starting to make the unfinished block")
            unfinished_block: UnfinishedBlock = create_unfinished_block(
                self.full_node.constants,
                total_iters_pos_slot,
                sub_slot_iters,
                request.signage_point_index,
                sp_iters,
                ip_iters,
                request.proof_of_space,
                cc_challenge_hash,
                farmer_ph,
                pool_target,
                get_plot_sig,
                get_pool_sig,
                sp_vdfs,
                timestamp,
                self.full_node.blockchain,
                b"",
                new_block_gen,
                prev_b,
                finished_sub_slots,
            )
            self.log.info("Made the unfinished block")
            if prev_b is not None:
                height = uint32(prev_b.height + 1)
            else:
                height = uint32(0)
            self.full_node.full_node_store.add_candidate_block(quality_string, height, unfinished_block)

            foliage_sb_data_hash = unfinished_block.foliage.foliage_block_data.get_hash()
            if unfinished_block.is_transaction_block():
                foliage_transaction_block_hash = unfinished_block.foliage.foliage_transaction_block_hash
            else:
                foliage_transaction_block_hash = bytes32.zeros
            assert foliage_transaction_block_hash is not None

            foliage_block_data: Optional[FoliageBlockData] = None
            foliage_transaction_block_data: Optional[FoliageTransactionBlock] = None
            rc_block_unfinished: Optional[RewardChainBlockUnfinished] = None
            if request.include_signature_source_data:
                foliage_block_data = unfinished_block.foliage.foliage_block_data
                rc_block_unfinished = unfinished_block.reward_chain_block
                if unfinished_block.is_transaction_block():
                    foliage_transaction_block_data = unfinished_block.foliage_transaction_block

            message = farmer_protocol.RequestSignedValues(
                quality_string,
                foliage_sb_data_hash,
                foliage_transaction_block_hash,
                foliage_block_data=foliage_block_data,
                foliage_transaction_block_data=foliage_transaction_block_data,
                rc_block_unfinished=rc_block_unfinished,
            )
            await peer.send_message(make_msg(ProtocolMessageTypes.request_signed_values, message))

            # Adds backup in case the first one fails
            if unfinished_block.is_transaction_block() and unfinished_block.transactions_generator is not None:
                unfinished_block_backup = create_unfinished_block(
                    self.full_node.constants,
                    total_iters_pos_slot,
                    sub_slot_iters,
                    request.signage_point_index,
                    sp_iters,
                    ip_iters,
                    request.proof_of_space,
                    cc_challenge_hash,
                    farmer_ph,
                    pool_target,
                    get_plot_sig,
                    get_pool_sig,
                    sp_vdfs,
                    timestamp,
                    self.full_node.blockchain,
                    b"",
                    None,
                    prev_b,
                    finished_sub_slots,
                )

                self.full_node.full_node_store.add_candidate_block(
                    quality_string, height, unfinished_block_backup, backup=True
                )
        return None

    @metadata.request(peer_required=True)
    async def signed_values(
        self, farmer_request: farmer_protocol.SignedValues, peer: WSChiaConnection
    ) -> Optional[Message]:
        """
        Signature of header hash, by the harvester. This is enough to create an unfinished
        block, which only needs a Proof of Time to be finished. If the signature is valid,
        we call the unfinished_block routine.
        """
        candidate_tuple: Optional[tuple[uint32, UnfinishedBlock]] = self.full_node.full_node_store.get_candidate_block(
            farmer_request.quality_string
        )

        if candidate_tuple is None:
            self.log.warning(f"Quality string {farmer_request.quality_string} not found in database")
            return None
        height, candidate = candidate_tuple

        if not AugSchemeMPL.verify(
            candidate.reward_chain_block.proof_of_space.plot_public_key,
            candidate.foliage.foliage_block_data.get_hash(),
            farmer_request.foliage_block_data_signature,
        ):
            self.log.warning("Signature not valid. There might be a collision in plots. Ignore this during tests.")
            return None

        fsb2 = candidate.foliage.replace(foliage_block_data_signature=farmer_request.foliage_block_data_signature)
        if candidate.is_transaction_block():
            fsb2 = fsb2.replace(foliage_transaction_block_signature=farmer_request.foliage_transaction_block_signature)

        new_candidate = candidate.replace(foliage=fsb2)
        if not self.full_node.has_valid_pool_sig(new_candidate):
            self.log.warning("Trying to make a pre-farm block but height is not 0")
            return None

        # Propagate to ourselves (which validates and does further propagations)
        try:
            await self.full_node.add_unfinished_block(new_candidate, None, True)
        except Exception as e:
            # If we have an error with this block, try making an empty block
            self.full_node.log.error(f"Error farming block {e} {new_candidate}")
            candidate_tuple = self.full_node.full_node_store.get_candidate_block(
                farmer_request.quality_string, backup=True
            )
            if candidate_tuple is not None:
                height, unfinished_block = candidate_tuple
                self.full_node.full_node_store.add_candidate_block(
                    farmer_request.quality_string, height, unfinished_block, False
                )
                # All unfinished blocks that we create will have the foliage transaction block and hash
                assert unfinished_block.foliage.foliage_transaction_block_hash is not None
                message = farmer_protocol.RequestSignedValues(
                    farmer_request.quality_string,
                    unfinished_block.foliage.foliage_block_data.get_hash(),
                    unfinished_block.foliage.foliage_transaction_block_hash,
                )
                await peer.send_message(make_msg(ProtocolMessageTypes.request_signed_values, message))
        return None

    # TIMELORD PROTOCOL
    @metadata.request(peer_required=True)
    async def new_infusion_point_vdf(
        self, request: timelord_protocol.NewInfusionPointVDF, peer: WSChiaConnection
    ) -> Optional[Message]:
        if self.full_node.sync_store.get_sync_mode():
            return None
        # Lookup unfinished blocks
        async with self.full_node.timelord_lock:
            return await self.full_node.new_infusion_point_vdf(request, peer)

    @metadata.request(peer_required=True)
    async def new_signage_point_vdf(
        self, request: timelord_protocol.NewSignagePointVDF, peer: WSChiaConnection
    ) -> None:
        if self.full_node.sync_store.get_sync_mode():
            return None

        full_node_message = full_node_protocol.RespondSignagePoint(
            request.index_from_challenge,
            request.challenge_chain_sp_vdf,
            request.challenge_chain_sp_proof,
            request.reward_chain_sp_vdf,
            request.reward_chain_sp_proof,
        )
        await self.respond_signage_point(full_node_message, peer)

    @metadata.request(peer_required=True)
    async def new_end_of_sub_slot_vdf(
        self, request: timelord_protocol.NewEndOfSubSlotVDF, peer: WSChiaConnection
    ) -> Optional[Message]:
        if self.full_node.sync_store.get_sync_mode():
            return None
        if (
            self.full_node.full_node_store.get_sub_slot(request.end_of_sub_slot_bundle.challenge_chain.get_hash())
            is not None
        ):
            return None
        # Calls our own internal message to handle the end of sub slot, and potentially broadcasts to other peers.
        msg, added = await self.full_node.add_end_of_sub_slot(request.end_of_sub_slot_bundle, peer)
        if not added:
            self.log.error(
                f"Was not able to add end of sub-slot: "
                f"{request.end_of_sub_slot_bundle.challenge_chain.challenge_chain_end_of_slot_vdf.challenge.hex()}. "
                f"Re-sending new-peak to timelord"
            )
            await self.full_node.send_peak_to_timelords(peer=peer)
            return None
        else:
            return msg

    @metadata.request()
    async def request_block_header(self, request: wallet_protocol.RequestBlockHeader) -> Optional[Message]:
        header_hash = self.full_node.blockchain.height_to_hash(request.height)
        if header_hash is None:
            msg = make_msg(ProtocolMessageTypes.reject_header_request, RejectHeaderRequest(request.height))
            return msg
        block: Optional[FullBlock] = await self.full_node.block_store.get_full_block(header_hash)
        if block is None:
            return None

        removals_and_additions: Optional[tuple[Collection[bytes32], Collection[Coin]]] = None

        if block.transactions_generator is not None:
            block_generator: Optional[BlockGenerator] = await get_block_generator(
                self.full_node.blockchain.lookup_block_generators, block
            )
            # get_block_generator() returns None in case the block we specify
            # does not have a generator (i.e. is not a transaction block).
            # in this case we've already made sure `block` does have a
            # transactions_generator, so the block_generator should always be set
            assert block_generator is not None, "failed to get block_generator for tx-block"

            flags = get_flags_for_height_and_constants(request.height, self.full_node.constants)
            additions, removals = await asyncio.get_running_loop().run_in_executor(
                self.executor,
                additions_and_removals,
                bytes(block.transactions_generator),
                block_generator.generator_refs,
                flags,
                self.full_node.constants,
            )
            # strip the hint from additions, and compute the puzzle hash for
            # removals
            removals_and_additions = ([name for name, _ in removals], [name for name, _ in additions])
        elif block.is_transaction_block():
            # This is a transaction block with just reward coins.
            removals_and_additions = ([], [])

        header_block = get_block_header(block, removals_and_additions)
        msg = make_msg(
            ProtocolMessageTypes.respond_block_header,
            wallet_protocol.RespondBlockHeader(header_block),
        )
        return msg

    @metadata.request()
    async def request_additions(self, request: wallet_protocol.RequestAdditions) -> Optional[Message]:
        if request.header_hash is None:
            header_hash: Optional[bytes32] = self.full_node.blockchain.height_to_hash(request.height)
        else:
            header_hash = request.header_hash
        if header_hash is None:
            raise ValueError(f"Block at height {request.height} not found")

        # Note: this might return bad data if there is a reorg in this time
        additions = await self.full_node.coin_store.get_coins_added_at_height(request.height)

        if self.full_node.blockchain.height_to_hash(request.height) != header_hash:
            raise ValueError(f"Block {header_hash} no longer in chain, or invalid header_hash")

        puzzlehash_coins_map: dict[bytes32, list[Coin]] = {}
        for coin_record in additions:
            if coin_record.coin.puzzle_hash in puzzlehash_coins_map:
                puzzlehash_coins_map[coin_record.coin.puzzle_hash].append(coin_record.coin)
            else:
                puzzlehash_coins_map[coin_record.coin.puzzle_hash] = [coin_record.coin]

        coins_map: list[tuple[bytes32, list[Coin]]] = []
        proofs_map: list[tuple[bytes32, bytes, Optional[bytes]]] = []

        if request.puzzle_hashes is None:
            for puzzle_hash, coins in puzzlehash_coins_map.items():
                coins_map.append((puzzle_hash, coins))
            response = wallet_protocol.RespondAdditions(request.height, header_hash, coins_map, None)
        else:
            # Create addition Merkle set
            # Addition Merkle set contains puzzlehash and hash of all coins with that puzzlehash
            leafs: list[bytes32] = []
            for puzzle, coins in puzzlehash_coins_map.items():
                leafs.append(puzzle)
                leafs.append(hash_coin_ids([c.name() for c in coins]))

            addition_merkle_set = MerkleSet(leafs)

            for puzzle_hash in request.puzzle_hashes:
                # This is a proof of inclusion if it's in (result==True), or exclusion of it's not in
                result, proof = addition_merkle_set.is_included_already_hashed(puzzle_hash)
                if puzzle_hash in puzzlehash_coins_map:
                    coins_map.append((puzzle_hash, puzzlehash_coins_map[puzzle_hash]))
                    hash_coin_str = hash_coin_ids([c.name() for c in puzzlehash_coins_map[puzzle_hash]])
                    # This is a proof of inclusion of all coin ids that have this ph
                    result_2, proof_2 = addition_merkle_set.is_included_already_hashed(hash_coin_str)
                    assert result
                    assert result_2
                    proofs_map.append((puzzle_hash, proof, proof_2))
                else:
                    coins_map.append((puzzle_hash, []))
                    assert not result
                    proofs_map.append((puzzle_hash, proof, None))
            response = wallet_protocol.RespondAdditions(request.height, header_hash, coins_map, proofs_map)
        return make_msg(ProtocolMessageTypes.respond_additions, response)

    @metadata.request()
    async def request_removals(self, request: wallet_protocol.RequestRemovals) -> Optional[Message]:
        block: Optional[FullBlock] = await self.full_node.block_store.get_full_block(request.header_hash)

        # We lock so that the coin store does not get modified
        peak_height = self.full_node.blockchain.get_peak_height()
        if (
            block is None
            or block.is_transaction_block() is False
            or block.height != request.height
            or (peak_height is not None and block.height > peak_height)
            or self.full_node.blockchain.height_to_hash(block.height) != request.header_hash
        ):
            reject = wallet_protocol.RejectRemovalsRequest(request.height, request.header_hash)
            msg = make_msg(ProtocolMessageTypes.reject_removals_request, reject)
            return msg

        assert block is not None and block.foliage_transaction_block is not None

        # Note: this might return bad data if there is a reorg in this time
        all_removals: list[CoinRecord] = await self.full_node.coin_store.get_coins_removed_at_height(block.height)

        if self.full_node.blockchain.height_to_hash(block.height) != request.header_hash:
            raise ValueError(f"Block {block.header_hash} no longer in chain")

        all_removals_dict: dict[bytes32, Coin] = {}
        for coin_record in all_removals:
            all_removals_dict[coin_record.coin.name()] = coin_record.coin

        coins_map: list[tuple[bytes32, Optional[Coin]]] = []
        proofs_map: list[tuple[bytes32, bytes]] = []

        # If there are no transactions, respond with empty lists
        if block.transactions_generator is None:
            proofs: Optional[list[tuple[bytes32, bytes]]]
            if request.coin_names is None:
                proofs = None
            else:
                proofs = []
            response = wallet_protocol.RespondRemovals(block.height, block.header_hash, [], proofs)
        elif request.coin_names is None or len(request.coin_names) == 0:
            for removed_name, removed_coin in all_removals_dict.items():
                coins_map.append((removed_name, removed_coin))
            response = wallet_protocol.RespondRemovals(block.height, block.header_hash, coins_map, None)
        else:
            assert block.transactions_generator
            leafs: list[bytes32] = []
            for removed_name, removed_coin in all_removals_dict.items():
                leafs.append(removed_name)
            removal_merkle_set = MerkleSet(leafs)
            assert removal_merkle_set.get_root() == block.foliage_transaction_block.removals_root
            for coin_name in request.coin_names:
                result, proof = removal_merkle_set.is_included_already_hashed(coin_name)
                proofs_map.append((coin_name, proof))
                if coin_name in all_removals_dict:
                    removed_coin = all_removals_dict[coin_name]
                    coins_map.append((coin_name, removed_coin))
                    assert result
                else:
                    coins_map.append((coin_name, None))
                    assert not result
            response = wallet_protocol.RespondRemovals(block.height, block.header_hash, coins_map, proofs_map)

        msg = make_msg(ProtocolMessageTypes.respond_removals, response)
        return msg

    @metadata.request()
    async def send_transaction(
        self, request: wallet_protocol.SendTransaction, *, test: bool = False
    ) -> Optional[Message]:
        spend_name = request.transaction.name()
        if self.full_node.mempool_manager.get_spendbundle(spend_name) is not None:
            self.full_node.mempool_manager.remove_seen(spend_name)
            response = wallet_protocol.TransactionAck(spend_name, uint8(MempoolInclusionStatus.SUCCESS), None)
            return make_msg(ProtocolMessageTypes.transaction_ack, response)

        queue_entry = TransactionQueueEntry(request.transaction, None, spend_name, None, test)
        await self.full_node.transaction_queue.put(queue_entry, peer_id=None, high_priority=True)
        try:
            with anyio.fail_after(delay=45):
                status, error = await queue_entry.done.wait()
        except TimeoutError:
            response = wallet_protocol.TransactionAck(spend_name, uint8(MempoolInclusionStatus.PENDING), None)
        else:
            error_name = error.name if error is not None else None
            if status == MempoolInclusionStatus.SUCCESS:
                response = wallet_protocol.TransactionAck(spend_name, uint8(status.value), error_name)
            else:
                # If it failed/pending, but it previously succeeded (in mempool), this is idempotence, return SUCCESS
                if self.full_node.mempool_manager.get_spendbundle(spend_name) is not None:
                    response = wallet_protocol.TransactionAck(
                        spend_name, uint8(MempoolInclusionStatus.SUCCESS.value), None
                    )
                else:
                    response = wallet_protocol.TransactionAck(spend_name, uint8(status.value), error_name)
        return make_msg(ProtocolMessageTypes.transaction_ack, response)

    @metadata.request()
    async def request_puzzle_solution(self, request: wallet_protocol.RequestPuzzleSolution) -> Optional[Message]:
        coin_name = request.coin_name
        height = request.height
        coin_record = await self.full_node.coin_store.get_coin_record(coin_name)
        reject = wallet_protocol.RejectPuzzleSolution(coin_name, height)
        reject_msg = make_msg(ProtocolMessageTypes.reject_puzzle_solution, reject)
        if coin_record is None or coin_record.spent_block_index != height:
            return reject_msg

        header_hash: Optional[bytes32] = self.full_node.blockchain.height_to_hash(height)
        if header_hash is None:
            return reject_msg

        block: Optional[BlockInfo] = await self.full_node.block_store.get_block_info(header_hash)

        if block is None or block.transactions_generator is None:
            return reject_msg

        block_generator: Optional[BlockGenerator] = await get_block_generator(
            self.full_node.blockchain.lookup_block_generators, block
        )
        assert block_generator is not None
        try:
            puzzle, solution = await asyncio.get_running_loop().run_in_executor(
                self.executor,
                get_puzzle_and_solution_for_coin,
                block_generator.program,
                block_generator.generator_refs,
                self.full_node.constants.MAX_BLOCK_COST_CLVM,
                coin_record.coin,
                get_flags_for_height_and_constants(height, self.full_node.constants),
            )
        except ValueError:
            return reject_msg
        wrapper = PuzzleSolutionResponse(coin_name, height, puzzle, solution)
        response = wallet_protocol.RespondPuzzleSolution(wrapper)
        response_msg = make_msg(ProtocolMessageTypes.respond_puzzle_solution, response)
        return response_msg

    @metadata.request()
    async def request_block_headers(self, request: wallet_protocol.RequestBlockHeaders) -> Optional[Message]:
        """Returns header blocks by directly streaming bytes into Message

        This method should be used instead of RequestHeaderBlocks
        """
        reject = RejectBlockHeaders(request.start_height, request.end_height)

        if request.end_height < request.start_height or request.end_height - request.start_height > 128:
            return make_msg(ProtocolMessageTypes.reject_block_headers, reject)
        try:
            blocks_bytes = await self.full_node.block_store.get_block_bytes_in_range(
                request.start_height, request.end_height
            )
        except ValueError:
            return make_msg(ProtocolMessageTypes.reject_block_headers, reject)

        if len(blocks_bytes) != (request.end_height - request.start_height + 1):  # +1 because interval is inclusive
            return make_msg(ProtocolMessageTypes.reject_block_headers, reject)
        return_filter = request.return_filter
        header_blocks_bytes: list[bytes] = []
        for b in blocks_bytes:
            b_mem_view = memoryview(b)
            height, is_tx_block = get_height_and_tx_status_from_block(b_mem_view)
            if not is_tx_block:
                tx_addition_coins = []
                removal_names = []
            else:
                added_coins_records_coroutine = self.full_node.coin_store.get_coins_added_at_height(height)
                removed_coins_records_coroutine = self.full_node.coin_store.get_coins_removed_at_height(height)
                added_coins_records, removed_coins_records = await asyncio.gather(
                    added_coins_records_coroutine, removed_coins_records_coroutine
                )
                tx_addition_coins = [record.coin for record in added_coins_records if not record.coinbase]
                removal_names = [record.coin.name() for record in removed_coins_records]
            header_blocks_bytes.append(
                header_block_from_block(b_mem_view, return_filter, tx_addition_coins, removal_names)
            )

        # we're building the RespondHeaderBlocks manually to avoid cost of
        # dynamic serialization
        # ---
        # we start building RespondBlockHeaders response (start_height, end_height)
        # and then need to define size of list object
        respond_header_blocks_manually_streamed: bytes = (
            uint32(request.start_height).stream_to_bytes()
            + uint32(request.end_height).stream_to_bytes()
            + uint32(len(header_blocks_bytes)).stream_to_bytes()
        )
        # and now stream the whole list in bytes
        respond_header_blocks_manually_streamed += b"".join(header_blocks_bytes)
        return make_msg(ProtocolMessageTypes.respond_block_headers, respond_header_blocks_manually_streamed)

    @metadata.request()
    async def request_header_blocks(self, request: wallet_protocol.RequestHeaderBlocks) -> Optional[Message]:
        """DEPRECATED: please use RequestBlockHeaders"""
        if (
            request.end_height < request.start_height
            or request.end_height - request.start_height > self.full_node.constants.MAX_BLOCK_COUNT_PER_REQUESTS
        ):
            return None
        height_to_hash = self.full_node.blockchain.height_to_hash
        header_hashes: list[bytes32] = []
        for i in range(request.start_height, request.end_height + 1):
            header_hash: Optional[bytes32] = height_to_hash(uint32(i))
            if header_hash is None:
                reject = RejectHeaderBlocks(request.start_height, request.end_height)
                msg = make_msg(ProtocolMessageTypes.reject_header_blocks, reject)
                return msg
            header_hashes.append(header_hash)

        blocks: list[FullBlock] = await self.full_node.block_store.get_blocks_by_hash(header_hashes)
        header_blocks = []
        for block in blocks:
            added_coins_records_coroutine = self.full_node.coin_store.get_coins_added_at_height(block.height)
            removed_coins_records_coroutine = self.full_node.coin_store.get_coins_removed_at_height(block.height)
            added_coins_records, removed_coins_records = await asyncio.gather(
                added_coins_records_coroutine, removed_coins_records_coroutine
            )
            added_coins = [record.coin for record in added_coins_records if not record.coinbase]
            removal_names = [record.coin.name() for record in removed_coins_records]
            header_block = get_block_header(block, (removal_names, added_coins))
            header_blocks.append(header_block)

        msg = make_msg(
            ProtocolMessageTypes.respond_header_blocks,
            wallet_protocol.RespondHeaderBlocks(request.start_height, request.end_height, header_blocks),
        )
        return msg

    @metadata.request(bytes_required=True, execute_task=True)
    async def respond_compact_proof_of_time(
        self, request: timelord_protocol.RespondCompactProofOfTime, request_bytes: bytes = b""
    ) -> None:
        if self.full_node.sync_store.get_sync_mode():
            return None
        name = std_hash(request_bytes)
        if name in self.full_node.compact_vdf_requests:
            self.log.debug(f"Ignoring CompactProofOfTime: {request}, already requested")
            return None

        self.full_node.compact_vdf_requests.add(name)

        # this semaphore will only allow a limited number of tasks call
        # new_compact_vdf() at a time, since it can be expensive
        try:
            async with self.full_node.compact_vdf_sem.acquire():
                try:
                    await self.full_node.add_compact_proof_of_time(request)
                finally:
                    self.full_node.compact_vdf_requests.remove(name)
        except LimitedSemaphoreFullError:
            self.log.debug(f"Ignoring CompactProofOfTime: {request}, _waiters")

        return None

    @metadata.request(peer_required=True, bytes_required=True, execute_task=True)
    async def new_compact_vdf(
        self, request: full_node_protocol.NewCompactVDF, peer: WSChiaConnection, request_bytes: bytes = b""
    ) -> None:
        if self.full_node.sync_store.get_sync_mode():
            return None

        name = std_hash(request_bytes)
        if name in self.full_node.compact_vdf_requests:
            self.log.debug("Ignoring NewCompactVDF, already requested: %s %s", peer.get_peer_logging(), request)
            return None
        self.full_node.compact_vdf_requests.add(name)

        # this semaphore will only allow a limited number of tasks call
        # new_compact_vdf() at a time, since it can be expensive
        try:
            async with self.full_node.compact_vdf_sem.acquire():
                try:
                    await self.full_node.new_compact_vdf(request, peer)
                finally:
                    self.full_node.compact_vdf_requests.remove(name)
        except LimitedSemaphoreFullError:
            self.log.debug("Ignoring NewCompactVDF, limited semaphore full: %s %s", peer.get_peer_logging(), request)
            return None

        return None

    @metadata.request(peer_required=True, reply_types=[ProtocolMessageTypes.respond_compact_vdf])
    async def request_compact_vdf(self, request: full_node_protocol.RequestCompactVDF, peer: WSChiaConnection) -> None:
        if self.full_node.sync_store.get_sync_mode():
            return None
        await self.full_node.request_compact_vdf(request, peer)
        return None

    @metadata.request(peer_required=True)
    async def respond_compact_vdf(self, request: full_node_protocol.RespondCompactVDF, peer: WSChiaConnection) -> None:
        if self.full_node.sync_store.get_sync_mode():
            return None
        await self.full_node.add_compact_vdf(request, peer)
        return None

    @metadata.request(peer_required=True)
    async def register_for_ph_updates(
        self, request: wallet_protocol.RegisterForPhUpdates, peer: WSChiaConnection
    ) -> Message:
        trusted = self.is_trusted(peer)
        max_items = self.max_subscribe_response_items(peer)
        max_subscriptions = self.max_subscriptions(peer)

        # the returned puzzle hashes are the ones we ended up subscribing to.
        # It will have filtered duplicates and ones exceeding the subscription
        # limit.
        puzzle_hashes = self.full_node.subscriptions.add_puzzle_subscriptions(
            peer.peer_node_id, request.puzzle_hashes, max_subscriptions
        )

        start_time = time.monotonic()

        # Note that coin state updates may arrive out-of-order on the client side.
        # We add the subscription before we're done collecting all the coin
        # state that goes into the response. CoinState updates may be sent
        # before we send the response

        # Send all coins with requested puzzle hash that have been created after the specified height
        states: set[CoinState] = await self.full_node.coin_store.get_coin_states_by_puzzle_hashes(
            include_spent_coins=True, puzzle_hashes=puzzle_hashes, min_height=request.min_height, max_items=max_items
        )
        max_items -= len(states)

        hint_coin_ids = await self.full_node.hint_store.get_coin_ids_multi(
            cast(set[bytes], puzzle_hashes), max_items=max_items
        )

        hint_states: list[CoinState] = []
        if len(hint_coin_ids) > 0:
            hint_states = await self.full_node.coin_store.get_coin_states_by_ids(
                include_spent_coins=True,
                coin_ids=hint_coin_ids,
                min_height=request.min_height,
                max_items=len(hint_coin_ids),
            )
            states.update(hint_states)

        end_time = time.monotonic()

        truncated = max_items <= 0

        if truncated or end_time - start_time > 5:
            self.log.log(
                logging.WARNING if trusted and truncated else logging.INFO,
                "RegisterForPhUpdates resulted in %d coin states. "
                "Request had %d (unique) puzzle hashes and matched %d hints. %s"
                "The request took %0.2fs",
                len(states),
                len(puzzle_hashes),
                len(hint_states),
                "The response was truncated. " if truncated else "",
                end_time - start_time,
            )

        response = RespondToPhUpdates(request.puzzle_hashes, request.min_height, list(states))
        msg = make_msg(ProtocolMessageTypes.respond_to_ph_updates, response)
        return msg

    @metadata.request(peer_required=True)
    async def register_for_coin_updates(
        self, request: wallet_protocol.RegisterForCoinUpdates, peer: WSChiaConnection
    ) -> Message:
        max_items = self.max_subscribe_response_items(peer)
        max_subscriptions = self.max_subscriptions(peer)

        # TODO: apparently we have tests that expect to receive a
        # RespondToCoinUpdates even when subscribing to the same coin multiple
        # times, so we can't optimize away such DB lookups (yet)
        self.full_node.subscriptions.add_coin_subscriptions(peer.peer_node_id, request.coin_ids, max_subscriptions)

        states: list[CoinState] = await self.full_node.coin_store.get_coin_states_by_ids(
            include_spent_coins=True, coin_ids=set(request.coin_ids), min_height=request.min_height, max_items=max_items
        )

        response = wallet_protocol.RespondToCoinUpdates(request.coin_ids, request.min_height, states)
        msg = make_msg(ProtocolMessageTypes.respond_to_coin_updates, response)
        return msg

    @metadata.request()
    async def request_children(self, request: wallet_protocol.RequestChildren) -> Optional[Message]:
        coin_records: list[CoinRecord] = await self.full_node.coin_store.get_coin_records_by_parent_ids(
            True, [request.coin_name]
        )
        states = [record.coin_state for record in coin_records]
        response = wallet_protocol.RespondChildren(states)
        msg = make_msg(ProtocolMessageTypes.respond_children, response)
        return msg

    @metadata.request()
    async def request_ses_hashes(self, request: wallet_protocol.RequestSESInfo) -> Message:
        """Returns the start and end height of a sub-epoch for the height specified in request"""

        ses_height = self.full_node.blockchain.get_ses_heights()
        start_height = request.start_height
        end_height = request.end_height
        ses_hash_heights = []
        ses_reward_hashes = []

        for idx, ses_start_height in enumerate(ses_height):
            if idx == len(ses_height) - 1:
                break

            next_ses_height = ses_height[idx + 1]
            # start_ses_hash
            if ses_start_height <= start_height < next_ses_height:
                ses_hash_heights.append([ses_start_height, next_ses_height])
                ses: SubEpochSummary = self.full_node.blockchain.get_ses(ses_start_height)
                ses_reward_hashes.append(ses.reward_chain_hash)
                if ses_start_height < end_height < next_ses_height:
                    break
                else:
                    if idx == len(ses_height) - 2:
                        break
                    # else add extra ses as request start <-> end spans two ses
                    next_next_height = ses_height[idx + 2]
                    ses_hash_heights.append([next_ses_height, next_next_height])
                    nex_ses: SubEpochSummary = self.full_node.blockchain.get_ses(next_ses_height)
                    ses_reward_hashes.append(nex_ses.reward_chain_hash)
                    break

        response = RespondSESInfo(ses_reward_hashes, ses_hash_heights)
        msg = make_msg(ProtocolMessageTypes.respond_ses_hashes, response)
        return msg

    @metadata.request(reply_types=[ProtocolMessageTypes.respond_fee_estimates])
    async def request_fee_estimates(self, request: wallet_protocol.RequestFeeEstimates) -> Message:
        def get_fee_estimates(est: FeeEstimatorInterface, req_times: list[uint64]) -> list[FeeEstimate]:
            now = datetime.now(timezone.utc)
            utc_time = now.replace(tzinfo=timezone.utc)
            utc_now = int(utc_time.timestamp())
            deltas = [max(0, req_ts - utc_now) for req_ts in req_times]
            fee_rates = [est.estimate_fee_rate(time_offset_seconds=d) for d in deltas]
            v1_fee_rates = [fee_rate_v2_to_v1(est) for est in fee_rates]
            return [FeeEstimate(None, req_ts, fee_rate) for req_ts, fee_rate in zip(req_times, v1_fee_rates)]

        fee_estimates: list[FeeEstimate] = get_fee_estimates(
            self.full_node.mempool_manager.mempool.fee_estimator, request.time_targets
        )
        response = RespondFeeEstimates(FeeEstimateGroup(error=None, estimates=fee_estimates))
        msg = make_msg(ProtocolMessageTypes.respond_fee_estimates, response)
        return msg

    @metadata.request(
        peer_required=True,
        reply_types=[ProtocolMessageTypes.respond_remove_puzzle_subscriptions],
    )
    async def request_remove_puzzle_subscriptions(
        self, request: wallet_protocol.RequestRemovePuzzleSubscriptions, peer: WSChiaConnection
    ) -> Message:
        peer_id = peer.peer_node_id
        subs = self.full_node.subscriptions

        if request.puzzle_hashes is None:
            removed = list(subs.puzzle_subscriptions(peer_id))
            subs.clear_puzzle_subscriptions(peer_id)
        else:
            removed = list(subs.remove_puzzle_subscriptions(peer_id, request.puzzle_hashes))

        response = wallet_protocol.RespondRemovePuzzleSubscriptions(removed)
        msg = make_msg(ProtocolMessageTypes.respond_remove_puzzle_subscriptions, response)
        return msg

    @metadata.request(
        peer_required=True,
        reply_types=[ProtocolMessageTypes.respond_remove_coin_subscriptions],
    )
    async def request_remove_coin_subscriptions(
        self, request: wallet_protocol.RequestRemoveCoinSubscriptions, peer: WSChiaConnection
    ) -> Message:
        peer_id = peer.peer_node_id
        subs = self.full_node.subscriptions

        if request.coin_ids is None:
            removed = list(subs.coin_subscriptions(peer_id))
            subs.clear_coin_subscriptions(peer_id)
        else:
            removed = list(subs.remove_coin_subscriptions(peer_id, request.coin_ids))

        response = wallet_protocol.RespondRemoveCoinSubscriptions(removed)
        msg = make_msg(ProtocolMessageTypes.respond_remove_coin_subscriptions, response)
        return msg

    @metadata.request(peer_required=True, reply_types=[ProtocolMessageTypes.respond_puzzle_state])
    async def request_puzzle_state(
        self, request: wallet_protocol.RequestPuzzleState, peer: WSChiaConnection
    ) -> Message:
        max_items = self.max_subscribe_response_items(peer)
        max_subscriptions = self.max_subscriptions(peer)
        subs = self.full_node.subscriptions

        request_puzzle_hashes = list(dict.fromkeys(request.puzzle_hashes))

        # This is a limit imposed by `batch_coin_states_by_puzzle_hashes`, due to the SQLite variable limit.
        # It can be increased in the future, and this protocol should be written and tested in a way that
        # this increase would not break the API.
        count = CoinStore.MAX_PUZZLE_HASH_BATCH_SIZE
        puzzle_hashes = request_puzzle_hashes[:count]

        previous_header_hash = (
            self.full_node.blockchain.height_to_hash(request.previous_height)
            if request.previous_height is not None
            else self.full_node.blockchain.constants.GENESIS_CHALLENGE
        )
        assert previous_header_hash is not None

        if request.header_hash != previous_header_hash:
            rejection = wallet_protocol.RejectPuzzleState(uint8(wallet_protocol.RejectStateReason.REORG))
            msg = make_msg(ProtocolMessageTypes.reject_puzzle_state, rejection)
            return msg

        # Check if the request would exceed the subscription limit now.
        def check_subscription_limit() -> Optional[Message]:
            new_subscription_count = len(puzzle_hashes) + subs.peer_subscription_count(peer.peer_node_id)

            if request.subscribe_when_finished and new_subscription_count > max_subscriptions:
                rejection = wallet_protocol.RejectPuzzleState(
                    uint8(wallet_protocol.RejectStateReason.EXCEEDED_SUBSCRIPTION_LIMIT)
                )
                msg = make_msg(ProtocolMessageTypes.reject_puzzle_state, rejection)
                return msg

            return None

        sub_rejection = check_subscription_limit()
        if sub_rejection is not None:
            return sub_rejection

        min_height = uint32((request.previous_height + 1) if request.previous_height is not None else 0)

        (coin_states, next_min_height) = await self.full_node.coin_store.batch_coin_states_by_puzzle_hashes(
            puzzle_hashes,
            min_height=min_height,
            include_spent=request.filters.include_spent,
            include_unspent=request.filters.include_unspent,
            include_hinted=request.filters.include_hinted,
            min_amount=request.filters.min_amount,
            max_items=max_items,
        )
        is_done = next_min_height is None

        peak_height = self.full_node.blockchain.get_peak_height()
        assert peak_height is not None

        height = uint32(next_min_height - 1) if next_min_height is not None else peak_height
        header_hash = self.full_node.blockchain.height_to_hash(height)
        assert header_hash is not None

        # Check if the request would exceed the subscription limit.
        # We do this again since we've crossed an `await` point, to prevent a race condition.
        sub_rejection = check_subscription_limit()
        if sub_rejection is not None:
            return sub_rejection

        if is_done and request.subscribe_when_finished:
            subs.add_puzzle_subscriptions(peer.peer_node_id, puzzle_hashes, max_subscriptions)
            await self.mempool_updates_for_puzzle_hashes(peer, set(puzzle_hashes), request.filters.include_hinted)

        response = wallet_protocol.RespondPuzzleState(puzzle_hashes, height, header_hash, is_done, coin_states)
        msg = make_msg(ProtocolMessageTypes.respond_puzzle_state, response)
        return msg

    @metadata.request(peer_required=True, reply_types=[ProtocolMessageTypes.respond_coin_state])
    async def request_coin_state(self, request: wallet_protocol.RequestCoinState, peer: WSChiaConnection) -> Message:
        max_items = self.max_subscribe_response_items(peer)
        max_subscriptions = self.max_subscriptions(peer)
        subs = self.full_node.subscriptions

        request_coin_ids = list(dict.fromkeys(request.coin_ids))
        coin_ids = request_coin_ids[:max_items]

        previous_header_hash = (
            self.full_node.blockchain.height_to_hash(request.previous_height)
            if request.previous_height is not None
            else self.full_node.blockchain.constants.GENESIS_CHALLENGE
        )
        assert previous_header_hash is not None

        if request.header_hash != previous_header_hash:
            rejection = wallet_protocol.RejectCoinState(uint8(wallet_protocol.RejectStateReason.REORG))
            msg = make_msg(ProtocolMessageTypes.reject_coin_state, rejection)
            return msg

        # Check if the request would exceed the subscription limit now.
        def check_subscription_limit() -> Optional[Message]:
            new_subscription_count = len(coin_ids) + subs.peer_subscription_count(peer.peer_node_id)

            if request.subscribe and new_subscription_count > max_subscriptions:
                rejection = wallet_protocol.RejectCoinState(
                    uint8(wallet_protocol.RejectStateReason.EXCEEDED_SUBSCRIPTION_LIMIT)
                )
                msg = make_msg(ProtocolMessageTypes.reject_coin_state, rejection)
                return msg

            return None

        sub_rejection = check_subscription_limit()
        if sub_rejection is not None:
            return sub_rejection

        min_height = uint32(request.previous_height + 1 if request.previous_height is not None else 0)

        coin_states = await self.full_node.coin_store.get_coin_states_by_ids(
            True, coin_ids, min_height=min_height, max_items=max_items
        )

        # Check if the request would exceed the subscription limit.
        # We do this again since we've crossed an `await` point, to prevent a race condition.
        sub_rejection = check_subscription_limit()
        if sub_rejection is not None:
            return sub_rejection

        if request.subscribe:
            subs.add_coin_subscriptions(peer.peer_node_id, coin_ids, max_subscriptions)
            await self.mempool_updates_for_coin_ids(peer, set(coin_ids))

        response = wallet_protocol.RespondCoinState(coin_ids, coin_states)
        msg = make_msg(ProtocolMessageTypes.respond_coin_state, response)
        return msg

    @metadata.request(reply_types=[ProtocolMessageTypes.respond_cost_info])
    async def request_cost_info(self, _request: wallet_protocol.RequestCostInfo) -> Optional[Message]:
        mempool_manager = self.full_node.mempool_manager
        response = wallet_protocol.RespondCostInfo(
            max_transaction_cost=mempool_manager.max_tx_clvm_cost,
            max_block_cost=mempool_manager.max_block_clvm_cost,
            max_mempool_cost=uint64(mempool_manager.mempool_max_total_cost),
            mempool_cost=uint64(mempool_manager.mempool._total_cost),
            mempool_fee=uint64(mempool_manager.mempool._total_fee),
            bump_fee_per_cost=uint8(mempool_manager.nonzero_fee_minimum_fpc),
        )
        msg = make_msg(ProtocolMessageTypes.respond_cost_info, response)
        return msg

    async def mempool_updates_for_puzzle_hashes(
        self, peer: WSChiaConnection, puzzle_hashes: set[bytes32], include_hints: bool
    ) -> None:
        if Capability.MEMPOOL_UPDATES not in peer.peer_capabilities:
            return

        start_time = time.monotonic()

        async with self.full_node.db_wrapper.reader() as conn:
            transaction_ids = set(
                self.full_node.mempool_manager.mempool.items_with_puzzle_hashes(puzzle_hashes, include_hints)
            )

            hinted_coin_ids: set[bytes32] = set()

            for batch in to_batches(puzzle_hashes, SQLITE_MAX_VARIABLE_NUMBER):
                hints_db: tuple[bytes, ...] = tuple(batch.entries)
                cursor = await conn.execute(
                    f"SELECT coin_id from hints INDEXED BY hint_index "
                    f"WHERE hint IN ({'?,' * (len(batch.entries) - 1)}?)",
                    hints_db,
                )
                for row in await cursor.fetchall():
                    hinted_coin_ids.add(bytes32(row[0]))
                await cursor.close()

            transaction_ids |= set(self.full_node.mempool_manager.mempool.items_with_coin_ids(hinted_coin_ids))

        if len(transaction_ids) > 0:
            message = wallet_protocol.MempoolItemsAdded(list(transaction_ids))
            await peer.send_message(make_msg(ProtocolMessageTypes.mempool_items_added, message))

        total_time = time.monotonic() - start_time

        self.log.log(
            logging.DEBUG if total_time < 2.0 else logging.WARNING,
            f"Sending initial mempool items to peer {peer.peer_node_id} took {total_time:.4f}s",
        )

    async def mempool_updates_for_coin_ids(self, peer: WSChiaConnection, coin_ids: set[bytes32]) -> None:
        if Capability.MEMPOOL_UPDATES not in peer.peer_capabilities:
            return

        start_time = time.monotonic()

        transaction_ids = self.full_node.mempool_manager.mempool.items_with_coin_ids(coin_ids)

        if len(transaction_ids) > 0:
            message = wallet_protocol.MempoolItemsAdded(list(transaction_ids))
            await peer.send_message(make_msg(ProtocolMessageTypes.mempool_items_added, message))

        total_time = time.monotonic() - start_time

        self.log.log(
            logging.DEBUG if total_time < 2.0 else logging.WARNING,
            f"Sending initial mempool items to peer {peer.peer_node_id} took {total_time:.4f}s",
        )

    def max_subscriptions(self, peer: WSChiaConnection) -> int:
        if self.is_trusted(peer):
            return cast(int, self.full_node.config.get("trusted_max_subscribe_items", 2000000))
        else:
            return cast(int, self.full_node.config.get("max_subscribe_items", 200000))

    def max_subscribe_response_items(self, peer: WSChiaConnection) -> int:
        if self.is_trusted(peer):
            return cast(int, self.full_node.config.get("trusted_max_subscribe_response_items", 500000))
        else:
            return cast(int, self.full_node.config.get("max_subscribe_response_items", 100000))

    def is_trusted(self, peer: WSChiaConnection) -> bool:
        return self.server.is_trusted_peer(peer, self.full_node.config.get("trusted_peers", {}))
