"""Event-loop lag: how late a 50 ms sleep wakes up.

Every participant's audio, every relay to the page and every gateway frame
share one asyncio loop. A blocking call anywhere on it (a file read, a JSON
parse, a sqlite write) delays all of them by the same amount. This probe
measures that delay directly: it sleeps INTERVAL_S, notes how much later than
asked it woke (the overshoot), and every REPORT_S prints the p99 and max
overshoot of the window to stdout, where the app's other diagnostics go.

LOOP_LAG_REPORT_S sets the report window in seconds; "0" switches the probe
off. Instrumentation only: it decides nothing and swallows its own errors.
"""
from __future__ import annotations

import asyncio
import os
import time
from typing import List, Optional

INTERVAL_S = 0.05


def report_s() -> float:
    try:
        return max(0.0, float(os.getenv("LOOP_LAG_REPORT_S", "60")))
    except ValueError:
        return 60.0


def summarize(samples_ms: List[float]) -> dict:
    """p50, p99 and max of overshoot samples, in ms (all None when empty)."""
    if not samples_ms:
        return {"n": 0, "p50_ms": None, "p99_ms": None, "max_ms": None}
    s = sorted(samples_ms)
    pick = lambda q: round(s[min(len(s) - 1, int(q * len(s)))], 2)
    return {"n": len(s), "p50_ms": pick(0.50), "p99_ms": pick(0.99),
            "max_ms": round(s[-1], 2)}


class LoopLagProbe:
    """Sleeps INTERVAL_S in a loop and records each wake-up's overshoot.

    `samples` holds the current window; `snapshot()` reads it without
    clearing; `drain()` reads and clears it. Used by the app (start/stop) and
    by tests and benchmarks (as an async context manager)."""

    def __init__(self, interval_s: float = INTERVAL_S,
                 report_every_s: float = 0.0, label: str = "loop_lag") -> None:
        self.interval_s = interval_s
        self.report_every_s = report_every_s
        self.label = label
        self.samples: List[float] = []
        self._task: Optional[asyncio.Task] = None

    async def _run(self) -> None:
        last_report = time.perf_counter()
        while True:
            t0 = time.perf_counter()
            await asyncio.sleep(self.interval_s)
            now = time.perf_counter()
            self.samples.append(max(0.0, (now - t0 - self.interval_s) * 1000.0))
            if self.report_every_s and now - last_report >= self.report_every_s:
                last_report = now
                try:
                    s = summarize(self.drain())
                    print(f"  {self.label}: n={s['n']} p99={s['p99_ms']}ms "
                          f"max={s['max_ms']}ms", flush=True)
                except Exception:  # noqa: BLE001 - instrumentation only
                    pass

    def start(self) -> "LoopLagProbe":
        if self._task is None or self._task.done():
            self._task = asyncio.ensure_future(self._run())
        return self

    async def stop(self) -> None:
        task, self._task = self._task, None
        if task is not None:
            task.cancel()
            try:
                await task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass

    def snapshot(self) -> dict:
        return summarize(list(self.samples))

    def drain(self) -> List[float]:
        out, self.samples = self.samples, []
        return out

    async def __aenter__(self) -> "LoopLagProbe":
        return self.start()

    async def __aexit__(self, *exc) -> None:
        await self.stop()


_APP_PROBE: Optional[LoopLagProbe] = None


async def start_app_probe() -> None:
    """Startup hook: the process-wide probe, reporting every report_s()."""
    global _APP_PROBE
    every = report_s()
    if every <= 0 or _APP_PROBE is not None:
        return
    _APP_PROBE = LoopLagProbe(report_every_s=every).start()


async def stop_app_probe() -> None:
    global _APP_PROBE
    probe, _APP_PROBE = _APP_PROBE, None
    if probe is not None:
        await probe.stop()
