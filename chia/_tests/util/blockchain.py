from __future__ import annotations

import contextlib
import os
import pickle  # noqa: S403  # TODO: use explicit serialization instead of pickle
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Optional

from chia_rs import ConsensusConstants, FullBlock
from chia_rs.sized_ints import uint64

from chia.consensus.block_height_map import BlockHeightMap
from chia.consensus.blockchain import Blockchain
from chia.full_node.block_store import BlockStore
from chia.full_node.coin_store import CoinStore
from chia.simulator.block_tools import BlockTools
from chia.util.db_wrapper import DBWrapper2, generate_in_memory_db_uri
from chia.util.default_root import DEFAULT_ROOT_PATH


@contextlib.asynccontextmanager
async def create_blockchain(
    constants: ConsensusConstants, db_version: int
) -> AsyncIterator[tuple[Blockchain, DBWrapper2]]:
    db_uri = generate_in_memory_db_uri()
    async with DBWrapper2.managed(database=db_uri, uri=True, reader_count=1, db_version=db_version) as wrapper:
        coin_store = await CoinStore.create(wrapper)
        store = await BlockStore.create(wrapper)
        path = Path(".")
        height_map = await BlockHeightMap.create(path, wrapper)
        bc1 = await Blockchain.create(coin_store, store, height_map, constants, 3, single_threaded=True, log_coins=True)
        try:
            assert bc1.get_peak() is None
            yield bc1, wrapper
        finally:
            bc1.shut_down()


def persistent_blocks(
    num_of_blocks: int,
    db_name: str,
    bt: BlockTools,
    seed: bytes = b"",
    empty_sub_slots: int = 0,
    *,
    normalized_to_identity_cc_eos: bool = False,
    normalized_to_identity_icc_eos: bool = False,
    normalized_to_identity_cc_sp: bool = False,
    normalized_to_identity_cc_ip: bool = False,
    block_list_input: Optional[list[FullBlock]] = None,
    time_per_block: Optional[float] = None,
    dummy_block_references: bool = False,
    include_transactions: bool = False,
) -> list[FullBlock]:
    # try loading from disc, if not create new blocks.db file
    # TODO hash fixtures.py and blocktool.py, add to path, delete if the files changed
    if block_list_input is None:
        block_list_input = []
    block_path_dir = DEFAULT_ROOT_PATH.parent.joinpath("blocks")
    file_path = block_path_dir.joinpath(db_name)

    ci = os.environ.get("CI")
    if ci is not None and not file_path.exists():
        raise Exception(f"Running in CI and expected path not found: {file_path!r}")

    block_path_dir.mkdir(parents=True, exist_ok=True)

    if file_path.exists():
        print(f"File found at: {file_path}")
        try:
            bytes_list = file_path.read_bytes()
            # TODO: use explicit serialization instead of pickle
            block_bytes_list: list[bytes] = pickle.loads(bytes_list)  # noqa: S301
            blocks: list[FullBlock] = []
            for block_bytes in block_bytes_list:
                blocks.append(FullBlock.from_bytes_unchecked(block_bytes))
            if len(blocks) == num_of_blocks + len(block_list_input):
                print(f"\n loaded {file_path} with {len(blocks)} blocks")

                return blocks
        except EOFError:
            print("\n error reading db file")
    else:
        print(f"File not found at: {file_path}")

    print("Creating a new test db")
    return new_test_db(
        file_path,
        num_of_blocks,
        seed,
        empty_sub_slots,
        bt,
        block_list_input,
        time_per_block,
        normalized_to_identity_cc_eos=normalized_to_identity_cc_eos,
        normalized_to_identity_icc_eos=normalized_to_identity_icc_eos,
        normalized_to_identity_cc_sp=normalized_to_identity_cc_sp,
        normalized_to_identity_cc_ip=normalized_to_identity_cc_ip,
        dummy_block_references=dummy_block_references,
        include_transactions=include_transactions,
    )


def new_test_db(
    path: Path,
    num_of_blocks: int,
    seed: bytes,
    empty_sub_slots: int,
    bt: BlockTools,
    block_list_input: list[FullBlock],
    time_per_block: Optional[float],
    *,
    normalized_to_identity_cc_eos: bool = False,  # CC_EOS,
    normalized_to_identity_icc_eos: bool = False,  # ICC_EOS
    normalized_to_identity_cc_sp: bool = False,  # CC_SP,
    normalized_to_identity_cc_ip: bool = False,  # CC_IP
    dummy_block_references: bool = False,
    include_transactions: bool = False,
) -> list[FullBlock]:
    print(f"create {path} with {num_of_blocks} blocks with ")
    blocks: list[FullBlock] = bt.get_consecutive_blocks(
        num_of_blocks,
        block_list_input=block_list_input,
        time_per_block=time_per_block,
        seed=seed,
        skip_slots=empty_sub_slots,
        normalized_to_identity_cc_eos=normalized_to_identity_cc_eos,
        normalized_to_identity_icc_eos=normalized_to_identity_icc_eos,
        normalized_to_identity_cc_sp=normalized_to_identity_cc_sp,
        normalized_to_identity_cc_ip=normalized_to_identity_cc_ip,
        dummy_block_references=dummy_block_references,
        include_transactions=include_transactions,
        genesis_timestamp=uint64(1234567890),
    )
    block_bytes_list: list[bytes] = []
    for block in blocks:
        block_bytes_list.append(bytes(block))
    bytes_fn = pickle.dumps(block_bytes_list)
    path.write_bytes(bytes_fn)
    return blocks
