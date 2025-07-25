#!/usr/bin/env python3

from __future__ import annotations

import sqlite3
import sys
from functools import partial
from pathlib import Path
from time import time
from typing import Callable, Optional, Union, cast

import click
import zstd
from chia_rs import (
    DONT_VALIDATE_SIGNATURE,
    MEMPOOL_MODE,
    AugSchemeMPL,
    FullBlock,
    G1Element,
    G2Element,
    SpendBundleConditions,
    run_block_generator,
)
from chia_rs.sized_bytes import bytes32

from chia.consensus.condition_tools import pkm_pairs
from chia.consensus.default_constants import DEFAULT_CONSTANTS
from chia.full_node.full_block_utils import block_info_from_block, generator_from_block
from chia.types.block_protocol import BlockInfo
from chia.types.blockchain_format.serialized_program import SerializedProgram


# returns an optional error code and an optional SpendBundleConditions (from chia_rs)
# exactly one of those will hold a value and the number of seconds it took to
# run
def run_gen(
    generator_program: SerializedProgram, block_program_args: list[bytes], flags: int
) -> tuple[Optional[int], Optional[SpendBundleConditions], float]:
    try:
        start_time = time()
        err, result = run_block_generator(
            bytes(generator_program),
            block_program_args,
            DEFAULT_CONSTANTS.MAX_BLOCK_COST_CLVM,
            flags | DONT_VALIDATE_SIGNATURE,
            G2Element(),
            None,
            DEFAULT_CONSTANTS,
        )
        run_time = time() - start_time
        return err, result, run_time
    except Exception as e:
        # GENERATOR_RUNTIME_ERROR
        sys.stderr.write(f"Exception: {e}\n")
        return 117, None, 0


def callable_for_module_function_path(
    call: str,
) -> Callable[[Union[BlockInfo, FullBlock], bytes32, int, list[bytes], float, int], None]:
    module_name, function_name = call.split(":", 1)
    module = __import__(module_name, fromlist=[function_name])
    # TODO: casting due to getattr type signature
    return cast(
        Callable[[Union[BlockInfo, FullBlock], bytes32, int, list[bytes], float, int], None],
        getattr(module, function_name),
    )


@click.command()
@click.argument("file", type=click.Path(), required=True)
@click.option(
    "--mempool-mode", default=False, is_flag=True, help="execute all block generators in the strict mempool mode"
)
@click.option("--verify-signatures", default=False, is_flag=True, help="Verify block signatures (slow)")
@click.option("--start", default=225000, help="first block to examine")
@click.option("--end", default=None, help="last block to examine")
@click.option("--call", default=None, help="function to pass block iterator to in form `module:function`")
def main(
    file: Path, mempool_mode: bool, start: int, end: Optional[int], call: Optional[str], verify_signatures: bool
) -> None:
    call_f: Callable[[Union[BlockInfo, FullBlock], bytes32, int, list[bytes], float, int], None]
    if call is None:
        call_f = partial(default_call, verify_signatures)
    else:
        call_f = callable_for_module_function_path(call)

    c = sqlite3.connect(file)

    end_limit_sql = "" if end is None else f"and height <= {end} "

    rows = c.execute(
        f"SELECT header_hash, height, block FROM full_blocks "
        f"WHERE height >= {start} {end_limit_sql} and in_main_chain=1 ORDER BY height"
    )

    for r in rows:
        hh: bytes32 = r[0]
        height: int = r[1]
        block: Union[BlockInfo, FullBlock]
        if verify_signatures:
            block = FullBlock.from_bytes_unchecked(zstd.decompress(r[2]))
        else:
            block = block_info_from_block(memoryview(zstd.decompress(r[2])))

        if block.transactions_generator is None:
            sys.stderr.write(f" no-generator. block {height}\r")
            continue

        start_time = time()
        generator_blobs = []
        for h in block.transactions_generator_ref_list:
            ref = c.execute("SELECT block FROM full_blocks WHERE height=? and in_main_chain=1", (h,))
            generator = generator_from_block(memoryview(zstd.decompress(ref.fetchone()[0])))
            assert generator is not None
            generator_blobs.append(generator)
            ref.close()

        ref_lookup_time = time() - start_time

        flags = 0

        if mempool_mode:
            flags |= MEMPOOL_MODE

        call_f(block, hh, height, generator_blobs, ref_lookup_time, flags)


def default_call(
    verify_signatures: bool,
    block: Union[BlockInfo, FullBlock],
    hh: bytes32,
    height: int,
    generator_blobs: list[bytes],
    ref_lookup_time: float,
    flags: int,
) -> None:
    num_refs = len(generator_blobs)

    # add the block program arguments
    assert block.transactions_generator is not None
    err, result, run_time = run_gen(block.transactions_generator, generator_blobs, flags)
    if err is not None:
        sys.stderr.write(f"ERROR: {hh.hex()} {height} {err}\n")
        return
    assert result is not None

    num_removals = len(result.spends)
    fees = result.reserve_fee
    cost = result.cost
    num_additions = 0
    for spends in result.spends:
        num_additions += len(spends.create_coin)

    if verify_signatures:
        assert isinstance(block, FullBlock)
        # create hash_key list for aggsig check
        pairs_pks: list[G1Element] = []
        pairs_msgs: list[bytes] = []
        pairs_pks, pairs_msgs = pkm_pairs(result, DEFAULT_CONSTANTS.AGG_SIG_ME_ADDITIONAL_DATA)
        assert block.transactions_info is not None
        assert block.transactions_info.aggregated_signature is not None
        assert AugSchemeMPL.aggregate_verify(pairs_pks, pairs_msgs, block.transactions_info.aggregated_signature)

    print(
        f"{hh.hex()}\t{height:7d}\t{cost:11d}\t{run_time:0.3f}\t{num_refs}\t{ref_lookup_time:0.3f}\t{fees:14}\t"
        f"{len(bytes(block.transactions_generator)):6d}\t"
        f"{num_removals:4d}\t{num_additions:4d}"
    )


if __name__ == "__main__":
    main()
