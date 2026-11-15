"""Sample the server through its PID namespace, outside its CPU cgroup."""
from __future__ import annotations

import json
from pathlib import Path
import time

from tools.load.measure import JsonlTail


class EncounterEvents:
    """Tail only error observations; stop opening a session once its store closes."""
    def __init__(self, directory=Path("/data/sessions")):
        self.directory = Path(directory)
        self.tails, self.finished = {}, set()

    def read(self):
        result = []
        for path in self.directory.glob("*/events.jsonl"):
            if path not in self.finished:
                self.tails.setdefault(path, JsonlTail(path))
        for path, tail in list(self.tails.items()):
            def drain():
                for row in tail.read():
                    if row.get("type") in {"voice_error", "error"}:
                        result.append({"kind": "encounter", "session_id": path.parent.name, "event": row})
            drain()
            try:
                closed = json.loads((path.parent / "manifest.json").read_text(encoding="utf-8")).get("status") == "closed"
            except (OSError, ValueError):
                closed = False
            if closed:
                drain()  # Include final rows written before the closed manifest.
                if not tail.partial:
                    self.finished.add(path)
                    del self.tails[path]
        return result


def cgroup_stats(proc=Path("/proc/1"), root=Path("/host-cgroup")):
    groups = [line.split(":", 2) for line in (proc / "cgroup").read_text(encoding="utf-8").splitlines()]
    for _, controllers, name in groups:
        if not controllers:
            directory = root / name.lstrip("/")
            values = dict(line.split() for line in (directory / "cpu.stat").read_text(encoding="utf-8").splitlines())
            return {"usage_usec": int(values["usage_usec"]),
                    "throttled_usec": int(values["throttled_usec"]),
                    "nr_throttled": int(values["nr_throttled"])}
    raise RuntimeError("resource observer requires cgroup v2 cpu.stat")


def resource_sample(proc=Path("/proc/1"), root=Path("/host-cgroup")):
    rss = next((int(line.split()[1]) * 1024 for line in (proc / "status").read_text(encoding="utf-8").splitlines()
                if line.startswith("VmRSS:")), None)
    return {"t": time.time(), "rss_bytes": rss, **cgroup_stats(proc, root)}


def main():
    tail = JsonlTail("/data/upstream-attempts.jsonl")
    due, previous = time.monotonic(), None
    events = EncounterEvents()
    discover = 0.0
    while True:
        try:
            for row in tail.read():
                print(json.dumps({"kind": "attempt", "event": row}), flush=True)
            if time.monotonic() >= discover:
                for row in events.read():
                    print(json.dumps(row), flush=True)
                discover = time.monotonic() + 0.5
            if time.monotonic() >= due:
                sample = resource_sample()
                sample["cpu_percent"] = (100 * (sample["usage_usec"] - previous["usage_usec"]) / 1e6
                                         / (sample["t"] - previous["t"])) if previous else None
                previous = sample
                print(json.dumps({"kind": "resource", "sample": sample}), flush=True)
                due += 1.0
        except (OSError, ValueError, RuntimeError, KeyError) as exc:
            print(json.dumps({"kind": "error", "detail": str(exc)}), flush=True)
        time.sleep(0.1)


if __name__ == "__main__":
    main()
