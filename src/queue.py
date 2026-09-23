from __future__ import annotations

import random
from collections import deque
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Generic, Iterable, TypeVar

T = TypeVar("T")


class LoopMode(StrEnum):
    OFF = "off"
    TRACK = "track"
    QUEUE = "queue"


class QueueFullError(ValueError):
    pass


@dataclass(slots=True)
class SessionQueue(Generic[T]):
    max_size: int = 500
    items: deque[T] = field(default_factory=deque)
    history: deque[T] = field(default_factory=lambda: deque(maxlen=100))
    current: T | None = None
    loop_mode: LoopMode = LoopMode.OFF
    autoplay: bool = False

    def add(self, tracks: Iterable[T], *, next_up: bool = False) -> int:
        incoming = list(tracks)
        available = self.max_size - len(self.items)
        if available <= 0:
            raise QueueFullError("La cola está llena")
        accepted = incoming[:available]
        if next_up:
            self.items.extendleft(reversed(accepted))
        else:
            self.items.extend(accepted)
        return len(accepted)

    def take_next(self) -> T | None:
        self.current = self.items.popleft() if self.items else None
        return self.current

    def finish_current(self, *, failed: bool = False) -> T | None:
        track = self.current
        self.current = None
        if track is None:
            return None
        if not failed and self.loop_mode is LoopMode.TRACK:
            self.items.appendleft(track)
        else:
            self.history.append(track)
            if not failed and self.loop_mode is LoopMode.QUEUE:
                self.items.append(track)
        return track

    def previous(self) -> T | None:
        if not self.history:
            return None
        previous = self.history.pop()
        if self.current is not None:
            self.items.appendleft(self.current)
        self.current = previous
        return previous

    def remove(self, position: int) -> T:
        index = self._index(position)
        item = self.items[index]
        del self.items[index]
        return item

    def move(self, origin: int, destination: int) -> None:
        origin_index = self._index(origin)
        if destination < 1 or destination > len(self.items):
            raise IndexError("Posición de destino inválida")
        item = self.items[origin_index]
        del self.items[origin_index]
        self.items.insert(destination - 1, item)

    def jump(self, position: int) -> list[T]:
        index = self._index(position)
        dropped = [self.items.popleft() for _ in range(index)]
        self.history.extend(dropped)
        return dropped

    def shuffle(self, rng: random.Random | None = None) -> None:
        values = list(self.items)
        (rng or random).shuffle(values)
        self.items = deque(values)

    def clear(self) -> int:
        count = len(self.items)
        self.items.clear()
        return count

    def reset(self) -> None:
        self.items.clear()
        self.history.clear()
        self.current = None
        self.loop_mode = LoopMode.OFF
        self.autoplay = False

    def _index(self, position: int) -> int:
        if position < 1 or position > len(self.items):
            raise IndexError("Posición fuera de la cola")
        return position - 1
