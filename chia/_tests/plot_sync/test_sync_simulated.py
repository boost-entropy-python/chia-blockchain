from __future__ import annotations

import asyncio
import contextlib
import functools
import logging
import random
import time
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any, Optional

import pytest
from chia_rs import G1Element
from chia_rs.sized_bytes import bytes32
from chia_rs.sized_ints import int16, uint8, uint64

from chia._tests.plot_sync.util import start_harvester_service
from chia._tests.util.time_out_assert import time_out_assert
from chia.farmer.farmer import Farmer
from chia.harvester.harvester import Harvester
from chia.plot_sync.receiver import Receiver
from chia.plot_sync.sender import Sender
from chia.plot_sync.util import Constants
from chia.plotting.manager import PlotManager
from chia.plotting.prover import V1Prover
from chia.plotting.util import PlotInfo
from chia.protocols.harvester_protocol import PlotSyncError, PlotSyncResponse
from chia.protocols.outbound_message import make_msg
from chia.protocols.protocol_message_types import ProtocolMessageTypes
from chia.server.aliases import FarmerService, HarvesterService
from chia.server.ws_connection import WSChiaConnection
from chia.simulator.block_tools import BlockTools
from chia.util.batches import to_batches

log = logging.getLogger(__name__)


class ErrorSimulation(Enum):
    DropEveryFourthMessage = 1
    DropThreeMessages = 2
    RespondTooLateEveryFourthMessage = 3
    RespondTwice = 4
    NonRecoverableError = 5
    NotConnected = 6


@dataclass
class TestData:
    harvester: Harvester
    plot_sync_sender: Sender
    plot_sync_receiver: Receiver
    event_loop: asyncio.AbstractEventLoop
    plots: dict[Path, PlotInfo] = field(default_factory=dict)
    invalid: list[PlotInfo] = field(default_factory=list)
    keys_missing: list[PlotInfo] = field(default_factory=list)
    duplicates: list[PlotInfo] = field(default_factory=list)

    async def run(
        self,
        *,
        loaded: list[PlotInfo],
        removed: list[PlotInfo],
        invalid: list[PlotInfo],
        keys_missing: list[PlotInfo],
        duplicates: list[PlotInfo],
        initial: bool,
    ) -> None:
        for plot_info in loaded:
            assert Path(plot_info.prover.get_filename()) not in self.plots
        for plot_info in removed:
            assert Path(plot_info.prover.get_filename()) in self.plots

        self.invalid = invalid
        self.keys_missing = keys_missing
        self.duplicates = duplicates

        removed_paths: list[Path] = [Path(p.prover.get_filename()) for p in removed] if removed is not None else []
        invalid_dict: dict[Path, int] = {Path(p.prover.get_filename()): 0 for p in self.invalid}
        keys_missing_set: set[Path] = {Path(p.prover.get_filename()) for p in self.keys_missing}
        duplicates_set: set[str] = {p.prover.get_filename() for p in self.duplicates}

        # Inject invalid plots into `PlotManager` of the harvester so that the callback calls below can use them
        # to sync them to the farmer.
        self.harvester.plot_manager.failed_to_open_filenames = invalid_dict
        # Inject key missing plots into `PlotManager` of the harvester so that the callback calls below can use them
        # to sync them to the farmer.
        self.harvester.plot_manager.no_key_filenames = keys_missing_set
        # Inject duplicated plots into `PlotManager` of the harvester so that the callback calls below can use them
        # to sync them to the farmer.
        for plot_info in loaded:
            plot_path = Path(plot_info.prover.get_filename())
            self.harvester.plot_manager.plot_filename_paths[plot_path.name] = (str(plot_path.parent), set())
        for duplicate in duplicates_set:
            plot_path = Path(duplicate)
            assert plot_path.name in self.harvester.plot_manager.plot_filename_paths
            self.harvester.plot_manager.plot_filename_paths[plot_path.name][1].add(str(plot_path.parent))

        batch_size = self.harvester.plot_manager.refresh_parameter.batch_size

        # Used to capture the sync id in `run_internal`
        sync_id: Optional[uint64] = None

        def run_internal() -> None:
            nonlocal sync_id
            # Simulate one plot manager refresh cycle by calling the methods directly.
            self.harvester.plot_sync_sender.sync_start(len(loaded), initial)
            sync_id = self.plot_sync_sender._sync_id
            if len(loaded) == 0:
                self.harvester.plot_sync_sender.process_batch([], 0)
            for batch in to_batches(loaded, batch_size):
                self.harvester.plot_sync_sender.process_batch(batch.entries, batch.remaining)
            self.harvester.plot_sync_sender.sync_done(removed_paths, 0)

        await self.event_loop.run_in_executor(None, run_internal)

        async def sync_done() -> bool:
            assert sync_id is not None
            return self.plot_sync_receiver.last_sync().sync_id == self.plot_sync_sender._last_sync_id == sync_id

        await time_out_assert(60, sync_done)

        for plot_info in loaded:
            self.plots[Path(plot_info.prover.get_filename())] = plot_info
        for plot_info in removed:
            del self.plots[Path(plot_info.prover.get_filename())]

    def validate_plot_sync(self) -> None:
        assert len(self.plots) == len(self.plot_sync_receiver.plots())
        assert len(self.invalid) == len(self.plot_sync_receiver.invalid())
        assert len(self.keys_missing) == len(self.plot_sync_receiver.keys_missing())
        for _, plot_info in self.plots.items():
            assert plot_info.prover.get_filename() not in self.plot_sync_receiver.invalid()
            assert plot_info.prover.get_filename() not in self.plot_sync_receiver.keys_missing()
            assert plot_info.prover.get_filename() in self.plot_sync_receiver.plots()
            synced_plot = self.plot_sync_receiver.plots()[plot_info.prover.get_filename()]
            assert plot_info.prover.get_filename() == synced_plot.filename
            assert plot_info.pool_public_key == synced_plot.pool_public_key
            assert plot_info.pool_contract_puzzle_hash == synced_plot.pool_contract_puzzle_hash
            assert plot_info.plot_public_key == synced_plot.plot_public_key
            assert plot_info.file_size == synced_plot.file_size
            assert uint64(plot_info.time_modified) == synced_plot.time_modified
        for plot_info in self.invalid:
            assert plot_info.prover.get_filename() not in self.plot_sync_receiver.plots()
            assert plot_info.prover.get_filename() in self.plot_sync_receiver.invalid()
            assert plot_info.prover.get_filename() not in self.plot_sync_receiver.keys_missing()
            assert plot_info.prover.get_filename() not in self.plot_sync_receiver.duplicates()
        for plot_info in self.keys_missing:
            assert plot_info.prover.get_filename() not in self.plot_sync_receiver.plots()
            assert plot_info.prover.get_filename() not in self.plot_sync_receiver.invalid()
            assert plot_info.prover.get_filename() in self.plot_sync_receiver.keys_missing()
            assert plot_info.prover.get_filename() not in self.plot_sync_receiver.duplicates()
        for plot_info in self.duplicates:
            assert plot_info.prover.get_filename() not in self.plot_sync_receiver.invalid()
            assert plot_info.prover.get_filename() not in self.plot_sync_receiver.keys_missing()
            assert plot_info.prover.get_filename() in self.plot_sync_receiver.duplicates()


@dataclass
class TestRunner:
    test_data: list[TestData]

    def __init__(
        self, harvesters: list[Harvester], farmer: Farmer, event_loop: asyncio.events.AbstractEventLoop
    ) -> None:
        self.test_data = []
        for harvester in harvesters:
            assert harvester.server is not None
            self.test_data.append(
                TestData(
                    harvester,
                    harvester.plot_sync_sender,
                    farmer.plot_sync_receivers[harvester.server.node_id],
                    event_loop,
                )
            )

    async def run(
        self,
        index: int,
        *,
        loaded: list[PlotInfo],
        removed: list[PlotInfo],
        invalid: list[PlotInfo],
        keys_missing: list[PlotInfo],
        duplicates: list[PlotInfo],
        initial: bool,
    ) -> None:
        await self.test_data[index].run(
            loaded=loaded,
            removed=removed,
            invalid=invalid,
            keys_missing=keys_missing,
            duplicates=duplicates,
            initial=initial,
        )
        for data in self.test_data:
            data.validate_plot_sync()


async def skip_processing(self: Any, _: WSChiaConnection, message_type: ProtocolMessageTypes, message: Any) -> bool:
    self.message_counter += 1
    if self.simulate_error == ErrorSimulation.DropEveryFourthMessage:
        if self.message_counter % 4 == 0:
            return True
    if self.simulate_error == ErrorSimulation.DropThreeMessages:
        if 2 < self.message_counter < 6:
            return True
    if self.simulate_error == ErrorSimulation.RespondTooLateEveryFourthMessage:
        if self.message_counter % 4 == 0:
            await asyncio.sleep(Constants.message_timeout + 1)
            return False
    if self.simulate_error == ErrorSimulation.RespondTwice:
        await self.connection().send_message(
            make_msg(
                ProtocolMessageTypes.plot_sync_response,
                PlotSyncResponse(message.identifier, int16(message_type.value), None),
            )
        )
    if self.simulate_error == ErrorSimulation.NonRecoverableError and self.message_counter > 1:
        await self.connection().send_message(
            make_msg(
                ProtocolMessageTypes.plot_sync_response,
                PlotSyncResponse(
                    message.identifier, int16(message_type.value), PlotSyncError(int16(0), "non recoverable", None)
                ),
            )
        )
        self.simulate_error = 0
        return True
    return False


async def _testable_process(
    self: Any, peer: WSChiaConnection, message_type: ProtocolMessageTypes, message: Any
) -> None:
    if await skip_processing(self, peer, message_type, message):
        return
    await self.original_process(peer, message_type, message)


@contextlib.asynccontextmanager
async def create_test_runner(
    harvester_services: list[HarvesterService],
    farmer_service: FarmerService,
    event_loop: asyncio.events.AbstractEventLoop,
) -> AsyncIterator[TestRunner]:
    async with farmer_service.manage():
        farmer: Farmer = farmer_service._node
        assert len(farmer.plot_sync_receivers) == 0
        async with contextlib.AsyncExitStack() as async_exit_stack:
            split_harvester_managers = [
                await async_exit_stack.enter_async_context(start_harvester_service(service, farmer_service))
                for service in harvester_services
            ]
            harvesters = [manager.object for manager in split_harvester_managers]
            for receiver in farmer.plot_sync_receivers.values():
                receiver.simulate_error = 0  # type: ignore[attr-defined]
                receiver.message_counter = 0  # type: ignore[attr-defined]
                receiver.original_process = receiver._process  # type: ignore[attr-defined]
                receiver._process = functools.partial(_testable_process, receiver)  # type: ignore[method-assign]
            yield TestRunner(harvesters, farmer, event_loop)


def create_example_plots(count: int, seeded_random: random.Random) -> list[PlotInfo]:
    @dataclass
    class DiskProver:
        file_name: str
        plot_id: bytes32
        size: int

        def get_filename(self) -> str:
            return self.file_name

        def get_id(self) -> bytes32:
            return self.plot_id

        def get_size(self) -> int:
            return self.size

        def get_compression_level(self) -> uint8:
            return uint8(0)

    return [
        PlotInfo(
            prover=V1Prover(DiskProver(f"{x}", bytes32.random(seeded_random), 25 + x % 26)),
            pool_public_key=None,
            pool_contract_puzzle_hash=None,
            plot_public_key=G1Element(),
            file_size=uint64(0),
            time_modified=time.time(),
        )
        for x in range(count)
    ]


@pytest.mark.anyio
async def test_sync_simulated(
    farmer_three_harvester_not_started: tuple[list[HarvesterService], FarmerService, BlockTools],
    event_loop: asyncio.events.AbstractEventLoop,
    seeded_random: random.Random,
) -> None:
    harvester_services, farmer_service, _ = farmer_three_harvester_not_started
    farmer: Farmer = farmer_service._node
    async with create_test_runner(harvester_services, farmer_service, event_loop) as test_runner:
        plots = create_example_plots(31000, seeded_random=seeded_random)

        await test_runner.run(
            0, loaded=plots[0:10000], removed=[], invalid=[], keys_missing=[], duplicates=plots[0:1000], initial=True
        )
        await test_runner.run(
            1,
            loaded=plots[10000:20000],
            removed=[],
            invalid=plots[30000:30100],
            keys_missing=[],
            duplicates=[],
            initial=True,
        )
        await test_runner.run(
            2,
            loaded=plots[20000:30000],
            removed=[],
            invalid=[],
            keys_missing=plots[30100:30200],
            duplicates=[],
            initial=True,
        )
        await test_runner.run(
            0,
            loaded=[],
            removed=[],
            invalid=plots[30300:30400],
            keys_missing=plots[30400:30453],
            duplicates=[],
            initial=False,
        )
        await test_runner.run(0, loaded=[], removed=[], invalid=[], keys_missing=[], duplicates=[], initial=False)
        await test_runner.run(
            0, loaded=[], removed=plots[5000:10000], invalid=[], keys_missing=[], duplicates=[], initial=False
        )
        await test_runner.run(
            1, loaded=[], removed=plots[10000:20000], invalid=[], keys_missing=[], duplicates=[], initial=False
        )
        await test_runner.run(
            2, loaded=[], removed=plots[20000:29000], invalid=[], keys_missing=[], duplicates=[], initial=False
        )
        await test_runner.run(
            0, loaded=[], removed=plots[0:5000], invalid=[], keys_missing=[], duplicates=[], initial=False
        )
        await test_runner.run(
            2,
            loaded=plots[5000:10000],
            removed=plots[29000:30000],
            invalid=plots[30000:30500],
            keys_missing=plots[30500:31000],
            duplicates=plots[5000:6000],
            initial=False,
        )
        await test_runner.run(
            2, loaded=[], removed=plots[5000:10000], invalid=[], keys_missing=[], duplicates=[], initial=False
        )
        assert len(farmer.plot_sync_receivers) == 3
        for plot_sync in farmer.plot_sync_receivers.values():
            assert len(plot_sync.plots()) == 0


@pytest.mark.parametrize(
    "simulate_error",
    [
        ErrorSimulation.DropEveryFourthMessage,
        ErrorSimulation.DropThreeMessages,
        ErrorSimulation.RespondTooLateEveryFourthMessage,
        ErrorSimulation.RespondTwice,
    ],
)
@pytest.mark.anyio
async def test_farmer_error_simulation(
    farmer_one_harvester_not_started: tuple[list[HarvesterService], FarmerService, BlockTools],
    event_loop: asyncio.events.AbstractEventLoop,
    simulate_error: ErrorSimulation,
    seeded_random: random.Random,
) -> None:
    Constants.message_timeout = 5
    harvester_services, farmer_service, _ = farmer_one_harvester_not_started
    async with create_test_runner(harvester_services, farmer_service, event_loop) as test_runner:
        batch_size = test_runner.test_data[0].harvester.plot_manager.refresh_parameter.batch_size
        plots = create_example_plots(batch_size + 3, seeded_random=seeded_random)
        receiver = test_runner.test_data[0].plot_sync_receiver
        receiver.simulate_error = simulate_error  # type: ignore[attr-defined]
        await test_runner.run(
            0,
            loaded=plots[0 : batch_size + 1],
            removed=[],
            invalid=[plots[batch_size + 1]],
            keys_missing=[plots[batch_size + 2]],
            duplicates=[],
            initial=True,
        )


@pytest.mark.parametrize("simulate_error", [ErrorSimulation.NonRecoverableError, ErrorSimulation.NotConnected])
@pytest.mark.anyio
async def test_sync_reset_cases(
    farmer_one_harvester_not_started: tuple[list[HarvesterService], FarmerService, BlockTools],
    event_loop: asyncio.events.AbstractEventLoop,
    simulate_error: ErrorSimulation,
    seeded_random: random.Random,
) -> None:
    harvester_services, farmer_service, _ = farmer_one_harvester_not_started
    async with create_test_runner(harvester_services, farmer_service, event_loop) as test_runner:
        test_data: TestData = test_runner.test_data[0]
        plot_manager: PlotManager = test_data.harvester.plot_manager
        plots = create_example_plots(30, seeded_random=seeded_random)
        # Inject some data into `PlotManager` of the harvester so that we can validate the reset worked and triggered a
        # fresh sync of all available data of the plot manager
        for plot_info in plots[0:10]:
            test_data.plots[Path(plot_info.prover.get_filename())] = plot_info
            plot_manager.plots = test_data.plots
        test_data.invalid = plots[10:20]
        test_data.keys_missing = plots[20:30]
        test_data.plot_sync_receiver.simulate_error = simulate_error  # type: ignore[attr-defined]
        sender: Sender = test_runner.test_data[0].plot_sync_sender
        started_sync_id: uint64 = uint64(0)

        plot_manager.failed_to_open_filenames = {Path(p.prover.get_filename()): 0 for p in test_data.invalid}
        plot_manager.no_key_filenames = {Path(p.prover.get_filename()) for p in test_data.keys_missing}

        async def wait_for_reset() -> bool:
            assert started_sync_id != 0
            return sender._sync_id != started_sync_id != 0

        async def sync_done() -> bool:
            assert started_sync_id != 0
            return test_data.plot_sync_receiver.last_sync().sync_id == sender._last_sync_id == started_sync_id

        # Send start and capture the sync_id
        sender.sync_start(len(plots), True)
        started_sync_id = sender._sync_id
        # Sleep 2 seconds to make sure we have a different sync_id after the reset which gets triggered
        await asyncio.sleep(2)
        saved_connection = sender._connection
        if simulate_error == ErrorSimulation.NotConnected:
            sender._connection = None
        sender.process_batch(plots, 0)
        await time_out_assert(60, wait_for_reset)
        started_sync_id = sender._sync_id
        sender._connection = saved_connection
        await time_out_assert(60, sync_done)
        test_runner.test_data[0].validate_plot_sync()
