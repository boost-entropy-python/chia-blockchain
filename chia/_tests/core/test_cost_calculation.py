from __future__ import annotations

import logging
import pathlib

import pytest
from chia_rs import G1Element, get_flags_for_height_and_constants
from chia_rs import get_puzzle_and_solution_for_coin2 as get_puzzle_and_solution_for_coin
from chia_rs.sized_bytes import bytes32
from chia_rs.sized_ints import uint32, uint64
from clvm_tools import binutils

from chia._tests.core.make_block_generator import make_block_generator
from chia._tests.util.get_name_puzzle_conditions import get_name_puzzle_conditions
from chia._tests.util.misc import BenchmarkRunner
from chia.consensus.condition_costs import ConditionCost
from chia.consensus.cost_calculator import NPCResult
from chia.consensus.default_constants import DEFAULT_CONSTANTS
from chia.full_node.bundle_tools import simple_solution_generator
from chia.simulator.block_tools import BlockTools, test_constants
from chia.types.blockchain_format.coin import Coin
from chia.types.blockchain_format.program import Program, run_with_cost
from chia.types.blockchain_format.serialized_program import SerializedProgram
from chia.types.generator_types import BlockGenerator
from chia.wallet.puzzles import p2_delegated_puzzle_or_hidden_puzzle

BURN_PUZZLE_HASH = bytes32(b"0" * 32)
SMALL_BLOCK_GENERATOR = make_block_generator(1)

log = logging.getLogger(__name__)


def large_block_generator(size: int) -> bytes:
    # make a small block and hash it
    # use this in the name for the cached big block
    # the idea is, if the algorithm for building the big block changes,
    # the name of the cache file will also change

    name = SMALL_BLOCK_GENERATOR.program.get_tree_hash().hex()[:16]

    my_dir = pathlib.Path(__file__).absolute().parent
    hex_path = my_dir / f"large-block-{name}-{size}.hex"
    try:
        with open(hex_path) as f:
            hex_str = f.read()
            return bytes.fromhex(hex_str)
    except FileNotFoundError:
        generator = make_block_generator(size)
        blob = bytes(generator.program)
        #  TODO: Re-enable large-block*.hex but cache in ~/.chia/subdir
        #  with open(hex_path, "w") as f:
        #      f.write(blob.hex())
        return blob


@pytest.mark.anyio
async def test_basics(softfork_height: int, bt: BlockTools) -> None:
    wallet_tool = bt.get_pool_wallet_tool()
    ph = wallet_tool.get_new_puzzlehash()
    num_blocks = 3
    blocks = bt.get_consecutive_blocks(
        num_blocks, [], guarantee_transaction_block=True, pool_reward_puzzle_hash=ph, farmer_reward_puzzle_hash=ph
    )
    coinbase = None
    for coin in blocks[2].get_included_reward_coins():
        if coin.puzzle_hash == ph and coin.amount == 250000000000:
            coinbase = coin
            break
    assert coinbase is not None
    spend_bundle = wallet_tool.generate_signed_transaction(
        uint64(coinbase.amount),
        BURN_PUZZLE_HASH,
        coinbase,
    )
    assert spend_bundle is not None
    program: BlockGenerator = simple_solution_generator(spend_bundle)

    npc_result: NPCResult = get_name_puzzle_conditions(
        program,
        bt.constants.MAX_BLOCK_COST_CLVM,
        mempool_mode=False,
        height=uint32(softfork_height),
        constants=bt.constants,
    )

    assert npc_result.error is None
    assert len(bytes(program.program)) == 433

    coin_spend = spend_bundle.coin_spends[0]
    assert npc_result.conds is not None
    assert coin_spend.coin.name() == npc_result.conds.spends[0].coin_id
    puzzle, solution = get_puzzle_and_solution_for_coin(
        program.program,
        program.generator_refs,
        bt.constants.MAX_BLOCK_COST_CLVM,
        coin_spend.coin,
        get_flags_for_height_and_constants(softfork_height, bt.constants),
    )
    assert puzzle == coin_spend.puzzle_reveal
    assert solution == coin_spend.solution

    if softfork_height >= bt.constants.HARD_FORK_HEIGHT:
        clvm_cost = 27360
    else:
        clvm_cost = 404560
    byte_cost = len(bytes(program.program)) * bt.constants.COST_PER_BYTE
    assert (
        npc_result.conds.cost == ConditionCost.CREATE_COIN.value + ConditionCost.AGG_SIG.value + clvm_cost + byte_cost
    )

    # Create condition + agg_sig_condition + length + cpu_cost
    assert (
        npc_result.conds.cost
        == ConditionCost.CREATE_COIN.value
        + ConditionCost.AGG_SIG.value
        + len(bytes(program.program)) * bt.constants.COST_PER_BYTE
        + clvm_cost
    )


@pytest.mark.anyio
async def test_mempool_mode(softfork_height: int, bt: BlockTools) -> None:
    wallet_tool = bt.get_pool_wallet_tool()
    ph = wallet_tool.get_new_puzzlehash()

    num_blocks = 3
    blocks = bt.get_consecutive_blocks(
        num_blocks, [], guarantee_transaction_block=True, pool_reward_puzzle_hash=ph, farmer_reward_puzzle_hash=ph
    )

    coinbase = None
    for coin in blocks[2].get_included_reward_coins():
        if coin.puzzle_hash == ph:
            coinbase = coin
            break
    assert coinbase is not None
    spend_bundle = wallet_tool.generate_signed_transaction(
        uint64(coinbase.amount),
        BURN_PUZZLE_HASH,
        coinbase,
    )
    assert spend_bundle is not None

    pk = bytes.fromhex(
        "88bc9360319e7c54ab42e19e974288a2d7a817976f7633f4b43f36ce72074e59c4ab8ddac362202f3e366f0aebbb6280"
    )
    puzzle = p2_delegated_puzzle_or_hidden_puzzle.puzzle_for_pk(G1Element.from_bytes(pk))
    disassembly = binutils.disassemble(puzzle)
    unknown_opcode = "15"
    program = SerializedProgram.from_bytes(
        binutils.assemble(
            f"(q ((0x3d2331635a58c0d49912bc1427d7db51afe3f20a7b4bcaffa17ee250dcbcbfaa {disassembly} 300"
            f"  (() (q . (({unknown_opcode} '00000000000000000000000000000000' 0x0cbba106e000))) ()))))"
        ).as_bin()
    )
    generator = BlockGenerator(program, [])
    npc_result: NPCResult = get_name_puzzle_conditions(
        generator,
        bt.constants.MAX_BLOCK_COST_CLVM,
        mempool_mode=True,
        height=uint32(softfork_height),
        constants=bt.constants,
    )
    assert npc_result.error is not None
    npc_result = get_name_puzzle_conditions(
        generator,
        bt.constants.MAX_BLOCK_COST_CLVM,
        mempool_mode=False,
        height=uint32(softfork_height),
        constants=bt.constants,
    )
    assert npc_result.error is None

    coin = Coin(
        bytes32.fromhex("3d2331635a58c0d49912bc1427d7db51afe3f20a7b4bcaffa17ee250dcbcbfaa"),
        bytes32.fromhex("14947eb0e69ee8fc8279190fc2d38cb4bbb61ba28f1a270cfd643a0e8d759576"),
        uint64(300),
    )
    puz, _solution = get_puzzle_and_solution_for_coin(
        generator.program,
        generator.generator_refs,
        bt.constants.MAX_BLOCK_COST_CLVM,
        coin,
        get_flags_for_height_and_constants(0, bt.constants),
    )
    assert puz == puzzle.to_serialized()


@pytest.mark.anyio
async def test_clvm_mempool_mode(softfork_height: int) -> None:
    block = Program.from_bytes(bytes(SMALL_BLOCK_GENERATOR.program))
    disassembly = binutils.disassemble(block)
    # this is a valid generator program except the first clvm
    # if-condition, that depends on executing an unknown operator
    # ("0xfe"). In mempool mode, this should fail, but in non-mempool
    # mode, the unknown operator should be treated as if it returns ().
    program = SerializedProgram.from_bytes(binutils.assemble(f"(i (0xfe (q . 0)) (q . ()) {disassembly})").as_bin())
    generator = BlockGenerator(program, [])
    npc_result: NPCResult = get_name_puzzle_conditions(
        generator,
        test_constants.MAX_BLOCK_COST_CLVM,
        mempool_mode=True,
        height=uint32(softfork_height),
        constants=test_constants,
    )
    assert npc_result.error is not None
    npc_result = get_name_puzzle_conditions(
        generator,
        test_constants.MAX_BLOCK_COST_CLVM,
        mempool_mode=False,
        height=uint32(softfork_height),
        constants=test_constants,
    )
    assert npc_result.error is None


@pytest.mark.anyio
async def test_tx_generator_speed(softfork_height: int, benchmark_runner: BenchmarkRunner) -> None:
    LARGE_BLOCK_COIN_CONSUMED_COUNT = 687
    generator_bytes = large_block_generator(LARGE_BLOCK_COIN_CONSUMED_COUNT)
    program = SerializedProgram.from_bytes(generator_bytes)

    with benchmark_runner.assert_runtime(seconds=1.25):
        generator = BlockGenerator(program, [])
        npc_result = get_name_puzzle_conditions(
            generator,
            test_constants.MAX_BLOCK_COST_CLVM,
            mempool_mode=False,
            height=uint32(softfork_height),
            constants=test_constants,
        )

    assert npc_result.error is None
    assert npc_result.conds is not None
    assert len(npc_result.conds.spends) == LARGE_BLOCK_COIN_CONSUMED_COUNT


@pytest.mark.anyio
async def test_clvm_max_cost(softfork_height: int) -> None:
    block = Program.from_bytes(bytes(SMALL_BLOCK_GENERATOR.program))
    disassembly = binutils.disassemble(block)
    # this is a valid generator program except the first clvm
    # if-condition, that depends on executing an unknown operator
    # ("0xfe"). In mempool mode, this should fail, but in non-mempool
    # mode, the unknown operator should be treated as if it returns ().
    # the CLVM program has a cost of 391969
    program = SerializedProgram.from_bytes(
        binutils.assemble(f"(i (softfork (q . 10000000)) (q . ()) {disassembly})").as_bin()
    )

    # ensure we fail if the program exceeds the cost
    generator = BlockGenerator(program, [])
    npc_result = get_name_puzzle_conditions(
        generator, 10000000, mempool_mode=False, height=uint32(softfork_height), constants=test_constants
    )

    assert npc_result.error is not None
    assert npc_result.conds is None

    # raise the max cost to make sure this passes
    # ensure we pass if the program does not exceeds the cost
    npc_result = get_name_puzzle_conditions(
        generator, 23000000, mempool_mode=False, height=uint32(softfork_height), constants=test_constants
    )

    assert npc_result.error is None
    assert npc_result.conds is not None
    assert npc_result.conds.cost > 10000000


@pytest.mark.anyio
async def test_standard_tx(benchmark_runner: BenchmarkRunner) -> None:
    # this isn't a real public key, but we don't care
    public_key = bytes.fromhex(
        "af949b78fa6a957602c3593a3d6cb7711e08720415dad831ab18adacaa9b27ec3dda508ee32e24bc811c0abc5781ae21"
    )
    puzzle_program = SerializedProgram.from_bytes(
        bytes(p2_delegated_puzzle_or_hidden_puzzle.puzzle_for_pk(G1Element.from_bytes(public_key)))
    )
    conditions = binutils.assemble(
        "((51 0x699eca24f2b6f4b25b16f7a418d0dc4fc5fce3b9145aecdda184158927738e3e 10)"
        " (51 0x847bb2385534070c39a39cc5dfdc7b35e2db472dc0ab10ab4dec157a2178adbf 0x00cbba106df6))"
    )
    solution_program = SerializedProgram.from_bytes(
        bytes(p2_delegated_puzzle_or_hidden_puzzle.solution_for_conditions(conditions))
    )

    with benchmark_runner.assert_runtime(seconds=0.1):
        total_cost = 0
        for i in range(1000):
            cost, _result = run_with_cost(puzzle_program, test_constants.MAX_BLOCK_COST_CLVM, solution_program)
            total_cost += cost


@pytest.mark.anyio
async def test_get_puzzle_and_solution_for_coin_performance(benchmark_runner: BenchmarkRunner) -> None:
    from chia_puzzles_py.programs import CHIALISP_DESERIALISATION

    from chia._tests.core.large_block import LARGE_BLOCK

    DESERIALIZE_MOD = Program.from_bytes(CHIALISP_DESERIALISATION)

    assert LARGE_BLOCK.transactions_generator is not None
    # first, list all spent coins in the block
    _, result = run_with_cost(
        LARGE_BLOCK.transactions_generator, DEFAULT_CONSTANTS.MAX_BLOCK_COST_CLVM, [DESERIALIZE_MOD, []]
    )

    coin_spends = result.first()
    spent_coins: list[Coin] = []
    for spend in coin_spends.as_iter():
        parent, puzzle, amount_program, _ = spend.as_iter()
        parent_coin_info = parent.as_atom()
        puzzle_hash = puzzle.get_tree_hash()
        amount = uint64(amount_program.as_int())
        coin = Coin(parent_coin_info=parent_coin_info, puzzle_hash=puzzle_hash, amount=amount)
        spent_coins.append(coin)

    print(f"found {len(spent_coins)} spent coins in block")

    # benchmark the function to pick out the puzzle and solution for a specific
    # coin
    generator = BlockGenerator(LARGE_BLOCK.transactions_generator, [])
    with benchmark_runner.assert_runtime(seconds=8.5):
        for _ in range(3):
            for c in spent_coins:
                puz, _solution = get_puzzle_and_solution_for_coin(
                    generator.program,
                    generator.generator_refs,
                    test_constants.MAX_BLOCK_COST_CLVM,
                    c,
                    get_flags_for_height_and_constants(0, test_constants),
                )
                assert puz.get_tree_hash() == c.puzzle_hash
