from __future__ import annotations

import asyncio
import dataclasses
import logging
from dataclasses import dataclass, field
from queue import SimpleQueue
from typing import ClassVar, Generic, Optional, TypeVar, Union

from chia_rs import SpendBundle
from chia_rs.sized_bytes import bytes32

from chia.server.ws_connection import WSChiaConnection
from chia.types.mempool_inclusion_status import MempoolInclusionStatus
from chia.util.errors import Err

T = TypeVar("T")


class TransactionQueueFull(Exception):
    pass


class ValuedEventSentinel:
    pass


@dataclasses.dataclass
class ValuedEvent(Generic[T]):
    _value_sentinel: ClassVar[ValuedEventSentinel] = ValuedEventSentinel()

    _event: asyncio.Event = dataclasses.field(default_factory=asyncio.Event)
    _value: Union[ValuedEventSentinel, T] = _value_sentinel

    def set(self, value: T) -> None:
        if not isinstance(self._value, ValuedEventSentinel):
            raise Exception("Value already set")
        self._value = value
        self._event.set()

    async def wait(self) -> T:
        await self._event.wait()
        if isinstance(self._value, ValuedEventSentinel):
            raise Exception("Value not set despite event being set")
        return self._value


@dataclass(frozen=True)
class TransactionQueueEntry:
    """
    A transaction received from peer. This is put into a queue, and not yet in the mempool.
    """

    transaction: SpendBundle = field(compare=False)
    transaction_bytes: Optional[bytes] = field(compare=False)
    spend_name: bytes32
    peer: Optional[WSChiaConnection] = field(compare=False)
    test: bool = field(compare=False)
    done: ValuedEvent[tuple[MempoolInclusionStatus, Optional[Err]]] = field(
        default_factory=ValuedEvent,
        compare=False,
    )


@dataclass
class TransactionQueue:
    """
    This class replaces one queue by using a high priority queue for local transactions and separate queues for peers.
    Local transactions are processed first.
    Then the next transaction is taken from the next non-empty queue after the last processed queue. (round-robin)
    This decreases the effects of one peer spamming your node with transactions.
    """

    _list_cursor: int  # this is which index
    _queue_length: asyncio.Semaphore
    _index_to_peer_map: list[bytes32]
    _queue_dict: dict[bytes32, SimpleQueue[TransactionQueueEntry]]
    _high_priority_queue: SimpleQueue[TransactionQueueEntry]
    peer_size_limit: int
    log: logging.Logger

    def __init__(self, peer_size_limit: int, log: logging.Logger) -> None:
        self._list_cursor = 0
        self._queue_length = asyncio.Semaphore(0)  # default is 1
        self._index_to_peer_map = []
        self._queue_dict = {}
        self._high_priority_queue = SimpleQueue()  # we don't limit the number of high priority transactions
        self.peer_size_limit = peer_size_limit
        self.log = log

    async def put(self, tx: TransactionQueueEntry, peer_id: Optional[bytes32], high_priority: bool = False) -> None:
        if peer_id is None or high_priority:  # when it's local there is no peer_id.
            self._high_priority_queue.put(tx)
        else:
            if peer_id not in self._queue_dict:
                self._queue_dict[peer_id] = SimpleQueue()
                self._index_to_peer_map.append(peer_id)
            if self._queue_dict[peer_id].qsize() < self.peer_size_limit:
                self._queue_dict[peer_id].put(tx)
            else:
                self.log.warning(f"Transaction queue full for peer {peer_id}")
                raise TransactionQueueFull(f"Transaction queue full for peer {peer_id}")
        self._queue_length.release()  # increment semaphore to indicate that we have a new item in the queue

    async def pop(self) -> TransactionQueueEntry:
        await self._queue_length.acquire()
        if not self._high_priority_queue.empty():
            return self._high_priority_queue.get()
        result: Optional[TransactionQueueEntry] = None
        while True:
            peer_queue = self._queue_dict[self._index_to_peer_map[self._list_cursor]]
            if not peer_queue.empty():
                result = peer_queue.get()
            self._list_cursor += 1
            if self._list_cursor > len(self._index_to_peer_map) - 1:
                # reset iterator
                self._list_cursor = 0
                new_peer_map = []
                for peer_id in self._index_to_peer_map:
                    if self._queue_dict[peer_id].empty():
                        self._queue_dict.pop(peer_id)
                    else:
                        new_peer_map.append(peer_id)
                self._index_to_peer_map = new_peer_map
            if result is not None:
                return result
