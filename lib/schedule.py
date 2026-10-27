"""Fixed-rate deadline schedule shared by crsfproxy and crsf_rx_sim.py.

Slot n is due at start + n / rate_hz. A check at or after a deadline consumes
that slot and moves the next deadline past the check time, so a late check
skips the missed slots instead of sending a burst. The deadline is computed
from the slot index, not by repeated addition, so it does not drift.

The caller still polls at its loop rate: the average rate equals rate_hz
while the loop runs faster than rate_hz, and each slot fires up to one loop
period late.
"""

from __future__ import annotations

import math


class DeadlineSchedule:
    def __init__(self, rate_hz: float, start: float) -> None:
        if not rate_hz > 0:
            raise ValueError(f"rate_hz={rate_hz} must_be_positive=1")
        self.period = 1.0 / rate_hz
        self.start = start
        self.slot = 0

    @property
    def next_due(self) -> float:
        return self.start + self.slot * self.period

    def due(self, now: float) -> bool:
        """True at most once per slot, when now has reached the next deadline."""
        if now < self.next_due:
            return False
        self.slot = math.floor((now - self.start) / self.period) + 1
        while self.next_due <= now:  # float rounding at an exact deadline
            self.slot += 1
        return True
