"""Throttling for the sign-in and sign-up routes.

Only failed attempts count. Counting successes would lock out an office of
legitimate users behind one NAT address at the start of the working day, and
it is failures -- guessing -- that the limit exists to slow down.

The engine deliberately has no lockout: its accounts module says rate limiting
"belongs at the edge, where the source address is visible". This is that edge.
It is per process and in memory, which is right for the single-process
deployment SQLite implies; a multi-replica deployment would move it to the
reverse proxy or a shared store.
"""

from __future__ import annotations

import threading
import time
from collections import OrderedDict, deque
from typing import Callable


class SlidingWindowLimiter:
    def __init__(self, limit: int, window_s: float, *, max_keys: int = 10_000,
                 clock: Callable[[], float] = time.monotonic) -> None:
        self.limit = limit
        self.window_s = window_s
        self.max_keys = max_keys
        self.clock = clock
        self._hits: "OrderedDict[str, deque[float]]" = OrderedDict()
        self._lock = threading.Lock()

    def _prune(self, key: str, now: float) -> "deque[float] | None":
        q = self._hits.get(key)
        if q is not None:
            while q and now - q[0] >= self.window_s:
                q.popleft()
        return q

    def check(self, key: str) -> float:
        """0 if the client may try now, else seconds to wait. Records nothing."""
        now = self.clock()
        with self._lock:
            q = self._prune(key, now)
            if q is not None and len(q) >= self.limit:
                return max(self.window_s - (now - q[0]), 1.0)
            return 0.0

    def record(self, key: str) -> None:
        """Count one failed attempt against the client."""
        now = self.clock()
        with self._lock:
            q = self._prune(key, now)
            if q is None:
                q = deque()
                self._hits[key] = q
            self._hits.move_to_end(key)
            q.append(now)
            #: Bounded, so a scan from many addresses cannot grow this without
            #: limit. Evicting the least recently seen key forgets a client
            #: that has gone quiet, which is the cheap direction to be wrong.
            while len(self._hits) > self.max_keys:
                self._hits.popitem(last=False)

    def hit(self, key: str) -> float:
        """Check, and if allowed record. Returns 0 if allowed, else the wait."""
        wait = self.check(key)
        if not wait:
            self.record(key)
        return wait
