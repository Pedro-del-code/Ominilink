"""Política de reconexão com recuo exponencial, jitter e número LIMITADO de tentativas."""
from __future__ import annotations

import random
from dataclasses import dataclass

from ..errors import OmniLinkError


@dataclass(frozen=True)
class RetryPolicy:
    max_attempts: int = 8        # falhas consecutivas toleradas antes de desistir
    base_delay: float = 0.5
    max_delay: float = 15.0
    jitter: float = 0.2          # variação relativa (±) para evitar reconexões sincronizadas

    def delay(self, attempt: int, rng=random.random) -> float:
        raw = min(self.max_delay, self.base_delay * (2 ** max(0, attempt - 1)))
        return max(0.0, raw * (1 + self.jitter * (2 * rng() - 1)))


class ReconnectLimitExceeded(OmniLinkError):
    status = 503

    def __init__(self, attempts: int):
        super().__init__("RECONNECT_LIMIT_EXCEEDED", attempts=attempts)
        self.attempts = attempts
