"""Selected server checkout, room smoke, and real room/fake-upstream contracts."""
import asyncio
import json
from pathlib import Path

import httpx
import httpx2
import pytest
from fastapi.testclient import TestClient

from tools.load import fast, run, smoke
from tools.load.checkout import revision
from tools.load.fake_upstream import create_app
from tools.load.runtime import ROOT
from tools.load.tts_proof import verify
from test_load_fastmode import healthy_smoke, sample_step
from test_load_fake_upstream import SocketAdapter, fast_config, isolated_llm
from test_load_latency import checkout
from test_load_run import FakeRuntime


def test_checkout_option_default_and_real_seam_without_trace_requirement(tmp_path):
    assert run.parse_args([]).server_checkout is None
    root = checkout(tmp_path)
    (root / "server/reply_trace.py").unlink()
    args = run.parse_args(["--backend", "local", "--server-checkout", str(root)])
    assert args.server_checkout == root.resolve()


@pytest.mark.parametrize("broken,missing", [
    ("server/upstream_telemetry.py", "upstream_telemetry.py"),
    ("server/voice/cascade.py", "ELEVENLABS_WS_URL"),
    ("server/video.py", "STORAGE_PREFLIGHT"),
])
def test_checkout_parse_refuses_missing_seams_before_process_or_key(tmp_path, monkeypatch, capsys, broken, missing):
    root = checkout(tmp_path)
    (root / broken).write_text("# missing implementation\n", encoding="utf-8")
    async def forbidden(*args, **kwargs):
        raise AssertionError("parse must not start a process")
    monkeypatch.setattr(asyncio, "create_subprocess_exec", forbidden)
    monkeypatch.setattr(run, "load_key", lambda: pytest.fail("parse accessed credentials"))
    with pytest.raises(SystemExit) as exc:
        run.parse_args(["--backend", "local", "--server-checkout", str(root), "--gateway", "live", "--allow-live", "--ramp", "1"])
    assert exc.value.code == 2 and missing in capsys.readouterr().err


def test_explicit_checkout_refuses_docker_before_startup(capsys):
    with pytest.raises(SystemExit):
        run.parse_args(["--server-checkout", str(ROOT)])
    assert "--server-checkout requires --backend local" in capsys.readouterr().err


class ProvenRuntime(FakeRuntime):
    instances = []

    def __init__(self, directory, **kwargs):
        super().__init__(directory, **kwargs)
        self.options = kwargs
        self.record_tts_destinations = kwargs.get("record_tts_destinations", False)
        self.instances.append(self)

    async def start(self):
        await super().start()
        (self.data / "upstream-attempts.jsonl").write_text(json.dumps({
            "upstream": "elevenlabs", "purpose": "tts", "outcome": "ok"}) + "\n", encoding="utf-8")
        return self

    async def fake_stats(self):
        return {"errors": [], "counts": {"tts_ws": 1}}

    async def keepalive_probe(self):
        return {"passed": True, "same_socket": True}


def session_row(runtime, scenario):
    sid = "s_1_" + scenario
    events = runtime.data / "sessions" / sid / "events.jsonl"
    events.parent.mkdir(parents=True, exist_ok=True)
    events.write_text('{"type":"user_turn","text":"Participant words"}\n'
                      '{"type":"assistant_turn","text":"Actor words"}\n', encoding="utf-8")
    return {"session_id": sid, "scenario": scenario, "events": str(events), "events_exists": True}


def test_selected_room_ramp_records_real_provenance_paths_and_tts_proof(tmp_path, monkeypatch):
    ProvenRuntime.instances = []
    expected = asyncio.run(revision(ROOT))
    assert expected["commit"] and isinstance(expected["dirty"], bool)
    async def step(runtime, args, n, repeat, ceiling, **kwargs):
        assert args.scenarios == ["S3A", "S4A"] and n == 1
        result = sample_step(n, "S3A", "all")
        result["sessions"] = [session_row(runtime, s) for s in args.scenarios]
        return result
    monkeypatch.setattr(run, "run_step", step)
    monkeypatch.setattr(run, "load_key", lambda: "mock-live-key")
    args = run.parse_args(["--backend", "local", "--server-checkout", str(ROOT),
        "--gateway", "live", "--allow-live", "--ramp", "1", "--max-concurrency", "1",
        "--scenarios", "S3A,S4A", "--report-dir", str(tmp_path)])
    report, directory = asyncio.run(run.run_experiment(args, runtime_factory=ProvenRuntime))
    assert report["verdict"] == "PASS" and report["server"] == expected
    assert report["steps"][0]["tts_proof"]["passed"]
    assert report["steps"][0]["tts_proof"]["guard_allowed"] is None
    rt = ProvenRuntime.instances[0]
    assert rt.options["server_checkout"] == ROOT and rt.options["server_environment"]["BUILD_SHA"] == expected["commit"]
    assert rt.stopped and not rt.record_tts_destinations
    saved = json.loads((directory / "report.json").read_text(encoding="utf-8"))
    assert saved["server"] == expected and len(saved["sessions"]) == 2
    markdown = (directory / "report.md").read_text(encoding="utf-8")
    for session in saved["sessions"]:
        assert session["events"] in markdown
        assert "assistant_turn" in Path(session["events"]).read_text(encoding="utf-8")


@pytest.mark.parametrize("fake_count,blocked,passed", [(1, False, True), (2, False, False), (1, True, False)])
def test_blocked_only_journal_still_proves_fake_counts_and_fails_real_destination(fake_count, blocked, passed):
    proof = verify(fake_count, [{"upstream": "elevenlabs"}],
                   [{"allowed": False}] if blocked else [], record_allowed=False)
    assert proof["passed"] is passed
    step = sample_step()
    step["tts_proof"] = proof
    assert fast.evaluate(step, fast.reference([step]))["verdict"] == ("PASS" if passed else "FAIL")
    if not passed:
        step["errors"] = ["generator failed"]
        assert fast.evaluate(step, fast.reference([step]))["verdict"] == "INVALID"


def test_live_selected_room_smoke_runs_each_room_at_n1_and_keeps_paths(tmp_path, monkeypatch):
    ProvenRuntime.instances = []
    calls = []
    async def step(runtime, args, n, repeat, ceiling, **kwargs):
        calls.append((args.scenarios, n, ceiling, args.webcam, kwargs["label"]))
        scenario = args.scenarios[0]
        result = healthy_smoke()
        result["concurrency"] = n
        result["metrics"].update(achieved_concurrency=n, scenario_mix={scenario: 1},
            by_scenario={scenario: result["metrics"]["by_scenario"][scenario]})
        result["timelines"] = result["timelines"][:1]
        result["timelines"][0]["scenario"] = scenario
        result["sessions"] = [session_row(runtime, scenario)]
        return result
    monkeypatch.setattr(run, "run_step", step)
    monkeypatch.setattr(run, "load_key", lambda: "mock-live-key")
    async def gate(args, directory):
        return await smoke.run_smoke(args, directory, runtime_factory=ProvenRuntime)
    args = run.parse_args(["--backend", "local", "--server-checkout", str(ROOT),
        "--gateway", "live", "--allow-live", "--smoke", "--max-concurrency", "1",
        "--scenarios", "S3A,S4A", "--report-dir", str(tmp_path)])
    report, directory = asyncio.run(smoke.run_with_smoke(args, smoke_runner=gate))
    assert calls == [(["S3A"], 1, 60, True, "room-S3A"), (["S4A"], 1, 60, True, "room-S4A")]
    assert report["verdict"] == "PASS" and report["capacity_eligible"] is False
    assert len(report["sessions"]) == 2 and report["server"]["checkout"] == str(ROOT)
    assert all(o["step"]["tts_proof"]["passed"] for o in report["smoke"]["outcomes"])
    assert ProvenRuntime.instances[0].options["server_checkout"] == ROOT
    assert ProvenRuntime.instances[0].stopped


def test_live_selected_room_smoke_kills_server_on_startup_429(tmp_path, monkeypatch):
    class Denied(ProvenRuntime):
        async def start(self):
            await super().start()
            with (self.data / "upstream-attempts.jsonl").open("a", encoding="utf-8") as stream:
                stream.write(json.dumps({"upstream": "gateway", "purpose": "steering", "status": 429,
                                         "outcome": "http_error", "route": "messages"}) + "\n")
            return self
        async def keepalive_probe(self):
            pytest.fail("must kill before probe or participant startup")
    monkeypatch.setattr(run, "load_key", lambda: "mock-live-key")
    args = run.parse_args(["--backend", "local", "--server-checkout", str(ROOT), "--smoke",
        "--gateway", "live", "--allow-live", "--scenarios", "S3A,S4A"])
    result = asyncio.run(smoke.run_smoke(args, tmp_path, runtime_factory=Denied))
    assert result["verdict"] == "FAIL" and "429" in result["outcomes"][0]["reasons"][0]
    assert Denied.instances[-1].killed and Denied.instances[-1].stopped
    assert result["gateway_errors"][0]["purpose"] == "steering"


def test_run_step_indexes_exported_events_for_every_room_session(tmp_path, monkeypatch):
    real_client = httpx.AsyncClient
    monkeypatch.setattr(run.httpx, "AsyncClient", lambda **kw: real_client(transport=httpx.MockTransport(
        lambda request: httpx.Response(307, headers={"location": "/v2?participant_id=p&run=r"})), **kw))
    runtime = FakeRuntime(tmp_path)
    class Participant:
        def __init__(self, base, scenario, steps, **kwargs):
            row = session_row(runtime, scenario)
            self.active = False
            self.tl = {**row, "completed": True, "lines": [], "hangup_requested": 0}
        async def run(self):
            return self.tl
    async def exercise():
        await runtime.start()
        args = run.parse_args(["--ramp", "1", "--warmup", "0", "--scenarios", "S3A,S4A"])
        return await run.run_step(runtime, args, 1, 1, 600, participant_factory=Participant)
    step = asyncio.run(exercise())
    assert len(step["sessions"]) == 2
    assert {s["scenario"] for s in step["sessions"]} == {"S3A", "S4A"}
    assert all(s["events_exists"] and Path(s["events"]).is_file() for s in step["sessions"])


@pytest.mark.parametrize("scenario_id", ["S3A", "S4A"])
def test_real_room_runner_uses_fake_gateway_and_tts_and_retains_turn_text(
        isolated_llm, monkeypatch, tmp_path, scenario_id):
    """Real registry/store/room/scribe/director/cascade; only transports are in-process."""
    from server import conversation_audio, llm, session, storage, upstream_telemetry
    from server.realtime_voice_session import RealtimeVoiceSessionRunner
    from server.voice import cascade, realtime
    from tools.sim import stim
    from test_live_realism_pipeline import FakeWS

    for name, value in {"DATA_DIR": tmp_path, "SESSIONS_DIR": tmp_path / "sessions",
                        "PARTICIPANTS_DIR": tmp_path / "participants", "DB_PATH": tmp_path / "index.db"}.items():
        monkeypatch.setattr(storage, name, value)
    monkeypatch.setattr(session.registry, "_sessions", {})
    monkeypatch.setattr(conversation_audio, "finish_later", lambda path: None)
    monkeypatch.setattr(realtime, "MODEL", "cascade-argon-elevenlabs")
    monkeypatch.setattr(realtime, "gateway_api_key", lambda: "mock-key")
    app = create_app(fast_config(tool_call_probability=0, second_speaker_probability=0))
    records = []
    upstream_telemetry.add_listener(records.append)
    with TestClient(app) as fake:
        async def connect(url, **kwargs):
            assert url.startswith("ws://fake/v1/text-to-dialogue/stream-input?")
            return await asyncio.to_thread(SocketAdapter, fake, url)
        monkeypatch.setattr(cascade.websockets, "connect", connect)

        async def exercise():
            async with (httpx.AsyncClient(transport=httpx.ASGITransport(app)) as http,
                        httpx2.AsyncClient(transport=httpx2.ASGITransport(app)) as sdk_http):
                client = llm.text_client().with_options(http_client=sdk_http)
                monkeypatch.setattr(session, "text_client", lambda: client)
                monkeypatch.setattr(cascade, "_shared_http", lambda: http)
                encounter = session.registry.create(scenario_id, capture_audio=True)
                encounter.auto_steering = False
                runner = RealtimeVoiceSessionRunner(encounter, FakeWS())
                try:
                    await runner._open_room()
                    await runner.room.hear(stim.load("a1"))
                    assert await runner.room.close_participant_turn()
                    async def wait_event(kind):
                        while True:
                            rows = [json.loads(line) for line in encounter.store.events_path.read_text(encoding="utf-8").splitlines()]
                            if any(r["type"] == kind for r in rows): return rows
                            await asyncio.sleep(.01)
                    await asyncio.wait_for(wait_event("user_turn"), 3)
                    await asyncio.wait_for(runner._run_group_turn(), 5)
                    rows = await asyncio.wait_for(wait_event("assistant_turn"), 3)
                    assert any(r["type"] == "user_turn" and r["text"] == stim.text("a1") for r in rows)
                    assert any(r["type"] == "assistant_turn" and r["text"] for r in rows)
                    own = [r for r in records if r["session_id"] == encounter.id]
                    assert {"stt", "director", "actor", "tts"} <= {r["purpose"] for r in own}
                    assert all(r["outcome"] == "ok" for r in own)
                    stats = app.state.fake
                    assert not stats.errors
                    proof = verify(stats.counts["tts_ws"], own, [], record_allowed=False)
                    assert proof["passed"], proof
                finally:
                    pumps = list(runner._pumps)
                    await runner._close_room()
                    await asyncio.gather(*pumps, return_exceptions=True)
                    session.registry.drop(encounter.id)
        asyncio.run(exercise())
