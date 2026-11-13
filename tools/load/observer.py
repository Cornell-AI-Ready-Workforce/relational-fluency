"""Sample the server through its PID namespace, outside its CPU cgroup."""
from __future__ import annotations

import json
from pathlib import Path
import time

from tools.load.measure import JsonlTail


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
    while True:
        for row in tail.read():
            print(json.dumps({"kind": "attempt", "event": row}), flush=True)
        if time.monotonic() >= due:
            try:
                sample = resource_sample()
                sample["cpu_percent"] = (100 * (sample["usage_usec"] - previous["usage_usec"]) / 1e6
                                         / (sample["t"] - previous["t"])) if previous else None
                previous = sample
                print(json.dumps({"kind": "resource", "sample": sample}), flush=True)
            except (OSError, ValueError, RuntimeError, KeyError) as exc:
                print(json.dumps({"kind": "error", "detail": str(exc)}), flush=True)
            due += 1.0
        time.sleep(0.1)


if __name__ == "__main__":
    main()
