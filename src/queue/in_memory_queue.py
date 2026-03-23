import asyncio
from typing import Generic, TypeVar

from src.queue.base import MessageQueue

T = TypeVar("T")


class InMemoryMessageQueue(MessageQueue[T], Generic[T]):
    def __init__(self) -> None:
        self._queue: asyncio.Queue[T] = asyncio.Queue()

    async def put(self, item: T) -> None:
        await self._queue.put(item)

    async def get(self) -> T:
        return await self._queue.get()

    def task_done(self) -> None:
        self._queue.task_done()

    def empty(self) -> bool:
        return self._queue.empty()

