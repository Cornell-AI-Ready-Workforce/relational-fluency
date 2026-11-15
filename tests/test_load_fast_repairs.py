"""Review regressions using real step orchestration and in-process doubles."""
import asyncio
import copy
import hashlib
from http.client import BadStatusLine, IncompleteRead
import json
from pathlib import Path
import time

import httpx
import pytest

from tools.load import fast, measure, run, smoke
from tools.load.observer import EncounterEvents
from test_load_fastmode import sample_step
from test_load_measure import REFERENCE
from test_load_run import FakeRuntime


def identity_client(monkeypatch):
    client = httpx.AsyncClient
    monkeypatch.setattr(run.httpx, "AsyncClient", lambda **kw: client(
        transport=httpx.MockTransport(lambda request: httpx.Response(
            307, headers={"location": "/v2?participant_id=p&run=r"})), **kw))


class Waiting:
    cancel_delay = 0

    def __init__(self, base, scenario, steps, **kw):
        self.active = False
        self.tl = {"session_id": "s_" + kw["index"], "scenario": scenario,
                   "completed": False, "lines": [], "hangup_requested": None,
                   "server_errors": []}

    async def run(self):
        self.active = True
        try:
            await asyncio.Event().wait()
        finally:
            self.active = False
            self.tl["hangup_requested"] = time.time()
            await asyncio.sleep(self.cancel_delay)


def test_early_stop_excludes_cancellation_tail_but_keeps_generator_invalid(monkeypatch, tmp_path):
    identity_client(monkeypatch)

    class SlowClose(Waiting):
        cancel_delay = 1.1

    async def exercise():
        rt = FakeRuntime(tmp_path)
        await rt.start()
        start = time.time()
        rt.event_rows = lambda: ([{"type": "voice_error", "where": "new", "wall": time.time()}]
                                 if time.time() - start > 1.1 else [])
        args = run.parse_args(["--scenarios", "S2A", "--warmup", "0.05"])
        return await asyncio.wait_for(run.run_step(rt, args, 5, 1, 600,
            participant_factory=SlowClose, reference=REFERENCE), 5)

    step = asyncio.run(exercise())
    stop = step["early_stop"]["t"]
    assert step["metrics"]["measurement_window"]["end"] == stop
    assert any(s["t"] > stop and s["active"] == 0 for s in step["metrics"]["concurrency_samples"])
    assert step["metrics"]["achieved_concurrency"] == 5
    assert fast.evaluate(step, REFERENCE)["verdict"] == "FAIL"
    step["errors"].append("independent generator failure")
    assert fast.evaluate(step, REFERENCE)["verdict"] == "INVALID"


@pytest.mark.parametrize("safety_failure", ["429", "observer exit"])
def test_live_guard_remains_active_during_settling_after_fail_fast(monkeypatch, tmp_path, safety_failure):
    identity_client(monkeypatch)

    class LiveRuntime(FakeRuntime):
        async def start(self):
            await super().start()
            self.in_settle, self.kill_seen = asyncio.Event(), asyncio.Event()
            self.participant_started = False
            self.settle_waiting = self.settle_timed_out = self.killed_during_settle = False

        def event_rows(self):
            return ([{"type": "voice_error", "where": "new", "wall": time.time()}]
                    if self.participant_started else [])

        def observer_telemetry(self):
            return {"failure": "observer log task ended" if safety_failure == "observer exit" and self.in_settle.is_set() else None}

        async def settled_sessions(self, ids):
            self.in_settle.set()
            self.settle_waiting = True
            if safety_failure == "429":
                row = {"upstream": "gateway", "purpose": "actor", "route": "chat_completions_stream",
                       "outcome": "http_error", "status": 429, "t": time.time()}
                (self.data / "upstream-attempts.jsonl").write_text(json.dumps(row) + "\n", encoding="utf-8")
            try:
                await asyncio.wait_for(self.kill_seen.wait(), 1.5)
            except asyncio.TimeoutError:
                self.settle_timed_out = True
                raise
            finally:
                self.settle_waiting = False
            return set()

        async def kill_server(self):
            self.killed = True
            self.killed_during_settle = self.settle_waiting and not self.settle_timed_out
            self.kill_seen.set()

    async def exercise():
        rt = LiveRuntime(tmp_path)
        await rt.start()
        class InFlight(Waiting):
            async def run(self):
                rt.participant_started = True
                return await super().run()
        args = run.parse_args(["--gateway", "live", "--allow-live", "--ramp", "1", "--scenarios", "S2A", "--warmup", "0"])
        step = await asyncio.wait_for(run.run_step(rt, args, 1, 1, 600,
            participant_factory=InFlight, reference=REFERENCE), 3)
        assert rt.killed and rt.in_settle.is_set()
        assert rt.killed_during_settle and not rt.settle_timed_out, "live guard killed only after the settle wait ended"
        return step

    step = asyncio.run(exercise())
    assert "new voice_error" in step["early_stop"]["reason"]
    assert step["live_stop"]["reason"] == ("first gateway 429" if safety_failure == "429" else "attempt telemetry unavailable")


@pytest.mark.parametrize("scenarios", [("S1A", "S3A"), ("S1A", "S2A", "S3A")])
@pytest.mark.parametrize("k", [1, 2])
def test_numeric_subset_baseline_covers_and_validates_each_scenario(monkeypatch, tmp_path, scenarios, k):
    identity_client(monkeypatch)
    original = run.run_step
    actual = []

    class Complete(Waiting):
        async def run(self):
            self.tl["completed"] = True
            return self.tl

    async def step(rt, args, n, repeat, ceiling, **kw):
        observed = await original(rt, args, n, repeat, ceiling, participant_factory=Complete,
                                  scenario_offset=kw.get("scenario_offset", 0), label=kw.get("label"))
        actual.append((n, observed["metrics"]["scenario_mix"]))
        result = sample_step(n, scenarios[0], k)
        result["metrics"]["scenario_mix"] = observed["metrics"]["scenario_mix"]
        result["metrics"]["by_scenario"] = {s: copy.deepcopy(sample_step()["metrics"]["by_scenario"]["S1A"])
                                              for s in observed["metrics"]["scenario_mix"]}
        return result

    monkeypatch.setattr(run, "run_step", step)
    args = run.parse_args(["--encounters-per-participant", str(k), "--scenarios", ",".join(scenarios),
                           "--ramp", "1,5", "--report-dir", str(tmp_path)])
    report, _ = asyncio.run(run.run_experiment(args, runtime_factory=FakeRuntime))
    assert report["verdict"] == "PASS" and report["capacity"] == 5
    units = report["steps"][0]["baseline_units"]
    assert len(units) == len(scenarios) and all(u["verdict"] == "PASS" for u in units)
    assert set().union(*(set(mix) for n, mix in actual if n == 1)) == set(scenarios)


def test_bisection_points_use_fresh_runtimes_after_failed_profile(monkeypatch, tmp_path):
    lifecycle, instances, calls = [], [], []

    class Tracked(FakeRuntime):
        def __init__(self, directory, **kw):
            super().__init__(directory, **kw)
            self.generation = len(instances)
            self.failed = False
            self.metadata = {"calibration": self.generation}
            instances.append(self)

        async def start(self):
            lifecycle.append(("start", self.generation))
            await super().start()

        async def stop(self):
            lifecycle.append(("stop", self.generation))

        async def profile_available(self):
            return True

    async def step(rt, args, n, repeat, ceiling, **kw):
        calls.append((n, rt.generation, kw.get("profile_only", False)))
        s = sample_step(n, k="all")
        if n == 20:
            rt.failed = True
            s["metrics"]["server_rss_bytes"]["max"] = 900_000_001
        else:
            assert not rt.failed, "failed server reused below failed N"
        s["profile"] = {"status": "sampled"}
        return s

    monkeypatch.setattr(run, "run_step", step)
    args = run.parse_args(["--ramp-search", "--report-dir", str(tmp_path)])
    report, _ = asyncio.run(run.run_experiment(args, runtime_factory=Tracked))
    assert calls == [(1, 0, False), (20, 0, False), (20, 0, True), (10, 1, False), (15, 2, False)]
    assert report["capacity"] == 15
    assert lifecycle == [("start", 0), ("stop", 0), ("start", 1), ("stop", 1), ("start", 2), ("stop", 2)]
    assert [r["host"]["calibration"] for r in report["ramps"][0]["runtime_history"]] == [0, 1, 2]
    assert [s["runtime_generation"] for s in report["steps"]] == [0, 0, 1, 2]
    assert "fresh runtime" in report["configuration"]["warmup_reset_policy"]
    assert report["ramps"][0]["search_path"][1]["next_runtime_reset"]


@pytest.mark.parametrize("cause", ["server error", "observation read error"])
def test_warmup_irreversible_errors_stop_with_specific_reason(monkeypatch, tmp_path, cause):
    identity_client(monkeypatch)

    class ServerFailure(Waiting):
        async def run(self):
            self.tl["server_errors"].append("recorded warmup server error")
            self.tl["server_error_events"] = [{"t": time.time(), "message": "recorded warmup server error"}]
            return await super().run()

    async def exercise():
        rt = FakeRuntime(tmp_path)
        await rt.start()
        def rows():
            if cause == "observation read error":
                raise OSError("tail failed")
            return [{"type": "error", "wall": time.time()}]
        rt.event_rows = rows
        args = run.parse_args(["--scenarios", "S2A", "--warmup", "30" if cause == "server error" else "0"])
        return await asyncio.wait_for(run.run_step(rt, args, 1, 1, 600,
            participant_factory=ServerFailure if cause == "server error" else Waiting, reference=REFERENCE), 2)

    step = asyncio.run(exercise())
    if cause == "observation read error":
        assert "incremental observation: OSError" == step["early_stop"]["reason"]
        assert fast.evaluate(step, REFERENCE)["verdict"] == "INVALID"
    else:
        assert step["early_stop"]["t"] < step["early_stop"]["measurement_start"]
        assert fast.evaluate(step, REFERENCE)["verdict"] == "FAIL"


def test_k4_rejected_in_favor_of_all(capsys):
    with pytest.raises(SystemExit):
        run.parse_args(["--encounters-per-participant", "4"])
    assert "use all" in capsys.readouterr().err


@pytest.mark.parametrize("failure", [BadStatusLine("bad"), IncompleteRead(b"partial")])
def test_smoke_http_framing_errors_are_reported_and_runtime_stops(tmp_path, failure):
    instances = []
    class BrokenProbe(FakeRuntime):
        def __init__(self, *a, **kw):
            super().__init__(*a, **kw)
            instances.append(self)
        async def keepalive_probe(self):
            raise failure
    args = run.parse_args([])
    result = asyncio.run(smoke.run_smoke(args, tmp_path, runtime_factory=BrokenProbe))
    assert result["verdict"] == "FAIL" and instances[0].stopped
    assert type(failure).__name__ in result["outcomes"][0]["reasons"][0]


def test_observer_drains_final_error_before_retiring_closed_session(monkeypatch, tmp_path):
    folder = tmp_path / "s"
    folder.mkdir()
    events = folder / "events.jsonl"
    events.write_text("", encoding="utf-8")
    manifest = folder / "manifest.json"
    manifest.write_text('{"status":"closed"}', encoding="utf-8")
    read = Path.read_text
    def close_race(path, *a, **kw):
        if path == manifest:
            with events.open("a", encoding="utf-8") as fh:
                fh.write('{"type":"voice_error","where":"final"}\n')
        return read(path, *a, **kw)
    monkeypatch.setattr(Path, "read_text", close_race)
    reader = EncounterEvents(tmp_path)
    rows = reader.read()
    assert [r["event"]["where"] for r in rows] == ["final"]
    assert events in reader.finished and reader.read() == []


def test_report_labels_subset_policy_and_records_fake_source(monkeypatch, tmp_path):
    async def step(rt, args, n, repeat, ceiling, **kw):
        return sample_step(k="all")
    monkeypatch.setattr(run, "run_step", step)
    args = run.parse_args(["--scenarios", "S2A", "--ramp", "1", "--report-dir", str(tmp_path)])
    report, _ = asyncio.run(run.run_experiment(args, runtime_factory=FakeRuntime))
    config = report["configuration"]
    assert "no study-run advance" in config["advance_policy"]
    assert config["warmup_reset_policy"].startswith("warmup per step;")
    assert config["fake_revision_sha256"] == hashlib.sha256((run.ROOT / "tools/load/fake_upstream.py").read_bytes()).hexdigest()
    assert "host (K=all)" in (tmp_path / "report.md").read_text(encoding="utf-8")


def test_smoke_window_excludes_identity_setup_and_completion_tail(monkeypatch, tmp_path):
    client = httpx.AsyncClient
    def handler(request):
        if request.url.path == "/test":
            return httpx.Response(307, headers={"location": "/v2?participant_id=p&run=r"})
        return httpx.Response(200, json={"current": {"id": "S1A"}})
    monkeypatch.setattr(run.httpx, "AsyncClient", lambda **kw: client(transport=httpx.MockTransport(handler), **kw))

    class Complete(Waiting):
        async def run(self):
            self.tl["started"] = time.time()
            await asyncio.sleep(0.03)
            self.tl.update(completed=True, completed_at=time.time())
            await asyncio.sleep(0.1)  # Playback/close/advance tail.
            return self.tl

    async def exercise():
        rt = FakeRuntime(tmp_path)
        await rt.start()
        return await run.run_step(rt, smoke.preset(run.parse_args([])), 1, 1, 60, participant_factory=Complete)

    step = asyncio.run(exercise())
    timeline = step["timelines"][0]
    assert step["metrics"]["measurement_window"] == {"start": timeline["started"], "end": timeline["completed_at"]}
    assert step["ended"] > timeline["completed_at"] + 0.08


def test_smoke_playback_evidence_covers_complete_encounters():
    from test_load_fastmode import healthy_smoke
    step = healthy_smoke()
    for i, t in enumerate(step["timelines"]):
        t["scenario"] = run.SCENARIOS[i % 4]
    step["metrics"]["whole_step"] = {"encounters": copy.deepcopy(step["metrics"]["encounters"])}
    for encounter in step["metrics"]["whole_step"]["encounters"]:
        encounter["lines_said"] = 1
    step["metrics"]["encounters"] = []
    step["metrics"]["by_scenario"] = {}
    assert smoke.diagnostic(step, {"passed": True, "same_socket": True})["verdict"] == "PASS"


@pytest.mark.parametrize("error_at", [1, 61], ids=["before-last-session", "after-first-completion"])
def test_smoke_rejects_whole_run_voice_errors_outside_concurrency_window(error_at):
    from test_load_fastmode import healthy_smoke
    step = healthy_smoke()
    m = step["metrics"]
    for i, timeline in enumerate(step["timelines"]):
        timeline["scenario"] = run.SCENARIOS[i % 4]
    whole = m["whole_step"] = {"encounters": copy.deepcopy(m["encounters"]), "voice_error": 0}
    for encounter in whole["encounters"]:
        encounter["lines_said"] = 1
    probe = {"passed": True, "same_socket": True}
    assert smoke.diagnostic(step, probe)["verdict"] == "PASS"
    events = {"0": [{"type": "voice_error", "where": "smoke-close", "wall": error_at}]}
    m["measurement_window"] = {"start": 2, "end": 60}
    m["voice_error"] = measure.event_metrics(measure.event_window(events, 2, 60), [], [])["voice_error"]
    whole["voice_error"] = measure.event_metrics(events, [], [])["voice_error"]
    assert m["voice_error"] == 0 and whole["voice_error"] == 1
    verdict = smoke.diagnostic(step, probe)
    assert verdict["verdict"] == "FAIL" and verdict["reasons"] == ["non-benign voice_error during smoke"]


def test_baseline_rule_describes_selected_scenarios():
    assert "per selected scenario" in fast.BASELINE_RULE
    assert "four sequential" not in fast.BASELINE_RULE
