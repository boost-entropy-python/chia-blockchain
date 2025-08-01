from __future__ import annotations

import asyncio
import os
import random
from dataclasses import dataclass
from pathlib import Path
from time import monotonic
from typing import Optional

import aiosqlite
import click
from chia_rs.sized_bytes import bytes32
from chia_rs.sized_ints import uint32

from chia.consensus.block_height_map import BlockHeightMap
from chia.consensus.blockchain import Blockchain
from chia.consensus.default_constants import DEFAULT_CONSTANTS
from chia.consensus.get_block_generator import get_block_generator
from chia.full_node.block_store import BlockStore
from chia.full_node.coin_store import CoinStore
from chia.types.blockchain_format.serialized_program import SerializedProgram
from chia.util.db_version import lookup_db_version
from chia.util.db_wrapper import DBWrapper2

# the first transaction block. Each byte in transaction_height_delta is the
# number of blocks to skip forward to get to the next transaction block
transaction_block_heights = []
last = 225698
file_path = os.path.realpath(__file__)
with open(Path(file_path).parent / "transaction_height_delta", "rb") as f:
    for delta in f.read():
        new = last + delta
        transaction_block_heights.append(new)
        last = new


@dataclass(frozen=True)
class BlockInfo:
    prev_header_hash: bytes32
    transactions_generator: Optional[SerializedProgram]
    transactions_generator_ref_list: list[uint32]


def random_refs() -> list[uint32]:
    ret = random.sample(transaction_block_heights, DEFAULT_CONSTANTS.MAX_GENERATOR_REF_LIST_SIZE)
    random.shuffle(ret)
    return [uint32(i) for i in ret]


REPETITIONS = 100


async def main(db_path: Path) -> None:
    random.seed(0x213FB154)

    async with aiosqlite.connect(db_path) as connection:
        await connection.execute("pragma journal_mode=wal")
        await connection.execute("pragma synchronous=FULL")
        await connection.execute("pragma query_only=ON")
        db_version: int = await lookup_db_version(connection)

        db_wrapper = DBWrapper2(connection, db_version=db_version)
        await db_wrapper.add_connection(await aiosqlite.connect(db_path))

        block_store = await BlockStore.create(db_wrapper)
        coin_store = await CoinStore.create(db_wrapper)

        start_time = monotonic()
        # make configurable
        reserved_cores = 4
        height_map = await BlockHeightMap.create(db_path.parent, db_wrapper)
        blockchain = await Blockchain.create(coin_store, block_store, height_map, DEFAULT_CONSTANTS, reserved_cores)

        peak = blockchain.get_peak()
        assert peak is not None
        timing = 0.0
        for i in range(REPETITIONS):
            block = BlockInfo(
                peak.header_hash,
                SerializedProgram.from_bytes(bytes.fromhex("80")),
                random_refs(),
            )

            start_time = monotonic()
            gen = await get_block_generator(blockchain.lookup_block_generators, block)
            one_call = monotonic() - start_time
            timing += one_call
            assert gen is not None

        print(f"get_block_generator(): {timing / REPETITIONS:0.3f}s")

        blockchain.shut_down()


@click.command()
@click.argument("db-path", type=click.Path())
def entry_point(db_path: Path) -> None:
    asyncio.run(main(Path(db_path)))


if __name__ == "__main__":
    entry_point()
