"""Per-session JSONL event log.

One file per session: logs/{session_id}.jsonl. Every line is a self-describing
event with a timestamp. Designed to be easy to load into pandas later for
relational-fluency scoring (turn latencies, knob trajectories, repair events).
"""
from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any, Dict

LOGS_DIR = Path(__file__).parent.parent / "logs"
LOGS_DIR.mkdir(exist_ok=True)


class SessionLog:
    def __init__(self, session_id: str):
        self.session_id = session_id
        self.path = LOGS_DIR / f"{session_id}.jsonl"
        self.t0 = time.time()
        self._fh = self.path.open("a", buffering=1, encoding="utf-8")  # line buffered, utf-8

    def event(self, type_: str, **fields: Any) -> None:
        rec: Dict[str, Any] = {
            "t": round(time.time() - self.t0, 3),
            "wall": time.time(),
            "type": type_,
        }
        rec.update(fields)
        self._fh.write(json.dumps(rec, ensure_ascii=False) + "\n")

    def close(self) -> None:
        try:
            self._fh.close()
        except Exception:
            pass
