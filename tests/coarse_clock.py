"""pytest plugin: Windows' clocks before Python 3.13, on any machine.

    PYTHONPATH=tests python -m pytest -p coarse_clock [tests...]

Off unless asked for: it is not a conftest and its name is not test_*.py.

time.time() and time.monotonic() advance in steps of COARSE_CLOCK_Q seconds
(default 1/64 s = 15.625 ms, GetSystemTimeAsFileTime / GetTickCount64), so two
readings a few microseconds apart are equal. asyncio's loop reads
time.monotonic() and its clock resolution from time.get_clock_info, so the
event loop runs on the same coarse clock. Loaded before any test module, so
every `import time; time.time()` call sees it.
"""
import math
import os
import time
import types

Q = float(os.environ.get("COARSE_CLOCK_Q", 1 / 64))
_time, _mono, _time_ns, _mono_ns, _info = (time.time, time.monotonic, time.time_ns,
                                           time.monotonic_ns, time.get_clock_info)


def _q(v):
    return math.floor(v / Q) * Q


time.time = lambda: _q(_time())
time.monotonic = lambda: _q(_mono())
time.time_ns = lambda: int(_q(_time_ns() / 1e9) * 1e9)
time.monotonic_ns = lambda: int(_q(_mono_ns() / 1e9) * 1e9)


def _clock_info(name):
    info = _info(name)
    if name in ("time", "monotonic"):
        return types.SimpleNamespace(adjustable=info.adjustable, implementation=info.implementation + " (coarse)",
                                     monotonic=info.monotonic, resolution=Q)
    return info


time.get_clock_info = _clock_info


def pytest_report_header(config):
    return f"coarse_clock: time.time/monotonic step {Q * 1000:.3f} ms"
