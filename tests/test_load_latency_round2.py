"""Round-2 live policy, swallowed cancellation, and journal-scope regressions."""
import asyncio
import json
import signal
import subprocess
import sys
import time

import httpx
import pytest

from tools.load import latency, measure, run, smoke, tts_proof
from tools.load.runtime import ROOT, Runtime, environment
from test_load_fastmode import sample_step
from test_load_latency import checkout
from test_load_run import FakeRuntime


OK = {"upstream": "gateway", "route": "chat_completions_stream", "purpose": "actor", "outcome": "ok", "status": 200}
TIMEOUT = {"upstream": "gateway", "route": "messages", "purpose": "steering", "outcome": "timeout",
           "status": None, "detail": "APITimeoutError"}


def test_one_timeout_at_attempt_four_does_not_stop_below_or_at_sample_floor():
    guard = measure.LiveGuard()
    for i in range(40):
        assert guard.observe(TIMEOUT if i == 3 else OK, 1) is None
    assert (guard.attempts, guard.errors) == (39, 0)
    assert (guard.gateway_attempts, guard.gateway_errors) == (40, 1)


def test_two_errors_in_twenty_five_gateway_attempts_stop():
    guard = measure.LiveGuard()
    for i in range(25):
        stop = guard.observe({**TIMEOUT, "purpose": "stt"} if i in {3, 24} else OK, 1)
        assert bool(stop) == (i == 24)
    assert stop["reason"] == "gateway attempt error rate > 5%"
    assert (stop["attempts"], stop["errors"]) == (25, 2)


@pytest.mark.parametrize("status", [429, 401, 403])
def test_rate_grace_never_delays_throttle_or_authorization_stop(status):
    guard = measure.LiveGuard()
    stop = guard.observe({**OK, "status": status, "outcome": "http_error"}, 1)
    assert stop["reason"] == f"first gateway {status}" and stop["attempts"] == 1


def test_sample_floor_is_twenty_and_slot_only_rows_never_count():
    guard = measure.LiveGuard()
    for _ in range(30):
        assert guard.observe({**TIMEOUT, "detail": "slot_wait"}, 1) is None
        assert guard.observe({"upstream": "elevenlabs", "outcome": "timeout"}, 1) is None
    for i in range(19):
        assert guard.observe({**TIMEOUT, "purpose": "actor"} if i in {3, 18} else OK, 1) is None
    stop = guard.observe(OK, 1)
    assert stop and stop["attempts"] == 20 and stop["errors"] == 2


def test_real_step_watcher_allows_early_steering_timeout(monkeypatch, tmp_path):
    client = httpx.AsyncClient
    monkeypatch.setattr(run.httpx, "AsyncClient", lambda **kw: client(transport=httpx.MockTransport(
        lambda request: httpx.Response(307, headers={"location": "/v2?participant_id=p&run=r"})), **kw))
    rt = FakeRuntime(tmp_path)
    class Participant:
        def __init__(self, base, scenario, steps, **kw):
            self.active = False
            self.tl = {"session_id": "s", "scenario": scenario, "lines": [], "completed": True, "hangup_requested": None}
        async def run(self):
            self.active = True
            (rt.data / "upstream-attempts.jsonl").write_text("".join(json.dumps(row) + "\n" for row in [OK, OK, OK, TIMEOUT]), encoding="utf-8")
            await asyncio.sleep(.25)
            self.active = False
            self.tl["hangup_requested"] = time.time()
            return self.tl
    async def exercise():
        await rt.start()
        args = run.parse_args(["--gateway", "live", "--allow-live", "--ramp", "1", "--scenarios", "S1A", "--duration", "1", "--warmup", "0"])
        return await run.run_step(rt, args, 1, 1, 600, participant_factory=Participant)
    result = asyncio.run(exercise())
    assert not result["live_stop"] and not rt.killed
    assert len(result["gateway_errors"]) == 1 and result["gateway_errors"][0]["purpose"] == "steering"


@pytest.mark.parametrize("steering_errors", [1, 30])
def test_latency_pass_keeps_steering_timeouts_in_both_reports_and_runs_both_scenarios(monkeypatch, tmp_path, steering_errors):
    root = checkout(tmp_path)
    calls = []
    class RT(FakeRuntime):
        async def start(self):
            await super().start()
            self.tts = 0
            (self.data / "upstream-attempts.jsonl").write_text("".join(json.dumps(OK) + "\n" for _ in range(3)), encoding="utf-8")
        async def fake_stats(self): return {"counts": {"tts_ws": self.tts}}
    async def step(rt, args, *a, **kw):
        calls.append(args.scenarios[0])
        sid = args.scenarios[0]
        folder = rt.data / "sessions" / sid
        folder.mkdir(parents=True)
        rows = [{"type": k, "schema_version": 1, "clock": "encounter_s", "units": "seconds",
                 "finalized_at": "encounter_teardown"} for k in ("reply_trace", "commit_trace", "turn_timing")]
        (folder / "events.jsonl").write_text("".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8")
        with (rt.data / "upstream-attempts.jsonl").open("a", encoding="utf-8") as fh:
            if steering_errors == 30:
                gateway_rows = [TIMEOUT] * 17 if len(calls) == 1 else [TIMEOUT] * 13 + [OK] * 7
            else:
                gateway_rows = [TIMEOUT] + [OK] * 16 if len(calls) == 1 else [OK] * 20
            for row in gateway_rows: fh.write(json.dumps(row) + "\n")
            fh.write('{"upstream":"elevenlabs","route":"tts_ws"}\n')
        with (rt.data / "tts-destinations.jsonl").open("a", encoding="utf-8") as fh: fh.write('{"allowed":true}\n')
        rt.tts += 1
        return {"timelines": [{"session_id": sid, "scenario": sid, "lines": [{}]}], "metrics": {"settled_artifacts": True},
                "errors": [], "server_errors": [], "fake_errors": [], "live_stop": None}
    monkeypatch.setattr(run, "run_step", step)
    args = latency.parse_args(["--server-checkout", str(root), "--report-dir", str(tmp_path / "run"), "--allow-live", "--turns", "1"])
    report, directory = asyncio.run(latency.run_latency(args, runtime_factory=RT, key_loader=lambda: "mock-only"))
    assert report["verdict"] == "PASS" and calls == ["S1A", "S2A"]
    assert report["gateway_request_count"] == 40 and len(report["gateway_errors"]) == steering_errors
    assert report["configuration"]["live_guard_counted_purposes"] == list(measure.LIVE_RATE_PURPOSES)
    row = report["gateway_errors"][0]
    for key in ("route", "purpose", "outcome", "detail"):
        assert row[key] == TIMEOUT[key] and TIMEOUT[key] in (directory / "report.md").read_text(encoding="utf-8")
    assert json.loads((directory / "report.json").read_text(encoding="utf-8"))["gateway_errors"] == report["gateway_errors"]


def test_capacity_pass_also_reports_recovered_gateway_error(monkeypatch, tmp_path):
    async def step(rt, args, n, repeat, ceiling, **kw):
        (rt.data / "upstream-attempts.jsonl").write_text(json.dumps(TIMEOUT) + "\n", encoding="utf-8")
        return sample_step(n, k="all")
    monkeypatch.setattr(run, "run_step", step)
    args = run.parse_args(["--ramp", "1", "--scenarios", "S1A", "--report-dir", str(tmp_path)])
    report, directory = asyncio.run(run.run_experiment(args, runtime_factory=FakeRuntime))
    assert report["verdict"] == "PASS" and len(report["gateway_errors"]) == 1
    assert report["configuration"]["live_guard_counted_purposes"] == list(measure.LIVE_RATE_PURPOSES)
    assert "steering" in (directory / "report.md").read_text(encoding="utf-8")


@pytest.mark.parametrize("phase,signals", [("workers", 1), ("workers", 2), ("cleanup", 2)])
@pytest.mark.skipif(sys.platform == "win32", reason="load harness requires POSIX signal handlers and SIGTERM delivery")
def test_sigterm_probe_cannot_pass_when_cancellation_is_absorbed(tmp_path, phase, signals):
    # Adapted from scratchpad/r2/probe_sigterm_lost.py. Signals stay in this
    # subprocess, never in pytest. Both normal traces and TTS proof would pass.
    root = checkout(tmp_path)
    directory = tmp_path / "run"
    code = f'''
import asyncio, functools, json, os, signal, sys
from pathlib import Path
sys.path.insert(0, {str(ROOT / 'tests')!r})
from tools.load import latency, run
from test_load_run import FakeRuntime
class RT(FakeRuntime):
    async def start(self):
        await super().start()
        self.tts = 0
        self.owner = asyncio.current_task()
    async def fake_stats(self): return {{'counts': {{'tts_ws': self.tts}}}}
    async def stop(self):
        if {phase!r} == 'cleanup':
            for i in range({signals}): asyncio.get_running_loop().call_later(.02 * (i+1), os.kill, os.getpid(), signal.SIGTERM)
            await asyncio.sleep(.1)
        self.stopped = True
        print('owner cancellation count', self.owner.cancelling())
calls = []
async def step(rt, args, *a, **kw):
    calls.append(args.scenarios[0])
    async def workers_done():
        for future in asyncio.as_completed([asyncio.create_task(asyncio.sleep(.2)) for _ in range(2)]):
            try: await future
            except asyncio.CancelledError: continue
    if len(calls) == 1 and {phase!r} == 'workers':
        for i in range({signals}): asyncio.get_running_loop().call_later(.02 * (i+1), os.kill, os.getpid(), signal.SIGTERM)
    await asyncio.create_task(workers_done())
    sid = args.scenarios[0]
    folder = rt.data / 'sessions' / sid
    folder.mkdir(parents=True)
    rows = [{{'type': kind, 'schema_version': 1, 'clock': 'encounter_s', 'units': 'seconds', 'finalized_at': 'encounter_teardown'}} for kind in ('reply_trace','commit_trace','turn_timing')]
    (folder / 'events.jsonl').write_text(''.join(json.dumps(row)+'\\n' for row in rows), encoding='utf-8')
    with (rt.data / 'upstream-attempts.jsonl').open('a', encoding='utf-8') as fh: fh.write('{{"upstream":"elevenlabs","route":"tts_ws"}}\\n')
    with (rt.data / 'tts-destinations.jsonl').open('a', encoding='utf-8') as fh: fh.write('{{"allowed":true}}\\n')
    rt.tts += 1
    return {{'timelines':[{{'session_id':sid,'scenario':sid,'lines':[{{}}],'replies_started':1}}], 'metrics':{{'settled_artifacts':True}}, 'errors':[], 'server_errors':[], 'fake_errors':[], 'live_stop':None}}
run.run_step = step
latency.run_latency = functools.partial(latency.run_latency, runtime_factory=RT)
raise SystemExit(latency.main(['--server-checkout',{str(root)!r},'--report-dir',{str(directory)!r},'--gateway','fake','--turns','1']))
'''
    result = subprocess.run([sys.executable, "-c", code], cwd=ROOT, capture_output=True, text=True, timeout=10)
    assert result.returncode == 1, (result.stdout, result.stderr)
    report = json.loads((directory / "report.json").read_text(encoding="utf-8"))
    assert report["verdict"] == "INVALID" and report["interrupted"] and report["tts_proof"]["passed"]
    assert f"owner cancellation count {signals}" in result.stdout
    assert len(report["steps"]) == (1 if phase == "workers" else 2)


@pytest.mark.parametrize("record_allowed", [False, True])
def test_guard_always_blocks_but_only_latency_journals_allowed(monkeypatch, tmp_path, record_allowed):
    import websockets
    from websockets.asyncio import client
    from websockets.legacy import client as legacy
    calls = []
    for module in (websockets, client, legacy):
        monkeypatch.setattr(module, "connect", lambda uri, **kw: calls.append(uri))
    path = tmp_path / "destinations.jsonl"
    tts_proof.install_guard("wss://fake.invalid/tts", path, record_allowed=record_allowed)
    websockets.connect("wss://fake.invalid/tts")
    assert path.exists() is record_allowed
    with pytest.raises(RuntimeError, match="blocked non-fake"):
        websockets.connect("wss://api.elevenlabs.io/tts")
    rows = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
    assert len(rows) == (2 if record_allowed else 1) and rows[-1]["allowed"] is False
    assert len(calls) == 1
    env = environment(gateway="fake", fake_url="https://fake.invalid", key="test", data_dir="/tmp/data",
                      production={"REALTIME_MODEL": "cascade-argon-elevenlabs"}, record_tts_destinations=record_allowed)
    assert env["RF_LOAD_FAKE_TTS_ONLY"] == "1" and env["RF_LOAD_TTS_JOURNAL_ALLOWED"] == str(int(record_allowed))
    assert Runtime(tmp_path / "default").record_tts_destinations is False
