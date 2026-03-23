from abc import ABC, abstractmethod
from typing import Generic, TypeVar

T = TypeVar("T")


class MessageQueue(ABC, Generic[T]):
    @abstractmethod
    async def put(self, item: T) -> None:
        raise NotImplementedError

    @abstractmethod
    async def get(self) -> T:
        raise NotImplementedError

    @abstractmethod
    def task_done(self) -> None:
        raise NotImplementedError

    @abstractmethod
    def empty(self) -> bool:
        raise NotImplementedError

