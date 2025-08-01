from __future__ import annotations

import copy
import dataclasses
from typing import Any, Optional

import pytest
from chia_rs import AugSchemeMPL, CoinSpend, G1Element, G2Element, PrivateKey, SpendBundle
from chia_rs.sized_bytes import bytes32
from chia_rs.sized_ints import uint64
from chiabip158 import PyBIP158

from chia._tests.clvm.test_puzzles import public_key_for_index, secret_exponent_for_index
from chia._tests.core.mempool.test_mempool_manager import (
    IDENTITY_PUZZLE,
    IDENTITY_PUZZLE_HASH,
    TEST_COIN,
    TEST_COIN_ID,
    TEST_HEIGHT,
    mempool_item_from_spendbundle,
    spend_bundle_from_conditions,
)
from chia._tests.util.key_tool import KeyTool
from chia._tests.util.spend_sim import SimClient, SpendSim, sim_and_client
from chia.consensus.default_constants import DEFAULT_CONSTANTS
from chia.full_node.eligible_coin_spends import (
    SingletonFastForward,
    perform_the_fast_forward,
)
from chia.types.blockchain_format.coin import Coin
from chia.types.blockchain_format.program import Program
from chia.types.blockchain_format.serialized_program import SerializedProgram
from chia.types.coin_spend import make_spend
from chia.types.condition_opcodes import ConditionOpcode
from chia.types.internal_mempool_item import InternalMempoolItem
from chia.types.mempool_inclusion_status import MempoolInclusionStatus
from chia.types.mempool_item import BundleCoinSpend, UnspentLineageInfo
from chia.util.errors import Err
from chia.wallet.puzzles import p2_conditions, p2_delegated_puzzle_or_hidden_puzzle
from chia.wallet.puzzles import singleton_top_layer_v1_1 as singleton_top_layer


def test_process_fast_forward_spends_nothing_to_do() -> None:
    """
    This tests the case when we don't have an eligible coin, so there is
    nothing to fast forward and the item remains unchanged
    """
    sk = AugSchemeMPL.key_gen(b"b" * 32)
    g1 = sk.get_g1()
    sig = AugSchemeMPL.sign(sk, b"foobar", g1)
    conditions = [[ConditionOpcode.AGG_SIG_UNSAFE, bytes(g1), b"foobar"]]
    sb = spend_bundle_from_conditions(conditions, TEST_COIN, sig)
    item = mempool_item_from_spendbundle(sb)
    # This coin is not eligible for fast forward
    assert item.bundle_coin_spends[TEST_COIN_ID].eligible_for_fast_forward is False
    internal_mempool_item = InternalMempoolItem(sb, item.conds, item.height_added_to_mempool, item.bundle_coin_spends)
    original_version = dataclasses.replace(internal_mempool_item)
    singleton_ff = SingletonFastForward()
    bundle_coin_spends = singleton_ff.process_fast_forward_spends(
        mempool_item=internal_mempool_item, height=TEST_HEIGHT, constants=DEFAULT_CONSTANTS
    )
    assert singleton_ff == SingletonFastForward()
    assert bundle_coin_spends == original_version.bundle_coin_spends


def test_process_fast_forward_spends_unknown_ff() -> None:
    """
    This tests the case when we process for the first time but we are unable
    to lookup the latest version from the item's latest singleton lineage
    """
    test_coin = Coin(TEST_COIN_ID, IDENTITY_PUZZLE_HASH, uint64(1))
    conditions = [[ConditionOpcode.CREATE_COIN, IDENTITY_PUZZLE_HASH, 1]]
    sb = spend_bundle_from_conditions(conditions, test_coin)
    item = mempool_item_from_spendbundle(sb)
    # The coin is eligible for fast forward
    assert item.bundle_coin_spends[test_coin.name()].eligible_for_fast_forward is True
    item.bundle_coin_spends[test_coin.name()].latest_singleton_lineage = None
    internal_mempool_item = InternalMempoolItem(sb, item.conds, item.height_added_to_mempool, item.bundle_coin_spends)
    singleton_ff = SingletonFastForward()
    # We have no fast forward records yet, so we'll process this coin for the
    # first time here, but the item's latest singleton lineage returns None
    with pytest.raises(ValueError, match="Cannot proceed with singleton spend fast forward."):
        singleton_ff.process_fast_forward_spends(
            mempool_item=internal_mempool_item, height=TEST_HEIGHT, constants=DEFAULT_CONSTANTS
        )


def test_process_fast_forward_spends_latest_unspent() -> None:
    """
    This tests the case when we are the latest singleton version already, so
    we don't need to fast forward, we just need to set the next version from
    our additions to chain ff spends.
    """
    test_amount = uint64(3)
    test_coin = Coin(TEST_COIN_ID, IDENTITY_PUZZLE_HASH, test_amount)
    test_unspent_lineage_info = UnspentLineageInfo(
        coin_id=test_coin.name(), parent_id=test_coin.parent_coin_info, parent_parent_id=TEST_COIN_ID
    )

    # At this point, spends are considered *potentially* eligible for singleton
    # fast forward mainly when their amount is odd and they don't have conditions
    # that disqualify them
    conditions = [[ConditionOpcode.CREATE_COIN, IDENTITY_PUZZLE_HASH, test_amount]]
    sb = spend_bundle_from_conditions(conditions, test_coin)
    item = mempool_item_from_spendbundle(sb)
    assert item.bundle_coin_spends[test_coin.name()].eligible_for_fast_forward is True
    item.bundle_coin_spends[test_coin.name()].latest_singleton_lineage = test_unspent_lineage_info
    internal_mempool_item = InternalMempoolItem(sb, item.conds, item.height_added_to_mempool, item.bundle_coin_spends)
    original_version = dataclasses.replace(internal_mempool_item)
    singleton_ff = SingletonFastForward()
    bundle_coin_spends = singleton_ff.process_fast_forward_spends(
        mempool_item=internal_mempool_item, height=TEST_HEIGHT, constants=DEFAULT_CONSTANTS
    )
    child_coin = item.bundle_coin_spends[test_coin.name()].additions[0]
    expected_fast_forward_spends = {
        IDENTITY_PUZZLE_HASH: UnspentLineageInfo(
            coin_id=child_coin.name(), parent_id=test_coin.name(), parent_parent_id=test_coin.parent_coin_info
        )
    }
    # We have set the next version from our additions to chain ff spends
    assert singleton_ff.fast_forward_spends == expected_fast_forward_spends
    # We didn't need to fast forward the item so it stays as is
    assert bundle_coin_spends == original_version.bundle_coin_spends


def test_perform_the_fast_forward() -> None:
    """
    This test attempts to spend a coin that is already spent and the current
    unspent version is its grandchild. We fast forward the test coin spend into
    a spend of that latest unspent
    """
    test_parent_id = bytes32.from_hexstr("0x039759eda861cd44c0af6c9501300f66fe4f5de144b8ae4fc4e8da35701f38ac")
    test_ph = bytes32.from_hexstr("0x9ae0917f3ca301f934468ec60412904c0a88b232aeabf220c01ef53054e0281a")
    test_amount = uint64(1337)
    test_coin = Coin(test_parent_id, test_ph, test_amount)
    test_child_coin = Coin(test_coin.name(), test_ph, test_amount)
    latest_unspent_coin = Coin(test_child_coin.name(), test_ph, test_amount)
    # This spend setup makes us eligible for fast forward so that we perform a
    # meaningful fast forward on the rust side. It was generated using the
    # singleton/child/grandchild dynamics that we have in
    # `test_singleton_fast_forward_different_block` to get a realistic test case
    test_puzzle_reveal = SerializedProgram.fromhex(
        "ff02ffff01ff02ffff01ff02ffff03ffff18ff2fff3480ffff01ff04ffff04ff20ffff04ff2fff808080ffff04ffff02ff3effff04ff0"
        "2ffff04ff05ffff04ffff02ff2affff04ff02ffff04ff27ffff04ffff02ffff03ff77ffff01ff02ff36ffff04ff02ffff04ff09ffff04"
        "ff57ffff04ffff02ff2effff04ff02ffff04ff05ff80808080ff808080808080ffff011d80ff0180ffff04ffff02ffff03ff77ffff018"
        "1b7ffff015780ff0180ff808080808080ffff04ff77ff808080808080ffff02ff3affff04ff02ffff04ff05ffff04ffff02ff0bff5f80"
        "ffff01ff8080808080808080ffff01ff088080ff0180ffff04ffff01ffffffff4947ff0233ffff0401ff0102ffffff20ff02ffff03ff0"
        "5ffff01ff02ff32ffff04ff02ffff04ff0dffff04ffff0bff3cffff0bff34ff2480ffff0bff3cffff0bff3cffff0bff34ff2c80ff0980"
        "ffff0bff3cff0bffff0bff34ff8080808080ff8080808080ffff010b80ff0180ffff02ffff03ffff22ffff09ffff0dff0580ff2280fff"
        "f09ffff0dff0b80ff2280ffff15ff17ffff0181ff8080ffff01ff0bff05ff0bff1780ffff01ff088080ff0180ff02ffff03ff0bffff01"
        "ff02ffff03ffff02ff26ffff04ff02ffff04ff13ff80808080ffff01ff02ffff03ffff20ff1780ffff01ff02ffff03ffff09ff81b3fff"
        "f01818f80ffff01ff02ff3affff04ff02ffff04ff05ffff04ff1bffff04ff34ff808080808080ffff01ff04ffff04ff23ffff04ffff02"
        "ff36ffff04ff02ffff04ff09ffff04ff53ffff04ffff02ff2effff04ff02ffff04ff05ff80808080ff808080808080ff738080ffff02f"
        "f3affff04ff02ffff04ff05ffff04ff1bffff04ff34ff8080808080808080ff0180ffff01ff088080ff0180ffff01ff04ff13ffff02ff"
        "3affff04ff02ffff04ff05ffff04ff1bffff04ff17ff8080808080808080ff0180ffff01ff02ffff03ff17ff80ffff01ff088080ff018"
        "080ff0180ffffff02ffff03ffff09ff09ff3880ffff01ff02ffff03ffff18ff2dffff010180ffff01ff0101ff8080ff0180ff8080ff01"
        "80ff0bff3cffff0bff34ff2880ffff0bff3cffff0bff3cffff0bff34ff2c80ff0580ffff0bff3cffff02ff32ffff04ff02ffff04ff07f"
        "fff04ffff0bff34ff3480ff8080808080ffff0bff34ff8080808080ffff02ffff03ffff07ff0580ffff01ff0bffff0102ffff02ff2eff"
        "ff04ff02ffff04ff09ff80808080ffff02ff2effff04ff02ffff04ff0dff8080808080ffff01ff0bffff0101ff058080ff0180ff02fff"
        "f03ffff21ff17ffff09ff0bff158080ffff01ff04ff30ffff04ff0bff808080ffff01ff088080ff0180ff018080ffff04ffff01ffa07f"
        "aa3253bfddd1e0decb0906b2dc6247bbc4cf608f58345d173adb63e8b47c9fffa030d940e53ed5b56fee3ae46ba5f4e59da5e2cc9242f"
        "6e482fe1f1e4d9a463639a0eff07522495060c066f66f32acc2a77e3a3e737aca8baea4d1a64ea4cdc13da9ffff04ffff010dff018080"
        "80"
    )
    test_solution = SerializedProgram.fromhex(
        "ffffa030d940e53ed5b56fee3ae46ba5f4e59da5e2cc9242f6e482fe1f1e4d9a463639ffa0c7b89cfb9abf2c4cb212a4840b37d762f4c"
        "880b8517b0dadb0c310ded24dd86dff82053980ff820539ffff80ffff01ffff33ffa0c7b89cfb9abf2c4cb212a4840b37d762f4c880b8"
        "517b0dadb0c310ded24dd86dff8205398080ff808080"
    )
    test_coin_spend = CoinSpend(test_coin, test_puzzle_reveal, test_solution)
    test_spend_data = BundleCoinSpend(test_coin_spend, False, True, [test_child_coin], uint64(0))
    test_unspent_lineage_info = UnspentLineageInfo(
        coin_id=latest_unspent_coin.name(),
        parent_id=latest_unspent_coin.parent_coin_info,
        parent_parent_id=test_child_coin.parent_coin_info,
    )
    # Start from a fresh state of fast forward spends
    fast_forward_spends: dict[bytes32, UnspentLineageInfo] = {}
    # Perform the fast forward on the test coin (the grandparent)
    new_coin_spend, patched_additions = perform_the_fast_forward(
        test_unspent_lineage_info, test_spend_data, fast_forward_spends
    )
    # Make sure the new coin we got is the grandchild (latest unspent version)
    assert new_coin_spend.coin == latest_unspent_coin
    # Make sure the puzzle reveal is intact
    assert new_coin_spend.puzzle_reveal == test_coin_spend.puzzle_reveal
    # Make sure the solution got patched
    assert new_coin_spend.solution != test_coin_spend.solution
    # Make sure the additions got patched
    expected_child_coin = Coin(latest_unspent_coin.name(), test_ph, test_amount)
    assert patched_additions == [expected_child_coin]
    # Make sure the new fast forward state got updated with the latest unspent
    # becoming the new child, with its parent being the version we just spent
    # (previously latest unspent)
    expected_unspent_lineage_info = UnspentLineageInfo(
        coin_id=expected_child_coin.name(),
        parent_id=latest_unspent_coin.name(),
        parent_parent_id=latest_unspent_coin.parent_coin_info,
    )
    assert fast_forward_spends == {test_ph: expected_unspent_lineage_info}


def sign_delegated_puz(del_puz: Program, coin: Coin) -> G2Element:
    synthetic_secret_key: PrivateKey = p2_delegated_puzzle_or_hidden_puzzle.calculate_synthetic_secret_key(
        PrivateKey.from_bytes(secret_exponent_for_index(1).to_bytes(32, "big")),
        p2_delegated_puzzle_or_hidden_puzzle.DEFAULT_HIDDEN_PUZZLE_HASH,
    )
    return AugSchemeMPL.sign(
        synthetic_secret_key, (del_puz.get_tree_hash() + coin.name() + DEFAULT_CONSTANTS.AGG_SIG_ME_ADDITIONAL_DATA)
    )


async def make_and_send_spend_bundle(
    sim: SpendSim,
    sim_client: SimClient,
    coin_spends: list[CoinSpend],
    is_eligible_for_ff: bool = True,
    *,
    is_launcher_coin: bool = False,
    signing_puzzle: Optional[Program] = None,
    signing_coin: Optional[Coin] = None,
    aggsig: G2Element = G2Element(),
) -> tuple[MempoolInclusionStatus, Optional[Err]]:
    if is_launcher_coin or not is_eligible_for_ff:
        assert signing_puzzle is not None
        assert signing_coin is not None
        signature = sign_delegated_puz(signing_puzzle, signing_coin)
        signature += aggsig
    else:
        signature = aggsig
    spend_bundle = SpendBundle(coin_spends, signature)
    status, error = await sim_client.push_tx(spend_bundle)
    if error is None:
        await sim.farm_block()
    return status, error


async def get_singleton_and_remaining_coins(sim: SpendSim) -> tuple[Coin, list[Coin]]:
    coins = await sim.all_non_reward_coins()
    singletons = [coin for coin in coins if coin.amount & 1]
    assert len(singletons) == 1
    singleton = singletons[0]
    coins.remove(singleton)
    return singleton, coins


def make_singleton_coin_spend(
    parent_coin_spend: CoinSpend,
    coin_to_spend: Coin,
    inner_puzzle: Program,
    inner_conditions: list[list[Any]],
    is_eve_spend: bool = False,
) -> tuple[CoinSpend, Program]:
    lineage_proof = singleton_top_layer.lineage_proof_for_coinsol(parent_coin_spend)
    delegated_puzzle = Program.to((1, inner_conditions))
    inner_solution = Program.to([[], delegated_puzzle, []])
    solution = singleton_top_layer.solution_for_singleton(lineage_proof, uint64(coin_to_spend.amount), inner_solution)
    if is_eve_spend:
        # Parent here is the launcher coin
        puzzle_reveal = singleton_top_layer.puzzle_for_singleton(
            parent_coin_spend.coin.name(), inner_puzzle
        ).to_serialized()
    else:
        puzzle_reveal = parent_coin_spend.puzzle_reveal
    return make_spend(coin_to_spend, puzzle_reveal, solution), delegated_puzzle


async def prepare_singleton_eve(
    sim: SpendSim, sim_client: SimClient, is_eligible_for_ff: bool, singleton_amount: uint64
) -> tuple[Program, CoinSpend, Program]:
    # Generate starting info
    key_lookup = KeyTool()
    pk = G1Element.from_bytes(public_key_for_index(1, key_lookup))
    starting_puzzle = p2_delegated_puzzle_or_hidden_puzzle.puzzle_for_pk(pk)
    if is_eligible_for_ff:
        # This program allows us to control conditions through solutions
        inner_puzzle = Program.to(13)
    else:
        inner_puzzle = starting_puzzle
    inner_puzzle_hash = inner_puzzle.get_tree_hash()
    # Get our starting standard coin created
    await sim.farm_block(starting_puzzle.get_tree_hash())
    records = await sim_client.get_coin_records_by_puzzle_hash(starting_puzzle.get_tree_hash())
    starting_coin = records[0].coin
    # Launching
    conditions, launcher_coin_spend = singleton_top_layer.launch_conditions_and_coinsol(
        coin=starting_coin, inner_puzzle=inner_puzzle, comment=[], amount=singleton_amount
    )
    # Keep a remaining coin with an even amount
    conditions.append(
        Program.to([ConditionOpcode.CREATE_COIN, IDENTITY_PUZZLE_HASH, starting_coin.amount - singleton_amount - 1])
    )
    # Create a solution for standard transaction
    delegated_puzzle = p2_conditions.puzzle_for_conditions(conditions)
    full_solution = p2_delegated_puzzle_or_hidden_puzzle.solution_for_conditions(conditions)
    starting_coin_spend = make_spend(starting_coin, starting_puzzle, full_solution)
    await make_and_send_spend_bundle(
        sim,
        sim_client,
        [starting_coin_spend, launcher_coin_spend],
        is_eligible_for_ff,
        is_launcher_coin=True,
        signing_puzzle=delegated_puzzle,
        signing_coin=starting_coin,
    )
    eve_coin, _ = await get_singleton_and_remaining_coins(sim)
    inner_conditions = [[ConditionOpcode.CREATE_COIN, inner_puzzle_hash, singleton_amount]]
    eve_coin_spend, eve_signing_puzzle = make_singleton_coin_spend(
        parent_coin_spend=launcher_coin_spend,
        coin_to_spend=eve_coin,
        inner_puzzle=inner_puzzle,
        inner_conditions=inner_conditions,
        is_eve_spend=True,
    )
    return inner_puzzle, eve_coin_spend, eve_signing_puzzle


async def prepare_and_test_singleton(
    sim: SpendSim, sim_client: SimClient, is_eligible_for_ff: bool, singleton_amount: uint64
) -> tuple[Coin, CoinSpend, Program, Coin]:
    inner_puzzle, eve_coin_spend, eve_signing_puzzle = await prepare_singleton_eve(
        sim, sim_client, is_eligible_for_ff, singleton_amount
    )
    # At this point we don't have any unspent singleton
    singleton_puzzle_hash = eve_coin_spend.coin.puzzle_hash
    unspent_lineage_info = await sim_client.service.coin_store.get_unspent_lineage_info_for_puzzle_hash(
        singleton_puzzle_hash
    )
    assert unspent_lineage_info is None
    eve_coin = eve_coin_spend.coin
    await make_and_send_spend_bundle(
        sim, sim_client, [eve_coin_spend], is_eligible_for_ff, signing_puzzle=eve_signing_puzzle, signing_coin=eve_coin
    )
    # Now we spent eve and we have an unspent singleton that we can test with
    singleton, [remaining_coin] = await get_singleton_and_remaining_coins(sim)
    assert singleton.amount == singleton_amount
    singleton_puzzle_hash = eve_coin.puzzle_hash
    unspent_lineage_info = await sim_client.service.coin_store.get_unspent_lineage_info_for_puzzle_hash(
        singleton_puzzle_hash
    )
    assert unspent_lineage_info == UnspentLineageInfo(
        coin_id=singleton.name(), parent_id=eve_coin.name(), parent_parent_id=eve_coin.parent_coin_info
    )
    return singleton, eve_coin_spend, inner_puzzle, remaining_coin


@pytest.mark.anyio
async def test_singleton_fast_forward_solo() -> None:
    """
    We don't allow a spend bundle with *only* fast forward spends, since those
    are difficult to evict from the mempool. They would always be valid as long as
    the singleton exists.
    """
    SINGLETON_AMOUNT = uint64(1337)
    async with sim_and_client() as (sim, sim_client):
        singleton, eve_coin_spend, inner_puzzle, _ = await prepare_and_test_singleton(
            sim, sim_client, True, SINGLETON_AMOUNT
        )
        singleton_puzzle_hash = eve_coin_spend.coin.puzzle_hash
        inner_puzzle_hash = inner_puzzle.get_tree_hash()
        inner_conditions: list[list[Any]] = [
            [ConditionOpcode.CREATE_COIN, inner_puzzle_hash, SINGLETON_AMOUNT],
        ]
        singleton_coin_spend, _ = make_singleton_coin_spend(eve_coin_spend, singleton, inner_puzzle, inner_conditions)
        # spending the eve coin is not eligible for fast forward, so we need to make this spend first, to test FF
        await make_and_send_spend_bundle(sim, sim_client, [singleton_coin_spend], aggsig=G2Element())
        unspent_lineage_info = await sim_client.service.coin_store.get_unspent_lineage_info_for_puzzle_hash(
            singleton_puzzle_hash
        )
        singleton_child, _ = await get_singleton_and_remaining_coins(sim)
        assert singleton_child.amount == SINGLETON_AMOUNT
        assert unspent_lineage_info == UnspentLineageInfo(
            coin_id=singleton_child.name(),
            parent_id=eve_coin_spend.coin.name(),
            parent_parent_id=eve_coin_spend.coin.parent_coin_info,
        )

        inner_conditions = [[ConditionOpcode.CREATE_COIN, inner_puzzle_hash, SINGLETON_AMOUNT]]
        # this is a FF spend that isn't combined with any other spend. It's not allowed
        singleton_coin_spend, _ = make_singleton_coin_spend(eve_coin_spend, singleton, inner_puzzle, inner_conditions)
        status, error = await sim_client.push_tx(SpendBundle([singleton_coin_spend], G2Element()))
        assert error is Err.INVALID_SPEND_BUNDLE
        assert status == MempoolInclusionStatus.FAILED


@pytest.mark.anyio
@pytest.mark.parametrize("is_eligible_for_ff", [True, False])
async def test_singleton_fast_forward_different_block(is_eligible_for_ff: bool) -> None:
    """
    This tests uses the `is_eligible_for_ff` parameter to cover both when a
    singleton is eligible for fast forward and when it's not, as we attempt to
    spend an earlier version of it, in a different block, and watch it either
    get properly fast forwarded to the latest unspent (when it's eligible) or
    get correctly rejected as a double spend (when it's not eligible)
    """
    SINGLETON_AMOUNT = uint64(1337)
    async with sim_and_client() as (sim, sim_client):
        singleton, eve_coin_spend, inner_puzzle, remaining_coin = await prepare_and_test_singleton(
            sim, sim_client, is_eligible_for_ff, SINGLETON_AMOUNT
        )
        # Let's spend this first version, to create a bigger singleton child
        singleton_puzzle_hash = eve_coin_spend.coin.puzzle_hash
        inner_puzzle_hash = inner_puzzle.get_tree_hash()

        sk = AugSchemeMPL.key_gen(b"1" * 32)
        g1 = sk.get_g1()
        sig = AugSchemeMPL.sign(sk, b"foobar", g1)
        inner_conditions: list[list[Any]] = [
            [ConditionOpcode.AGG_SIG_UNSAFE, bytes(g1), b"foobar"],
            [ConditionOpcode.CREATE_COIN, inner_puzzle_hash, SINGLETON_AMOUNT],
        ]
        singleton_coin_spend, singleton_signing_puzzle = make_singleton_coin_spend(
            eve_coin_spend, singleton, inner_puzzle, inner_conditions
        )
        # Spend also a remaining coin
        remaining_spend_solution = SerializedProgram.to(
            [[ConditionOpcode.CREATE_COIN, IDENTITY_PUZZLE_HASH, remaining_coin.amount]]
        )
        remaining_coin_spend = CoinSpend(remaining_coin, IDENTITY_PUZZLE, remaining_spend_solution)
        await make_and_send_spend_bundle(
            sim,
            sim_client,
            [remaining_coin_spend, singleton_coin_spend],
            is_eligible_for_ff,
            signing_puzzle=singleton_signing_puzzle,
            signing_coin=singleton,
            aggsig=sig,
        )
        unspent_lineage_info = await sim_client.service.coin_store.get_unspent_lineage_info_for_puzzle_hash(
            singleton_puzzle_hash
        )
        singleton_child, [remaining_coin] = await get_singleton_and_remaining_coins(sim)
        assert singleton_child.amount == SINGLETON_AMOUNT
        assert unspent_lineage_info == UnspentLineageInfo(
            coin_id=singleton_child.name(), parent_id=singleton.name(), parent_parent_id=eve_coin_spend.coin.name()
        )
        # Now let's spend the first version again (despite being already spent by now)
        remaining_spend_solution = SerializedProgram.to(
            [[ConditionOpcode.CREATE_COIN, IDENTITY_PUZZLE_HASH, remaining_coin.amount]]
        )
        remaining_coin_spend = CoinSpend(remaining_coin, IDENTITY_PUZZLE, remaining_spend_solution)
        status, error = await make_and_send_spend_bundle(
            sim,
            sim_client,
            [remaining_coin_spend, singleton_coin_spend],
            is_eligible_for_ff,
            signing_puzzle=singleton_signing_puzzle,
            signing_coin=singleton,
            aggsig=sig,
        )
        if is_eligible_for_ff:
            # Instead of rejecting this as double spend, we perform a fast forward,
            # spending the singleton child as a result, and creating the latest
            # version which is the grandchild in this scenario
            assert status == MempoolInclusionStatus.SUCCESS
            assert error is None
            unspent_lineage_info = await sim_client.service.coin_store.get_unspent_lineage_info_for_puzzle_hash(
                singleton_puzzle_hash
            )
            singleton_grandchild, [remaining_coin] = await get_singleton_and_remaining_coins(sim)
            assert unspent_lineage_info == UnspentLineageInfo(
                coin_id=singleton_grandchild.name(), parent_id=singleton_child.name(), parent_parent_id=singleton.name()
            )
        else:
            # As this singleton is not eligible for fast forward, attempting to
            # spend one of its earlier versions is considered a double spend
            assert status == MempoolInclusionStatus.FAILED
            assert error == Err.DOUBLE_SPEND


@pytest.mark.anyio
async def test_singleton_fast_forward_same_block() -> None:
    """
    This tests covers sending multiple transactions that spend an already spent
    singleton version, all in the same block, to make sure they get properly
    fast forwarded and chained down to a latest unspent version
    """
    SINGLETON_AMOUNT = uint64(1337)
    async with sim_and_client() as (sim, sim_client):
        singleton, eve_coin_spend, inner_puzzle, remaining_coin = await prepare_and_test_singleton(
            sim, sim_client, True, SINGLETON_AMOUNT
        )
        # Let's spend this first version, to create a bigger singleton child
        singleton_puzzle_hash = eve_coin_spend.coin.puzzle_hash
        inner_puzzle_hash = inner_puzzle.get_tree_hash()
        sk = AugSchemeMPL.key_gen(b"9" * 32)
        g1 = sk.get_g1()
        sig = AugSchemeMPL.sign(sk, b"foobar", g1)
        inner_conditions: list[list[Any]] = [
            [ConditionOpcode.AGG_SIG_UNSAFE, bytes(g1), b"foobar"],
            [ConditionOpcode.CREATE_COIN, inner_puzzle_hash, SINGLETON_AMOUNT],
        ]
        singleton_coin_spend, _ = make_singleton_coin_spend(eve_coin_spend, singleton, inner_puzzle, inner_conditions)
        # Spend also a remaining coin. Change amount to create a new coin ID.
        # The test assumes any odd amount is a singleton, so we must keep it
        # even
        remaining_spend_solution = SerializedProgram.to(
            [[ConditionOpcode.CREATE_COIN, IDENTITY_PUZZLE_HASH, remaining_coin.amount - 2]]
        )
        remaining_coin_spend = CoinSpend(remaining_coin, IDENTITY_PUZZLE, remaining_spend_solution)
        await make_and_send_spend_bundle(sim, sim_client, [remaining_coin_spend, singleton_coin_spend], aggsig=sig)
        unspent_lineage_info = await sim_client.service.coin_store.get_unspent_lineage_info_for_puzzle_hash(
            singleton_puzzle_hash
        )
        singleton_child, [remaining_coin] = await get_singleton_and_remaining_coins(sim)
        assert singleton_child.amount == SINGLETON_AMOUNT
        assert unspent_lineage_info == UnspentLineageInfo(
            coin_id=singleton_child.name(), parent_id=singleton.name(), parent_parent_id=eve_coin_spend.coin.name()
        )
        # Now let's send 3 arbitrary spends of the already spent singleton in
        # one block. They should all properly fast forward

        sk = AugSchemeMPL.key_gen(b"a" * 32)
        g1 = sk.get_g1()
        sig = AugSchemeMPL.sign(sk, b"foobar", g1)
        for i in range(3):
            # This cost adjustment allows us to maintain the order of spends due to fee per
            # cost and amounts dynamics
            cost_factor = (i + 1) * 5
            inner_conditions = [[ConditionOpcode.AGG_SIG_UNSAFE, bytes(g1), b"foobar"] for _ in range(cost_factor)]
            aggsig = G2Element()
            for _ in range(cost_factor):
                aggsig += sig
            inner_conditions.append([ConditionOpcode.CREATE_COIN, inner_puzzle_hash, SINGLETON_AMOUNT])
            singleton_coin_spend, _ = make_singleton_coin_spend(
                eve_coin_spend, singleton, inner_puzzle, inner_conditions
            )
            remaining_coin_spend = CoinSpend(remaining_coin, IDENTITY_PUZZLE, remaining_spend_solution)
            status, error = await sim_client.push_tx(SpendBundle([singleton_coin_spend, remaining_coin_spend], aggsig))
            assert error is None
            assert status == MempoolInclusionStatus.SUCCESS

        # Farm a block to process all these spend bundles
        await sim.farm_block()
        unspent_lineage_info = await sim_client.service.coin_store.get_unspent_lineage_info_for_puzzle_hash(
            singleton_puzzle_hash
        )
        latest_singleton, [remaining_coin] = await get_singleton_and_remaining_coins(sim)
        assert unspent_lineage_info is not None
        # The unspent coin ID should reflect the latest version
        assert unspent_lineage_info.coin_id == latest_singleton.name()
        # The unspent parent ID should reflect the latest version's parent
        assert unspent_lineage_info.parent_id == latest_singleton.parent_coin_info


@pytest.mark.anyio
async def test_mempool_items_immutability_on_ff() -> None:
    """
    This tests processing singleton fast forward spends for mempool items using
    modified copies, without altering those original mempool items.
    """
    SINGLETON_AMOUNT = uint64(1337)
    async with sim_and_client() as (sim, sim_client):
        singleton, eve_coin_spend, inner_puzzle, remaining_coin = await prepare_and_test_singleton(
            sim, sim_client, True, SINGLETON_AMOUNT
        )
        singleton_name = singleton.name()
        singleton_puzzle_hash = eve_coin_spend.coin.puzzle_hash
        inner_puzzle_hash = inner_puzzle.get_tree_hash()
        sk = AugSchemeMPL.key_gen(b"1" * 32)
        g1 = sk.get_g1()
        sig = AugSchemeMPL.sign(sk, b"foobar", g1)
        inner_conditions: list[list[Any]] = [
            [ConditionOpcode.AGG_SIG_UNSAFE, bytes(g1), b"foobar"],
            [ConditionOpcode.CREATE_COIN, inner_puzzle_hash, SINGLETON_AMOUNT],
        ]
        singleton_coin_spend, singleton_signing_puzzle = make_singleton_coin_spend(
            eve_coin_spend, singleton, inner_puzzle, inner_conditions
        )
        remaining_spend_solution = SerializedProgram.to(
            [[ConditionOpcode.CREATE_COIN, IDENTITY_PUZZLE_HASH, remaining_coin.amount]]
        )
        remaining_coin_spend = CoinSpend(remaining_coin, IDENTITY_PUZZLE, remaining_spend_solution)
        await make_and_send_spend_bundle(
            sim,
            sim_client,
            [remaining_coin_spend, singleton_coin_spend],
            signing_puzzle=singleton_signing_puzzle,
            signing_coin=singleton,
            aggsig=sig,
        )
        unspent_lineage_info = await sim_client.service.coin_store.get_unspent_lineage_info_for_puzzle_hash(
            singleton_puzzle_hash
        )
        singleton_child, [remaining_coin] = await get_singleton_and_remaining_coins(sim)
        singleton_child_name = singleton_child.name()
        assert singleton_child.amount == SINGLETON_AMOUNT
        assert unspent_lineage_info == UnspentLineageInfo(
            coin_id=singleton_child_name, parent_id=singleton_name, parent_parent_id=eve_coin_spend.coin.name()
        )
        # Now let's spend the first version again (despite being already spent
        # by now) to exercise its fast forward.
        remaining_spend_solution = SerializedProgram.to(
            [[ConditionOpcode.CREATE_COIN, IDENTITY_PUZZLE_HASH, remaining_coin.amount]]
        )
        remaining_coin_spend = CoinSpend(remaining_coin, IDENTITY_PUZZLE, remaining_spend_solution)
        sb = SpendBundle([remaining_coin_spend, singleton_coin_spend], sig)
        sb_name = sb.name()
        status, error = await sim_client.push_tx(sb)
        assert status == MempoolInclusionStatus.SUCCESS
        assert error is None
        original_item = copy.copy(sim_client.service.mempool_manager.get_mempool_item(sb_name))
        original_filter = sim_client.service.mempool_manager.get_filter()
        # Let's trigger the fast forward by creating a mempool bundle
        result = sim.mempool_manager.create_bundle_from_mempool(sim_client.service.block_records[-1].header_hash)
        assert result is not None
        bundle, _ = result
        # Make sure the mempool bundle we created contains the result of our
        # fast forward, instead of our original spend.
        assert any(cs.coin.name() == singleton_child_name for cs in bundle.coin_spends)
        assert not any(cs.coin.name() == singleton_name for cs in bundle.coin_spends)
        # We should have processed our item without modifying it in-place
        new_item = copy.copy(sim_client.service.mempool_manager.get_mempool_item(sb_name))
        new_filter = sim_client.service.mempool_manager.get_filter()
        assert new_item == original_item
        assert new_filter == original_filter
        sb_filter = PyBIP158(bytearray(original_filter))
        items_not_in_sb_filter = sim_client.service.mempool_manager.get_items_not_in_filter(sb_filter)
        assert len(items_not_in_sb_filter) == 0


@pytest.mark.anyio
async def test_double_spend_ff_spend_no_latest_unspent() -> None:
    """
    This test covers the scenario where we receive a spend bundle with a
    singleton fast forward spend that has currently no unspent coin.
    """
    singleton_amount = uint64(1337)
    async with sim_and_client() as (sim, sim_client):
        # Prepare a singleton spend
        singleton, eve_coin_spend, inner_puzzle, _ = await prepare_and_test_singleton(
            sim, sim_client, True, singleton_amount=singleton_amount
        )
        singleton_name = singleton.name()
        singleton_puzzle_hash = eve_coin_spend.coin.puzzle_hash
        inner_puzzle_hash = inner_puzzle.get_tree_hash()
        sk = AugSchemeMPL.key_gen(b"9" * 32)
        g1 = sk.get_g1()
        sig = AugSchemeMPL.sign(sk, b"foobar", g1)
        inner_conditions: list[list[Any]] = [
            [ConditionOpcode.AGG_SIG_UNSAFE, bytes(g1), b"foobar"],
            [ConditionOpcode.CREATE_COIN, inner_puzzle_hash, singleton_amount],
        ]
        singleton_coin_spend, _ = make_singleton_coin_spend(eve_coin_spend, singleton, inner_puzzle, inner_conditions)
        # Get its current latest unspent info
        unspent_lineage_info = await sim_client.service.coin_store.get_unspent_lineage_info_for_puzzle_hash(
            singleton_puzzle_hash
        )
        assert unspent_lineage_info == UnspentLineageInfo(
            coin_id=singleton_name,
            parent_id=eve_coin_spend.coin.name(),
            parent_parent_id=eve_coin_spend.coin.parent_coin_info,
        )
        # Let's remove this latest unspent coin from the coin store
        async with sim_client.service.coin_store.db_wrapper.writer_maybe_transaction() as conn:
            await conn.execute("DELETE FROM coin_record WHERE coin_name = ?", (unspent_lineage_info.coin_id,))
        # This singleton no longer has a latest unspent coin
        unspent_lineage_info = await sim_client.service.coin_store.get_unspent_lineage_info_for_puzzle_hash(
            singleton_puzzle_hash
        )
        assert unspent_lineage_info is None
        # Let's attempt to spend this singleton and get get it fast forwarded
        status, error = await make_and_send_spend_bundle(sim, sim_client, [singleton_coin_spend], aggsig=sig)
        # It fails validation because it doesn't currently have a latest unspent
        assert status == MempoolInclusionStatus.FAILED
        assert error == Err.DOUBLE_SPEND
