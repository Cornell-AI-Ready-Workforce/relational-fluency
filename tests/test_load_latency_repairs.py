"""Latency review regressions. No upstream service or server HTTP is used."""
import asyncio
from dataclasses import asdict
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import time

import pytest
from fastapi.testclient import TestClient

from tools.load import fake_upstream as fake, latency, run
from tools.load.checkout import validate_checkout
from tools.load.runtime import ROOT, Runtime
from tools.load.tts_proof import install_guard, verify
from test_load_latency import checkout
from test_load_run import FakeRuntime


@pytest.mark.parametrize("broken,expected", [
    ("server/upstream_telemetry.py", "upstream_telemetry.py"),
    ("server/voice/cascade.py", "ELEVENLABS_WS_URL"),
    ("server/video.py", "STORAGE_PREFLIGHT"),
    ("server/reply_trace.py", "reply_trace.py"),
])
def test_incompatible_checkout_refused_before_process_or_key_access(tmp_path, monkeypatch, capsys, broken, expected):
    root = checkout(tmp_path)
    (root / broken).write_text("# ELEVENLABS_WS_URL STORAGE_PREFLIGHT are only comments\n", encoding="utf-8")
    async def forbidden(*a, **kw): raise AssertionError("process started during parse")
    monkeypatch.setattr(asyncio, "create_subprocess_exec", forbidden)
    with pytest.raises(SystemExit) as error:
        latency.parse_args(["--server-checkout", str(root), "--report-dir", str(tmp_path / "run"), "--allow-live"])
    assert error.value.code == 2 and expected in capsys.readouterr().err
    assert not (tmp_path / "run").exists()


def test_noop_telemetry_stub_is_not_a_compatible_checkout(tmp_path):
    root = checkout(tmp_path)
    (root / "server/upstream_telemetry.py").write_text("def add_listener(callback): pass\n", encoding="utf-8")
    with pytest.raises(ValueError, match="record, _Attempt"):
        Runtime(tmp_path / "run", backend="local", server_checkout=root)


def test_real_upstream_seam_is_used_and_trace_contract_is_optional_outside_latency(tmp_path):
    root = checkout(tmp_path)
    for name in ("upstream_telemetry.py", "voice/cascade.py", "video.py"):
        assert (root / "server" / name).read_bytes() == (ROOT / "server" / name).read_bytes()
    (root / "server/reply_trace.py").unlink()
    assert validate_checkout(root) == root
    with pytest.raises(ValueError, match="reply_trace.py"):
        validate_checkout(root, require_traces=True)


def test_unversioned_archived_config_round_trips_as_coherent_legacy(tmp_path):
    original = asdict(fake.FakeConfig(tts_profile="legacy"))
    archived = {key: value for key, value in original.items() if key not in {
        "tts_profile", "tts_connect_ms", "tts_close_ms", "tts_final_flush_only",
        "actor_sse_mode", "actor_replies", "stt_plan"}}
    path = tmp_path / "fake-config.json"
    path.write_text(json.dumps(archived), encoding="utf-8")
    restored = fake.load_config(path)
    assert asdict(restored) == original
    assert restored.tts_profile == "legacy" and restored.tts_connect_ms == fake.Distribution(0, 0)
    assert fake.load_config().tts_profile == "calibrated"
    path.write_text(json.dumps(asdict(restored)), encoding="utf-8")
    assert asdict(fake.load_config(path)) == original


def test_legacy_optional_connect_and_close_do_not_consume_seeded_draws():
    app = fake.create_app(fake.FakeConfig(tts_profile="legacy"))
    before = app.state.fake.rng.getstate()
    with TestClient(app) as client:
        with client.websocket_connect("/v1/text-to-dialogue/stream-input?output_format=pcm_24000&model_id=fake") as ws:
            ws.send_json({"voices": ["v"]})
            ws.send_json({"close_socket": True})
            assert ws.receive_json() == {"is_final": True}
    assert before == app.state.fake.rng.getstate()


@pytest.mark.parametrize("fake_count,allowed,blocked,passed", [
    (2, 2, 0, True), (0, 2, 0, False), (1, 2, 0, False), (3, 2, 0, False),
    (2, 0, 0, False), (2, 2, 1, False),
])
def test_fake_tts_proof_requires_exact_independent_counts(fake_count, allowed, blocked, passed):
    attempts = [{"upstream": "elevenlabs", "route": "tts_ws"}] * 2 + [
        {"upstream": "elevenlabs", "detail": "slot_wait"}, {"upstream": "gateway"}]
    destinations = [{"allowed": True}] * allowed + [{"allowed": False}] * blocked
    result = verify(fake_count, attempts, destinations)
    assert result["passed"] is passed and result["server_elevenlabs_attempts"] == 2


def test_destination_guard_blocks_real_elevenlabs_without_calling_transport(monkeypatch, tmp_path):
    import websockets
    from websockets.asyncio import client
    from websockets.legacy import client as legacy
    calls = []
    sentinel = object()
    def connect(uri, *a, **kw): calls.append(uri); return sentinel
    for module in (websockets, client, legacy): monkeypatch.setattr(module, "connect", connect)
    path = tmp_path / "destinations.jsonl"
    install_guard("wss://fake.invalid:8443/tts", path, record_allowed=True)
    for module in (websockets, client, legacy):
        assert module.connect("wss://fake.invalid:8443/tts?model_id=fake") is sentinel
        with pytest.raises(RuntimeError, match="blocked non-fake"):
            module.connect("wss://api.elevenlabs.io/tts?secret=must-not-log")
    assert len(calls) == 3
    rows = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
    assert sum(not row["allowed"] for row in rows) == 3
    assert "must-not-log" not in path.read_text(encoding="utf-8")


@pytest.mark.parametrize("fault", [None, "zero", "mismatch", "real", "units", "lost-reply", "startup-429", "final-429", "startup-401", "final-401"])
def test_latency_run_enforces_tts_trace_and_full_journal_guards(monkeypatch, tmp_path, capsys, fault):
    root = checkout(tmp_path)
    instances = []
    class RuntimeDouble(FakeRuntime):
        def __init__(self, *a, **kw): super().__init__(*a, **kw); instances.append(self)
        async def start(self):
            await super().start()
            row = {"upstream": "gateway", "route": "models", "purpose": "preflight", "t": time.time()}
            if fault in {"startup-429", "startup-401"}: row.update(status=int(fault.rsplit("-", 1)[1]), outcome="http_error")
            (self.data / "upstream-attempts.jsonl").write_text(json.dumps(row) + "\n", encoding="utf-8")
        async def fake_stats(self):
            return {"counts": {"tts_ws": 0 if fault == "zero" else 2 if fault == "mismatch" else 1}, "errors": []}

    calls = []
    async def step(rt, args, *a, **kw):
        calls.append(True)
        folder = rt.data / "sessions/s"
        folder.mkdir(parents=True)
        rows = [{"type": kind, "schema_version": 1, "clock": "encounter_s", "units": "milliseconds" if fault == "units" else "seconds",
                 "finalized_at": "encounter_teardown"} for kind in ("reply_trace", "commit_trace", "turn_timing")]
        (folder / "events.jsonl").write_text("".join(json.dumps(r) + "\n" for r in rows), encoding="utf-8")
        with (rt.data / "upstream-attempts.jsonl").open("a", encoding="utf-8") as fh:
            fh.write('{"upstream":"elevenlabs","route":"tts_ws","purpose":"tts"}\n')
            if fault in {"final-429", "final-401"}:
                fh.write(json.dumps({"upstream": "gateway", "route": "chat_completions_stream", "purpose": "actor",
                                    "status": int(fault.rsplit("-", 1)[1]), "outcome": "http_error", "t": time.time()}) + "\n")
        destinations = [{"allowed": True}]
        if fault == "real": destinations.append({"allowed": False, "destination": ["wss", "api.elevenlabs.io", 443, "/tts"]})
        (rt.data / "tts-destinations.jsonl").write_text("".join(json.dumps(r) + "\n" for r in destinations), encoding="utf-8")
        return {"timelines": [{"session_id": "s", "scenario": "S1A", "lines": [{}],
                "replies_started": 2 if fault == "lost-reply" else 1}], "metrics": {"settled_artifacts": True},
                "errors": [], "server_errors": [], "fake_errors": [], "live_stop": None}
    monkeypatch.setattr(run, "run_step", step)
    args = latency.parse_args(["--server-checkout", str(root), "--report-dir", str(tmp_path / "run"),
                              "--allow-live", "--turns", "1", "--scenarios", "S1A"])
    report, _ = asyncio.run(latency.run_latency(args, runtime_factory=RuntimeDouble, key_loader=lambda: "mock-only"))
    assert report["verdict"] == ("PASS" if fault is None else "FAIL")
    assert instances[0].stopped and instances[0].killed
    assert "Verdict: " + report["verdict"] in capsys.readouterr().out
    if fault in {"startup-429", "final-429", "startup-401", "final-401"}:
        assert report["live_stop"]["reason"] == "first gateway " + fault.rsplit("-", 1)[1]
        assert bool(calls) == fault.startswith("final-")
    if fault in {"zero", "mismatch", "real"}: assert not report["tts_proof"]["passed"]
    if fault == "lost-reply": assert any("observed replies" in r for r in report["errors"])


def test_abandoned_runtime_is_rejected_before_startup(tmp_path, capsys):
    root = checkout(tmp_path)
    (tmp_path / "run/runtime/server-data").mkdir(parents=True)
    with pytest.raises(SystemExit):
        latency.parse_args(["--server-checkout", str(root), "--report-dir", str(tmp_path / "run"), "--allow-live"])
    assert "fresh and empty" in capsys.readouterr().err


@pytest.mark.skipif(sys.platform == "win32", reason="load harness requires POSIX signal handlers and SIGTERM delivery")
def test_sigterm_cleans_up_children_and_writes_failed_report(tmp_path):
    root = checkout(tmp_path)
    ready, cleaned, directory = tmp_path / "ready.json", tmp_path / "cleaned", tmp_path / "run"
    # Real child processes, but no HTTP server, provider or network access.
    code = f'''
import asyncio, functools, json, sys
from pathlib import Path
from tools.load import latency
from tools.load.runtime import Runtime
class Children(Runtime):
    async def start(self):
        self.data.mkdir(parents=True)
        self.processes = [await asyncio.create_subprocess_exec(sys.executable, '-c', 'import time; time.sleep(60)') for _ in range(2)]
        self.server_pid = self.processes[-1].pid
    async def fake_stats(self): return {{'counts': {{'tts_ws': 0}}}}
    async def stop(self):
        await super().stop()
        Path({str(cleaned)!r}).write_text('stopped', encoding='utf-8')
async def step(rt, *a, **kw):
    Path({str(ready)!r}).write_text(json.dumps([p.pid for p in rt.processes]), encoding='utf-8')
    await asyncio.Event().wait()
latency.run.run_step = step
latency.run_latency = functools.partial(latency.run_latency, runtime_factory=Children)
raise SystemExit(latency.main(['--server-checkout', {str(root)!r}, '--report-dir', {str(directory)!r}, '--gateway', 'fake', '--turns', '1', '--scenarios', 'S1A']))
'''
    process = subprocess.Popen([sys.executable, "-c", code], cwd=ROOT, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    pids = []
    try:
        deadline = time.monotonic() + 8
        while not ready.exists() and process.poll() is None and time.monotonic() < deadline:
            time.sleep(.01)
        assert ready.exists(), process.communicate(timeout=2)
        pids = json.loads(ready.read_text(encoding="utf-8"))
        process.send_signal(signal.SIGTERM)
        out, err = process.communicate(timeout=10)
        assert process.returncode == 1 and cleaned.exists(), (out, err)
        assert "Verdict: INVALID" in out and (directory / "report.json").exists()
        for pid in pids:
            with pytest.raises(ProcessLookupError): os.kill(pid, 0)
    finally:
        if process.poll() is None: process.kill(); process.wait()
        for pid in pids:
            try: os.kill(pid, signal.SIGKILL)
            except ProcessLookupError: pass
