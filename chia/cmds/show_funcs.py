from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Optional, Union

from chia_rs.sized_bytes import bytes32

from chia.full_node.full_node_rpc_client import FullNodeRpcClient


async def print_blockchain_state(node_client: FullNodeRpcClient, config: dict[str, Any]) -> bool:
    import time

    from chia_rs import BlockRecord
    from chia_rs.sized_ints import uint64

    from chia.cmds.cmds_util import format_bytes

    blockchain_state = await node_client.get_blockchain_state()
    if blockchain_state is None:
        print("There is no blockchain found yet. Try again shortly")
        return True
    peak: Optional[BlockRecord] = blockchain_state["peak"]
    node_id = blockchain_state["node_id"]
    difficulty = blockchain_state["difficulty"]
    sub_slot_iters = blockchain_state["sub_slot_iters"]
    synced = blockchain_state["sync"]["synced"]
    sync_mode = blockchain_state["sync"]["sync_mode"]
    num_blocks: int = 10
    network_name = config["selected_network"]
    genesis_challenge = config["farmer"]["network_overrides"]["constants"][network_name]["GENESIS_CHALLENGE"]
    full_node_port = config["full_node"]["port"]
    full_node_rpc_port = config["full_node"]["rpc_port"]

    print(f"Network: {network_name}    Port: {full_node_port}   RPC Port: {full_node_rpc_port}")
    print(f"Node ID: {node_id}")
    print(f"Genesis Challenge: {genesis_challenge}")

    if synced:
        print("Current Blockchain Status: Full Node Synced")
        print("\nPeak: Hash:", bytes32(peak.header_hash) if peak is not None else "")
    elif peak is not None and sync_mode:
        sync_max_block = blockchain_state["sync"]["sync_tip_height"]
        sync_current_block = blockchain_state["sync"]["sync_progress_height"]
        print(
            f"Current Blockchain Status: Syncing {sync_current_block}/{sync_max_block} "
            f"({sync_max_block - sync_current_block} behind). "
            f"({sync_current_block * 100.0 / sync_max_block:2.2f}% synced)"
        )
        print("Peak: Hash:", bytes32(peak.header_hash) if peak is not None else "")
    elif peak is not None:
        print(f"Current Blockchain Status: Not Synced. Peak height: {peak.height}")
    else:
        print("\nSearching for an initial chain\n")
        print("You may be able to expedite with 'chia peer full_node -a host:port' using a known node.\n")

    if peak is not None:
        if peak.is_transaction_block:
            peak_time = peak.timestamp
        else:
            peak_hash = bytes32(peak.header_hash)
            curr = await node_client.get_block_record(peak_hash)
            while curr is not None and not curr.is_transaction_block:
                curr = await node_client.get_block_record(curr.prev_hash)
            if curr is not None:
                peak_time = curr.timestamp
            else:
                peak_time = uint64(0)
        peak_time_struct = time.struct_time(time.localtime(peak_time))

        print(
            "      Time:",
            f"{time.strftime('%a %b %d %Y %T %Z', peak_time_struct)}",
            f"                 Height: {peak.height:>10}\n",
        )

        print("Estimated network space: ", end="")
        print(format_bytes(blockchain_state["space"]))
        print(f"Current difficulty: {difficulty}")
        print(f"Current VDF sub_slot_iters: {sub_slot_iters}")
        print("\n  Height: |   Hash:")

        added_blocks: list[BlockRecord] = []
        curr = await node_client.get_block_record(peak.header_hash)
        while curr is not None and len(added_blocks) < num_blocks and curr.height > 0:
            added_blocks.append(curr)
            curr = await node_client.get_block_record(curr.prev_hash)

        for b in added_blocks:
            print(f"{b.height:>9} | {bytes32(b.header_hash)}")
    else:
        print("Blockchain has no blocks yet")
    return False


async def print_block_from_hash(
    node_client: FullNodeRpcClient, config: dict[str, Any], block_by_header_hash: str
) -> None:
    import time

    from chia_rs import BlockRecord, FullBlock
    from chia_rs.sized_bytes import bytes32

    from chia.util.bech32m import encode_puzzle_hash

    block: Optional[BlockRecord] = await node_client.get_block_record(bytes32.from_hexstr(block_by_header_hash))
    full_block: Optional[FullBlock] = await node_client.get_block(bytes32.from_hexstr(block_by_header_hash))
    # Would like to have a verbose flag for this
    if block is not None:
        assert full_block is not None
        prev_b = await node_client.get_block_record(block.prev_hash)
        if prev_b is not None:
            difficulty = block.weight - prev_b.weight
        else:
            difficulty = block.weight
        if block.is_transaction_block:
            assert full_block.transactions_info is not None
            block_time = time.struct_time(
                time.localtime(
                    full_block.foliage_transaction_block.timestamp if full_block.foliage_transaction_block else None
                )
            )
            block_time_string = time.strftime("%a %b %d %Y %T %Z", block_time)
            cost = str(full_block.transactions_info.cost)
            tx_filter_hash: Union[str, bytes32] = "Not a transaction block"
            if full_block.foliage_transaction_block:
                tx_filter_hash = bytes32(full_block.foliage_transaction_block.filter_hash)
            fees: Any = block.fees
        else:
            block_time_string = "Not a transaction block"
            cost = "Not a transaction block"
            tx_filter_hash = "Not a transaction block"
            fees = "Not a transaction block"
        address_prefix = config["network_overrides"]["config"][config["selected_network"]]["address_prefix"]
        farmer_address = encode_puzzle_hash(block.farmer_puzzle_hash, address_prefix)
        pool_address = encode_puzzle_hash(block.pool_puzzle_hash, address_prefix)
        pool_pk = (
            full_block.reward_chain_block.proof_of_space.pool_public_key
            if full_block.reward_chain_block.proof_of_space.pool_public_key is not None
            else "Pay to pool puzzle hash"
        )
        print(
            f"Block Height           {block.height}\n"
            f"Header Hash            0x{block.header_hash.hex()}\n"
            f"Timestamp              {block_time_string}\n"
            f"Weight                 {block.weight}\n"
            f"Previous Block         0x{block.prev_hash.hex()}\n"
            f"Difficulty             {difficulty}\n"
            f"Sub-slot iters         {block.sub_slot_iters}\n"
            f"Cost                   {cost}\n"
            f"Total VDF Iterations   {block.total_iters}\n"
            f"Is a Transaction Block?{block.is_transaction_block}\n"
            f"Deficit                {block.deficit}\n"
            f"PoSpace 'k' Size (v1)  {full_block.reward_chain_block.proof_of_space.size().size_v1}\n"
            f"PoSpace 'k' Size (v2)  {full_block.reward_chain_block.proof_of_space.size().size_v2}\n"
            f"Plot Public Key        0x{full_block.reward_chain_block.proof_of_space.plot_public_key}\n"
            f"Pool Public Key        {pool_pk}\n"
            f"Tx Filter Hash         {tx_filter_hash}\n"
            f"Farmer Address         {farmer_address}\n"
            f"Pool Address           {pool_address}\n"
            f"Fees Amount            {fees}\n"
        )
    else:
        print("Block with header hash", block_by_header_hash, "not found")


async def print_fee_info(node_client: FullNodeRpcClient) -> None:
    target_times = [60, 120, 300]
    target_times_names = ["1  minute", "2 minutes", "5 minutes"]
    res = await node_client.get_fee_estimate(target_times=target_times, cost=1)
    print(json.dumps(res))
    print("\n")
    print(f"  Mempool max cost: {res['mempool_max_size']:>12} CLVM cost")
    print(f"      Mempool cost: {res['mempool_size']:>12} CLVM cost")
    print(f"     Mempool count: {res['num_spends']:>12} spends")
    print(f"   Fees in Mempool: {res['mempool_fees']:>12} mojos")
    print()

    print("Stats for last transaction block:")
    print(f"      Block height: {res['last_tx_block_height']:>12}")
    print(f"        Block fees: {res['fees_last_block']:>12} mojos")
    print(f"        Block cost: {res['last_block_cost']:>12} CLVM cost")
    print(f"          Fee rate: {res['fee_rate_last_block']:>12.5} mojos per CLVM cost")

    print("\nFee Rate Estimates:")
    max_name_len = max(len(name) for name in target_times_names)
    for n, e in zip(target_times_names, res["estimates"]):
        print(f"    {n:>{max_name_len}}: {e:.3f} mojo per CLVM cost")
    print("")


async def show_async(
    rpc_port: Optional[int],
    root_path: Path,
    print_fee_info_flag: bool,
    print_state: bool,
    block_header_hash_by_height: Optional[int],
    block_by_header_hash: str,
) -> None:
    from chia.cmds.cmds_util import get_any_service_client

    async with get_any_service_client(FullNodeRpcClient, root_path, rpc_port) as (node_client, config):
        # Check State
        if print_state:
            if await print_blockchain_state(node_client, config) is True:
                return None  # if no blockchain is found
        if print_fee_info_flag:
            await print_fee_info(node_client)
        # Get Block Information
        if block_header_hash_by_height is not None:
            block_header = await node_client.get_block_record_by_height(block_header_hash_by_height)
            if block_header is not None:
                print(f"Header hash of block {block_header_hash_by_height}: {block_header.header_hash.hex()}")
            else:
                print("Block height", block_header_hash_by_height, "not found")
        if block_by_header_hash != "":
            await print_block_from_hash(node_client, config, block_by_header_hash)
