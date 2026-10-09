"""Independently sampled, pause-aware delays before browser actions."""

from random import uniform
from time import sleep


class BrowserActions:
    def __init__(self, check=None, stage=None):
        self.check = check or (lambda: None)
        self.stage = stage or (lambda message: None)

    def perform(self, name, operation, *args, **kwargs):
        return self._perform(name, operation, (0.5, 1.2), *args, **kwargs)

    def perform_detail(self, name, operation, *args, **kwargs):
        """Pace interactions that reveal product detail images."""
        return self._perform(name, operation, (0.5, 1.2), *args, **kwargs)

    def _perform(self, name, operation, delay_range, *args, **kwargs):
        self.check()
        delay = uniform(*delay_range)
        self.stage(f"等待 {delay:.1f} 秒后{name}")
        remaining = delay
        while remaining > 0:
            self.check()
            interval = min(0.2, remaining)
            sleep(interval)
            remaining -= interval
        self.check()
        self.stage(f"正在{name}")
        return operation(*args, **kwargs)
