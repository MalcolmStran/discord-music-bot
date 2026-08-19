"""Track queue (per guild)."""
from __future__ import annotations

import random
from collections import deque
from typing import Iterable, Optional

from .ytdl import Track


class TrackQueue:
    def __init__(self, max_size: int = 50, history_size: int = 20):
        self.max_size = max_size
        self._q: deque[Track] = deque()
        self.history: deque[Track] = deque(maxlen=history_size)

    # --- mutation -------------------------------------------------------
    def add(self, track: Track) -> bool:
        if self.is_full:
            return False
        self._q.append(track)
        return True

    def extend(self, tracks: Iterable[Track]) -> int:
        n = 0
        for t in tracks:
            if not self.add(t):
                break
            n += 1
        return n

    def pop_next(self) -> Optional[Track]:
        if not self._q:
            return None
        t = self._q.popleft()
        self.history.append(t)
        return t

    def push_front(self, track: Track) -> None:
        self._q.appendleft(track)

    def remove(self, index: int) -> Optional[Track]:
        if 0 <= index < len(self._q):
            t = self._q[index]
            del self._q[index]
            return t
        return None

    def move(self, src: int, dst: int) -> bool:
        n = len(self._q)
        if not (0 <= src < n and 0 <= dst < n):
            return False
        t = self._q[src]
        del self._q[src]
        self._q.insert(dst, t)
        return True

    def shuffle(self) -> None:
        items = list(self._q)
        random.shuffle(items)
        self._q = deque(items)

    def clear(self) -> None:
        self._q.clear()

    # --- inspection -----------------------------------------------------
    def peek(self) -> Optional[Track]:
        return self._q[0] if self._q else None

    def __len__(self) -> int:
        return len(self._q)

    def __iter__(self):
        return iter(self._q)

    @property
    def is_empty(self) -> bool:
        return not self._q

    @property
    def is_full(self) -> bool:
        return len(self._q) >= self.max_size

    @property
    def total_duration(self) -> int:
        return sum(t.duration or 0 for t in self._q)

    def snapshot(self) -> list[Track]:
        return list(self._q)
