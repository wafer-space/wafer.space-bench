"""Append-only JSON-lines event log."""

import json
import time
from collections.abc import Callable
from pathlib import Path


class EventLog:
    def __init__(self, path: Path, clock: Callable[[], time.struct_time] = time.gmtime):
        self.path = Path(path)
        self._clock = clock

    def write(self, event: str, **fields) -> None:
        """Append one line: UTC `time`, `event`, then `fields`."""
        record = {"time": time.strftime("%Y-%m-%dT%H:%M:%SZ", self._clock()), "event": event, **fields}
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with open(self.path, "a") as f:
            f.write(json.dumps(record) + "\n")
