from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from .state import StateStore, now_iso


class TraceWriter:
    def __init__(self, run_dir: Path, run_id: str, state: StateStore | None = None):
        self.run_dir = run_dir
        self.run_id = run_id
        self.state = state
        self.path = run_dir / "trace.jsonl"
        self.run_dir.mkdir(parents=True, exist_ok=True)

    def event(self, event: str, data: dict[str, Any] | None = None) -> None:
        payload = {
            "ts": now_iso(),
            "run_id": self.run_id,
            "event": event,
            "data": data or {},
        }
        with self.path.open("a", encoding="utf-8", newline="\n") as handle:
            handle.write(json.dumps(payload, sort_keys=True) + "\n")
        if self.state is not None:
            self.state.add_event(self.run_id, event, data or {})
