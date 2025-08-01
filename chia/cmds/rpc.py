from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path
from typing import Any, Optional, TextIO

import click
from aiohttp import ClientResponseError
from chia_rs.sized_ints import uint16

from chia.cmds.cmd_classes import ChiaCliContext
from chia.util.config import load_config

services: list[str] = ["crawler", "daemon", "farmer", "full_node", "harvester", "timelord", "wallet", "data_layer"]


async def call_endpoint(
    service: str,
    endpoint: str,
    request: dict[str, Any],
    config: dict[str, Any],
    root_path: Path,
    quiet: bool = False,
) -> dict[str, Any]:
    if service == "daemon":
        return await call_daemon_command(endpoint, request, config, root_path=root_path, quiet=quiet)

    return await call_rpc_service_endpoint(service, endpoint, request, config, root_path=root_path)


async def call_rpc_service_endpoint(
    service: str,
    endpoint: str,
    request: dict[str, Any],
    config: dict[str, Any],
    root_path: Path,
) -> dict[str, Any]:
    from chia.rpc.rpc_client import RpcClient

    port: uint16
    if service == "crawler":
        # crawler config is inside the seeder config
        port = uint16(config["seeder"][service]["rpc_port"])
    else:
        port = uint16(config[service]["rpc_port"])

    try:
        client = await RpcClient.create(config["self_hostname"], port, root_path, config)
    except Exception as e:
        raise Exception(f"Failed to create RPC client {service}: {e}")
    result: dict[str, Any]
    try:
        result = await client.fetch(endpoint, request)
    except ClientResponseError as e:
        if e.code == 404:
            raise Exception(f"Invalid endpoint for {service}: {endpoint}")
        raise
    except Exception as e:
        raise Exception(f"Request failed: {e}")
    finally:
        client.close()
        await client.await_closed()
    return result


async def call_daemon_command(
    command: str, request: dict[str, Any], config: dict[str, Any], root_path: Path, quiet: bool = False
) -> dict[str, Any]:
    from chia.daemon.client import connect_to_daemon_and_validate

    daemon = await connect_to_daemon_and_validate(root_path, config, quiet=quiet)

    if daemon is None:
        raise Exception("Failed to connect to chia daemon")

    result: dict[str, Any]
    try:
        ws_request = daemon.format_request(command, request)
        ws_response = await daemon._get(ws_request)
        result = ws_response["data"]
    except Exception as e:
        raise Exception(f"Request failed: {e}")
    finally:
        await daemon.close()
    return result


def print_result(json_dict: dict[str, Any]) -> None:
    print(json.dumps(json_dict, indent=2, sort_keys=True))


def get_routes(service: str, config: dict[str, Any], root_path: Path, quiet: bool = False) -> dict[str, Any]:
    return asyncio.run(call_endpoint(service, "get_routes", {}, config, root_path=root_path, quiet=quiet))


@click.group("rpc", help="RPC Client")
def rpc_cmd() -> None:
    pass


@rpc_cmd.command("endpoints", help="Print all endpoints of a service")
@click.argument("service", type=click.Choice(services))
@click.pass_context
def endpoints_cmd(ctx: click.Context, service: str) -> None:
    root_path = ChiaCliContext.set_default(ctx).root_path
    config = load_config(root_path, "config.yaml")
    try:
        routes = get_routes(service, config, root_path=root_path)
        for route in routes["routes"]:
            print(route.lstrip("/"))
    except Exception as e:
        print(e)


@rpc_cmd.command("status", help="Print the status of all available RPC services")
@click.option("--json-output", "json_output", is_flag=True, help="Output status as JSON")
@click.pass_context
def status_cmd(ctx: click.Context, json_output: bool) -> None:
    import json

    root_path = ChiaCliContext.set_default(ctx).root_path
    config = load_config(root_path, "config.yaml")

    def print_row(c0: str, c1: str) -> None:
        print(f"│ {c0:<12} │ {c1:<9} │")

    status_data = {}
    for service in services:
        status = "ACTIVE"
        try:
            if not get_routes(service, config, root_path=root_path, quiet=True)["success"]:
                raise Exception
        except Exception:
            status = "INACTIVE"
        status_data[service] = status

    if json_output:
        # If --json-output option is used, print the status data as JSON
        print(json.dumps(status_data, indent=2))
    else:
        print("╭──────────────┬───────────╮")
        print_row("SERVICE", "STATUS")
        print("├──────────────┼───────────┤")
        for service, status in status_data.items():
            print_row(service, status)
            if service != services[-1]:  # Don't print the separator after the last service
                print("├──────────────┼───────────┤")
        print("╰──────────────┴───────────╯")


def create_commands() -> None:
    for service in services:

        @rpc_cmd.command(
            service,
            short_help=f"RPC client for the {service} RPC API",
            help=(
                f"Call ENDPOINT (RPC endpoint as as string) of the {service} "
                "RPC API with REQUEST (must be a JSON string) as request data."
            ),
        )
        @click.argument("endpoint", type=str)
        @click.argument("request", type=str, required=False)
        @click.option(
            "-j",
            "--json-file",
            help="Optionally instead of REQUEST you can provide a json file containing the request data",
            type=click.File("r"),
            default=None,
        )
        @click.pass_context
        def rpc_client_cmd(
            ctx: click.Context,
            endpoint: str,
            request: Optional[str],
            json_file: Optional[TextIO],
            service: str = service,
        ) -> None:
            root_path: Path = ChiaCliContext.set_default(ctx).root_path
            config = load_config(root_path, "config.yaml")
            if request is not None and json_file is not None:
                sys.exit(
                    "Can only use one request source: REQUEST argument OR -j/--json-file option. See the help with -h"
                )

            request_json: dict[str, Any] = {}
            if json_file is not None:
                try:
                    request_json = json.load(json_file)
                except Exception as e:
                    sys.exit(f"Invalid JSON file: {e}")
            if request is not None:
                try:
                    request_json = json.loads(request)
                except Exception as e:
                    sys.exit(f"Invalid REQUEST JSON: {e}")

            try:
                if endpoint[0] == "/":
                    endpoint = endpoint[1:]
                print_result(asyncio.run(call_endpoint(service, endpoint, request_json, config, root_path=root_path)))
            except Exception as e:
                sys.exit(str(e))


create_commands()
