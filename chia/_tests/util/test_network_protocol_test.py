from __future__ import annotations

import ast
import inspect
from typing import Any, cast

from chia.protocols import (
    farmer_protocol,
    full_node_protocol,
    harvester_protocol,
    introducer_protocol,
    pool_protocol,
    protocol_message_types,
    shared_protocol,
    timelord_protocol,
    wallet_protocol,
)

# this test is to ensure the network protocol message regression test always
# stays up to date. It's a test for the test


def types_in_module(mod: Any) -> set[str]:
    parsed = ast.parse(inspect.getsource(mod))
    types = set()
    for line in parsed.body:
        if isinstance(line, ast.Assign):
            name = cast(ast.Name, line.targets[0])
            if inspect.isclass(getattr(mod, name.id)):
                types.add(name.id)
        elif isinstance(line, ast.ClassDef):
            types.add(line.name)
    return types


STANDARD_ADVICE = "Make sure to update the protocol message regression test to include the new message"


def test_missing_messages_state_machine() -> None:
    from chia.protocols.protocol_state_machine import NO_REPLY_EXPECTED, VALID_REPLY_MESSAGE_MAP

    # if these asserts fail, make sure to add the new network protocol messages
    # to the visitor in build_network_protocol_files.py and rerun it. Then
    # update this test
    assert len(VALID_REPLY_MESSAGE_MAP) == 26, f"A message was added to the protocol state machine. {STANDARD_ADVICE}"
    assert len(NO_REPLY_EXPECTED) == 10, f"A message was added to the protocol state machine. {STANDARD_ADVICE}"


def test_message_ids() -> None:
    parsed = ast.parse(inspect.getsource(protocol_message_types))
    message_ids: dict[int, str] = {}
    for line in parsed.body:
        if not isinstance(line, ast.ClassDef) or line.name != "ProtocolMessageTypes":
            continue
        for entry in line.body:
            if not isinstance(entry, ast.Assign):  # pragma: no cover
                continue
            assert isinstance(entry.value, ast.Constant)
            assert isinstance(entry.targets[0], ast.Name)
            message_id = entry.value.value
            assert isinstance(message_id, int), f"message ID must be an int, got {type(message_id).__name__}"
            message_name = entry.targets[0].id
            if message_id in message_ids:  # pragma: no cover
                raise AssertionError(
                    f'protocol message ID clash between "{message_name}" and "{message_ids[message_id]}". '
                    f"Value {message_id}"
                )
            message_ids[message_id] = message_name
            if message_id < 0 or message_id > 255:  # pragma: no cover
                raise AssertionError(f'message ID must fit in a uint8. "{message_name}" has value {message_id}')
        break
    assert len(message_ids) > 0


def test_missing_messages() -> None:
    wallet_msgs = {
        "CoinState",
        "CoinStateFilters",
        "CoinStateUpdate",
        "MempoolItemsAdded",
        "MempoolItemsRemoved",
        "NewPeakWallet",
        "PuzzleSolutionResponse",
        "RegisterForCoinUpdates",
        "RegisterForPhUpdates",
        "RejectAdditionsRequest",
        "RejectBlockHeaders",
        "RejectCoinState",
        "RejectHeaderBlocks",
        "RejectHeaderRequest",
        "RejectPuzzleSolution",
        "RejectPuzzleState",
        "RejectRemovalsRequest",
        "RejectStateReason",
        "RemovedMempoolItem",
        "RequestAdditions",
        "RequestBlockHeader",
        "RequestBlockHeaders",
        "RequestChildren",
        "RequestCoinState",
        "RequestCostInfo",
        "RequestFeeEstimates",
        "RequestHeaderBlocks",
        "RequestPuzzleSolution",
        "RequestPuzzleState",
        "RequestRemovals",
        "RequestRemoveCoinSubscriptions",
        "RequestRemovePuzzleSubscriptions",
        "RequestSESInfo",
        "RespondAdditions",
        "RespondBlockHeader",
        "RespondBlockHeaders",
        "RespondChildren",
        "RespondCoinState",
        "RespondCostInfo",
        "RespondFeeEstimates",
        "RespondHeaderBlocks",
        "RespondPuzzleSolution",
        "RespondPuzzleState",
        "RespondRemovals",
        "RespondRemoveCoinSubscriptions",
        "RespondRemovePuzzleSubscriptions",
        "RespondSESInfo",
        "RespondToCoinUpdates",
        "RespondToPhUpdates",
        "SendTransaction",
        "TransactionAck",
    }

    farmer_msgs = {
        "DeclareProofOfSpace",
        "FarmingInfo",
        "SPSubSlotSourceData",
        "SPVDFSourceData",
        "SignagePointSourceData",
        "NewSignagePoint",
        "RequestSignedValues",
        "SignedValues",
    }

    full_node_msgs = {
        "NewCompactVDF",
        "NewPeak",
        "NewSignagePointOrEndOfSubSlot",
        "NewTransaction",
        "NewUnfinishedBlock",
        "NewUnfinishedBlock2",
        "RejectBlock",
        "RejectBlocks",
        "RequestBlock",
        "RequestBlocks",
        "RequestCompactVDF",
        "RequestMempoolTransactions",
        "RequestPeers",
        "RequestProofOfWeight",
        "RequestSignagePointOrEndOfSubSlot",
        "RequestTransaction",
        "RequestUnfinishedBlock",
        "RequestUnfinishedBlock2",
        "RespondBlock",
        "RespondBlocks",
        "RespondCompactVDF",
        "RespondEndOfSubSlot",
        "RespondPeers",
        "RespondProofOfWeight",
        "RespondSignagePoint",
        "RespondTransaction",
        "RespondUnfinishedBlock",
    }

    harvester_msgs = {
        "HarvesterHandshake",
        "ProofOfSpaceFeeInfo",
        "NewProofOfSpace",
        "NewSignagePointHarvester",
        "Plot",
        "PlotSyncDone",
        "PlotSyncError",
        "PlotSyncIdentifier",
        "PlotSyncPathList",
        "PlotSyncPlotList",
        "PlotSyncResponse",
        "PlotSyncStart",
        "PoolDifficulty",
        "RequestPlots",
        "SigningDataKind",
        "SignatureRequestSourceData",
        "RequestSignatures",
        "RespondPlots",
        "RespondSignatures",
    }

    introducer_msgs = {"RequestPeersIntroducer", "RespondPeersIntroducer"}

    pool_msgs = {
        "AuthenticationPayload",
        "ErrorResponse",
        "GetFarmerResponse",
        "GetPoolInfoResponse",
        "PoolErrorCode",
        "PostFarmerPayload",
        "PostFarmerRequest",
        "PostFarmerResponse",
        "PostPartialPayload",
        "PostPartialRequest",
        "PostPartialResponse",
        "PutFarmerPayload",
        "PutFarmerRequest",
        "PutFarmerResponse",
    }

    timelord_msgs = {
        "NewEndOfSubSlotVDF",
        "NewInfusionPointVDF",
        "NewPeakTimelord",
        "NewSignagePointVDF",
        "NewUnfinishedBlockTimelord",
        "RequestCompactProofOfTime",
        "RespondCompactProofOfTime",
    }

    shared_msgs = {"Handshake", "Capability", "Error"}

    # if these asserts fail, make sure to add the new network protocol messages
    # to the visitor in build_network_protocol_files.py and rerun it. Then
    # update this test
    assert types_in_module(wallet_protocol) == wallet_msgs, (
        f"message types were added or removed from wallet_protocol. {STANDARD_ADVICE}"
    )

    assert types_in_module(farmer_protocol) == farmer_msgs, (
        f"message types were added or removed from farmer_protocol. {STANDARD_ADVICE}"
    )

    assert types_in_module(full_node_protocol) == full_node_msgs, (
        f"message types were added or removed from full_node_protocol. {STANDARD_ADVICE}"
    )

    assert types_in_module(harvester_protocol) == harvester_msgs, (
        f"message types were added or removed from harvester_protocol. {STANDARD_ADVICE}"
    )

    assert types_in_module(introducer_protocol) == introducer_msgs, (
        f"message types were added or removed from introducer_protocol. {STANDARD_ADVICE}"
    )

    assert types_in_module(pool_protocol) == pool_msgs, (
        f"message types were added or removed from pool_protocol. {STANDARD_ADVICE}"
    )

    assert types_in_module(timelord_protocol) == timelord_msgs, (
        f"message types were added or removed from timelord_protocol. {STANDARD_ADVICE}"
    )

    assert types_in_module(shared_protocol) == shared_msgs, (
        f"message types were added or removed from shared_protocol. {STANDARD_ADVICE}"
    )
