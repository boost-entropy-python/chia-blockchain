from __future__ import annotations

from typing import Optional

import click

from chia.cmds.cmd_classes import ChiaCliContext


@click.group("farm", help="Manage your farm")
def farm_cmd() -> None:
    pass


@farm_cmd.command("summary", help="Summary of farming information")
@click.option(
    "-p",
    "--rpc-port",
    help=(
        "Set the port where the Full Node is hosting the RPC interface. See the rpc_port under full_node in config.yaml"
    ),
    type=int,
    default=None,
    show_default=True,
)
@click.option(
    "-wp",
    "--wallet-rpc-port",
    help="Set the port where the Wallet is hosting the RPC interface. See the rpc_port under wallet in config.yaml",
    type=int,
    default=None,
    show_default=True,
)
@click.option(
    "-hp",
    "--harvester-rpc-port",
    help=(
        "Set the port where the Harvester is hosting the RPC interface. See the rpc_port under harvester in config.yaml"
    ),
    type=int,
    default=None,
    show_default=True,
)
@click.option(
    "-fp",
    "--farmer-rpc-port",
    help=("Set the port where the Farmer is hosting the RPC interface. See the rpc_port under farmer in config.yaml"),
    type=int,
    default=None,
    show_default=True,
)
@click.option(
    "-i",
    "--include-pool-rewards",
    help="Include pool farming rewards in the total farmed amount",
    is_flag=True,
    default=False,
)
@click.pass_context
def summary_cmd(
    ctx: click.Context,
    rpc_port: Optional[int],
    wallet_rpc_port: Optional[int],
    harvester_rpc_port: Optional[int],
    farmer_rpc_port: Optional[int],
    include_pool_rewards: bool,
) -> None:
    import asyncio

    from chia.cmds.farm_funcs import summary

    asyncio.run(
        summary(
            rpc_port,
            wallet_rpc_port,
            harvester_rpc_port,
            farmer_rpc_port,
            include_pool_rewards,
            root_path=ChiaCliContext.set_default(ctx).root_path,
        )
    )


@farm_cmd.command("challenges", help="Show the latest challenges")
@click.option(
    "-fp",
    "--farmer-rpc-port",
    help="Set the port where the Farmer is hosting the RPC interface. See the rpc_port under farmer in config.yaml",
    type=int,
    default=None,
    show_default=True,
)
@click.option(
    "-l",
    "--limit",
    help="Limit the number of challenges shown. Use 0 to disable the limit",
    type=click.IntRange(0),
    default=20,
    show_default=True,
)
@click.pass_context
def challenges_cmd(ctx: click.Context, farmer_rpc_port: Optional[int], limit: int) -> None:
    import asyncio

    from chia.cmds.farm_funcs import challenges

    asyncio.run(challenges(ChiaCliContext.set_default(ctx).root_path, farmer_rpc_port, limit))
