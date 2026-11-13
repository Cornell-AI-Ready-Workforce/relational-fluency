import asyncio
import json
import time

import httpx

from tools.load import run
from tools.load.runtime import read_encounters
from test_load_measure import valid_metrics


class FakeRuntime:
    def __init__(self, directory, **kwargs):
        self.directory = directory
        self.data = directory / "server-data"
        self.base = "http://server"
        self.key = "synthetic"
        self.isolation = {"label": "mocked runtime, not a capacity result", "passed": False}
        self.health = {"gateway": {"ok": True}}
        self.image = None
        self.stopped = False
        self.killed = False
        self.metadata = {}
    async def start(self):
        self.data.mkdir(parents=True)
        return self
    async def stop(self): self.stopped = True
    async def kill_server(self): self.killed = True
    async def settled(self, timelines): return True
    async def export(self, timelines): pass
    async def profile_available(self): return False
    async def resources(self): return {"t": time.time(), "cpu_percent": 5, "rss_bytes": 1000}
    async def fake_stats(self): return {"errors": []}
    async def profile(self, *args, **kwargs): return {"status": "unavailable", "reason": "test"}


def test_report_repeats_baseline_capacity_and_first_failure_profile(monkeypatch, tmp_path):
    calls = []
    async def step(runtime, args, n, repeat, ceiling, **kwargs):
        calls.append((n, repeat))
        metrics = valid_metrics()
        metrics["achieved_concurrency"] = n
        if n == 5:
            metrics["speech_end_to_first_played_s"]["p90"] = 10
            metrics["latency_samples_s"] = [10]
        return {"concurrency": n, "repeat": repeat, "metrics": metrics, "errors": [],
                "fake_errors": [], "full_length": True, "live_stop": None,
                "profile": {"status": "sampled", "native": False, "top20": [{"frame": "hot", "self_samples": 3}]}}
    monkeypatch.setattr(run, "run_step", step)
    args = run.parse_args(["--ramp", "1,5", "--repeat", "2", "--scenarios", "S2A", "--report-dir", str(tmp_path)])
    report, directory = asyncio.run(run.run_experiment(args, runtime_factory=FakeRuntime))
    assert calls == [(1, 1), (1, 2), (5, 1)]
    assert report["capacity"] == 1 and report["verdict"] == "FAIL"
    assert report["first_failure_profile"]["concurrency"] == 5
    assert report["first_failure_profile"]["status"] == "unavailable"
    assert json.loads((directory / "report.json").read_text(encoding="utf-8"))["capacity"] == 1
    assert "capacity for the tested configuration" in (directory / "report.md").read_text(encoding="utf-8").lower()


def test_live_stop_prevents_any_later_step_with_mock_key_only(monkeypatch, tmp_path):
    calls = []
    monkeypatch.setattr(run, "load_key", lambda: "mock-only")
    async def step(runtime, args, n, repeat, ceiling, **kwargs):
        calls.append(n)
        metrics = valid_metrics()
        metrics["achieved_concurrency"] = n
        return {"concurrency": n, "repeat": repeat, "metrics": metrics, "errors": [],
                "fake_errors": [], "full_length": True, "profile": {"status": "unavailable"},
                "live_stop": {"reason": "first gateway 429", "route": "messages", "purpose": "director", "concurrency": n}}
    monkeypatch.setattr(run, "run_step", step)
    args = run.parse_args(["--gateway", "live", "--allow-live", "--ramp", "1,5", "--repeat", "2",
                           "--scenarios", "S2A", "--report-dir", str(tmp_path)])
    report, _ = asyncio.run(run.run_experiment(args, runtime_factory=FakeRuntime))
    assert calls == [1] and report["verdict"] == "FAIL" and report["capacity"] is None
    assert report["live_stop"]["purpose"] == "director"


def test_live_429_watcher_cancels_inflight_participant(monkeypatch, tmp_path):
    real_client = httpx.AsyncClient
    def handle(request):
        return httpx.Response(307, headers={"location": "/v2?participant_id=p_test&run=r_test"})
    monkeypatch.setattr(run.httpx, "AsyncClient", lambda **kwargs: real_client(
        transport=httpx.MockTransport(handle), **kwargs))
    monkeypatch.setattr(run, "read_encounters", lambda *args: ({}, {}, True))
    instance = FakeRuntime(tmp_path)

    class WaitingParticipant:
        def __init__(self, *args, **kwargs):
            self.active = False
            self.tl = {"session_id": "s_1_abcdef", "scenario": "S2A", "lines": [],
                       "completed": False, "hangup_requested": None}
        async def run(self):
            self.active = True
            row = {"upstream": "gateway", "route": "chat_completions_stream", "purpose": "actor",
                   "outcome": "http_error", "status": 429, "duration_ms": 1, "t": time.time()}
            await asyncio.to_thread((instance.data / "upstream-attempts.jsonl").write_text,
                                    json.dumps(row) + "\n", encoding="utf-8")
            try:
                await asyncio.Event().wait()
            finally:
                self.active = False
                self.tl["hangup_requested"] = time.time()

    async def execute():
        await instance.start()
        args = run.parse_args(["--gateway", "live", "--allow-live", "--ramp", "1", "--scenarios", "S2A",
                               "--warmup", "0", "--close-timeout", "1"])
        return await asyncio.wait_for(run.run_step(instance, args, 1, 1, 600,
                                                   participant_factory=WaitingParticipant), 3)
    result = asyncio.run(execute())
    assert result["live_stop"]["reason"] == "first gateway 429"
    assert result["live_stop"]["route"] == "chat_completions_stream"
    assert result["live_stop"]["concurrency"] == 1
    assert instance.killed


def test_close_burst_requires_all_sessions_closed_and_all_mixes_rendered(tmp_path):
    timelines = [{"session_id": "s_1_abcdef"}, {"session_id": "s_2_abcdef"}]
    for timeline in timelines:
        directory = tmp_path / "sessions" / timeline["session_id"]
        directory.mkdir(parents=True)
        (directory / "events.jsonl").write_text('{"type":"session_end"}\n', encoding="utf-8")
        (directory / "manifest.json").write_text('{"status":"closed"}', encoding="utf-8")
    first = tmp_path / "sessions" / timelines[0]["session_id"]
    (first / "conversation_audio.wav").write_bytes(b"mix")
    assert read_encounters(tmp_path, timelines)[2] is False
    second = tmp_path / "sessions" / timelines[1]["session_id"]
    (second / "conversation_audio.wav").write_bytes(b"mix")
    events, sizes, settled = read_encounters(tmp_path, timelines)
    assert settled and len(events) == len(sizes) == 2


def test_concurrency_sampler_is_not_delayed_by_resource_collection(monkeypatch, tmp_path):
    real_client = httpx.AsyncClient
    monkeypatch.setattr(run.httpx, "AsyncClient", lambda **kwargs: real_client(
        transport=httpx.MockTransport(lambda request: httpx.Response(307, headers={
            "location": "/v2?participant_id=p_test&run=r_test"})), **kwargs))
    monkeypatch.setattr(run, "read_encounters", lambda *args: ({}, {}, True))
    instance = FakeRuntime(tmp_path)
    async def slow_resources():
        await asyncio.sleep(2)
        return {"t": time.time(), "cpu_percent": 1, "rss_bytes": 100}
    instance.resources = slow_resources

    class ShortParticipant:
        def __init__(self, *args, **kwargs):
            self.active = False
            self.tl = {"session_id": "s_1_abcdef", "scenario": "S2A", "lines": [],
                       "completed": True, "hangup_requested": None}
        async def run(self):
            self.active = True
            await asyncio.sleep(1.15)
            self.active = False
            self.tl["hangup_requested"] = time.time()
            return self.tl

    async def execute():
        await instance.start()
        args = run.parse_args(["--ramp", "1", "--scenarios", "S2A", "--warmup", "0"])
        return await run.run_step(instance, args, 1, 1, 600, participant_factory=ShortParticipant)
    result = asyncio.run(execute())
    samples = result["metrics"]["concurrency_samples"]
    assert len(samples) >= 2 and any(sample["active"] == 1 for sample in samples)


def test_default_workers_follow_server_williams_order_reuse_identity_and_advance(monkeypatch, tmp_path):
    real_client = httpx.AsyncClient
    assigned = ["S3A", "S4A", "S2A", "S1A"]
    index = [0]
    calls, identities = [], []
    def http(request):
        if request.url.path == "/test":
            return httpx.Response(307, headers={"location": "/v2?participant_id=p_test&run=r_test"})
        if request.method == "POST":
            assert request.url.path == "/api/run/r_test/advance"
            index[0] += 1
        return httpx.Response(200, json={"current": {"id": assigned[index[0]]} if index[0] < 4 else None})
    monkeypatch.setattr(run.httpx, "AsyncClient", lambda **kwargs: real_client(
        transport=httpx.MockTransport(http), **kwargs))
    monkeypatch.setattr(run, "read_encounters", lambda *args: ({}, {}, True))
    async def closed(*args): return True
    monkeypatch.setattr(run, "_closed", closed)
    instance = FakeRuntime(tmp_path)
    class FinishedParticipant:
        def __init__(self, base, scenario, steps, **kwargs):
            calls.append(scenario)
            identities.append(kwargs["identity"])
            self.active = False
            self.tl = {"session_id": f"s_{len(calls)}_abcdef", "scenario": scenario, "lines": [],
                       "completed": True, "hangup_requested": None}
        async def run(self):
            self.tl["hangup_requested"] = time.time()
            return self.tl
    async def execute():
        await instance.start()
        args = run.parse_args(["--ramp", "1", "--warmup", "0"])
        return await run.run_step(instance, args, 1, 1, 600, participant_factory=FinishedParticipant)
    result = asyncio.run(execute())
    assert calls == assigned and index[0] == 4
    assert all(identity is identities[0] for identity in identities)
    assert len(result["metrics"]["encounters"]) == 4


def test_failed_admission_does_not_wait_for_nonexistent_session_close(monkeypatch, tmp_path):
    real_client = httpx.AsyncClient
    monkeypatch.setattr(run.httpx, "AsyncClient", lambda **kwargs: real_client(
        transport=httpx.MockTransport(lambda request: httpx.Response(401)), **kwargs))
    instance = FakeRuntime(tmp_path)
    async def execute():
        await instance.start()
        args = run.parse_args(["--ramp", "1", "--scenarios", "S2A"])
        return await asyncio.wait_for(run.run_step(instance, args, 1, 1, 600), 1)
    result = asyncio.run(execute())
    assert result["metrics"]["close_burst_s"] is None and result["errors"]
