"""Limitador de taxa por chave (token bucket), seguro para uso com várias threads."""
from __future__ import annotations

import threading
import time
from typing import Callable


class RateLimiter:
    def __init__(self, per_minute: int, burst: int | None = None, *, clock: Callable[[], float] = time.monotonic,
                 max_keys: int = 20000):
        self.enabled = per_minute > 0
        self.rate = per_minute / 60.0 if self.enabled else 0.0
        self.capacity = float(burst or max(1, per_minute))
        self._clock = clock
        self._max_keys = max_keys
        self._buckets: dict[str, tuple[float, float]] = {}
        self._lock = threading.Lock()

    def check(self, key: str) -> tuple[bool, float]:
        """Consome 1 ficha. Retorna (permitido, segundos até nova tentativa)."""
        if not self.enabled:
            return True, 0.0
        now = self._clock()
        with self._lock:
            tokens, last = self._buckets.get(key, (self.capacity, now))
            tokens = min(self.capacity, tokens + (now - last) * self.rate)
            allowed = tokens >= 1.0
            if allowed:
                tokens -= 1.0
            self._buckets[key] = (tokens, now)
            if len(self._buckets) > self._max_keys:
                self._purge(now)
            return allowed, 0.0 if allowed else (1.0 - tokens) / self.rate

    def blocked(self, key: str) -> tuple[bool, float]:
        """Consulta sem consumir: o balde da chave está esgotado? (usado contra tentativas de adivinhação)"""
        if not self.enabled:
            return False, 0.0
        now = self._clock()
        with self._lock:
            tokens, last = self._buckets.get(key, (self.capacity, now))
            tokens = min(self.capacity, tokens + (now - last) * self.rate)
            return (tokens < 1.0), (0.0 if tokens >= 1.0 else (1.0 - tokens) / self.rate)

    def _purge(self, now: float) -> None:
        full_after = self.capacity / self.rate
        for k in [k for k, (_, last) in self._buckets.items() if now - last > full_after]:
            del self._buckets[k]
