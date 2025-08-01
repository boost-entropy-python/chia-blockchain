from __future__ import annotations

import re

import pytest
from _pytest.capture import CaptureFixture

from chia._tests.util.time_out_assert import time_out_assert
from chia.cmds.farm_funcs import summary
from chia.farmer.farmer import Farmer
from chia.harvester.harvester import Harvester
from chia.server.aliases import FarmerService, HarvesterService, WalletService
from chia.simulator.block_tools import BlockTools
from chia.simulator.start_simulator import SimulatorFullNodeService


@pytest.mark.anyio
async def test_farm_summary_command(
    capsys: CaptureFixture[str],
    farmer_one_harvester_simulator_wallet: tuple[
        HarvesterService,
        FarmerService,
        SimulatorFullNodeService,
        WalletService,
        BlockTools,
    ],
) -> None:
    harvester_service, farmer_service, full_node_service, wallet_service, bt = farmer_one_harvester_simulator_wallet
    harvester: Harvester = harvester_service._node
    farmer: Farmer = farmer_service._node

    async def receiver_available() -> bool:
        return harvester.server.node_id in farmer.plot_sync_receivers

    # Wait for the receiver to show up
    await time_out_assert(20, receiver_available)
    receiver = farmer.plot_sync_receivers[harvester.server.node_id]
    # And wait until the first sync from the harvester to the farmer is done
    await time_out_assert(20, receiver.initial_sync, False)

    assert full_node_service.rpc_server and full_node_service.rpc_server.webserver
    assert wallet_service.rpc_server and wallet_service.rpc_server.webserver
    assert farmer_service.rpc_server and farmer_service.rpc_server.webserver

    full_node_rpc_port = full_node_service.rpc_server.webserver.listen_port
    wallet_rpc_port = wallet_service.rpc_server.webserver.listen_port
    farmer_rpc_port = farmer_service.rpc_server.webserver.listen_port

    # Test with include_pool_rewards=False (original test)
    await summary(
        rpc_port=full_node_rpc_port,
        wallet_rpc_port=wallet_rpc_port,
        harvester_rpc_port=None,
        farmer_rpc_port=farmer_rpc_port,
        include_pool_rewards=False,
        root_path=bt.root_path,
    )

    captured = capsys.readouterr()
    match = re.search(r"^.+(Farming status:.+)$", captured.out, re.DOTALL)
    assert match is not None
    lines = match.group(1).split("\n")

    assert lines[0] == "Farming status: Not synced or not connected to peers"
    assert "Total chia farmed:" in lines[1]
    assert "User transaction fees:" in lines[2]
    assert "Block rewards:" in lines[3]
    assert "Last height farmed:" in lines[4]
    assert lines[5] == "Local Harvester"
    assert "e (effective)" in lines[6]
    assert "Plot count for all harvesters:" in lines[7]
    assert "e (effective)" in lines[8]
    assert "Estimated network space:" in lines[9]
    assert "Expected time to win:" in lines[10]

    # Test with include_pool_rewards=True
    await summary(
        rpc_port=full_node_rpc_port,
        wallet_rpc_port=wallet_rpc_port,
        harvester_rpc_port=None,
        farmer_rpc_port=farmer_rpc_port,
        include_pool_rewards=True,
        root_path=bt.root_path,
    )

    captured = capsys.readouterr()
    match = re.search(r"Farming status:.*", captured.out, re.DOTALL)
    assert match, "no 'Farming status:' line"
    output = match.group(0).strip()
    lines = [line.strip() for line in output.splitlines()]

    # always check these first six lines
    assert lines[0].startswith("Farming status:")
    assert lines[1].startswith("Total chia farmed:")
    assert lines[2].startswith("User transaction fees:")
    assert lines[3].startswith("Farmer rewards:")
    assert lines[4].startswith("Pool rewards:")
    assert lines[5].startswith("Total rewards:")

    # decide where the harvester section starts
    if "Current/Last height farmed:" in output:
        # we saw the height-farmed block, so it occupies lines[6-8]
        assert lines[6].startswith("Current/Last height farmed:")
        assert lines[7].startswith("Blocks since last farmed:")
        assert lines[8].startswith("Time since last farmed:")
        harvester_idx = 9
    else:
        # no height block, so harvester begins at line 6
        harvester_idx = 6

    # now the harvester lines
    assert lines[harvester_idx] == "Local Harvester"
    assert "plots of size" in lines[harvester_idx + 1]
    assert lines[harvester_idx + 2].startswith("Plot count for all harvesters:")
    assert lines[harvester_idx + 3].startswith("Total size of plots:")
    assert lines[harvester_idx + 4].startswith("Estimated network space:")
    assert lines[harvester_idx + 5].startswith("Expected time to win:")
    assert lines[harvester_idx + 6].startswith("Note:")
