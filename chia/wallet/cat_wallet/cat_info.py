from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from chia_rs.sized_bytes import bytes32
from chia_rs.sized_ints import uint64

from chia.types.blockchain_format.program import Program
from chia.util.streamable import Streamable, streamable
from chia.wallet.lineage_proof import LineageProof
from chia.wallet.vc_wallet.cr_cat_drivers import ProofsChecker


@streamable
@dataclass(frozen=True)
class CATInfo(Streamable):
    limitations_program_hash: bytes32
    my_tail: Optional[Program]  # this is the program


@streamable
@dataclass(frozen=True)
class RCATInfo(CATInfo):
    hidden_puzzle_hash: bytes32


@streamable
@dataclass(frozen=True)
class CATCoinData(Streamable):
    mod_hash: bytes32
    tail_program_hash: bytes32
    inner_puzzle: Program
    parent_coin_id: bytes32
    amount: uint64


# We used to store all of the lineage proofs here but it was very slow to serialize for a lot of transactions
# so we moved it to CATLineageStore.  We keep this around for migration purposes.
@streamable
@dataclass(frozen=True)
class LegacyCATInfo(Streamable):
    limitations_program_hash: bytes32
    my_tail: Optional[Program]  # this is the program
    lineage_proofs: list[tuple[bytes32, Optional[LineageProof]]]  # {coin.name(): lineage_proof}


@streamable
@dataclass(frozen=True)
class CRCATInfo(CATInfo):
    authorized_providers: list[bytes32]
    proofs_checker: ProofsChecker
