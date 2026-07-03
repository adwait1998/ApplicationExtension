"""Per-host politeness for all Atlas live HTTP (spec §7.2: token buckets,
backoff, honest bot UA, well inside per-host budgets). Pure/injectable clock +
sleep so tests never actually wait."""
from __future__ import annotations

import time
from collections import defaultdict


class HostRateLimiter:
    """Simple per-host minimum-spacing limiter (target ~4 rps/host).

    Each host gets an independent budget: a burst against one board never
    steals another host's allowance. Injectable clock/sleep keep it pure for
    tests (a fake sleep advances the fake clock)."""

    def __init__(self, rps: float = 4.0, *, clock=time.monotonic, sleep=time.sleep):
        self._min_gap = 1.0 / rps
        self._clock = clock
        self._sleep = sleep
        self._last: dict[str, float] = defaultdict(lambda: float("-inf"))

    def acquire(self, host: str) -> None:
        now = self._clock()
        wait = self._last[host] + self._min_gap - now
        if wait > 0:
            self._sleep(wait)
            now = self._clock()
        self._last[host] = now


class Backoff:
    """Exponential backoff delay for repeated failures, capped."""

    def __init__(self, base: float = 1.0, cap: float = 60.0):
        self._base = base
        self._cap = cap

    def delay(self, attempt: int) -> float:
        return min(self._cap, self._base * (2 ** attempt))
