from __future__ import annotations

from chia_rs import ConsensusConstants
from chia_rs.sized_bytes import bytes32
from chia_rs.sized_ints import uint8, uint64
from chiavdf import prove

from chia.types.blockchain_format.classgroup import ClassgroupElement
from chia.types.blockchain_format.vdf import VDFInfo, VDFProof


def get_vdf_info_and_proof(
    constants: ConsensusConstants,
    vdf_input: ClassgroupElement,
    challenge_hash: bytes32,
    number_iters: uint64,
    normalized_to_identity: bool = False,
) -> tuple[VDFInfo, VDFProof]:
    form_size = ClassgroupElement.get_size()
    result: bytes = prove(
        bytes(challenge_hash),
        vdf_input.data,
        constants.DISCRIMINANT_SIZE_BITS,
        number_iters,
        "",
    )

    output = ClassgroupElement.create(result[:form_size])
    proof_bytes = result[form_size : 2 * form_size]
    return VDFInfo(challenge_hash, number_iters, output), VDFProof(uint8(0), proof_bytes, normalized_to_identity)
