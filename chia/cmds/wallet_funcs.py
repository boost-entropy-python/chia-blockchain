from __future__ import annotations

import asyncio
import json
import os
import pathlib
import sys
import time
from collections.abc import Awaitable, Sequence
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any, Callable, Optional, Union

from chia_rs.sized_bytes import bytes32
from chia_rs.sized_ints import uint16, uint32, uint64

from chia.cmds.cmds_util import (
    CMDTXConfigLoader,
    cli_confirm,
    get_wallet_client,
    transaction_status_msg,
    transaction_submitted_msg,
)
from chia.cmds.param_types import CliAddress, CliAmount
from chia.cmds.peer_funcs import print_connections
from chia.cmds.units import units
from chia.util.bech32m import bech32_decode, decode_puzzle_hash, encode_puzzle_hash
from chia.util.byte_types import hexstr_to_bytes
from chia.util.config import selected_network_address_prefix
from chia.wallet.conditions import ConditionValidTimes, CreateCoinAnnouncement, CreatePuzzleAnnouncement
from chia.wallet.nft_wallet.nft_info import NFTInfo
from chia.wallet.outer_puzzles import AssetType
from chia.wallet.puzzle_drivers import PuzzleInfo
from chia.wallet.trade_record import TradeRecord
from chia.wallet.trading.offer import Offer
from chia.wallet.trading.trade_status import TradeStatus
from chia.wallet.transaction_record import TransactionRecord
from chia.wallet.transaction_sorting import SortKey
from chia.wallet.util.address_type import AddressType
from chia.wallet.util.puzzle_decorator_type import PuzzleDecoratorType
from chia.wallet.util.query_filter import HashFilter, TransactionTypeFilter
from chia.wallet.util.transaction_type import CLAWBACK_INCOMING_TRANSACTION_TYPES, TransactionType
from chia.wallet.util.wallet_types import WalletType
from chia.wallet.vc_wallet.vc_store import VCProofs
from chia.wallet.wallet_coin_store import GetCoinRecords
from chia.wallet.wallet_request_types import (
    CATSpendResponse,
    DIDFindLostDID,
    DIDGetDID,
    DIDGetInfo,
    DIDMessageSpend,
    DIDSetWalletName,
    DIDTransferDID,
    DIDUpdateMetadata,
    FungibleAsset,
    GetNotifications,
    GetWallets,
    NFTAddURI,
    NFTCalculateRoyalties,
    NFTCalculateRoyaltiesResponse,
    NFTGetInfo,
    NFTGetNFTs,
    NFTGetWalletDID,
    NFTMintNFTRequest,
    NFTSetNFTDID,
    NFTTransferNFT,
    RoyaltyAsset,
    SendTransactionResponse,
    VCAddProofs,
    VCGet,
    VCGetList,
    VCGetProofsForRoot,
    VCMint,
    VCRevoke,
    VCSpend,
)
from chia.wallet.wallet_rpc_client import WalletRpcClient

CATNameResolver = Callable[[bytes32], Awaitable[Optional[tuple[Optional[uint32], str]]]]

transaction_type_descriptions = {
    TransactionType.INCOMING_TX: "received",
    TransactionType.OUTGOING_TX: "sent",
    TransactionType.COINBASE_REWARD: "rewarded",
    TransactionType.FEE_REWARD: "rewarded",
    TransactionType.INCOMING_TRADE: "received in trade",
    TransactionType.OUTGOING_TRADE: "sent in trade",
    TransactionType.INCOMING_CLAWBACK_RECEIVE: "received in clawback as recipient",
    TransactionType.INCOMING_CLAWBACK_SEND: "received in clawback as sender",
    TransactionType.OUTGOING_CLAWBACK: "claim/clawback",
}


def transaction_description_from_type(tx: TransactionRecord) -> str:
    return transaction_type_descriptions.get(TransactionType(tx.type), "(unknown reason)")


def print_transaction(
    tx: TransactionRecord,
    verbose: bool,
    name: str,
    address_prefix: str,
    mojo_per_unit: int,
    coin_record: Optional[dict[str, Any]] = None,
) -> None:
    if verbose:
        print(tx)
    else:
        chia_amount = Decimal(int(tx.amount)) / mojo_per_unit
        to_address = encode_puzzle_hash(tx.to_puzzle_hash, address_prefix)
        print(f"Transaction {tx.name}")
        print(f"Status: {'Confirmed' if tx.confirmed else ('In mempool' if tx.is_in_mempool() else 'Pending')}")
        description = transaction_description_from_type(tx)
        print(f"Amount {description}: {chia_amount} {name}")
        print(f"To address: {to_address}")
        print("Created at:", datetime.fromtimestamp(tx.created_at_time).strftime("%Y-%m-%d %H:%M:%S"))
        if coin_record is not None:
            print(
                "Recipient claimable time:",
                datetime.fromtimestamp(tx.created_at_time + coin_record["metadata"]["time_lock"]).strftime(
                    "%Y-%m-%d %H:%M:%S"
                ),
            )
        print("")


def get_mojo_per_unit(wallet_type: WalletType) -> int:
    mojo_per_unit: int
    if wallet_type in {
        WalletType.STANDARD_WALLET,
        WalletType.POOLING_WALLET,
        WalletType.DATA_LAYER,
        WalletType.VC,
    }:
        mojo_per_unit = units["chia"]
    elif wallet_type in {WalletType.CAT, WalletType.CRCAT, WalletType.RCAT}:
        mojo_per_unit = units["cat"]
    elif wallet_type in {WalletType.NFT, WalletType.DECENTRALIZED_ID}:
        mojo_per_unit = units["mojo"]
    else:
        raise LookupError(f"Operation is not supported for Wallet type {wallet_type.name}")

    return mojo_per_unit


async def get_wallet_type(wallet_id: int, wallet_client: WalletRpcClient) -> WalletType:
    summaries_response = await wallet_client.get_wallets(GetWallets())
    for summary in summaries_response.wallets:
        summary_id: int = summary.id
        summary_type: int = summary.type
        if wallet_id == summary_id:
            return WalletType(summary_type)

    raise LookupError(f"Wallet ID not found: {wallet_id}")


async def get_unit_name_for_wallet_id(
    config: dict[str, Any],
    wallet_type: WalletType,
    wallet_id: int,
    wallet_client: WalletRpcClient,
) -> str:
    if wallet_type in {
        WalletType.STANDARD_WALLET,
        WalletType.POOLING_WALLET,
        WalletType.DATA_LAYER,
        WalletType.VC,
    }:
        name: str = config["network_overrides"]["config"][config["selected_network"]]["address_prefix"].upper()
    elif wallet_type in {WalletType.CAT, WalletType.CRCAT, WalletType.RCAT}:
        name = await wallet_client.get_cat_name(wallet_id=wallet_id)
    else:
        raise LookupError(f"Operation is not supported for Wallet type {wallet_type.name}")

    return name


async def get_transaction(
    *, root_path: pathlib.Path, wallet_rpc_port: Optional[int], fingerprint: Optional[int], tx_id: str, verbose: int
) -> None:
    async with get_wallet_client(root_path, wallet_rpc_port, fingerprint) as (wallet_client, fingerprint, config):
        transaction_id = bytes32.from_hexstr(tx_id)
        address_prefix = selected_network_address_prefix(config)
        tx: TransactionRecord = await wallet_client.get_transaction(transaction_id=transaction_id)

        try:
            wallet_type = await get_wallet_type(wallet_id=tx.wallet_id, wallet_client=wallet_client)
            mojo_per_unit = get_mojo_per_unit(wallet_type=wallet_type)
            name = await get_unit_name_for_wallet_id(
                config=config,
                wallet_type=wallet_type,
                wallet_id=tx.wallet_id,
                wallet_client=wallet_client,
            )
        except LookupError as e:
            print(e.args[0])
            return

        print_transaction(
            tx,
            verbose=(verbose > 0),
            name=name,
            address_prefix=address_prefix,
            mojo_per_unit=mojo_per_unit,
        )


async def get_transactions(
    *,
    root_path: pathlib.Path,
    wallet_rpc_port: Optional[int],
    fp: Optional[int],
    wallet_id: int,
    verbose: int,
    paginate: Optional[bool],
    offset: int,
    limit: int,
    sort_key: SortKey,
    reverse: bool,
    clawback: bool,
) -> None:
    async with get_wallet_client(root_path, wallet_rpc_port, fp) as (wallet_client, _, config):
        if paginate is None:
            paginate = sys.stdout.isatty()
        type_filter = (
            None
            if not clawback
            else TransactionTypeFilter.include(
                [TransactionType.INCOMING_CLAWBACK_RECEIVE, TransactionType.INCOMING_CLAWBACK_SEND]
            )
        )
        txs: list[TransactionRecord] = await wallet_client.get_transactions(
            wallet_id, start=offset, end=(offset + limit), sort_key=sort_key, reverse=reverse, type_filter=type_filter
        )

        address_prefix = selected_network_address_prefix(config)
        if len(txs) == 0:
            print("There are no transactions to this address")

        try:
            wallet_type = await get_wallet_type(wallet_id=wallet_id, wallet_client=wallet_client)
            mojo_per_unit = get_mojo_per_unit(wallet_type=wallet_type)
            name = await get_unit_name_for_wallet_id(
                config=config,
                wallet_type=wallet_type,
                wallet_id=wallet_id,
                wallet_client=wallet_client,
            )
        except LookupError as e:
            print(e.args[0])
            return

        skipped = 0
        num_per_screen = 5 if paginate else len(txs)
        for i in range(0, len(txs), num_per_screen):
            for j in range(num_per_screen):
                if i + j + skipped >= len(txs):
                    break
                coin_record: Optional[dict[str, Any]] = None
                if txs[i + j + skipped].type in CLAWBACK_INCOMING_TRANSACTION_TYPES:
                    coin_records = await wallet_client.get_coin_records(
                        GetCoinRecords(coin_id_filter=HashFilter.include([txs[i + j + skipped].additions[0].name()]))
                    )
                    if len(coin_records["coin_records"]) > 0:
                        coin_record = coin_records["coin_records"][0]
                    else:
                        j -= 1
                        skipped += 1
                        continue
                print_transaction(
                    txs[i + j + skipped],
                    verbose=(verbose > 0),
                    name=name,
                    address_prefix=address_prefix,
                    mojo_per_unit=mojo_per_unit,
                    coin_record=coin_record,
                )
            if i + num_per_screen >= len(txs):
                return None
            print("Press q to quit, or c to continue")
            while True:
                entered_key = sys.stdin.read(1)
                if entered_key == "q":
                    return None
                elif entered_key == "c":
                    break


def check_unusual_transaction(amount: uint64, fee: uint64) -> bool:
    return fee >= amount


async def send(
    *,
    root_path: pathlib.Path,
    wallet_rpc_port: Optional[int],
    fp: Optional[int],
    wallet_id: int,
    amount: CliAmount,
    memo: Optional[str],
    fee: uint64,
    address: CliAddress,
    override: bool,
    min_coin_amount: CliAmount,
    max_coin_amount: CliAmount,
    excluded_coin_ids: Sequence[bytes32],
    reuse_puzhash: Optional[bool],
    clawback_time_lock: int,
    push: bool,
    condition_valid_times: ConditionValidTimes,
) -> list[TransactionRecord]:
    async with get_wallet_client(root_path, wallet_rpc_port, fp) as (wallet_client, fingerprint, config):
        if memo is None:
            memos = None
        else:
            memos = [memo]

        if clawback_time_lock < 0:
            print("Clawback time lock seconds cannot be negative.")
            return []
        try:
            typ = await get_wallet_type(wallet_id=wallet_id, wallet_client=wallet_client)
            mojo_per_unit = get_mojo_per_unit(typ)
        except LookupError:
            print(f"Wallet id: {wallet_id} not found.")
            return []

        final_amount: uint64 = amount.convert_amount(mojo_per_unit)

        if not override and check_unusual_transaction(final_amount, fee):
            print(
                f"A transaction of amount {final_amount / units['chia']} and fee {fee} is unusual.\n"
                f"Pass in --override if you are sure you mean to do this."
            )
            return []
        if final_amount == 0:
            print("You can not send an empty transaction")
            return []

        if typ == WalletType.STANDARD_WALLET:
            print("Submitting transaction...")
            res: Union[CATSpendResponse, SendTransactionResponse] = await wallet_client.send_transaction(
                wallet_id,
                final_amount,
                address.original_address,
                CMDTXConfigLoader(
                    min_coin_amount=min_coin_amount,
                    max_coin_amount=max_coin_amount,
                    excluded_coin_ids=list(excluded_coin_ids),
                    reuse_puzhash=reuse_puzhash,
                ).to_tx_config(mojo_per_unit, config, fingerprint),
                fee,
                memos,
                puzzle_decorator_override=(
                    [{"decorator": PuzzleDecoratorType.CLAWBACK.name, "clawback_timelock": clawback_time_lock}]
                    if clawback_time_lock > 0
                    else None
                ),
                push=push,
                timelock_info=condition_valid_times,
            )
        elif typ in {WalletType.CAT, WalletType.CRCAT, WalletType.RCAT}:
            print("Submitting transaction...")
            res = await wallet_client.cat_spend(
                wallet_id,
                CMDTXConfigLoader(
                    min_coin_amount=min_coin_amount,
                    max_coin_amount=max_coin_amount,
                    excluded_coin_ids=list(excluded_coin_ids),
                    reuse_puzhash=reuse_puzhash,
                ).to_tx_config(mojo_per_unit, config, fingerprint),
                final_amount,
                address.original_address,
                fee,
                memos,
                push=push,
                timelock_info=condition_valid_times,
            )
        else:
            print("Only standard wallet and CAT wallets are supported")
            return []

        tx_id = res.transaction.name
        if push:
            start = time.time()
            while time.time() - start < 10:
                await asyncio.sleep(0.1)
                tx = await wallet_client.get_transaction(tx_id)
                if len(tx.sent_to) > 0:
                    print(transaction_submitted_msg(tx))
                    print(transaction_status_msg(fingerprint, tx_id))
                    return res.transactions

        print("Transaction not yet submitted to nodes")
        if push:  # pragma: no cover
            print(f"To get status, use command: chia wallet get_transaction -f {fingerprint} -tx 0x{tx_id}")

        return res.transactions  # pragma: no cover


async def get_address(
    root_path: pathlib.Path, wallet_rpc_port: Optional[int], fp: Optional[int], wallet_id: int, new_address: bool
) -> None:
    async with get_wallet_client(root_path, wallet_rpc_port, fp) as (wallet_client, _, _):
        res = await wallet_client.get_next_address(wallet_id, new_address)
        print(res)


async def delete_unconfirmed_transactions(
    root_path: pathlib.Path, wallet_rpc_port: Optional[int], fp: Optional[int], wallet_id: int
) -> None:
    async with get_wallet_client(root_path, wallet_rpc_port, fp) as (wallet_client, fingerprint, _):
        await wallet_client.delete_unconfirmed_transactions(wallet_id)
        print(f"Successfully deleted all unconfirmed transactions for wallet id {wallet_id} on key {fingerprint}")


async def get_derivation_index(root_path: pathlib.Path, wallet_rpc_port: Optional[int], fp: Optional[int]) -> None:
    async with get_wallet_client(root_path, wallet_rpc_port, fp) as (wallet_client, _, _):
        res = await wallet_client.get_current_derivation_index()
        print(f"Last derivation index: {res}")


async def update_derivation_index(
    root_path: pathlib.Path, wallet_rpc_port: Optional[int], fp: Optional[int], index: int
) -> None:
    async with get_wallet_client(root_path, wallet_rpc_port, fp) as (wallet_client, _, _):
        print("Updating derivation index... This may take a while.")
        res = await wallet_client.extend_derivation_index(index)
        print(f"Updated derivation index: {res}")
        print("Your balances may take a while to update.")


async def add_token(
    root_path: pathlib.Path, wallet_rpc_port: Optional[int], fp: Optional[int], asset_id: bytes32, token_name: str
) -> None:
    async with get_wallet_client(root_path, wallet_rpc_port, fp) as (wallet_client, fingerprint, _):
        existing_info: Optional[tuple[Optional[uint32], str]] = await wallet_client.cat_asset_id_to_name(asset_id)
        if existing_info is None:
            wallet_id = None
            old_name = None
        else:
            wallet_id, old_name = existing_info

        if wallet_id is None:
            response = await wallet_client.create_wallet_for_existing_cat(asset_id)
            wallet_id = response["wallet_id"]
            await wallet_client.set_cat_name(wallet_id, token_name)
            print(f"Successfully added {token_name} with wallet id {wallet_id} on key {fingerprint}")
        else:
            await wallet_client.set_cat_name(wallet_id, token_name)
            print(f"Successfully renamed {old_name} with wallet_id {wallet_id} on key {fingerprint} to {token_name}")


async def make_offer(
    *,
    root_path: pathlib.Path,
    wallet_rpc_port: Optional[int],
    fp: Optional[int],
    fee: uint64,
    offers: Sequence[str],
    requests: Sequence[str],
    filepath: pathlib.Path,
    reuse_puzhash: Optional[bool],
    condition_valid_times: ConditionValidTimes,
) -> None:
    async with get_wallet_client(root_path, wallet_rpc_port, fp) as (wallet_client, fingerprint, config):
        if offers == [] or requests == []:
            print("Not creating offer: Must be offering and requesting at least one asset")
        else:
            offer_dict: dict[Union[uint32, str], int] = {}
            driver_dict: dict[str, Any] = {}
            printable_dict: dict[str, tuple[str, int, int]] = {}  # dict[asset_name, tuple[amount, unit, multiplier]]
            royalty_assets: list[RoyaltyAsset] = []
            fungible_assets: list[FungibleAsset] = []
            for item in [*offers, *requests]:
                name, amount = tuple(item.split(":")[0:2])
                try:
                    b32_id = bytes32.from_hexstr(name)
                    id: Union[uint32, str] = b32_id.hex()
                    result = await wallet_client.cat_asset_id_to_name(b32_id)
                    if result is not None:
                        name = result[1]
                    else:
                        name = "Unknown CAT"
                    unit = units["cat"]
                    if item in offers:
                        fungible_assets.append(FungibleAsset(name, uint64(abs(int(Decimal(amount) * unit)))))
                except ValueError:
                    try:
                        hrp, _ = bech32_decode(name)
                        if hrp == "nft":
                            coin_id = decode_puzzle_hash(name)
                            unit = 1
                            info = (await wallet_client.get_nft_info(NFTGetInfo(coin_id.hex()))).nft_info
                            id = info.launcher_id.hex()
                            assert isinstance(id, str)
                            if item in requests:
                                driver_dict[id] = {
                                    "type": "singleton",
                                    "launcher_id": "0x" + id,
                                    "launcher_ph": "0x" + info.launcher_puzhash.hex(),
                                    "also": {
                                        "type": "metadata",
                                        "metadata": info.chain_info,
                                        "updater_hash": "0x" + info.updater_puzhash.hex(),
                                    },
                                }
                                if info.supports_did:
                                    assert info.royalty_puzzle_hash is not None
                                    assert info.royalty_percentage is not None
                                    driver_dict[id]["also"]["also"] = {
                                        "type": "ownership",
                                        "owner": "()",
                                        "transfer_program": {
                                            "type": "royalty transfer program",
                                            "launcher_id": "0x" + info.launcher_id.hex(),
                                            "royalty_address": "0x" + info.royalty_puzzle_hash.hex(),
                                            "royalty_percentage": str(info.royalty_percentage),
                                        },
                                    }
                                    royalty_assets.append(
                                        RoyaltyAsset(
                                            name,
                                            encode_puzzle_hash(info.royalty_puzzle_hash, AddressType.XCH.hrp(config)),
                                            info.royalty_percentage,
                                        )
                                    )
                        else:
                            id = decode_puzzle_hash(name).hex()
                            assert hrp is not None
                            unit = units[hrp]
                    except ValueError:
                        id = uint32(name)
                        if id == 1:
                            name = "XCH"
                            unit = units["chia"]
                        else:
                            name = await wallet_client.get_cat_name(id)
                            unit = units["cat"]
                        if item in offers:
                            fungible_assets.append(FungibleAsset(name, uint64(abs(int(Decimal(amount) * unit)))))
                multiplier: int = -1 if item in offers else 1
                printable_dict[name] = (amount, unit, multiplier)
                if id in offer_dict:
                    print("Not creating offer: Cannot offer and request the same asset in a trade")
                    break
                else:
                    offer_dict[id] = int(Decimal(amount) * unit) * multiplier
            else:
                print("Creating Offer")
                print("--------------")
                print()
                print("OFFERING:")
                for name, data in printable_dict.items():
                    amount, unit, multiplier = data
                    if multiplier < 0:
                        print(f"  - {amount} {name} ({int(Decimal(amount) * unit)} mojos)")
                print("REQUESTING:")
                for name, data in printable_dict.items():
                    amount, unit, multiplier = data
                    if multiplier > 0:
                        print(f"  - {amount} {name} ({int(Decimal(amount) * unit)} mojos)")

                if fee > 0:
                    print()
                    print(f"Including Fees: {Decimal(fee) / units['chia']} XCH, {fee} mojos")

                if len(royalty_assets) > 0:
                    royalty_summary: NFTCalculateRoyaltiesResponse = await wallet_client.nft_calculate_royalties(
                        NFTCalculateRoyalties(royalty_assets, fungible_assets)
                    )
                    total_amounts_requested: dict[Any, int] = {}
                    print()
                    print("Royalties Summary:")
                    for nft_id, summaries in royalty_summary.to_json_dict().items():
                        print(f"  - For {nft_id}:")
                        for summary in summaries:
                            divisor = units["chia"] if summary["asset"] == "XCH" else units["cat"]
                            converted_amount = Decimal(summary["amount"]) / divisor
                            total_amounts_requested.setdefault(
                                summary["asset"], next(a.amount for a in fungible_assets if a.asset == summary["asset"])
                            )
                            total_amounts_requested[summary["asset"]] += summary["amount"]
                            print(
                                f"    - {converted_amount} {summary['asset']} ({summary['amount']} mojos) to {summary['address']}"  # noqa
                            )

                    print()
                    print("Total Amounts Offered:")
                    for asset, requested_amount in total_amounts_requested.items():
                        divisor = units["chia"] if asset == "XCH" else units["cat"]
                        converted_amount = Decimal(requested_amount) / divisor
                        print(f"  - {converted_amount} {asset} ({requested_amount} mojos)")

                    cli_confirm(
                        "\nOffers for NFTs will have royalties automatically added. "
                        "Are you sure you would like to continue? (y/n): ",
                        "Not creating offer...",
                    )

                cli_confirm("Confirm (y/n): ", "Not creating offer...")

                with filepath.open(mode="w") as file:
                    res = await wallet_client.create_offer_for_ids(
                        offer_dict,
                        driver_dict=driver_dict,
                        fee=fee,
                        tx_config=CMDTXConfigLoader(
                            reuse_puzhash=reuse_puzhash,
                        ).to_tx_config(units["chia"], config, fingerprint),
                        timelock_info=condition_valid_times,
                    )
                    if res.offer is not None:
                        file.write(res.offer.to_bech32())
                        print(f"Created offer with ID {res.trade_record.trade_id}")
                        print(
                            f"Use chia wallet get_offers --id "
                            f"{res.trade_record.trade_id} -f {fingerprint} to view status"
                        )
                    else:
                        print("Error creating offer")


def timestamp_to_time(timestamp: int) -> str:
    return datetime.fromtimestamp(timestamp).strftime("%Y-%m-%d %H:%M:%S")


async def print_offer_summary(
    cat_name_resolver: CATNameResolver, sum_dict: dict[str, int], has_fee: bool = False, network_xch: str = "XCH"
) -> None:
    for asset_id, amount in sum_dict.items():
        description: str = ""
        unit: int = units["chia"]
        wid: str = "1" if asset_id == "xch" else ""
        mojo_amount: int = int(Decimal(amount))
        name: str = network_xch
        if asset_id != "xch":
            name = asset_id
            if asset_id == "unknown":
                name = "Unknown"
                unit = units["mojo"]
                if has_fee:
                    description = " [Typically represents change returned from the included fee]"
            else:
                unit = units["cat"]
                result = await cat_name_resolver(bytes32.from_hexstr(asset_id))
                if result is not None:
                    wid = str(result[0])
                    name = result[1]
        output: str = f"    - {name}"
        mojo_str: str = f"{mojo_amount} {'mojo' if mojo_amount == 1 else 'mojos'}"
        if len(wid) > 0:
            output += f" (Wallet ID: {wid})"
        if unit == units["mojo"]:
            output += f": {mojo_str}"
        else:
            output += f": {mojo_amount / unit} ({mojo_str})"
        if len(description) > 0:
            output += f" {description}"
        print(output)


def format_timestamp_with_timezone(timestamp: int) -> str:
    tzinfo = datetime.now(timezone.utc).astimezone().tzinfo
    return datetime.fromtimestamp(timestamp, tz=tzinfo).strftime("%Y-%m-%d %H:%M %Z")


async def print_trade_record(record: TradeRecord, wallet_client: WalletRpcClient, summaries: bool = False) -> None:
    print()
    print(f"Record with id: {record.trade_id}")
    print("---------------")
    print(f"Created at: {timestamp_to_time(record.created_at_time)}")
    print(f"Confirmed at: {record.confirmed_at_index if record.confirmed_at_index > 0 else 'Not confirmed'}")
    print(f"Accepted at: {timestamp_to_time(record.accepted_at_time) if record.accepted_at_time else 'N/A'}")
    print(f"Status: {TradeStatus(record.status).name}")
    if summaries:
        print("Summary:")
        offer = Offer.from_bytes(record.offer)
        offered, requested, _, _ = offer.summary()
        outbound_balances: dict[str, int] = offer.get_pending_amounts()
        fees: Decimal = Decimal(offer.fees())
        cat_name_resolver = wallet_client.cat_asset_id_to_name
        print("  OFFERED:")
        await print_offer_summary(cat_name_resolver, offered)
        print("  REQUESTED:")
        await print_offer_summary(cat_name_resolver, requested)
        print("Pending Outbound Balances:")
        await print_offer_summary(cat_name_resolver, outbound_balances, has_fee=(fees > 0))
        print(f"Included Fees: {fees / units['chia']} XCH, {fees} mojos")
        print("Timelock information:")
        if record.valid_times.min_time is not None:
            print(f"  - Not valid until {format_timestamp_with_timezone(record.valid_times.min_time)}")
        if record.valid_times.min_height is not None:
            print(f"  - Not valid until height {record.valid_times.min_height}")
        if record.valid_times.max_time is not None:
            print(f"  - Expires at {format_timestamp_with_timezone(record.valid_times.max_time)} (+/- 10 min)")
        if record.valid_times.max_height is not None:
            print(f"  - Expires at height {record.valid_times.max_height} (wait ~10 blocks after to be reorg safe)")
    print("---------------")


async def get_offers(
    *,
    root_path: pathlib.Path,
    wallet_rpc_port: Optional[int],
    fp: Optional[int],
    offer_id: Optional[bytes32],
    filepath: Optional[str],
    exclude_my_offers: bool = False,
    exclude_taken_offers: bool = False,
    include_completed: bool = False,
    summaries: bool = False,
    reverse: bool = False,
    sort_by_relevance: bool = True,
) -> None:
    async with get_wallet_client(root_path, wallet_rpc_port, fp) as (wallet_client, _, _):
        file_contents: bool = (filepath is not None) or summaries
        records: list[TradeRecord] = []
        if offer_id is None:
            batch_size: int = 10
            start: int = 0
            end: int = start + batch_size

            # Traverse offers page by page
            while True:
                new_records: list[TradeRecord] = await wallet_client.get_all_offers(
                    start,
                    end,
                    sort_key="RELEVANCE" if sort_by_relevance else "CONFIRMED_AT_HEIGHT",
                    reverse=reverse,
                    file_contents=file_contents,
                    exclude_my_offers=exclude_my_offers,
                    exclude_taken_offers=exclude_taken_offers,
                    include_completed=include_completed,
                )
                records.extend(new_records)

                # If fewer records were returned than requested, we're done
                if len(new_records) < batch_size:
                    break

                start = end
                end += batch_size
        else:
            records = [await wallet_client.get_offer(offer_id, file_contents)]
            if filepath is not None:
                with open(pathlib.Path(filepath), "w") as file:
                    file.write(Offer.from_bytes(records[0].offer).to_bech32())
                    file.close()

        for record in records:
            await print_trade_record(record, wallet_client, summaries=summaries)


async def take_offer(
    root_path: pathlib.Path,
    wallet_rpc_port: Optional[int],
    fp: Optional[int],
    fee: uint64,
    file: str,
    examine_only: bool,
    push: bool,
    condition_valid_times: ConditionValidTimes,
) -> list[TransactionRecord]:
    async with get_wallet_client(root_path, wallet_rpc_port, fp) as (wallet_client, fingerprint, config):
        if os.path.exists(file):
            filepath = pathlib.Path(file)
            with open(filepath) as ffile:
                offer_hex: str = ffile.read()
                ffile.close()
        else:
            offer_hex = file

        try:
            offer = Offer.from_bech32(offer_hex)
        except ValueError:
            print("Please enter a valid offer file or hex blob")
            return []

        offered, requested, _, _ = offer.summary()
        cat_name_resolver = wallet_client.cat_asset_id_to_name
        network_xch = AddressType.XCH.hrp(config).upper()
        print("Summary:")
        print("  OFFERED:")
        await print_offer_summary(cat_name_resolver, offered, network_xch=network_xch)
        print("  REQUESTED:")
        await print_offer_summary(cat_name_resolver, requested, network_xch=network_xch)

        print()

        royalty_assets = []
        for royalty_asset_id in nft_coin_ids_supporting_royalties_from_offer(offer):
            if royalty_asset_id.hex() in offered:
                percentage, address = await get_nft_royalty_percentage_and_address(royalty_asset_id, wallet_client)
                royalty_assets.append(
                    RoyaltyAsset(
                        encode_puzzle_hash(royalty_asset_id, AddressType.NFT.hrp(config)),
                        encode_puzzle_hash(address, AddressType.XCH.hrp(config)),
                        percentage,
                    )
                )

        if len(royalty_assets) > 0:
            fungible_assets = []
            for fungible_asset_id in fungible_assets_from_offer(offer):
                fungible_asset_id_str = fungible_asset_id.hex() if fungible_asset_id is not None else "xch"
                if fungible_asset_id_str in requested:
                    nft_royalty_currency: str = "Unknown CAT"
                    if fungible_asset_id is None:
                        nft_royalty_currency = network_xch
                    else:
                        result = await wallet_client.cat_asset_id_to_name(fungible_asset_id)
                        if result is not None:
                            nft_royalty_currency = result[1]
                    fungible_assets.append(
                        FungibleAsset(nft_royalty_currency, uint64(requested[fungible_asset_id_str]))
                    )

            if len(fungible_assets) > 0:
                royalty_summary = await wallet_client.nft_calculate_royalties(
                    NFTCalculateRoyalties(royalty_assets, fungible_assets)
                )
                total_amounts_requested: dict[Any, int] = {}
                print("Royalties Summary:")
                for nft_id, summaries in royalty_summary.to_json_dict().items():
                    print(f"  - For {nft_id}:")
                    for summary in summaries:
                        divisor = units["chia"] if summary["asset"] == network_xch else units["cat"]
                        converted_amount = Decimal(summary["amount"]) / divisor
                        total_amounts_requested.setdefault(
                            summary["asset"], next(a.amount for a in fungible_assets if a.asset == summary["asset"])
                        )
                        total_amounts_requested[summary["asset"]] += summary["amount"]
                        print(
                            f"    - {converted_amount} {summary['asset']} ({summary['amount']} mojos) to {summary['address']}"  # noqa
                        )

                print()
                print("Total Amounts Requested:")
                for asset, amount in total_amounts_requested.items():
                    divisor = units["chia"] if asset == network_xch else units["cat"]
                    converted_amount = Decimal(amount) / divisor
                    print(f"  - {converted_amount} {asset} ({amount} mojos)")

        print(f"Included Fees: {Decimal(offer.fees()) / units['chia']} {network_xch}, {offer.fees()} mojos")

        if not examine_only:
            print()
            cli_confirm("Would you like to take this offer? (y/n): ")
            res = await wallet_client.take_offer(
                offer,
                fee=fee,
                tx_config=CMDTXConfigLoader().to_tx_config(units["chia"], config, fingerprint),
                push=push,
                timelock_info=condition_valid_times,
            )
            if push:
                print(f"Accepted offer with ID {res.trade_record.trade_id}")
                print(
                    f"Use chia wallet get_offers --id {res.trade_record.trade_id} -f {fingerprint} to view its status"
                )

            return res.transactions
        else:
            return []


async def cancel_offer(
    root_path: pathlib.Path,
    wallet_rpc_port: Optional[int],
    fp: Optional[int],
    fee: uint64,
    offer_id: bytes32,
    secure: bool,
    push: bool,
    condition_valid_times: ConditionValidTimes,
) -> list[TransactionRecord]:
    async with get_wallet_client(root_path, wallet_rpc_port, fp) as (wallet_client, fingerprint, config):
        trade_record = await wallet_client.get_offer(offer_id, file_contents=True)
        await print_trade_record(trade_record, wallet_client, summaries=True)

        cli_confirm(f"Are you sure you wish to cancel offer with ID: {trade_record.trade_id}? (y/n): ")
        res = await wallet_client.cancel_offer(
            offer_id,
            CMDTXConfigLoader().to_tx_config(units["chia"], config, fingerprint),
            secure=secure,
            fee=fee,
            push=push,
            timelock_info=condition_valid_times,
        )
        if push or not secure:
            print(f"Cancelled offer with ID {trade_record.trade_id}")
        if secure and push:
            print(f"Use chia wallet get_offers --id {trade_record.trade_id} -f {fingerprint} to view cancel status")

        return res.transactions


def wallet_coin_unit(typ: WalletType, address_prefix: str) -> tuple[str, int]:
    if typ in {WalletType.CAT, WalletType.CRCAT, WalletType.RCAT}:
        return "", units["cat"]
    if typ in {WalletType.STANDARD_WALLET, WalletType.POOLING_WALLET, WalletType.MULTI_SIG}:
        return address_prefix, units["chia"]
    return "", units["mojo"]


def print_balance(amount: int, scale: int, address_prefix: str, *, decimal_only: bool = False) -> str:
    if decimal_only:  # dont use scientific notation.
        final_amount = f"{amount / scale:.12f}"
    else:
        final_amount = f"{amount / scale}"
    ret = f"{final_amount} {address_prefix} "
    if scale > 1:
        ret += f"({amount} mojo)"
    return ret


async def print_balances(
    root_path: pathlib.Path, wallet_rpc_port: Optional[int], fp: Optional[int], wallet_type: Optional[WalletType] = None
) -> None:
    async with get_wallet_client(root_path, wallet_rpc_port, fp) as (wallet_client, fingerprint, config):
        summaries_response = await wallet_client.get_wallets(GetWallets(uint16.construct_optional(wallet_type)))
        address_prefix = selected_network_address_prefix(config)

        sync_response = await wallet_client.get_sync_status()

        print(f"Wallet height: {(await wallet_client.get_height_info()).height}")
        if sync_response.syncing:
            print("Sync status: Syncing...")
        elif sync_response.synced:
            print("Sync status: Synced")
        else:
            print("Sync status: Not synced")

        if not sync_response.syncing and sync_response.synced:
            if len(summaries_response.wallets) == 0:
                type_hint = " " if wallet_type is None else f" from type {wallet_type.name} "
                print(f"\nNo wallets{type_hint}available for fingerprint: {fingerprint}")
            else:
                print(f"Balances, fingerprint: {fingerprint}")
            for summary in summaries_response.wallets:
                indent: str = "   "
                # asset_id currently contains both the asset ID and TAIL program bytes concatenated together.
                # A future RPC update may split them apart, but for now we'll show the first 32 bytes (64 chars)
                asset_id = summary.data[:64]
                wallet_id = summary.id
                balances = await wallet_client.get_wallet_balance(wallet_id)
                typ = WalletType(int(summary.type))
                address_prefix, scale = wallet_coin_unit(typ, address_prefix)
                total_balance: str = print_balance(balances["confirmed_wallet_balance"], scale, address_prefix)
                unconfirmed_wallet_balance: str = print_balance(
                    balances["unconfirmed_wallet_balance"], scale, address_prefix
                )
                spendable_balance: str = print_balance(balances["spendable_balance"], scale, address_prefix)
                my_did: Optional[str] = None
                ljust = 23
                if typ == WalletType.CRCAT:
                    ljust = 36
                print()
                print(f"{summary.name}:")
                print(f"{indent}{'-Total Balance:'.ljust(ljust)} {total_balance}")
                if typ == WalletType.CRCAT:
                    print(
                        f"{indent}{'-Balance Pending VC Approval:'.ljust(ljust)} "
                        f"{print_balance(balances['pending_approval_balance'], scale, address_prefix)}"
                    )
                print(f"{indent}{'-Pending Total Balance:'.ljust(ljust)} {unconfirmed_wallet_balance}")
                print(f"{indent}{'-Spendable:'.ljust(ljust)} {spendable_balance}")
                print(f"{indent}{'-Type:'.ljust(ljust)} {typ.name}")
                if typ == WalletType.DECENTRALIZED_ID:
                    get_did_response = await wallet_client.get_did_id(DIDGetDID(wallet_id))
                    my_did = get_did_response.my_did
                    print(f"{indent}{'-DID ID:'.ljust(ljust)} {my_did}")
                elif typ == WalletType.NFT:
                    my_did = (await wallet_client.get_nft_wallet_did(NFTGetWalletDID(wallet_id))).did_id
                    if my_did is not None and len(my_did) > 0:
                        print(f"{indent}{'-DID ID:'.ljust(ljust)} {my_did}")
                elif len(asset_id) > 0:
                    print(f"{indent}{'-Asset ID:'.ljust(ljust)} {asset_id}")
                print(f"{indent}{'-Wallet ID:'.ljust(ljust)} {wallet_id}")

        print(" ")
        trusted_peers: dict[str, str] = config["wallet"].get("trusted_peers", {})
        trusted_cidrs: list[str] = config["wallet"].get("trusted_cidrs", [])
        await print_connections(wallet_client, trusted_peers, trusted_cidrs)


async def create_did_wallet(
    root_path: pathlib.Path,
    wallet_rpc_port: Optional[int],
    fp: Optional[int],
    fee: uint64,
    name: Optional[str],
    amount: int,
    push: bool,
    condition_valid_times: ConditionValidTimes,
) -> list[TransactionRecord]:
    async with get_wallet_client(root_path, wallet_rpc_port, fp) as (wallet_client, fingerprint, config):
        try:
            response = await wallet_client.create_new_did_wallet(
                amount,
                CMDTXConfigLoader().to_tx_config(units["chia"], config, fingerprint),
                fee,
                name,
                push=push,
                timelock_info=condition_valid_times,
            )
            wallet_id = response["wallet_id"]
            my_did = response["my_did"]
            print(f"Successfully created a DID wallet with name {name} and id {wallet_id} on key {fingerprint}")
            print(f"Successfully created a DID {my_did} in the newly created DID wallet")
            return []  # TODO: fix this endpoint to return transactions
        except Exception as e:
            print(f"Failed to create DID wallet: {e}")
            return []


async def did_set_wallet_name(
    root_path: pathlib.Path, wallet_rpc_port: Optional[int], fp: Optional[int], wallet_id: int, name: str
) -> None:
    async with get_wallet_client(root_path, wallet_rpc_port, fp) as (wallet_client, _, _):
        try:
            await wallet_client.did_set_wallet_name(DIDSetWalletName(uint32(wallet_id), name))
            print(f"Successfully set a new name for DID wallet with id {wallet_id}: {name}")
        except Exception as e:
            print(f"Failed to set DID wallet name: {e}")


async def get_did(
    root_path: pathlib.Path, wallet_rpc_port: Optional[int], fp: Optional[int], did_wallet_id: int
) -> None:
    async with get_wallet_client(root_path, wallet_rpc_port, fp) as (wallet_client, _, _):
        try:
            response = await wallet_client.get_did_id(DIDGetDID(uint32(did_wallet_id)))
            print(f"{'DID:'.ljust(23)} {response.my_did}")
            print(f"{'Coin ID:'.ljust(23)} {response.coin_id.hex() if response.coin_id is not None else 'Unknown'}")
        except Exception as e:
            print(f"Failed to get DID: {e}")


async def get_did_info(
    root_path: pathlib.Path, wallet_rpc_port: Optional[int], fp: Optional[int], coin_id: str, latest: bool
) -> None:
    async with get_wallet_client(root_path, wallet_rpc_port, fp) as (wallet_client, _, _):
        did_padding_length = 23
        try:
            response = await wallet_client.get_did_info(DIDGetInfo(coin_id, latest))
            print(f"{'DID:'.ljust(did_padding_length)} {response.did_id}")
            print(f"{'Coin ID:'.ljust(did_padding_length)} {response.latest_coin.hex()}")
            print(f"{'Inner P2 Address:'.ljust(did_padding_length)} {response.p2_address}")
            print(f"{'Public Key:'.ljust(did_padding_length)} {response.public_key.hex()}")
            print(f"{'Launcher ID:'.ljust(did_padding_length)} {response.launcher_id.hex()}")
            print(f"{'DID Metadata:'.ljust(did_padding_length)} {response.metadata}")
            print(
                f"{'Recovery List Hash:'.ljust(did_padding_length)} "
                + (response.recovery_list_hash.hex() if response.recovery_list_hash is not None else "")
            )
            print(f"{'Recovery Required Verifications:'.ljust(did_padding_length)} {response.num_verification}")
            print(f"{'Last Spend Puzzle:'.ljust(did_padding_length)} {bytes(response.full_puzzle).hex()}")
            print(f"{'Last Spend Solution:'.ljust(did_padding_length)} {bytes(response.solution).hex()}")
            print(f"{'Last Spend Hints:'.ljust(did_padding_length)} {[hint.hex() for hint in response.hints]}")

        except Exception as e:
            print(f"Failed to get DID details: {e}")


async def update_did_metadata(
    root_path: pathlib.Path,
    wallet_rpc_port: Optional[int],
    fp: Optional[int],
    did_wallet_id: int,
    metadata: str,
    reuse_puzhash: bool,
    push: bool,
    condition_valid_times: ConditionValidTimes,
) -> list[TransactionRecord]:
    async with get_wallet_client(root_path, wallet_rpc_port, fp) as (wallet_client, fingerprint, config):
        try:
            response = await wallet_client.update_did_metadata(
                DIDUpdateMetadata(
                    wallet_id=uint32(did_wallet_id),
                    metadata=json.loads(metadata),
                    push=push,
                ),
                tx_config=CMDTXConfigLoader(
                    reuse_puzhash=reuse_puzhash,
                ).to_tx_config(units["chia"], config, fingerprint),
                timelock_info=condition_valid_times,
            )
            if push:
                print(
                    f"Successfully updated DID wallet ID: {response.wallet_id}, "
                    f"Spend Bundle: {response.spend_bundle.to_json_dict()}"
                )
            return response.transactions
        except Exception as e:
            print(f"Failed to update DID metadata: {e}")
            return []


async def did_message_spend(
    root_path: pathlib.Path,
    wallet_rpc_port: Optional[int],
    fp: Optional[int],
    did_wallet_id: int,
    puzzle_announcements: list[str],
    coin_announcements: list[str],
    push: bool,
    condition_valid_times: ConditionValidTimes,
) -> list[TransactionRecord]:
    async with get_wallet_client(root_path, wallet_rpc_port, fp) as (wallet_client, fingerprint, config):
        try:
            response = await wallet_client.did_message_spend(
                DIDMessageSpend(wallet_id=uint32(did_wallet_id), push=push),
                CMDTXConfigLoader().to_tx_config(units["chia"], config, fingerprint),
                extra_conditions=(
                    *(CreateCoinAnnouncement(hexstr_to_bytes(ca)) for ca in coin_announcements),
                    *(CreatePuzzleAnnouncement(hexstr_to_bytes(pa)) for pa in puzzle_announcements),
                ),
                timelock_info=condition_valid_times,
            )
            print(f"Message Spend Bundle: {response.spend_bundle.to_json_dict()}")
            return response.transactions
        except Exception as e:
            print(f"Failed to update DID metadata: {e}")
            return []


async def transfer_did(
    root_path: pathlib.Path,
    wallet_rpc_port: Optional[int],
    fp: Optional[int],
    did_wallet_id: int,
    fee: uint64,
    target_cli_address: CliAddress,
    with_recovery: bool,
    reuse_puzhash: Optional[bool],
    push: bool,
    condition_valid_times: ConditionValidTimes,
) -> list[TransactionRecord]:
    async with get_wallet_client(root_path, wallet_rpc_port, fp) as (wallet_client, fingerprint, config):
        try:
            target_address = target_cli_address.original_address
            response = await wallet_client.did_transfer_did(
                DIDTransferDID(
                    wallet_id=uint32(did_wallet_id),
                    inner_address=target_address,
                    fee=fee,
                    with_recovery_info=with_recovery,
                    push=push,
                ),
                tx_config=CMDTXConfigLoader(
                    reuse_puzhash=reuse_puzhash,
                ).to_tx_config(units["chia"], config, fingerprint),
                timelock_info=condition_valid_times,
            )
            if push:
                print(f"Successfully transferred DID to {target_address}")
            print(f"Transaction ID: {response.transaction_id.hex()}")
            print(f"Transaction: {response.transaction.to_json_dict_convenience(config)}")
            return response.transactions
        except Exception as e:
            print(f"Failed to transfer DID: {e}")
            return []


async def find_lost_did(
    *,
    root_path: pathlib.Path,
    wallet_rpc_port: Optional[int],
    fp: Optional[int],
    coin_id: str,
    metadata: Optional[str],
    recovery_list_hash: Optional[str],
    num_verification: Optional[int],
) -> None:
    async with get_wallet_client(root_path, wallet_rpc_port, fp) as (wallet_client, _, _):
        try:
            response = await wallet_client.find_lost_did(
                DIDFindLostDID(
                    coin_id,
                    bytes32.from_hexstr(recovery_list_hash) if recovery_list_hash is not None else None,
                    uint16.construct_optional(num_verification),
                    json.loads(metadata) if metadata is not None else None,
                )
            )
            print(f"Successfully found lost DID {coin_id}, latest coin ID: {response.latest_coin_id.hex()}")
        except Exception as e:
            print(f"Failed to find lost DID: {e}")


async def create_nft_wallet(
    root_path: pathlib.Path,
    wallet_rpc_port: Optional[int],
    fp: Optional[int],
    did_id: Optional[CliAddress] = None,
    name: Optional[str] = None,
) -> None:
    async with get_wallet_client(root_path, wallet_rpc_port, fp) as (wallet_client, fingerprint, _):
        try:
            response = await wallet_client.create_new_nft_wallet(did_id.original_address if did_id else None, name)
            wallet_id = response["wallet_id"]
            print(f"Successfully created an NFT wallet with id {wallet_id} on key {fingerprint}")
        except Exception as e:
            print(f"Failed to create NFT wallet: {e}")


async def mint_nft(
    *,
    root_path: pathlib.Path,
    wallet_rpc_port: Optional[int],
    fp: Optional[int],
    wallet_id: int,
    royalty_cli_address: Optional[CliAddress],
    target_cli_address: Optional[CliAddress],
    no_did_ownership: bool,
    hash: str,
    uris: list[str],
    metadata_hash: Optional[str],
    metadata_uris: list[str],
    license_hash: Optional[str],
    license_uris: list[str],
    edition_total: Optional[int],
    edition_number: Optional[int],
    fee: uint64,
    royalty_percentage: int,
    reuse_puzhash: Optional[bool],
    push: bool,
    condition_valid_times: ConditionValidTimes,
) -> list[TransactionRecord]:
    async with get_wallet_client(root_path, wallet_rpc_port, fp) as (wallet_client, fingerprint, config):
        royalty_address = royalty_cli_address.validate_address_type(AddressType.XCH) if royalty_cli_address else None
        target_address = target_cli_address.validate_address_type(AddressType.XCH) if target_cli_address else None
        try:
            response = await wallet_client.get_nft_wallet_did(NFTGetWalletDID(uint32(wallet_id)))
            wallet_did = response.did_id
            wallet_has_did = wallet_did is not None
            did_id: Optional[str] = wallet_did
            # Handle the case when the user wants to disable DID ownership
            if no_did_ownership:
                if wallet_has_did:
                    raise ValueError("Disabling DID ownership is not supported for this NFT wallet, it does have a DID")
                else:
                    did_id = None
            else:
                if not wallet_has_did:
                    did_id = ""

            mint_response = await wallet_client.mint_nft(
                request=NFTMintNFTRequest(
                    wallet_id=uint32(wallet_id),
                    royalty_address=royalty_address,
                    target_address=target_address,
                    hash=bytes32.from_hexstr(hash),
                    uris=uris,
                    meta_hash=bytes32.from_hexstr(metadata_hash) if metadata_hash is not None else None,
                    meta_uris=metadata_uris,
                    license_hash=bytes32.from_hexstr(license_hash) if license_hash is not None else None,
                    license_uris=license_uris,
                    edition_total=uint64(edition_total) if edition_total is not None else uint64(1),
                    edition_number=uint64(edition_number) if edition_number is not None else uint64(1),
                    fee=fee,
                    royalty_amount=uint16(royalty_percentage),
                    did_id=did_id,
                    push=push,
                ),
                tx_config=CMDTXConfigLoader(
                    reuse_puzhash=reuse_puzhash,
                ).to_tx_config(units["chia"], config, fingerprint),
                timelock_info=condition_valid_times,
            )
            spend_bundle = mint_response.spend_bundle
            if push:
                print(f"NFT minted Successfully with spend bundle: {spend_bundle}")
            return mint_response.transactions
        except Exception as e:
            print(f"Failed to mint NFT: {e}")
            return []


async def add_uri_to_nft(
    *,
    root_path: pathlib.Path,
    wallet_rpc_port: Optional[int],
    fp: Optional[int],
    wallet_id: int,
    fee: uint64,
    nft_coin_id: str,
    uri: Optional[str],
    metadata_uri: Optional[str],
    license_uri: Optional[str],
    reuse_puzhash: Optional[bool],
    push: bool,
    condition_valid_times: ConditionValidTimes,
) -> list[TransactionRecord]:
    async with get_wallet_client(root_path, wallet_rpc_port, fp) as (wallet_client, fingerprint, config):
        try:
            if len([x for x in (uri, metadata_uri, license_uri) if x is not None]) > 1:
                raise ValueError("You must provide only one of the URI flags")
            if uri is not None and len(uri) > 0:
                key = "u"
                uri_value = uri
            elif metadata_uri is not None and len(metadata_uri) > 0:
                key = "mu"
                uri_value = metadata_uri
            elif license_uri is not None and len(license_uri) > 0:
                key = "lu"
                uri_value = license_uri
            else:
                raise ValueError("You must provide at least one of the URI flags")
            response = await wallet_client.add_uri_to_nft(
                NFTAddURI(
                    wallet_id=uint32(wallet_id),
                    nft_coin_id=nft_coin_id,
                    key=key,
                    uri=uri_value,
                    fee=fee,
                    push=push,
                ),
                tx_config=CMDTXConfigLoader(
                    reuse_puzhash=reuse_puzhash,
                ).to_tx_config(units["chia"], config, fingerprint),
                timelock_info=condition_valid_times,
            )
            spend_bundle = response.spend_bundle.to_json_dict()
            if push:
                print(f"URI added successfully with spend bundle: {spend_bundle}")
            return response.transactions
        except Exception as e:
            print(f"Failed to add URI to NFT: {e}")
            return []


async def transfer_nft(
    *,
    root_path: pathlib.Path,
    wallet_rpc_port: Optional[int],
    fp: Optional[int],
    wallet_id: int,
    fee: uint64,
    nft_coin_id: str,
    target_cli_address: CliAddress,
    reuse_puzhash: Optional[bool],
    push: bool,
    condition_valid_times: ConditionValidTimes,
) -> list[TransactionRecord]:
    async with get_wallet_client(root_path, wallet_rpc_port, fp) as (wallet_client, fingerprint, config):
        try:
            target_address = target_cli_address.validate_address_type(AddressType.XCH)
            response = await wallet_client.transfer_nft(
                NFTTransferNFT(
                    wallet_id=uint32(wallet_id),
                    nft_coin_id=nft_coin_id,
                    target_address=target_address,
                    fee=fee,
                    push=push,
                ),
                tx_config=CMDTXConfigLoader(
                    reuse_puzhash=reuse_puzhash,
                ).to_tx_config(units["chia"], config, fingerprint),
                timelock_info=condition_valid_times,
            )
            spend_bundle = response.spend_bundle.to_json_dict()
            if push:
                print("NFT transferred successfully")
            print(f"spend bundle: {spend_bundle}")
            return response.transactions
        except Exception as e:
            print(f"Failed to transfer NFT: {e}")
            return []


def print_nft_info(nft: NFTInfo, *, config: dict[str, Any]) -> None:
    indent: str = "   "
    owner_did = None if nft.owner_did is None else encode_puzzle_hash(nft.owner_did, AddressType.DID.hrp(config))
    minter_did = None if nft.minter_did is None else encode_puzzle_hash(nft.minter_did, AddressType.DID.hrp(config))
    print()
    print(f"{'NFT identifier:'.ljust(26)} {encode_puzzle_hash(nft.launcher_id, AddressType.NFT.hrp(config))}")
    print(f"{'Launcher coin ID:'.ljust(26)} {nft.launcher_id}")
    print(f"{'Launcher puzhash:'.ljust(26)} {nft.launcher_puzhash}")
    print(f"{'Current NFT coin ID:'.ljust(26)} {nft.nft_coin_id}")
    print(f"{'On-chain data/info:'.ljust(26)} {nft.chain_info}")
    print(f"{'Owner DID:'.ljust(26)} {owner_did}")
    print(f"{'Minter DID:'.ljust(26)} {minter_did}")
    print(f"{'Royalty percentage:'.ljust(26)} {nft.royalty_percentage}")
    print(f"{'Royalty puzhash:'.ljust(26)} {nft.royalty_puzzle_hash}")
    print(f"{'NFT content hash:'.ljust(26)} {nft.data_hash.hex()}")
    print(f"{'Metadata hash:'.ljust(26)} {nft.metadata_hash.hex()}")
    print(f"{'License hash:'.ljust(26)} {nft.license_hash.hex()}")
    print(f"{'NFT edition total:'.ljust(26)} {nft.edition_total}")
    print(f"{'Current NFT number in the edition:'.ljust(26)} {nft.edition_number}")
    print(f"{'Metadata updater puzhash:'.ljust(26)} {nft.updater_puzhash}")
    print(f"{'NFT minting block height:'.ljust(26)} {nft.mint_height}")
    print(f"{'Inner puzzle supports DID:'.ljust(26)} {nft.supports_did}")
    print(f"{'NFT is pending for a transaction:'.ljust(26)} {nft.pending_transaction}")
    print()
    print("URIs:")
    for uri in nft.data_uris:
        print(f"{indent}{uri}")
    print()
    print("Metadata URIs:")
    for metadata_uri in nft.metadata_uris:
        print(f"{indent}{metadata_uri}")
    print()
    print("License URIs:")
    for license_uri in nft.license_uris:
        print(f"{indent}{license_uri}")


async def list_nfts(
    root_path: pathlib.Path,
    wallet_rpc_port: Optional[int],
    fp: Optional[int],
    wallet_id: int,
    num: int,
    start_index: int,
) -> None:
    async with get_wallet_client(root_path, wallet_rpc_port, fp) as (wallet_client, fingerprint, config):
        try:
            response = await wallet_client.list_nfts(NFTGetNFTs(uint32(wallet_id), uint32(start_index), uint32(num)))
            nft_list = response.nft_list
            if len(nft_list) > 0:
                for nft in nft_list:
                    print_nft_info(nft, config=config)
            else:
                print(f"No NFTs found for wallet with id {wallet_id} on key {fingerprint}")
        except Exception as e:
            print(f"Failed to list NFTs for wallet with id {wallet_id} on key {fingerprint}: {e}")


async def set_nft_did(
    *,
    root_path: pathlib.Path,
    wallet_rpc_port: Optional[int],
    fp: Optional[int],
    wallet_id: int,
    fee: uint64,
    nft_coin_id: str,
    did_id: str,
    reuse_puzhash: Optional[bool],
    push: bool,
    condition_valid_times: ConditionValidTimes,
) -> list[TransactionRecord]:
    async with get_wallet_client(root_path, wallet_rpc_port, fp) as (wallet_client, fingerprint, config):
        try:
            response = await wallet_client.set_nft_did(
                NFTSetNFTDID(
                    wallet_id=uint32(wallet_id),
                    did_id=did_id,
                    nft_coin_id=bytes32.from_hexstr(nft_coin_id),
                    fee=fee,
                    push=push,
                ),
                tx_config=CMDTXConfigLoader(
                    reuse_puzhash=reuse_puzhash,
                ).to_tx_config(units["chia"], config, fingerprint),
                timelock_info=condition_valid_times,
            )
            spend_bundle = response.spend_bundle.to_json_dict()
            print(f"Transaction to set DID on NFT has been initiated with: {spend_bundle}")
            return response.transactions
        except Exception as e:
            print(f"Failed to set DID on NFT: {e}")
            return []


async def get_nft_info(
    root_path: pathlib.Path, wallet_rpc_port: Optional[int], fp: Optional[int], nft_coin_id: str
) -> None:
    async with get_wallet_client(root_path, wallet_rpc_port, fp) as (wallet_client, _, config):
        try:
            response = await wallet_client.get_nft_info(NFTGetInfo(nft_coin_id))
            print_nft_info(response.nft_info, config=config)
        except Exception as e:
            print(f"Failed to get NFT info: {e}")


async def get_nft_royalty_percentage_and_address(
    nft_coin_id: bytes32, wallet_client: WalletRpcClient
) -> tuple[uint16, bytes32]:
    info = (await wallet_client.get_nft_info(NFTGetInfo(nft_coin_id.hex()))).nft_info
    assert info.royalty_puzzle_hash is not None
    percentage = uint16(info.royalty_percentage) if info.royalty_percentage is not None else 0
    return uint16(percentage), info.royalty_puzzle_hash


def calculate_nft_royalty_amount(
    offered: dict[str, Any], requested: dict[str, Any], nft_coin_id: bytes32, nft_royalty_percentage: int
) -> tuple[str, int, int]:
    nft_asset_id = nft_coin_id.hex()
    amount_dict: dict[str, Any] = requested if nft_asset_id in offered else offered
    amounts: list[tuple[str, int]] = list(amount_dict.items())

    if len(amounts) != 1 or not isinstance(amounts[0][1], int):
        raise ValueError("Royalty enabled NFTs only support offering/requesting one NFT for one currency")

    royalty_amount: uint64 = uint64(amounts[0][1] * nft_royalty_percentage / 10000)
    royalty_asset_id = amounts[0][0]
    total_amount_requested = (requested[royalty_asset_id] if amount_dict == requested else 0) + royalty_amount
    return royalty_asset_id, royalty_amount, total_amount_requested


def driver_dict_asset_is_nft_supporting_royalties(driver_dict: dict[bytes32, PuzzleInfo], asset_id: bytes32) -> bool:
    asset_dict: PuzzleInfo = driver_dict[asset_id]
    return asset_dict.check_type(
        [
            AssetType.SINGLETON.value,
            AssetType.METADATA.value,
            AssetType.OWNERSHIP.value,
        ]
    )


def driver_dict_asset_is_fungible(driver_dict: dict[bytes32, PuzzleInfo], asset_id: bytes32) -> bool:
    asset_dict: PuzzleInfo = driver_dict[asset_id]
    return asset_dict.type() != AssetType.SINGLETON.value


def nft_coin_ids_supporting_royalties_from_offer(offer: Offer) -> list[bytes32]:
    return [
        key for key in offer.driver_dict.keys() if driver_dict_asset_is_nft_supporting_royalties(offer.driver_dict, key)
    ]


def fungible_assets_from_offer(offer: Offer) -> list[Optional[bytes32]]:
    return [
        asset for asset in offer.arbitrage() if asset is None or driver_dict_asset_is_fungible(offer.driver_dict, asset)
    ]


async def send_notification(
    root_path: pathlib.Path,
    wallet_rpc_port: Optional[int],
    fp: Optional[int],
    fee: uint64,
    address: CliAddress,
    message: bytes,
    cli_amount: CliAmount,
    push: bool,
    condition_valid_times: ConditionValidTimes,
) -> list[TransactionRecord]:
    async with get_wallet_client(root_path, wallet_rpc_port, fp) as (wallet_client, fingerprint, _):
        amount: uint64 = cli_amount.convert_amount(units["chia"])

        tx = await wallet_client.send_notification(
            address.puzzle_hash,
            message,
            amount,
            fee,
            push=push,
            timelock_info=condition_valid_times,
        )

        if push:
            print("Notification sent successfully.")
            print(f"To get status, use command: chia wallet get_transaction -f {fingerprint} -tx 0x{tx.name}")
        return [tx]


async def get_notifications(
    root_path: pathlib.Path,
    wallet_rpc_port: Optional[int],
    fp: Optional[int],
    ids: Optional[Sequence[bytes32]],
    start: Optional[int],
    end: Optional[int],
) -> None:
    async with get_wallet_client(root_path, wallet_rpc_port, fp) as (wallet_client, _, _):
        if ids is not None:
            ids = None if len(ids) == 0 else list(ids)
        response = await wallet_client.get_notifications(
            GetNotifications(ids=ids, start=uint32.construct_optional(start), end=uint32.construct_optional(end))
        )
        for notification in response.notifications:
            print("")
            print(f"ID: {notification.id.hex()}")
            print(f"message: {notification.message.decode('utf-8')}")
            print(f"amount: {notification.amount}")


async def delete_notifications(
    root_path: pathlib.Path, wallet_rpc_port: Optional[int], fp: Optional[int], ids: Sequence[bytes32], delete_all: bool
) -> None:
    async with get_wallet_client(root_path, wallet_rpc_port, fp) as (wallet_client, _, _):
        if delete_all:
            print(f"Success: {await wallet_client.delete_notifications()}")
        else:
            print(f"Success: {await wallet_client.delete_notifications(ids=list(ids))}")


async def sign_message(
    *,
    root_path: pathlib.Path,
    wallet_rpc_port: Optional[int],
    fp: Optional[int],
    addr_type: AddressType,
    message: str,
    address: Optional[CliAddress] = None,
    did_id: Optional[CliAddress] = None,
    nft_id: Optional[CliAddress] = None,
) -> None:
    async with get_wallet_client(root_path, wallet_rpc_port, fp) as (wallet_client, _, _):
        if addr_type == AddressType.XCH:
            if address is None:
                print("Address is required for XCH address type.")
                return
            pubkey, signature, signing_mode = await wallet_client.sign_message_by_address(
                address.original_address, message
            )
        elif addr_type == AddressType.DID:
            if did_id is None:
                print("DID id is required for DID address type.")
                return
            pubkey, signature, signing_mode = await wallet_client.sign_message_by_id(did_id.original_address, message)
        elif addr_type == AddressType.NFT:
            if nft_id is None:
                print("NFT id is required for NFT address type.")
                return
            pubkey, signature, signing_mode = await wallet_client.sign_message_by_id(nft_id.original_address, message)
        else:
            print("Invalid wallet type.")
            return
        print("")
        print(f"Message: {message}")
        print(f"Public Key: {pubkey}")
        print(f"Signature: {signature}")
        print(f"Signing Mode: {signing_mode}")


async def spend_clawback(
    *,
    root_path: pathlib.Path,
    wallet_rpc_port: Optional[int],
    fp: Optional[int],
    fee: uint64,
    tx_ids_str: str,
    force: bool = False,
    push: bool,
    condition_valid_times: ConditionValidTimes,
) -> list[TransactionRecord]:
    async with get_wallet_client(root_path, wallet_rpc_port, fp) as (wallet_client, _, _):
        tx_ids = []
        for tid in tx_ids_str.split(","):
            tx_ids.append(bytes32.from_hexstr(tid))
        if len(tx_ids) == 0:
            print("Transaction ID is required.")
            return []
        if fee < 0:
            print("Batch fee cannot be negative.")
            return []
        response = await wallet_client.spend_clawback_coins(
            tx_ids,
            fee,
            force,
            push=push,
            timelock_info=condition_valid_times,
        )
        print(str(response))
        return [TransactionRecord.from_json_dict_convenience(tx) for tx in response["transactions"]]


async def mint_vc(
    root_path: pathlib.Path,
    wallet_rpc_port: Optional[int],
    fp: Optional[int],
    did: CliAddress,
    fee: uint64,
    target_address: Optional[CliAddress],
    push: bool,
    condition_valid_times: ConditionValidTimes,
) -> list[TransactionRecord]:
    async with get_wallet_client(root_path, wallet_rpc_port, fp) as (wallet_client, fingerprint, config):
        res = await wallet_client.vc_mint(
            VCMint(
                did_id=did.validate_address_type(AddressType.DID),
                target_address=target_address.validate_address_type(AddressType.XCH) if target_address else None,
                fee=fee,
                push=push,
            ),
            CMDTXConfigLoader().to_tx_config(units["chia"], config, fingerprint),
            timelock_info=condition_valid_times,
        )

        if push:
            print(f"New VC with launcher ID minted: {res.vc_record.vc.launcher_id.hex()}")
        print("Relevant TX records:")
        print("")
        for tx in res.transactions:
            print_transaction(
                tx,
                verbose=False,
                name="XCH",
                address_prefix=selected_network_address_prefix(config),
                mojo_per_unit=get_mojo_per_unit(wallet_type=WalletType.STANDARD_WALLET),
            )
        return res.transactions


async def get_vcs(
    root_path: pathlib.Path, wallet_rpc_port: Optional[int], fp: Optional[int], start: int, count: int
) -> None:
    async with get_wallet_client(root_path, wallet_rpc_port, fp) as (wallet_client, _, config):
        get_list_response = await wallet_client.vc_get_list(VCGetList(uint32(start), uint32(count)))
        print("Proofs:")
        for hash, proof_dict in get_list_response.proof_dict.items():
            if proof_dict is not None:
                print(f"- {hash}")
                for proof in proof_dict:
                    print(f"  - {proof}")
        for record in get_list_response.vc_records:
            print("")
            print(f"Launcher ID: {record.vc.launcher_id.hex()}")
            print(f"Coin ID: {record.vc.coin.name().hex()}")
            print(
                f"Inner Address:"
                f" {encode_puzzle_hash(record.vc.inner_puzzle_hash, selected_network_address_prefix(config))}"
            )
            if record.vc.proof_hash is None:
                pass
            else:
                print(f"Proof Hash: {record.vc.proof_hash.hex()}")


async def spend_vc(
    *,
    root_path: pathlib.Path,
    wallet_rpc_port: Optional[int],
    fp: Optional[int],
    vc_id: bytes32,
    fee: uint64,
    new_puzhash: Optional[bytes32],
    new_proof_hash: str,
    reuse_puzhash: bool,
    push: bool,
    condition_valid_times: ConditionValidTimes,
) -> list[TransactionRecord]:
    async with get_wallet_client(root_path, wallet_rpc_port, fp) as (wallet_client, fingerprint, config):
        txs = (
            await wallet_client.vc_spend(
                VCSpend(
                    vc_id=vc_id,
                    new_puzhash=new_puzhash,
                    new_proof_hash=bytes32.from_hexstr(new_proof_hash),
                    fee=fee,
                    push=push,
                ),
                tx_config=CMDTXConfigLoader(
                    reuse_puzhash=reuse_puzhash,
                ).to_tx_config(units["chia"], config, fingerprint),
                timelock_info=condition_valid_times,
            )
        ).transactions

        if push:
            print("Proofs successfully updated!")
        print("Relevant TX records:")
        print("")
        for tx in txs:
            print_transaction(
                tx,
                verbose=False,
                name="XCH",
                address_prefix=selected_network_address_prefix(config),
                mojo_per_unit=get_mojo_per_unit(wallet_type=WalletType.STANDARD_WALLET),
            )
        return txs


async def add_proof_reveal(
    root_path: pathlib.Path, wallet_rpc_port: Optional[int], fp: Optional[int], proofs: Sequence[str], root_only: bool
) -> None:
    async with get_wallet_client(root_path, wallet_rpc_port, fp) as (wallet_client, _, _):
        if len(proofs) == 0:
            print("Must specify at least one proof")
            return

        proof_dict: dict[str, str] = {proof: "1" for proof in proofs}
        if root_only:
            print(f"Proof Hash: {VCProofs(proof_dict).root()}")
            return
        else:
            await wallet_client.vc_add_proofs(VCAddProofs.from_json_dict({"proofs": proof_dict}))
            print("Proofs added to DB successfully!")
            return


async def get_proofs_for_root(
    root_path: pathlib.Path, wallet_rpc_port: Optional[int], fp: Optional[int], proof_hash: str
) -> None:
    async with get_wallet_client(root_path, wallet_rpc_port, fp) as (wallet_client, _, _):
        proof_dict: dict[str, str] = (
            (await wallet_client.vc_get_proofs_for_root(VCGetProofsForRoot(bytes32.from_hexstr(proof_hash))))
            .to_vc_proofs()
            .key_value_pairs
        )
        print("Proofs:")
        for proof in proof_dict:
            print(f" - {proof}")


async def revoke_vc(
    root_path: pathlib.Path,
    wallet_rpc_port: Optional[int],
    fp: Optional[int],
    parent_coin_id: Optional[bytes32],
    vc_id: Optional[bytes32],
    fee: uint64,
    reuse_puzhash: bool,
    push: bool,
    condition_valid_times: ConditionValidTimes,
) -> list[TransactionRecord]:
    async with get_wallet_client(root_path, wallet_rpc_port, fp) as (wallet_client, fingerprint, config):
        if parent_coin_id is None:
            if vc_id is None:
                print("Must specify either --parent-coin-id or --vc-id")
                return []
            record = (await wallet_client.vc_get(VCGet(vc_id))).vc_record
            if record is None:
                print(f"Cannot find a VC with ID {vc_id.hex()}")
                return []
            parent_id: bytes32 = bytes32(record.vc.coin.parent_coin_info)
        else:
            parent_id = parent_coin_id
        txs = (
            await wallet_client.vc_revoke(
                VCRevoke(
                    vc_parent_id=parent_id,
                    fee=fee,
                    push=push,
                ),
                tx_config=CMDTXConfigLoader(
                    reuse_puzhash=reuse_puzhash,
                ).to_tx_config(units["chia"], config, fingerprint),
                timelock_info=condition_valid_times,
            )
        ).transactions

        if push:
            print("VC successfully revoked!")
        print("Relevant TX records:")
        print("")
        for tx in txs:
            print_transaction(
                tx,
                verbose=False,
                name="XCH",
                address_prefix=selected_network_address_prefix(config),
                mojo_per_unit=get_mojo_per_unit(wallet_type=WalletType.STANDARD_WALLET),
            )
        return txs


async def approve_r_cats(
    root_path: pathlib.Path,
    wallet_rpc_port: Optional[int],
    fingerprint: int,
    wallet_id: uint32,
    min_amount_to_claim: CliAmount,
    fee: uint64,
    min_coin_amount: CliAmount,
    max_coin_amount: CliAmount,
    reuse: bool,
    push: bool,
    condition_valid_times: ConditionValidTimes,
) -> list[TransactionRecord]:
    async with get_wallet_client(root_path, wallet_rpc_port, fingerprint) as (wallet_client, fingerprint, config):
        if wallet_client is None:
            return
        txs = await wallet_client.crcat_approve_pending(
            wallet_id=wallet_id,
            min_amount_to_claim=min_amount_to_claim.convert_amount(units["cat"]),
            fee=fee,
            tx_config=CMDTXConfigLoader(
                min_coin_amount=min_coin_amount,
                max_coin_amount=max_coin_amount,
                reuse_puzhash=reuse,
            ).to_tx_config(units["cat"], config, fingerprint),
            push=push,
            timelock_info=condition_valid_times,
        )

        if push:
            print("VC successfully approved R-CATs!")
        print("Relevant TX records:")
        print("")
        for tx in txs:
            try:
                wallet_type = await get_wallet_type(wallet_id=tx.wallet_id, wallet_client=wallet_client)
                mojo_per_unit = get_mojo_per_unit(wallet_type=wallet_type)
                name = await get_unit_name_for_wallet_id(
                    config=config,
                    wallet_type=wallet_type,
                    wallet_id=tx.wallet_id,
                    wallet_client=wallet_client,
                )
            except LookupError as e:
                print(e.args[0])
                return txs

            print_transaction(
                tx,
                verbose=False,
                name=name,
                address_prefix=selected_network_address_prefix(config),
                mojo_per_unit=mojo_per_unit,
            )
        return txs
