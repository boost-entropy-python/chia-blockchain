from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

import pytest
from chia_rs import G2Element, Program
from chia_rs.sized_bytes import bytes32
from chia_rs.sized_ints import uint64

from chia._tests.cmds.wallet.test_consts import STD_TX
from chia.data_layer.singleton_record import SingletonRecord
from chia.types.blockchain_format.coin import Coin
from chia.wallet.signer_protocol import SigningResponse
from chia.wallet.transaction_record import TransactionRecord
from chia.wallet.util.tx_config import DEFAULT_TX_CONFIG
from chia.wallet.wallet_action_scope import WalletSideEffects
from chia.wallet.wallet_spend_bundle import WalletSpendBundle
from chia.wallet.wallet_state_manager import WalletStateManager

MOCK_SR = SigningResponse(b"hey", bytes32.zeros)
MOCK_SB = WalletSpendBundle([], G2Element())
MOCK_COIN = Coin(bytes32.zeros, bytes32.zeros, uint64(0))


def test_back_and_forth_serialization() -> None:
    assert bytes(WalletSideEffects())
    assert WalletSideEffects.from_bytes(bytes(WalletSideEffects())) == WalletSideEffects()
    assert WalletSideEffects.from_bytes(
        bytes(WalletSideEffects([STD_TX], [MOCK_SR], [MOCK_SB], [MOCK_COIN]))
    ) == WalletSideEffects([STD_TX], [MOCK_SR], [MOCK_SB], [MOCK_COIN])
    assert WalletSideEffects.from_bytes(
        bytes(WalletSideEffects([STD_TX, STD_TX], [MOCK_SR, MOCK_SR], [MOCK_SB, MOCK_SB], [MOCK_COIN, MOCK_COIN]))
    ) == WalletSideEffects([STD_TX, STD_TX], [MOCK_SR, MOCK_SR], [MOCK_SB, MOCK_SB], [MOCK_COIN, MOCK_COIN])


@dataclass
class MockWalletStateManager:
    most_recent_call: Optional[
        tuple[
            list[TransactionRecord],
            bool,
            bool,
            bool,
            list[SigningResponse],
            list[WalletSpendBundle],
            list[SingletonRecord],
        ],
    ] = None

    async def add_pending_transactions(
        self,
        txs: list[TransactionRecord],
        push: bool,
        merge_spends: bool,
        sign: bool,
        additional_signing_responses: list[SigningResponse],
        extra_spends: list[WalletSpendBundle],
        singleton_records: list[SingletonRecord],
    ) -> list[TransactionRecord]:
        self.most_recent_call = (
            txs,
            push,
            merge_spends,
            sign,
            additional_signing_responses,
            extra_spends,
            singleton_records,
        )
        return txs


MockWalletStateManager.new_action_scope = WalletStateManager.new_action_scope  # type: ignore[attr-defined]


@pytest.mark.anyio
async def test_wallet_action_scope() -> None:
    wsm = MockWalletStateManager()
    async with wsm.new_action_scope(  # type: ignore[attr-defined]
        DEFAULT_TX_CONFIG,
        push=True,
        merge_spends=False,
        sign=True,
        additional_signing_responses=[],
        extra_spends=[],
        puzzle_for_pk=lambda _: Program.to(1),
    ) as action_scope:
        async with action_scope.use() as interface:
            interface.side_effects.transactions = [STD_TX]

        with pytest.raises(RuntimeError):
            action_scope.side_effects

    assert action_scope.side_effects.transactions == [STD_TX]
    assert wsm.most_recent_call == ([STD_TX], True, False, True, [], [], [])

    async with wsm.new_action_scope(  # type: ignore[attr-defined]
        DEFAULT_TX_CONFIG,
        push=False,
        merge_spends=True,
        sign=True,
        additional_signing_responses=[],
        extra_spends=[],
        puzzle_for_pk=lambda _: Program.to(1),
    ) as action_scope:
        async with action_scope.use() as interface:
            interface.side_effects.transactions = []

    assert action_scope.side_effects.transactions == []
    assert wsm.most_recent_call == ([], False, True, True, [], [], [])
