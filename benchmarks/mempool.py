from __future__ import annotations

import asyncio
import cProfile
from collections.abc import Collection, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from subprocess import check_call
from time import monotonic
from typing import Optional

from chia_rs import SpendBundle
from chia_rs.sized_bytes import bytes32
from chia_rs.sized_ints import uint32, uint64

from chia.consensus.coinbase import create_farmer_coin, create_pool_coin
from chia.consensus.default_constants import DEFAULT_CONSTANTS
from chia.full_node.mempool_manager import MempoolManager
from chia.simulator.wallet_tools import WalletTool
from chia.types.blockchain_format.coin import Coin
from chia.types.coin_record import CoinRecord
from chia.types.mempool_inclusion_status import MempoolInclusionStatus
from chia.types.mempool_item import UnspentLineageInfo
from chia.util.batches import to_batches
from chia.util.task_referencer import create_referenced_task

NUM_ITERS = 200
NUM_PEERS = 5


@contextmanager
def enable_profiler(profile: bool, name: str) -> Iterator[None]:
    if not profile:
        yield
        return

    with cProfile.Profile() as pr:
        yield

    pr.create_stats()
    output_file = f"mempool-{name}"
    pr.dump_stats(output_file + ".profile")
    check_call(["gprof2dot", "-f", "pstats", "-o", output_file + ".dot", output_file + ".profile"])
    with open(output_file + ".png", "w+") as f:
        check_call(["dot", "-T", "png", output_file + ".dot"], stdout=f)
    print(f"  output written to: {output_file}.png")


def make_hash(height: int) -> bytes32:
    return bytes32(height.to_bytes(32, byteorder="big"))


@dataclass(frozen=True)
class BenchBlockRecord:
    """
    This is a subset of BlockRecord that the mempool manager uses for peak.
    """

    header_hash: bytes32
    height: uint32
    timestamp: Optional[uint64]
    prev_transaction_block_height: uint32
    prev_transaction_block_hash: Optional[bytes32]

    @property
    def is_transaction_block(self) -> bool:
        return self.timestamp is not None


def fake_block_record(block_height: uint32, timestamp: uint64) -> BenchBlockRecord:
    this_hash = make_hash(block_height)
    prev_hash = make_hash(block_height - 1)
    return BenchBlockRecord(
        header_hash=this_hash,
        height=block_height,
        timestamp=timestamp,
        prev_transaction_block_height=uint32(block_height - 1),
        prev_transaction_block_hash=prev_hash,
    )


async def run_mempool_benchmark() -> None:
    all_coins: dict[bytes32, CoinRecord] = {}

    async def get_coin_records(coin_ids: Collection[bytes32]) -> list[CoinRecord]:
        ret: list[CoinRecord] = []
        for name in coin_ids:
            r = all_coins.get(name)
            if r is not None:
                ret.append(r)
        return ret

    # We currently don't need to keep track of these for our purpose
    async def get_unspent_lineage_info_for_puzzle_hash(_: bytes32) -> Optional[UnspentLineageInfo]:
        assert False

    wt = WalletTool(DEFAULT_CONSTANTS)

    spend_bundles: list[list[SpendBundle]] = []

    # these spend the same coins as spend_bundles but with a higher fee
    replacement_spend_bundles: list[list[SpendBundle]] = []

    # these spend the same coins as spend_bundles, but they are organized in
    # much larger bundles
    large_spend_bundles: list[list[SpendBundle]] = []

    timestamp = uint64(1631794488)

    height = uint32(1)

    print("Building spend bundles")
    for peer in range(NUM_PEERS):
        print(f"  peer {peer}")
        print("     reward coins")
        unspent: list[Coin] = []
        for idx in range(NUM_ITERS):
            height = uint32(height + 1)

            # 19 seconds per block
            timestamp = uint64(timestamp + 19)

            # farm rewards
            farmer_coin = create_farmer_coin(
                height, wt.get_new_puzzlehash(), uint64(250000000), DEFAULT_CONSTANTS.GENESIS_CHALLENGE
            )
            pool_coin = create_pool_coin(
                height, wt.get_new_puzzlehash(), uint64(1750000000), DEFAULT_CONSTANTS.GENESIS_CHALLENGE
            )
            all_coins[farmer_coin.name()] = CoinRecord(farmer_coin, height, uint32(0), True, timestamp)
            all_coins[pool_coin.name()] = CoinRecord(pool_coin, height, uint32(0), True, timestamp)
            unspent.extend([farmer_coin, pool_coin])

        print("     spend bundles")
        bundles = []
        for coin in unspent:
            tx = wt.generate_signed_transaction(uint64(coin.amount // 2), wt.get_new_puzzlehash(), coin, fee=peer + idx)
            bundles.append(tx)
        spend_bundles.append(bundles)

        bundles = []
        print("     replacement spend bundles")
        for coin in unspent:
            tx = wt.generate_signed_transaction(
                uint64(coin.amount // 2), wt.get_new_puzzlehash(), coin, fee=peer + idx + 10000000
            )
            bundles.append(tx)
        replacement_spend_bundles.append(bundles)

        bundles = []
        print("     large spend bundles")
        for batch in to_batches(unspent, 200):
            print(f"{len(batch.entries)} coins")
            tx = SpendBundle.aggregate(
                [
                    wt.generate_signed_transaction(uint64(c.amount // 2), wt.get_new_puzzlehash(), c, fee=peer + idx)
                    for c in batch.entries
                ]
            )
            bundles.append(tx)
        large_spend_bundles.append(bundles)

    start_height = height
    for single_threaded in [False, True]:
        if single_threaded:
            print("\n== Single-threaded")
        else:
            print("\n== Multi-threaded")

        mempool = MempoolManager(
            get_coin_records,
            get_unspent_lineage_info_for_puzzle_hash,
            DEFAULT_CONSTANTS,
            single_threaded=single_threaded,
        )

        height = start_height
        rec = fake_block_record(height, timestamp)
        await mempool.new_peak(rec, None)

        async def add_spend_bundles(spend_bundles: list[SpendBundle]) -> None:
            for tx in spend_bundles:
                spend_bundle_id = tx.name()
                npc = await mempool.pre_validate_spendbundle(tx, spend_bundle_id)
                assert npc is not None
                info = await mempool.add_spend_bundle(tx, npc, spend_bundle_id, height)
                assert info.status == MempoolInclusionStatus.SUCCESS
                assert info.error is None

        suffix = "st" if single_threaded else "mt"

        print("\nProfiling add_spend_bundle() with large bundles")
        total_bundles = 0
        tasks = []
        with enable_profiler(True, f"add-large-{suffix}"):
            start = monotonic()
            for peer in range(NUM_PEERS):
                total_bundles += len(large_spend_bundles[peer])
                tasks.append(create_referenced_task(add_spend_bundles(large_spend_bundles[peer])))
            await asyncio.gather(*tasks)
            stop = monotonic()
        print(f"  time: {stop - start:0.4f}s")
        print(f"  per call: {(stop - start) / total_bundles * 1000:0.2f}ms")

        mempool = MempoolManager(
            get_coin_records,
            get_unspent_lineage_info_for_puzzle_hash,
            DEFAULT_CONSTANTS,
            single_threaded=single_threaded,
        )

        height = start_height
        rec = fake_block_record(height, timestamp)
        await mempool.new_peak(rec, None)

        print("\nProfiling add_spend_bundle()")
        total_bundles = 0
        tasks = []
        with enable_profiler(True, f"add-{suffix}"):
            start = monotonic()
            for peer in range(NUM_PEERS):
                total_bundles += len(spend_bundles[peer])
                tasks.append(create_referenced_task(add_spend_bundles(spend_bundles[peer])))
            await asyncio.gather(*tasks)
            stop = monotonic()
        print(f"  time: {stop - start:0.4f}s")
        print(f"  per call: {(stop - start) / total_bundles * 1000:0.2f}ms")

        print("\nProfiling add_spend_bundle() with replace-by-fee")
        total_bundles = 0
        tasks = []
        with enable_profiler(True, f"replace-{suffix}"):
            start = monotonic()
            for peer in range(NUM_PEERS):
                total_bundles += len(replacement_spend_bundles[peer])
                tasks.append(create_referenced_task(add_spend_bundles(replacement_spend_bundles[peer])))
            await asyncio.gather(*tasks)
            stop = monotonic()
        print(f"  time: {stop - start:0.4f}s")
        print(f"  per call: {(stop - start) / total_bundles * 1000:0.2f}ms")

        print("\nProfiling create_block_generator()")
        with enable_profiler(True, f"create-{suffix}"):
            start = monotonic()
            for _ in range(10):
                mempool.create_block_generator(rec.header_hash, 2.0)
            stop = monotonic()
        print(f"  time: {stop - start:0.4f}s")
        print(f"  per call: {(stop - start) / 10 * 1000:0.2f}ms")

        print("\nProfiling create_block_generator2()")
        with enable_profiler(True, f"create2-{suffix}"):
            start = monotonic()
            for _ in range(10):
                mempool.create_block_generator2(rec.header_hash, 2.0)
            stop = monotonic()
        print(f"  time: {stop - start:0.4f}s")
        print(f"  per call: {(stop - start) / 10 * 1000:0.2f}ms")

        print("\nProfiling new_peak() (optimized)")
        blocks: list[tuple[BenchBlockRecord, list[bytes32]]] = []
        for coin_id in all_coins.keys():
            height = uint32(height + 1)
            timestamp = uint64(timestamp + 19)
            rec = fake_block_record(height, timestamp)
            blocks.append((rec, [coin_id]))

        with enable_profiler(True, f"new-peak-{suffix}"):
            start = monotonic()
            for rec, spends in blocks:
                await mempool.new_peak(rec, spends)
            stop = monotonic()
        print(f"  time: {stop - start:0.4f}s")
        print(f"  per call: {(stop - start) / len(blocks) * 1000:0.2f}ms")

        print("\nProfiling new_peak() (reorg)")
        blocks = []
        for coin_id in all_coins.keys():
            height = uint32(height + 2)
            timestamp = uint64(timestamp + 28)
            rec = fake_block_record(height, timestamp)
            blocks.append((rec, [coin_id]))

        with enable_profiler(True, f"new-peak-reorg-{suffix}"):
            start = monotonic()
            for rec, spends in blocks:
                await mempool.new_peak(rec, spends)
            stop = monotonic()
        print(f"  time: {stop - start:0.4f}s")
        print(f"  per call: {(stop - start) / len(blocks) * 1000:0.2f}ms")


if __name__ == "__main__":
    import logging

    logger = logging.getLogger()
    logger.addHandler(logging.StreamHandler())
    logger.setLevel(logging.WARNING)
    asyncio.run(run_mempool_benchmark())
