"""Per-call event log, written by our own code.

Every input and output of the voice pipeline is recorded with two timestamps:

- ``at``: wall-clock UTC, for lining events up with the outside world.
- ``t_ms``: milliseconds since the call started, from a monotonic clock, so
  latency maths is immune to clock adjustments.

Nothing here reads provider metrics. The pipeline stages (speech recognition,
LLM, speech synthesis, tools) are wrapped in agent.py and report to this log
as data passes through them, so the timings describe what our process saw.

One JSON object per line in ``logs/<room>_<stamp>.jsonl``. The files contain
transcripts and patient details, so the directory is gitignored.
"""

from __future__ import annotations

import json
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

LOG_DIR = Path(__file__).parent / "logs"


class CallLog:
    def __init__(self, room_name: str) -> None:
        LOG_DIR.mkdir(exist_ok=True)
        stamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
        self.path = LOG_DIR / f"{room_name}_{stamp}.jsonl"
        self._start = time.monotonic()
        # Line-buffered: each event reaches disk as it happens, so a crash or a
        # call still in progress can be inspected.
        self._fh = self.path.open("a", buffering=1)

    def elapsed_ms(self) -> float:
        return round((time.monotonic() - self._start) * 1000, 1)

    def event(self, kind: str, /, **fields: Any) -> float:
        """Record one event and return its t_ms, for computing durations."""
        t_ms = self.elapsed_ms()
        row = {"t_ms": t_ms, "at": datetime.now(timezone.utc).isoformat(timespec="milliseconds"),
               "event": kind, **fields}
        if not self._fh.closed:
            self._fh.write(json.dumps(row, default=str) + "\n")
        return t_ms

    def close(self) -> None:
        if not self._fh.closed:
            self.event("log_closed")
            self._fh.close()


class NullLog(CallLog):
    """Stand-in for agents built outside a call, such as tests and rehearsals."""

    def __init__(self) -> None:  # noqa: D107 - deliberately opens no file
        self._start = time.monotonic()

    def event(self, kind: str, /, **fields: Any) -> float:
        return self.elapsed_ms()

    def close(self) -> None:
        pass
