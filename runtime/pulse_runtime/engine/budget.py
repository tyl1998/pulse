"""Nested monotonic deadlines.

The innermost declared limit is always intersected with every enclosing case/suite limit;
calls never reset an outer clock.
"""

from __future__ import annotations

import asyncio
import math
from dataclasses import dataclass


@dataclass(frozen=True)
class Deadline:
    ends_at: float | None

    @classmethod
    def after(cls, seconds: float, outer: Deadline | None = None) -> Deadline:
        now = asyncio.get_running_loop().time()
        own = now + max(0.0, seconds)
        if outer is None or outer.ends_at is None:
            return cls(own)
        return cls(min(own, outer.ends_at))

    def remaining(self) -> float:
        if self.ends_at is None:
            return math.inf
        return max(0.0, self.ends_at - asyncio.get_running_loop().time())

    @property
    def expired(self) -> bool:
        return self.remaining() <= 0

    def limit_ms(self, requested: int) -> int:
        remaining = self.remaining()
        if remaining <= 0:
            return 0
        return max(1, min(requested, int(remaining * 1000)))
