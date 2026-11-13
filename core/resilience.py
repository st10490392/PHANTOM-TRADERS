"""
Shared unattended-operation primitives for LiveEngine and SyntheticScanner:

* Halt switch  - an operator/kill-switch check with no extra infra (env var or
  a touch-file), polled every tick so a stuck/misbehaving engine can be paused
  without killing the process (positions already open keep being *managed*,
  only new entries are blocked).
* Backoff      - exponential backoff for the run loop's outer try/except, so a
  crashing tick slows down instead of hot-looping, but the process never dies
  on its own; something outside it (systemd/NSSM/a watchdog) should still
  restart the process if it exits, this only keeps it alive across logic bugs.
* StaleFeedGuard - tracks consecutive empty/stale ticks per feed and decides
  when it's time to call feed.reconnect().
"""
import os
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional

HALT_ENV_VAR = "PHANTOM_HALT"
HALT_FILE_ENV_VAR = "PHANTOM_HALT_FILE"
DEFAULT_HALT_FILE = "phantom.halt"


def halted() -> Optional[str]:
    """
    Returns a human-readable reason if the engine should stop opening new
    positions, else None. Checked every tick, so flipping either source off
    resumes trading on the next cycle - no restart required.

    - env PHANTOM_HALT=1/true/yes
    - a touch-file (default ./phantom.halt, override with PHANTOM_HALT_FILE);
      if the file has text in it, that text is the reason shown in logs.
    """
    if os.getenv(HALT_ENV_VAR, "").strip().lower() in ("1", "true", "yes", "on"):
        return "PHANTOM_HALT env var set"
    path = Path(os.getenv(HALT_FILE_ENV_VAR, DEFAULT_HALT_FILE))
    if path.exists():
        try:
            text = path.read_text(encoding="utf-8").strip()
        except OSError:
            text = ""
        return text or f"halt file present ({path})"
    return None


class Backoff:
    """Exponential backoff with a cap, reset after a clean tick."""

    def __init__(self, base: float = 1.0, cap: float = 60.0, factor: float = 2.0):
        self.base = base
        self.cap = cap
        self.factor = factor
        self._failures = 0

    def ok(self) -> None:
        self._failures = 0

    def fail(self) -> float:
        self._failures += 1
        delay = min(self.cap, self.base * (self.factor ** (self._failures - 1)))
        return delay

    def sleep_after_failure(self) -> float:
        delay = self.fail()
        time.sleep(delay)
        return delay


class StaleFeedGuard:
    """
    Flags a feed as stale when every symbol has returned no candles (or only
    old candles) for too many consecutive ticks, so the runner can call
    feed.reconnect() instead of quietly polling a dead connection forever.
    """

    def __init__(self, max_consecutive_empty: int = 5, max_bar_age: timedelta = timedelta(minutes=30)):
        self.max_consecutive_empty = max_consecutive_empty
        self.max_bar_age = max_bar_age
        self._empty_streak = 0

    def observe(self, any_symbol_had_candles: bool, newest_bar_time: Optional[datetime] = None) -> bool:
        """Call once per tick with whether *any* symbol returned candles this
        tick, and (optionally) the newest bar timestamp seen. Returns True
        when the caller should attempt a reconnect."""
        stale_by_age = (
            newest_bar_time is not None
            and datetime.now(newest_bar_time.tzinfo) - newest_bar_time > self.max_bar_age
        )
        if not any_symbol_had_candles or stale_by_age:
            self._empty_streak += 1
        else:
            self._empty_streak = 0
        return self._empty_streak >= self.max_consecutive_empty

    def reset(self) -> None:
        self._empty_streak = 0
