from __future__ import annotations

import json
import logging
import time
from typing import TYPE_CHECKING, Any, ClassVar, Optional, Union, cast

import aiohttp
from chia_rs import AugSchemeMPL, G2Element, PoolTarget, PrivateKey
from chia_rs.sized_bytes import bytes32
from chia_rs.sized_ints import uint8, uint16, uint32, uint64

from chia import __version__
from chia.consensus.pot_iterations import calculate_iterations_quality, calculate_sp_interval_iters
from chia.farmer.farmer import Farmer, increment_pool_stats, strip_old_entries
from chia.harvester.harvester_api import HarvesterAPI
from chia.protocols import farmer_protocol, harvester_protocol
from chia.protocols.farmer_protocol import DeclareProofOfSpace, SignedValues
from chia.protocols.harvester_protocol import (
    PlotSyncDone,
    PlotSyncPathList,
    PlotSyncPlotList,
    PlotSyncStart,
    PoolDifficulty,
    SignatureRequestSourceData,
    SigningDataKind,
)
from chia.protocols.outbound_message import Message, NodeType, make_msg
from chia.protocols.pool_protocol import (
    PoolErrorCode,
    PostPartialPayload,
    PostPartialRequest,
    get_current_authentication_token,
)
from chia.protocols.protocol_message_types import ProtocolMessageTypes
from chia.server.api_protocol import ApiMetadata
from chia.server.server import ssl_context_for_root
from chia.server.ws_connection import WSChiaConnection
from chia.ssl.create_ssl import get_mozilla_ca_crt
from chia.types.blockchain_format.proof_of_space import (
    generate_plot_public_key,
    generate_taproot_sk,
    get_plot_id,
    verify_and_get_quality_string,
)


class FarmerAPI:
    if TYPE_CHECKING:
        from chia.server.api_protocol import ApiProtocol

        _protocol_check: ClassVar[ApiProtocol] = cast("FarmerAPI", None)

    log: logging.Logger
    farmer: Farmer
    metadata: ClassVar[ApiMetadata] = ApiMetadata()

    def __init__(self, farmer: Farmer) -> None:
        self.log = logging.getLogger(__name__)
        self.farmer = farmer

    def ready(self) -> bool:
        return self.farmer.started

    @metadata.request(peer_required=True)
    async def new_proof_of_space(
        self, new_proof_of_space: harvester_protocol.NewProofOfSpace, peer: WSChiaConnection
    ) -> None:
        """
        This is a response from the harvester, for a NewSignagePointHarvester.
        Here we check if the proof of space is sufficiently good, and if so, we
        ask for the whole proof.
        """
        if new_proof_of_space.sp_hash not in self.farmer.number_of_responses:
            self.farmer.number_of_responses[new_proof_of_space.sp_hash] = 0
            self.farmer.cache_add_time[new_proof_of_space.sp_hash] = uint64(time.time())

        max_pos_per_sp = 5

        if self.farmer.config.get("selected_network") != "mainnet":
            # This is meant to make testnets more stable, when difficulty is very low
            if self.farmer.number_of_responses[new_proof_of_space.sp_hash] > max_pos_per_sp:
                self.farmer.log.info(
                    f"Surpassed {max_pos_per_sp} PoSpace for one SP, no longer submitting PoSpace for signage point "
                    f"{new_proof_of_space.sp_hash}"
                )
                return None

        if new_proof_of_space.sp_hash not in self.farmer.sps:
            self.farmer.log.warning(
                f"Received response for a signage point that we do not have {new_proof_of_space.sp_hash}"
            )
            return None

        sps = self.farmer.sps[new_proof_of_space.sp_hash]
        for sp in sps:
            computed_quality_string = verify_and_get_quality_string(
                new_proof_of_space.proof,
                self.farmer.constants,
                new_proof_of_space.challenge_hash,
                new_proof_of_space.sp_hash,
                height=sp.peak_height,
            )
            if computed_quality_string is None:
                plotid: bytes32 = get_plot_id(new_proof_of_space.proof)
                self.farmer.log.error(f"Invalid proof of space: {plotid.hex()} proof: {new_proof_of_space.proof}")
                return None

            self.farmer.number_of_responses[new_proof_of_space.sp_hash] += 1

            required_iters: uint64 = calculate_iterations_quality(
                self.farmer.constants,
                computed_quality_string,
                new_proof_of_space.proof.size(),
                sp.difficulty,
                new_proof_of_space.sp_hash,
                sp.sub_slot_iters,
                sp.last_tx_height,
            )

            # If the iters are good enough to make a block, proceed with the block making flow
            if required_iters < calculate_sp_interval_iters(self.farmer.constants, sp.sub_slot_iters):
                if new_proof_of_space.farmer_reward_address_override is not None:
                    self.farmer.notify_farmer_reward_taken_by_harvester_as_fee(sp, new_proof_of_space)

                sp_src_data: Optional[list[Optional[SignatureRequestSourceData]]] = None
                if (
                    new_proof_of_space.include_source_signature_data
                    or new_proof_of_space.farmer_reward_address_override is not None
                ):
                    assert sp.sp_source_data

                    cc_data: SignatureRequestSourceData
                    rc_data: SignatureRequestSourceData
                    if sp.sp_source_data.vdf_data is not None:
                        cc_data = SignatureRequestSourceData(
                            uint8(SigningDataKind.CHALLENGE_CHAIN_VDF), bytes(sp.sp_source_data.vdf_data.cc_vdf)
                        )
                        rc_data = SignatureRequestSourceData(
                            uint8(SigningDataKind.REWARD_CHAIN_VDF), bytes(sp.sp_source_data.vdf_data.rc_vdf)
                        )
                    else:
                        assert sp.sp_source_data.sub_slot_data is not None
                        cc_data = SignatureRequestSourceData(
                            uint8(SigningDataKind.CHALLENGE_CHAIN_SUB_SLOT),
                            bytes(sp.sp_source_data.sub_slot_data.cc_sub_slot),
                        )
                        rc_data = SignatureRequestSourceData(
                            uint8(SigningDataKind.REWARD_CHAIN_SUB_SLOT),
                            bytes(sp.sp_source_data.sub_slot_data.rc_sub_slot),
                        )

                    sp_src_data = [cc_data, rc_data]

                # Proceed at getting the signatures for this PoSpace
                request = harvester_protocol.RequestSignatures(
                    new_proof_of_space.plot_identifier,
                    new_proof_of_space.challenge_hash,
                    new_proof_of_space.sp_hash,
                    [sp.challenge_chain_sp, sp.reward_chain_sp],
                    message_data=sp_src_data,
                    rc_block_unfinished=None,
                )

                if new_proof_of_space.sp_hash not in self.farmer.proofs_of_space:
                    self.farmer.proofs_of_space[new_proof_of_space.sp_hash] = []
                self.farmer.proofs_of_space[new_proof_of_space.sp_hash].append(
                    (
                        new_proof_of_space.plot_identifier,
                        new_proof_of_space.proof,
                    )
                )
                self.farmer.cache_add_time[new_proof_of_space.sp_hash] = uint64(time.time())
                self.farmer.quality_str_to_identifiers[computed_quality_string] = (
                    new_proof_of_space.plot_identifier,
                    new_proof_of_space.challenge_hash,
                    new_proof_of_space.sp_hash,
                    peer.peer_node_id,
                )
                self.farmer.cache_add_time[computed_quality_string] = uint64(time.time())

                await peer.send_message(make_msg(ProtocolMessageTypes.request_signatures, request))

            p2_singleton_puzzle_hash = new_proof_of_space.proof.pool_contract_puzzle_hash
            if p2_singleton_puzzle_hash is not None:
                # Otherwise, send the proof of space to the pool
                # When we win a block, we also send the partial to the pool
                if p2_singleton_puzzle_hash not in self.farmer.pool_state:
                    self.farmer.log.info(f"Did not find pool info for {p2_singleton_puzzle_hash}")
                    return
                pool_state_dict: dict[str, Any] = self.farmer.pool_state[p2_singleton_puzzle_hash]
                pool_url = pool_state_dict["pool_config"].pool_url
                if pool_url == "":
                    # `pool_url == ""` means solo plotNFT farming
                    increment_pool_stats(
                        self.farmer.pool_state,
                        p2_singleton_puzzle_hash,
                        "valid_partials",
                        time.time(),
                    )
                    return

                if pool_state_dict["current_difficulty"] is None:
                    self.farmer.log.warning(
                        f"No pool specific difficulty has been set for {p2_singleton_puzzle_hash}, "
                        f"check communication with the pool, skipping this partial to {pool_url}."
                    )
                    increment_pool_stats(
                        self.farmer.pool_state,
                        p2_singleton_puzzle_hash,
                        "missing_partials",
                        time.time(),
                    )
                    self.farmer.state_changed(
                        "failed_partial",
                        {"p2_singleton_puzzle_hash": p2_singleton_puzzle_hash.hex()},
                    )
                    return

                required_iters = calculate_iterations_quality(
                    self.farmer.constants,
                    computed_quality_string,
                    new_proof_of_space.proof.size(),
                    pool_state_dict["current_difficulty"],
                    new_proof_of_space.sp_hash,
                    sp.sub_slot_iters,
                    sp.last_tx_height,
                )
                if required_iters >= calculate_sp_interval_iters(
                    self.farmer.constants, self.farmer.constants.POOL_SUB_SLOT_ITERS
                ):
                    self.farmer.log.info(
                        f"Proof of space not good enough for pool {pool_url}: {pool_state_dict['current_difficulty']}"
                    )
                    increment_pool_stats(
                        self.farmer.pool_state,
                        p2_singleton_puzzle_hash,
                        "insufficient_partials",
                        time.time(),
                    )
                    self.farmer.state_changed(
                        "failed_partial",
                        {"p2_singleton_puzzle_hash": p2_singleton_puzzle_hash.hex()},
                    )
                    return

                authentication_token_timeout = pool_state_dict["authentication_token_timeout"]
                if authentication_token_timeout is None:
                    self.farmer.log.warning(
                        f"No pool specific authentication_token_timeout has been set for {p2_singleton_puzzle_hash}"
                        f", check communication with the pool."
                    )
                    increment_pool_stats(
                        self.farmer.pool_state,
                        p2_singleton_puzzle_hash,
                        "missing_partials",
                        time.time(),
                    )
                    self.farmer.state_changed(
                        "failed_partial",
                        {"p2_singleton_puzzle_hash": p2_singleton_puzzle_hash.hex()},
                    )
                    return

                # Submit partial to pool
                is_eos = new_proof_of_space.signage_point_index == 0

                payload = PostPartialPayload(
                    pool_state_dict["pool_config"].launcher_id,
                    get_current_authentication_token(authentication_token_timeout),
                    new_proof_of_space.proof,
                    new_proof_of_space.sp_hash,
                    is_eos,
                    peer.peer_node_id,
                )

                # The plot key is 2/2 so we need the harvester's half of the signature
                m_to_sign = payload.get_hash()
                m_src_data: Optional[list[Optional[SignatureRequestSourceData]]] = None

                if (  # pragma: no cover
                    new_proof_of_space.include_source_signature_data
                    or new_proof_of_space.farmer_reward_address_override is not None
                ):
                    m_src_data = [SignatureRequestSourceData(uint8(SigningDataKind.PARTIAL), bytes(payload))]

                request = harvester_protocol.RequestSignatures(
                    new_proof_of_space.plot_identifier,
                    new_proof_of_space.challenge_hash,
                    new_proof_of_space.sp_hash,
                    [m_to_sign],
                    message_data=m_src_data,
                    rc_block_unfinished=None,
                )
                response: Any = await peer.call_api(HarvesterAPI.request_signatures, request)
                if not isinstance(response, harvester_protocol.RespondSignatures):
                    self.farmer.log.error(f"Invalid response from harvester: {response}")
                    increment_pool_stats(
                        self.farmer.pool_state,
                        p2_singleton_puzzle_hash,
                        "invalid_partials",
                        time.time(),
                    )
                    self.farmer.state_changed(
                        "failed_partial",
                        {"p2_singleton_puzzle_hash": p2_singleton_puzzle_hash.hex()},
                    )
                    return

                assert len(response.message_signatures) == 1

                plot_signature: Optional[G2Element] = None
                for sk in self.farmer.get_private_keys():
                    pk = sk.get_g1()
                    if pk == response.farmer_pk:
                        agg_pk = generate_plot_public_key(response.local_pk, pk, True)
                        assert agg_pk == new_proof_of_space.proof.plot_public_key
                        sig_farmer = AugSchemeMPL.sign(sk, m_to_sign, agg_pk)
                        taproot_sk: PrivateKey = generate_taproot_sk(response.local_pk, pk)
                        taproot_sig: G2Element = AugSchemeMPL.sign(taproot_sk, m_to_sign, agg_pk)

                        plot_signature = AugSchemeMPL.aggregate(
                            [sig_farmer, response.message_signatures[0][1], taproot_sig]
                        )
                        assert AugSchemeMPL.verify(agg_pk, m_to_sign, plot_signature)

                authentication_sk: Optional[PrivateKey] = self.farmer.get_authentication_sk(
                    pool_state_dict["pool_config"]
                )
                if authentication_sk is None:
                    self.farmer.log.error(f"No authentication sk for {p2_singleton_puzzle_hash}")
                    increment_pool_stats(
                        self.farmer.pool_state,
                        p2_singleton_puzzle_hash,
                        "missing_partials",
                        time.time(),
                    )
                    self.farmer.state_changed(
                        "failed_partial",
                        {"p2_singleton_puzzle_hash": p2_singleton_puzzle_hash.hex()},
                    )
                    return

                authentication_signature = AugSchemeMPL.sign(authentication_sk, m_to_sign)

                assert plot_signature is not None

                agg_sig: G2Element = AugSchemeMPL.aggregate([plot_signature, authentication_signature])

                post_partial_request: PostPartialRequest = PostPartialRequest(payload, agg_sig)
                self.farmer.log.info(
                    f"Submitting partial for {post_partial_request.payload.launcher_id.hex()} to {pool_url}"
                )
                increment_pool_stats(
                    self.farmer.pool_state,
                    p2_singleton_puzzle_hash,
                    "points_found",
                    time.time(),
                    count=pool_state_dict["current_difficulty"],
                    value=pool_state_dict["current_difficulty"],
                )
                self.farmer.log.debug(f"POST /partial request {post_partial_request}")
                try:
                    async with aiohttp.ClientSession() as session:
                        async with session.post(
                            f"{pool_url}/partial",
                            json=post_partial_request.to_json_dict(),
                            ssl=ssl_context_for_root(get_mozilla_ca_crt(), log=self.farmer.log),
                            headers={
                                "User-Agent": f"Chia Blockchain v.{__version__}",
                                "chia-farmer-version": __version__,
                                "chia-harvester-version": peer.version,
                            },
                        ) as resp:
                            if not resp.ok:
                                self.farmer.log.error(f"Error sending partial to {pool_url}, {resp.status}")
                                increment_pool_stats(
                                    self.farmer.pool_state,
                                    p2_singleton_puzzle_hash,
                                    "invalid_partials",
                                    time.time(),
                                )
                                return

                            pool_response: dict[str, Any] = json.loads(await resp.text())
                            self.farmer.log.info(f"Pool response: {pool_response}")
                            if "error_code" in pool_response:
                                self.farmer.log.error(
                                    f"Error in pooling: {pool_response['error_code'], pool_response['error_message']}"
                                )

                                increment_pool_stats(
                                    self.farmer.pool_state,
                                    p2_singleton_puzzle_hash,
                                    "pool_errors",
                                    time.time(),
                                    value=pool_response,
                                )

                                if pool_response["error_code"] == PoolErrorCode.TOO_LATE.value:
                                    increment_pool_stats(
                                        self.farmer.pool_state,
                                        p2_singleton_puzzle_hash,
                                        "stale_partials",
                                        time.time(),
                                    )
                                elif pool_response["error_code"] == PoolErrorCode.PROOF_NOT_GOOD_ENOUGH.value:
                                    self.farmer.log.error(
                                        "Partial not good enough, forcing pool farmer update to "
                                        "get our current difficulty."
                                    )
                                    increment_pool_stats(
                                        self.farmer.pool_state,
                                        p2_singleton_puzzle_hash,
                                        "insufficient_partials",
                                        time.time(),
                                    )
                                    pool_state_dict["next_farmer_update"] = 0
                                    await self.farmer.update_pool_state()
                                else:
                                    increment_pool_stats(
                                        self.farmer.pool_state,
                                        p2_singleton_puzzle_hash,
                                        "invalid_partials",
                                        time.time(),
                                    )
                                return

                            increment_pool_stats(
                                self.farmer.pool_state,
                                p2_singleton_puzzle_hash,
                                "valid_partials",
                                time.time(),
                            )
                            new_difficulty = pool_response["new_difficulty"]
                            increment_pool_stats(
                                self.farmer.pool_state,
                                p2_singleton_puzzle_hash,
                                "points_acknowledged",
                                time.time(),
                                new_difficulty,
                                new_difficulty,
                            )
                            pool_state_dict["current_difficulty"] = new_difficulty
                except Exception as e:
                    self.farmer.log.error(f"Error connecting to pool: {e}")

                    error_resp = {"error_code": uint16(PoolErrorCode.REQUEST_FAILED.value), "error_message": str(e)}
                    increment_pool_stats(
                        self.farmer.pool_state,
                        p2_singleton_puzzle_hash,
                        "pool_errors",
                        time.time(),
                        value=error_resp,
                    )
                    increment_pool_stats(
                        self.farmer.pool_state,
                        p2_singleton_puzzle_hash,
                        "invalid_partials",
                        time.time(),
                    )
                    self.farmer.state_changed(
                        "failed_partial",
                        {"p2_singleton_puzzle_hash": p2_singleton_puzzle_hash.hex()},
                    )
                    return

                self.farmer.state_changed(
                    "submitted_partial",
                    {
                        "launcher_id": post_partial_request.payload.launcher_id.hex(),
                        "pool_url": pool_url,
                        "current_difficulty": pool_state_dict["current_difficulty"],
                        "points_acknowledged_since_start": pool_state_dict["points_acknowledged_since_start"],
                        "points_acknowledged_24h": pool_state_dict["points_acknowledged_24h"],
                    },
                )

                return

    @metadata.request()
    async def respond_signatures(self, response: harvester_protocol.RespondSignatures) -> None:
        request = self._process_respond_signatures(response)
        if request is None:
            return None

        message: Message | None = None
        if isinstance(request, DeclareProofOfSpace):
            self.farmer.state_changed("proof", {"proof": request, "passed_filter": True})
            message = make_msg(ProtocolMessageTypes.declare_proof_of_space, request)
        if isinstance(request, SignedValues):
            message = make_msg(ProtocolMessageTypes.signed_values, request)
        await self.farmer.server.send_to_all([message], NodeType.FULL_NODE)

    """
    FARMER PROTOCOL (FARMER <-> FULL NODE)
    """

    @metadata.request()
    async def new_signage_point(self, new_signage_point: farmer_protocol.NewSignagePoint) -> None:
        if new_signage_point.challenge_chain_sp not in self.farmer.sps:
            self.farmer.sps[new_signage_point.challenge_chain_sp] = []
        if new_signage_point in self.farmer.sps[new_signage_point.challenge_chain_sp]:
            self.farmer.log.debug(f"Duplicate signage point {new_signage_point.signage_point_index}")
            return

        # Mark this SP as known, so we do not process it multiple times
        self.farmer.sps[new_signage_point.challenge_chain_sp].append(new_signage_point)

        try:
            pool_difficulties: list[PoolDifficulty] = []
            for p2_singleton_puzzle_hash, pool_dict in self.farmer.pool_state.items():
                if pool_dict["pool_config"].pool_url == "":
                    # Self pooling
                    continue

                if pool_dict["current_difficulty"] is None:
                    self.farmer.log.warning(
                        f"No pool specific difficulty has been set for {p2_singleton_puzzle_hash}, "
                        f"check communication with the pool, skipping this signage point, pool: "
                        f"{pool_dict['pool_config'].pool_url} "
                    )
                    continue
                pool_difficulties.append(
                    PoolDifficulty(
                        pool_dict["current_difficulty"],
                        self.farmer.constants.POOL_SUB_SLOT_ITERS,
                        p2_singleton_puzzle_hash,
                    )
                )
            message = harvester_protocol.NewSignagePointHarvester(
                new_signage_point.challenge_hash,
                new_signage_point.difficulty,
                new_signage_point.sub_slot_iters,
                new_signage_point.signage_point_index,
                new_signage_point.challenge_chain_sp,
                pool_difficulties,
                new_signage_point.peak_height,
                new_signage_point.last_tx_height,
            )

            msg = make_msg(ProtocolMessageTypes.new_signage_point_harvester, message)
            await self.farmer.server.send_to_all([msg], NodeType.HARVESTER)
        except Exception as exception:
            # Remove here, as we want to reprocess the SP should it be sent again
            self.farmer.sps[new_signage_point.challenge_chain_sp].remove(new_signage_point)

            raise exception
        finally:
            # Age out old 24h information for every signage point regardless
            # of any failures.  Note that this still lets old data remain if
            # the client isn't receiving signage points.
            cutoff_24h = time.time() - (24 * 60 * 60)
            for p2_singleton_puzzle_hash, pool_dict in self.farmer.pool_state.items():
                for key in ["points_found_24h", "points_acknowledged_24h"]:
                    if key not in pool_dict:
                        continue

                    pool_dict[key] = strip_old_entries(pairs=pool_dict[key], before=cutoff_24h)

        now = uint64(time.time())
        self.farmer.cache_add_time[new_signage_point.challenge_chain_sp] = now
        missing_signage_points = self.farmer.check_missing_signage_points(now, new_signage_point)
        self.farmer.state_changed(
            "new_signage_point",
            {"sp_hash": new_signage_point.challenge_chain_sp, "missing_signage_points": missing_signage_points},
        )

    @metadata.request()
    async def request_signed_values(self, full_node_request: farmer_protocol.RequestSignedValues) -> Optional[Message]:
        if full_node_request.quality_string not in self.farmer.quality_str_to_identifiers:
            self.farmer.log.error(f"Do not have quality string {full_node_request.quality_string}")
            return None

        (plot_identifier, challenge_hash, sp_hash, node_id) = self.farmer.quality_str_to_identifiers[
            full_node_request.quality_string
        ]

        message_data: Optional[list[Optional[SignatureRequestSourceData]]] = None

        if full_node_request.foliage_block_data is not None:
            message_data = [
                SignatureRequestSourceData(
                    uint8(SigningDataKind.FOLIAGE_BLOCK_DATA), bytes(full_node_request.foliage_block_data)
                ),
                (
                    None
                    if full_node_request.foliage_transaction_block_data is None
                    else SignatureRequestSourceData(
                        uint8(SigningDataKind.FOLIAGE_TRANSACTION_BLOCK),
                        bytes(full_node_request.foliage_transaction_block_data),
                    )
                ),
            ]

        request = harvester_protocol.RequestSignatures(
            plot_identifier,
            challenge_hash,
            sp_hash,
            [full_node_request.foliage_block_data_hash, full_node_request.foliage_transaction_block_hash],
            message_data=message_data,
            rc_block_unfinished=full_node_request.rc_block_unfinished,
        )

        response = await self.farmer.server.call_api_of_specific(HarvesterAPI.request_signatures, request, node_id)
        if response is None or not isinstance(response, harvester_protocol.RespondSignatures):
            self.farmer.log.error(f"Invalid response from harvester {node_id} for request_signatures: {response}")
            return None

        # Use the same processing as for unsolicited respond signature requests
        signed_values = self._process_respond_signatures(response)
        if signed_values is None:
            return None
        assert isinstance(signed_values, SignedValues)

        return make_msg(ProtocolMessageTypes.signed_values, signed_values)

    @metadata.request(peer_required=True)
    async def farming_info(self, request: farmer_protocol.FarmingInfo, peer: WSChiaConnection) -> None:
        self.farmer.state_changed(
            "new_farming_info",
            {
                "farming_info": {
                    "challenge_hash": request.challenge_hash,
                    "signage_point": request.sp_hash,
                    "passed_filter": request.passed,
                    "proofs": request.proofs,
                    "total_plots": request.total_plots,
                    "timestamp": request.timestamp,
                    "node_id": peer.peer_node_id,
                    "lookup_time": request.lookup_time,
                }
            },
        )

    @metadata.request(peer_required=True)
    async def respond_plots(self, _: harvester_protocol.RespondPlots, peer: WSChiaConnection) -> None:
        self.farmer.log.warning(f"Respond plots came too late from: {peer.get_peer_logging()}")

    @metadata.request(peer_required=True)
    async def plot_sync_start(self, message: PlotSyncStart, peer: WSChiaConnection) -> None:
        await self.farmer.plot_sync_receivers[peer.peer_node_id].sync_started(message)

    @metadata.request(peer_required=True)
    async def plot_sync_loaded(self, message: PlotSyncPlotList, peer: WSChiaConnection) -> None:
        await self.farmer.plot_sync_receivers[peer.peer_node_id].process_loaded(message)

    @metadata.request(peer_required=True)
    async def plot_sync_removed(self, message: PlotSyncPathList, peer: WSChiaConnection) -> None:
        await self.farmer.plot_sync_receivers[peer.peer_node_id].process_removed(message)

    @metadata.request(peer_required=True)
    async def plot_sync_invalid(self, message: PlotSyncPathList, peer: WSChiaConnection) -> None:
        await self.farmer.plot_sync_receivers[peer.peer_node_id].process_invalid(message)

    @metadata.request(peer_required=True)
    async def plot_sync_keys_missing(self, message: PlotSyncPathList, peer: WSChiaConnection) -> None:
        await self.farmer.plot_sync_receivers[peer.peer_node_id].process_keys_missing(message)

    @metadata.request(peer_required=True)
    async def plot_sync_duplicates(self, message: PlotSyncPathList, peer: WSChiaConnection) -> None:
        await self.farmer.plot_sync_receivers[peer.peer_node_id].process_duplicates(message)

    @metadata.request(peer_required=True)
    async def plot_sync_done(self, message: PlotSyncDone, peer: WSChiaConnection) -> None:
        await self.farmer.plot_sync_receivers[peer.peer_node_id].sync_done(message)

    def _process_respond_signatures(
        self, response: harvester_protocol.RespondSignatures
    ) -> Optional[Union[DeclareProofOfSpace, SignedValues]]:
        """
        Processing the responded signatures happens when receiving an unsolicited request for an SP or when receiving
        the signature response for a block from a harvester.
        """
        if response.sp_hash not in self.farmer.sps:
            self.farmer.log.warning(f"Do not have challenge hash {response.challenge_hash}")
            return None
        is_sp_signatures: bool = False
        sps = self.farmer.sps[response.sp_hash]
        peak_height = sps[0].peak_height
        signage_point_index = sps[0].signage_point_index
        found_sp_hash_debug = False
        for sp_candidate in sps:
            if response.sp_hash == response.message_signatures[0][0]:
                found_sp_hash_debug = True
                if sp_candidate.reward_chain_sp == response.message_signatures[1][0]:
                    is_sp_signatures = True
        if found_sp_hash_debug:
            assert is_sp_signatures

        pospace = None
        for plot_identifier, candidate_pospace in self.farmer.proofs_of_space[response.sp_hash]:
            if plot_identifier == response.plot_identifier:
                pospace = candidate_pospace
        assert pospace is not None
        include_taproot: bool = pospace.pool_contract_puzzle_hash is not None

        computed_quality_string = verify_and_get_quality_string(
            pospace, self.farmer.constants, response.challenge_hash, response.sp_hash, height=peak_height
        )
        if computed_quality_string is None:
            self.farmer.log.warning(f"Have invalid PoSpace {pospace}")
            return None

        if is_sp_signatures:
            (
                challenge_chain_sp,
                challenge_chain_sp_harv_sig,
            ) = response.message_signatures[0]
            reward_chain_sp, reward_chain_sp_harv_sig = response.message_signatures[1]
            for sk in self.farmer.get_private_keys():
                pk = sk.get_g1()
                if pk == response.farmer_pk:
                    agg_pk = generate_plot_public_key(response.local_pk, pk, include_taproot)
                    assert agg_pk == pospace.plot_public_key
                    if include_taproot:
                        taproot_sk: PrivateKey = generate_taproot_sk(response.local_pk, pk)
                        taproot_share_cc_sp: G2Element = AugSchemeMPL.sign(taproot_sk, challenge_chain_sp, agg_pk)
                        taproot_share_rc_sp: G2Element = AugSchemeMPL.sign(taproot_sk, reward_chain_sp, agg_pk)
                    else:
                        taproot_share_cc_sp = G2Element()
                        taproot_share_rc_sp = G2Element()
                    farmer_share_cc_sp = AugSchemeMPL.sign(sk, challenge_chain_sp, agg_pk)
                    agg_sig_cc_sp = AugSchemeMPL.aggregate(
                        [challenge_chain_sp_harv_sig, farmer_share_cc_sp, taproot_share_cc_sp]
                    )
                    assert AugSchemeMPL.verify(agg_pk, challenge_chain_sp, agg_sig_cc_sp)

                    # This means it passes the sp filter
                    farmer_share_rc_sp = AugSchemeMPL.sign(sk, reward_chain_sp, agg_pk)
                    agg_sig_rc_sp = AugSchemeMPL.aggregate(
                        [reward_chain_sp_harv_sig, farmer_share_rc_sp, taproot_share_rc_sp]
                    )
                    assert AugSchemeMPL.verify(agg_pk, reward_chain_sp, agg_sig_rc_sp)

                    if pospace.pool_public_key is not None:
                        assert pospace.pool_contract_puzzle_hash is None
                        pool_pk = bytes(pospace.pool_public_key)
                        if pool_pk not in self.farmer.pool_sks_map:
                            self.farmer.log.error(
                                f"Don't have the private key for the pool key used by harvester: {pool_pk.hex()}"
                            )
                            return None

                        pool_target: Optional[PoolTarget] = PoolTarget(self.farmer.pool_target, uint32(0))
                        assert pool_target is not None
                        pool_target_signature: Optional[G2Element] = AugSchemeMPL.sign(
                            self.farmer.pool_sks_map[pool_pk], bytes(pool_target)
                        )
                    else:
                        assert pospace.pool_contract_puzzle_hash is not None
                        pool_target = None
                        pool_target_signature = None

                    include_source_signature_data = response.include_source_signature_data

                    farmer_reward_address = self.farmer.farmer_target
                    if response.farmer_reward_address_override is not None:
                        farmer_reward_address = response.farmer_reward_address_override
                        include_source_signature_data = True

                    return farmer_protocol.DeclareProofOfSpace(
                        response.challenge_hash,
                        challenge_chain_sp,
                        signage_point_index,
                        reward_chain_sp,
                        pospace,
                        agg_sig_cc_sp,
                        agg_sig_rc_sp,
                        farmer_reward_address,
                        pool_target,
                        pool_target_signature,
                        include_signature_source_data=include_source_signature_data,
                    )
        else:
            # This is a response with block signatures
            for sk in self.farmer.get_private_keys():
                (
                    foliage_block_data_hash,
                    foliage_sig_harvester,
                ) = response.message_signatures[0]
                (
                    foliage_transaction_block_hash,
                    foliage_transaction_block_sig_harvester,
                ) = response.message_signatures[1]
                pk = sk.get_g1()
                if pk == response.farmer_pk:
                    agg_pk = generate_plot_public_key(response.local_pk, pk, include_taproot)
                    assert agg_pk == pospace.plot_public_key
                    if include_taproot:
                        taproot_sk = generate_taproot_sk(response.local_pk, pk)
                        foliage_sig_taproot: G2Element = AugSchemeMPL.sign(taproot_sk, foliage_block_data_hash, agg_pk)
                        foliage_transaction_block_sig_taproot: G2Element = AugSchemeMPL.sign(
                            taproot_sk, foliage_transaction_block_hash, agg_pk
                        )
                    else:
                        foliage_sig_taproot = G2Element()
                        foliage_transaction_block_sig_taproot = G2Element()

                    foliage_sig_farmer = AugSchemeMPL.sign(sk, foliage_block_data_hash, agg_pk)
                    foliage_transaction_block_sig_farmer = AugSchemeMPL.sign(sk, foliage_transaction_block_hash, agg_pk)

                    foliage_agg_sig = AugSchemeMPL.aggregate(
                        [foliage_sig_harvester, foliage_sig_farmer, foliage_sig_taproot]
                    )
                    foliage_block_agg_sig = AugSchemeMPL.aggregate(
                        [
                            foliage_transaction_block_sig_harvester,
                            foliage_transaction_block_sig_farmer,
                            foliage_transaction_block_sig_taproot,
                        ]
                    )
                    assert AugSchemeMPL.verify(agg_pk, foliage_block_data_hash, foliage_agg_sig)
                    assert AugSchemeMPL.verify(agg_pk, foliage_transaction_block_hash, foliage_block_agg_sig)

                    return farmer_protocol.SignedValues(
                        computed_quality_string,
                        foliage_agg_sig,
                        foliage_block_agg_sig,
                    )

        return None
