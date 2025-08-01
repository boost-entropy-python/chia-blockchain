from __future__ import annotations

import asyncio
import contextlib
import ipaddress
import logging
import time
import traceback
from collections import defaultdict
from collections.abc import AsyncIterator, Awaitable
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable, ClassVar, Optional, cast

import aiosqlite
from chia_rs import ConsensusConstants
from chia_rs.sized_ints import uint32, uint64

from chia.full_node.full_node_api import FullNodeAPI
from chia.protocols import full_node_protocol
from chia.protocols.full_node_protocol import RespondPeers
from chia.protocols.outbound_message import NodeType
from chia.rpc.rpc_server import StateChangedProtocol, default_get_connections
from chia.seeder.crawl_store import CrawlStore
from chia.seeder.peer_record import PeerRecord, PeerReliability
from chia.server.server import ChiaServer
from chia.server.ws_connection import WSChiaConnection
from chia.types.peer_info import PeerInfo
from chia.util.chia_version import chia_short_version
from chia.util.network import resolve
from chia.util.path import path_from_root
from chia.util.task_referencer import create_referenced_task

log = logging.getLogger(__name__)


@dataclass
class Crawler:
    if TYPE_CHECKING:
        from chia.rpc.rpc_server import RpcServiceProtocol

        _protocol_check: ClassVar[RpcServiceProtocol] = cast("Crawler", None)

    config: dict[str, Any]
    root_path: Path
    constants: ConsensusConstants
    print_status: bool = True
    state_changed_callback: Optional[StateChangedProtocol] = None
    _server: Optional[ChiaServer] = None
    crawl_task: Optional[asyncio.Task[None]] = None
    crawl_store: Optional[CrawlStore] = None
    log: logging.Logger = log
    _shut_down: bool = False
    peer_count: int = 0
    with_peak: set[PeerInfo] = field(default_factory=set)
    seen_nodes: set[str] = field(default_factory=set)
    minimum_version_count: int = 0
    peers_retrieved: list[RespondPeers] = field(default_factory=list)
    host_to_version: dict[str, str] = field(default_factory=dict)
    versions: dict[str, int] = field(default_factory=lambda: defaultdict(int))
    version_cache: list[tuple[str, str]] = field(default_factory=list)
    handshake_time: dict[str, uint64] = field(default_factory=dict)
    best_timestamp_per_peer: dict[str, uint64] = field(default_factory=dict)
    start_crawler_loop: bool = True

    @property
    def server(self) -> ChiaServer:
        # This is a stop gap until the class usage is refactored such the values of
        # integral attributes are known at creation of the instance.
        if self._server is None:
            raise RuntimeError("server not assigned")

        return self._server

    @contextlib.asynccontextmanager
    async def manage(self) -> AsyncIterator[None]:
        # We override the default peer_connect_timeout when running from the crawler
        crawler_peer_timeout = self.config.get("peer_connect_timeout", 2)
        self.server.config["peer_connect_timeout"] = crawler_peer_timeout

        # Connect to the DB
        self.crawl_store: CrawlStore = await CrawlStore.create(await aiosqlite.connect(self.db_path))
        if self.start_crawler_loop:
            # Bootstrap the initial peers
            await self.load_bootstrap_peers()
            self.crawl_task = create_referenced_task(self.crawl())
        try:
            yield
        finally:
            self._shut_down = True

            if self.crawl_task is not None:
                try:
                    await asyncio.wait_for(self.crawl_task, timeout=10)  # wait 10 seconds before giving up
                except asyncio.TimeoutError:
                    self.log.error("Crawl task did not exit in time, killing task.")
                    self.crawl_task.cancel()
            if self.crawl_store is not None:
                self.log.info("Closing connection to DB.")
                await self.crawl_store.crawl_db.close()

    def __post_init__(self) -> None:
        # get db path
        crawler_db_path: str = self.config.get("crawler_db_path", "crawler.db")
        self.db_path = path_from_root(self.root_path, crawler_db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        # load data from config
        self.bootstrap_peers = self.config["bootstrap_peers"]
        self.minimum_height = self.config["minimum_height"]
        self.other_peers_port = self.config["other_peers_port"]
        self.minimum_version_count: int = self.config.get("minimum_version_count", 100)
        if self.minimum_version_count < 1:
            self.log.warning(
                f"Crawler configuration minimum_version_count expected to be greater than zero: "
                f"{self.minimum_version_count!r}"
            )

    def _set_state_changed_callback(self, callback: StateChangedProtocol) -> None:
        self.state_changed_callback = callback

    def get_connections(self, request_node_type: Optional[NodeType]) -> list[dict[str, Any]]:
        return default_get_connections(server=self.server, request_node_type=request_node_type)

    async def create_client(
        self, peer_info: PeerInfo, on_connect: Callable[[WSChiaConnection], Awaitable[None]]
    ) -> bool:
        return await self.server.start_client(peer_info, on_connect)

    async def connect_task(self, peer: PeerRecord) -> None:
        if self.crawl_store is None:
            raise ValueError("Not Connected to DB")

        async def peer_action(peer: WSChiaConnection) -> None:
            peer_info = peer.get_peer_info()
            version = chia_short_version(peer.get_version())
            if peer_info is not None and version is not None:
                self.version_cache.append((peer_info.host, version))
            # Ask peer for peers
            response = await peer.call_api(FullNodeAPI.request_peers, full_node_protocol.RequestPeers(), timeout=3)
            # Add peers to DB
            if isinstance(response, full_node_protocol.RespondPeers):
                self.peers_retrieved.append(response)
            peer_info = peer.get_peer_info()
            tries = 0
            got_peak = False
            while tries < 25:
                tries += 1
                if peer_info is None:
                    break
                if peer_info in self.with_peak:
                    got_peak = True
                    break
                await asyncio.sleep(0.1)
            if not got_peak and peer_info is not None and self.crawl_store is not None:
                await self.crawl_store.peer_connected_hostname(peer_info.host, False)
            await peer.close()

        try:
            connected = await self.create_client(
                PeerInfo(await resolve(peer.ip_address, prefer_ipv6=self.config.get("prefer_ipv6", False)), peer.port),
                peer_action,
            )
            if not connected:
                await self.crawl_store.peer_failed_to_connect(peer)
        except Exception as e:
            self.log.warning(f"Exception: {e}. Traceback: {traceback.format_exc()}.")
            await self.crawl_store.peer_failed_to_connect(peer)

    async def load_bootstrap_peers(self) -> None:
        assert self.crawl_store is not None
        try:
            self.log.warning("Bootstrapping initial peers...")
            t_start = time.time()
            for peer in self.bootstrap_peers:
                new_peer = PeerRecord(
                    peer,
                    peer,
                    self.other_peers_port,
                    False,
                    uint64(0),
                    uint32(0),
                    uint64(0),
                    uint64(time.time()),
                    uint64(0),
                    "undefined",
                    uint64(0),
                    tls_version="unknown",
                )
                new_peer_reliability = PeerReliability(peer)
                self.crawl_store.maybe_add_peer(new_peer, new_peer_reliability)

            self.host_to_version, self.handshake_time = self.crawl_store.load_host_to_version()
            self.best_timestamp_per_peer = self.crawl_store.load_best_peer_reliability()
            for host, version in self.host_to_version.items():
                self.versions[version] += 1

            self.log.warning(f"Bootstrapped initial peers in {time.time() - t_start} seconds")
        except Exception as e:
            self.log.error(f"Error bootstrapping initial peers: {e}")

    async def crawl(self) -> None:
        # Ensure the state_changed callback is set up before moving on
        # Sometimes, the daemon connection + state changed callback isn't up and ready
        # by the time we get to the first _state_changed call, so this just ensures it's there before moving on
        while self.state_changed_callback is None:
            self.log.warning("Waiting for state changed callback...")
            await asyncio.sleep(0.1)
        self.log.warning("  - Got state changed callback...")
        assert self.crawl_store is not None
        t_start = time.time()
        total_nodes = 0
        tried_nodes = set()
        try:
            while not self._shut_down:
                peers_to_crawl = await self.crawl_store.get_peers_to_crawl(25000, 250000)
                self.log.warning(f"Crawling {len(peers_to_crawl)} peers...")
                tasks = set()
                for peer in peers_to_crawl:
                    if peer.port == self.other_peers_port:
                        total_nodes += 1
                        if peer.ip_address not in tried_nodes:
                            tried_nodes.add(peer.ip_address)
                        task = create_referenced_task(self.connect_task(peer))
                        tasks.add(task)
                        if len(tasks) >= 250:
                            await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
                        tasks = set(filter(lambda t: not t.done(), tasks))
                if len(tasks) > 0:
                    await asyncio.wait(tasks, timeout=30)

                for response in self.peers_retrieved:
                    for response_peer in response.peer_list:
                        if response_peer.host not in self.best_timestamp_per_peer:
                            self.best_timestamp_per_peer[response_peer.host] = response_peer.timestamp
                        self.best_timestamp_per_peer[response_peer.host] = max(
                            self.best_timestamp_per_peer[response_peer.host], response_peer.timestamp
                        )
                        if (
                            response_peer.host not in self.seen_nodes
                            and response_peer.timestamp > time.time() - 5 * 24 * 3600
                        ):
                            self.seen_nodes.add(response_peer.host)
                            new_peer = PeerRecord(
                                response_peer.host,
                                response_peer.host,
                                uint32(response_peer.port),
                                False,
                                uint64(0),
                                uint32(0),
                                uint64(0),
                                uint64(time.time()),
                                uint64(response_peer.timestamp),
                                "undefined",
                                uint64(0),
                                tls_version="unknown",
                            )
                            new_peer_reliability = PeerReliability(response_peer.host)
                            self.crawl_store.maybe_add_peer(new_peer, new_peer_reliability)
                        await self.crawl_store.update_best_timestamp(
                            response_peer.host,
                            self.best_timestamp_per_peer[response_peer.host],
                        )
                for host, version in self.version_cache:
                    self.handshake_time[host] = uint64(time.time())
                    self.host_to_version[host] = version
                    await self.crawl_store.update_version(host, version, uint64(time.time()))

                to_remove = set()
                now = int(time.time())
                for host in self.host_to_version.keys():
                    active = True
                    if host not in self.handshake_time:
                        active = False
                    elif self.handshake_time[host] < now - 5 * 24 * 3600:
                        active = False
                    if not active:
                        to_remove.add(host)

                self.host_to_version = {
                    host: version for host, version in self.host_to_version.items() if host not in to_remove
                }
                self.best_timestamp_per_peer = {
                    host: timestamp
                    for host, timestamp in self.best_timestamp_per_peer.items()
                    if timestamp >= now - 5 * 24 * 3600
                }
                self.versions = defaultdict(int)
                for host, version in self.host_to_version.items():
                    self.versions[version] += 1

                # clear caches
                self.version_cache: list[tuple[str, str]] = []
                self.peers_retrieved = []
                self.server.banned_peers = {}
                self.with_peak = set()

                if len(peers_to_crawl) > 0:
                    peer_cutoff = int(self.config.get("crawler", {}).get("prune_peer_days", 90))
                    await self.save_to_db()
                    await self.crawl_store.prune_old_peers(older_than_days=peer_cutoff)
                    await self.print_summary(t_start, total_nodes, tried_nodes)
                await asyncio.sleep(15)  # 15 seconds between db updates
                self._state_changed("crawl_batch_completed")
        except Exception as e:
            self.log.error(f"Exception: {e}. Traceback: {traceback.format_exc()}.")

    async def save_to_db(self) -> None:
        # Try up to 5 times to write to the DB in case there is a lock that causes a timeout
        if self.crawl_store is None:
            raise ValueError("Not Connected to DB")
        for i in range(1, 5):
            try:
                await self.crawl_store.load_to_db()
                await self.crawl_store.load_reliable_peers_to_db()
                return
            except Exception as e:
                self.log.error(f"Exception while saving to DB: {e}.")
                self.log.error("Waiting 5 seconds before retry...")
                await asyncio.sleep(5)
                continue

    def set_server(self, server: ChiaServer) -> None:
        self._server = server

    def _state_changed(self, change: str, change_data: Optional[dict[str, Any]] = None) -> None:
        if self.state_changed_callback is not None:
            self.state_changed_callback(change, change_data)

    async def new_peak(self, request: full_node_protocol.NewPeak, peer: WSChiaConnection) -> None:
        try:
            peer_info = peer.get_peer_info()
            tls_version = peer.get_tls_version()
            if tls_version is None:
                tls_version = "unknown"
            if peer_info is None:
                return
            # validate peer ip address:
            try:
                ipaddress.ip_address(peer_info.host)
            except ValueError:
                raise ValueError(f"Invalid peer ip address: {peer_info.host}")
            if request.height >= self.minimum_height:
                if self.crawl_store is not None:
                    await self.crawl_store.peer_connected_hostname(peer_info.host, True, tls_version)
            self.with_peak.add(peer_info)
        except Exception as e:
            self.log.error(f"Exception: {e}. Traceback: {traceback.format_exc()}.")

    async def on_connect(self, connection: WSChiaConnection) -> None:
        pass

    async def print_summary(self, t_start: float, total_nodes: int, tried_nodes: set[str]) -> None:
        assert self.crawl_store is not None  # this is only ever called from the crawl task
        if not self.print_status:
            return
        total_records = self.crawl_store.get_total_records()
        ipv6_count = self.crawl_store.get_ipv6_peers()
        self.log.warning("***")
        self.log.warning("Finished batch:")
        self.log.warning(f"Total IPs stored in DB: {total_records}.")
        self.log.warning(f"Total IPV6 addresses stored in DB: {ipv6_count}")
        self.log.warning(f"Total connections attempted since crawler started: {total_nodes}.")
        self.log.warning(f"Total unique nodes attempted since crawler started: {len(tried_nodes)}.")
        t_now = time.time()
        t_delta = int(t_now - t_start)
        if t_delta > 0:
            self.log.warning(f"Avg connections per second: {total_nodes // t_delta}.")
        # Periodically print detailed stats.
        reliable_peers = self.crawl_store.get_reliable_peers()
        self.log.warning(f"High quality reachable nodes, used by DNS introducer in replies: {reliable_peers}")
        banned_peers = self.crawl_store.get_banned_peers()
        ignored_peers = self.crawl_store.get_ignored_peers()
        available_peers = len(self.host_to_version)
        addresses_count = len(self.best_timestamp_per_peer)
        total_records = self.crawl_store.get_total_records()
        ipv6_addresses_count = 0
        for host in self.best_timestamp_per_peer.keys():
            try:
                ipaddress.IPv6Address(host)
                ipv6_addresses_count += 1
            except ipaddress.AddressValueError:
                continue
        self.log.warning(
            "IPv4 addresses gossiped with timestamp in the last 5 days with respond_peers messages: "
            f"{addresses_count - ipv6_addresses_count}."
        )
        self.log.warning(
            "IPv6 addresses gossiped with timestamp in the last 5 days with respond_peers messages: "
            f"{ipv6_addresses_count}."
        )
        ipv6_available_peers = 0
        for host in self.host_to_version.keys():
            try:
                ipaddress.IPv6Address(host)
                ipv6_available_peers += 1
            except ipaddress.AddressValueError:
                continue
        self.log.warning(f"Total IPv4 nodes reachable in the last 5 days: {available_peers - ipv6_available_peers}.")
        self.log.warning(f"Total IPv6 nodes reachable in the last 5 days: {ipv6_available_peers}.")
        self.log.warning("Version distribution among reachable in the last 5 days (at least 100 nodes):")
        for version, count in sorted(self.versions.items(), key=lambda kv: kv[1], reverse=True):
            if count >= self.minimum_version_count:
                self.log.warning(f"Version: {version} - Count: {count}")
        self.log.warning(f"Banned addresses in the DB: {banned_peers}")
        self.log.warning(f"Temporary ignored addresses in the DB: {ignored_peers}")
        self.log.warning(
            "Peers to crawl from in the next batch (total IPs - ignored - banned): "
            f"{total_records - banned_peers - ignored_peers}"
        )
        self.log.warning("***")
