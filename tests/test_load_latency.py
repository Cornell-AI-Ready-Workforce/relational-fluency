"""Calibrated fake and latency mode; all gateway/transport calls stay offline."""
import asyncio
import base64
import json
from pathlib import Path
import subprocess
import sys
import shutil

import httpx
import pytest
from fastapi.testclient import TestClient

from server.voice import cascade
from tools.load import fake_upstream as fake, latency, run
from tools.load.runtime import ROOT, Runtime, environment
from tools.sim import sequences, stim
from test_load_fake_upstream import fast_config, isolated_llm, SocketAdapter
from test_load_run import FakeRuntime


def test_calibrated_and_legacy_profiles_and_overrides():
    config = fake.FakeConfig()
    assert config.tts_first_ms == fake.Distribution(486, 696)
    assert (config.tts_words_per_minute, config.tts_realtime_factor) == (189, 9.3)
    assert (config.tts_connect_ms.low + config.tts_connect_ms.high) / 2 == 107
    assert config.tts_connect_ms.low + .9 * (config.tts_connect_ms.high - config.tts_connect_ms.low) == 176
    legacy = fake.FakeConfig(tts_profile="legacy")
    assert legacy.tts_first_ms == fake.Distribution(150, 350)
    assert legacy.tts_connect_ms == fake.Distribution(0, 0)
    assert (legacy.tts_words_per_minute, legacy.tts_realtime_factor) == (160, 2)
    assert fake.FakeConfig.from_dict({"tts_profile": "legacy", "tts_connect_ms": {"low": 10, "high": 20}}).tts_connect_ms.low == 10


@pytest.mark.parametrize("values", [
    {"tts_profile": "unknown"}, {"actor_sse_mode": "tokens"}, {"actor_replies": [None]},
    {"stt_plan": [{"delay_ms": -1}]}, {"stt_plan": [{"status": 200}]},
    {"stt_plan": [{"content": []}]}, {"stt_plan": [{"commit_id": "not-on-wire"}]},
])
def test_parity_configuration_rejects_invalid_cases(values):
    with pytest.raises(ValueError):
        fake.FakeConfig.from_dict(values)


@pytest.mark.parametrize("reply", ["A reply held until final flush", "A reply held until final flush."])
def test_fragmented_content_and_terminal_punctuation_need_final_flush(reply, isolated_llm, monkeypatch):
    config = fast_config(actor_sse_mode="fragmented", actor_replies=[reply], tool_call_probability=0)
    chunks = fake.content_chunks(reply, "fragmented")
    assert "".join(chunks) == reply and all(len(chunk) <= 3 for chunk in chunks)
    if reply.endswith("."):
        assert chunks[-1] == "."  # No invented following whitespace.
    app = fake.create_app(config)
    with TestClient(app) as client:
        async def connect(url, **kw):
            return await asyncio.to_thread(SocketAdapter, client, url)
        monkeypatch.setattr(cascade.websockets, "connect", connect)
        async def exercise():
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app)) as http:
                sock = cascade.CascadeSocket(http=http, api_key="test-only")
                await sock._respond("final-flush")
                frames = [json.loads(sock._out.get_nowait()) for _ in range(sock._out.qsize())]
                assert not [f for f in frames if f["type"] == "error"]
                assert any(f["type"] == "response.output_audio.delta" for f in frames)
                assert next(f["transcript"] for f in frames if f["type"] == "response.output_audio_transcript.done") == reply
        asyncio.run(exercise())
        assert app.state.fake.protocol["inputs"] == 1
        assert app.state.fake.protocol["close_socket"] == 1


def test_tts_connect_delay_before_accept_and_final_flush_audio_then_slow_close(monkeypatch):
    config = fast_config(tts_final_flush_only=True, tts_close_ms=fake.Distribution(25, 25))
    config.tts_connect_ms = fake.Distribution(107, 107)
    phases = []
    async def delay(self, distribution):
        phases.append("connect" if distribution is config.tts_connect_ms else
                      "first" if distribution is config.tts_first_ms else "close")
    monkeypatch.setattr(fake.FakeState, "delay", delay)
    class Wire:
        query_params = {"output_format": "pcm_24000", "model_id": "fake"}
        messages = iter([{"voices": ["v"]}, {"inputs": [{"voice_id": "v", "text": "Hello there"}], "flush": True},
                         {"close_socket": True}])
        async def accept(self): phases.append("accept")
        async def receive_json(self):
            await asyncio.sleep(0)
            item = next(self.messages)
            if item.get("close_socket"):
                assert "audio" not in phases
                phases.append("final-flush")
            return item
        async def send_json(self, item): phases.append("audio" if "audio" in item else "final")
    app = fake.create_app(config)
    endpoint = next(r.endpoint for r in app.routes if r.path == "/v1/text-to-dialogue/stream-input")
    asyncio.run(endpoint(Wire()))
    assert phases[0:2] == ["connect", "accept"]
    assert phases.index("final-flush") < phases.index("first") < phases.index("audio") < phases.index("close") < phases.index("final")


def test_cancelling_tts_connection_wait_never_accepts_socket(monkeypatch):
    entered = None
    async def wait(self, distribution):
        entered.set()
        await asyncio.Event().wait()
    monkeypatch.setattr(fake.FakeState, "delay", wait)
    class Wire:
        query_params = {"output_format": "pcm_24000", "model_id": "fake"}
        async def accept(self): raise AssertionError("cancelled handshake accepted")
    app = fake.create_app(fast_config(tts_connect_ms=fake.Distribution(1, 1)))
    endpoint = next(r.endpoint for r in app.routes if r.path == "/v1/text-to-dialogue/stream-input")
    async def exercise():
        nonlocal entered
        entered = asyncio.Event()
        task = asyncio.create_task(endpoint(Wire()))
        await entered.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError): await task
    asyncio.run(exercise())


def test_stt_completion_reversal_failure_and_no_speech_keep_commit_ids(isolated_llm):
    app = fake.create_app(fast_config(stt_plan=[{"delay_ms": 80, "content": "first"},
        {"delay_ms": 0, "content": "second"}, {"status": 503}, {"content": None}, {"content": ""}]))
    app.state.fake.matcher = fake.StimulusMatcher()
    async def exercise():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app)) as http:
            sock = cascade.CascadeSocket(http=http, api_key="test-only")
            items = [sock._add_item("user", "")["id"] for _ in range(5)]
            await asyncio.gather(*(sock._transcribe(iid, stim.load("a1")) for iid in items))
            frames = [json.loads(sock._out.get_nowait()) for _ in range(sock._out.qsize())]
            heard = [f for f in frames if f["type"].endswith("transcription.completed")]
            assert heard[0]["item_id"] == items[1]
            assert next(f for f in heard if f["item_id"] == items[0])["transcript"] == "first"
            assert next(f for f in frames if f["type"].endswith("transcription.failed"))["item_id"] == items[2]
            assert all(next(f for f in heard if f["item_id"] == iid)["transcript"] == "" for iid in items[3:])
    asyncio.run(exercise())


def test_late_stt_request_can_be_cancelled(isolated_llm):
    app = fake.create_app(fast_config(stt_plan=[{"delay_ms": 10000}]))
    app.state.fake.matcher = fake.StimulusMatcher()
    rows = []
    app.state.fake.log = lambda kind, **fields: rows.append({"kind": kind, **fields})
    async def exercise():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app)) as http:
            sock = cascade.CascadeSocket(http=http, api_key="test-only")
            task = asyncio.create_task(sock._transcribe("late-commit", stim.load("a1")))
            while not any(r["kind"] == "stt_started" for r in rows): await asyncio.sleep(0)
            task.cancel()
            with pytest.raises(asyncio.CancelledError): await task
    asyncio.run(exercise())
    assert {"kind": "stt_cancelled", "request_number": 1} in rows


def checkout(tmp_path):
    root = tmp_path / "sut"
    # Exercise the real checked-in upstream seam, not a no-op telemetry stub.
    # The small functional trace contract below is test data, not a merge of A6.
    shutil.copytree(ROOT / "server", root / "server", ignore=shutil.ignore_patterns("__pycache__"))
    (root / "server/reply_trace.py").write_text(
        'class ReplyTrace:\n'
        '    def __init__(self, store): self.store = store\n'
        '    def flush(self):\n'
        '        for kind in ("reply_trace", "commit_trace"):\n'
        '            self.store.event(kind, schema_version=1, clock="encounter_s", units="seconds", finalized_at="encounter_teardown")\n',
        encoding="utf-8")
    tf = root / "infra/terraform"
    tf.mkdir(parents=True)
    (tf / "ecs.tf").write_text('environment = [\n { name = "REALTIME_MODEL", value = "cascade-argon-elevenlabs" },\n'
                              ' { name = "LLM_BASE_URL", value = "https://gateway.invalid" }\n    ]', encoding="utf-8")
    return root


def test_server_bootstrap_imports_selected_checkout_without_reading_its_credentials(tmp_path):
    root = checkout(tmp_path)
    (root / ".env").write_text('ELEVENLABS_WS_URL="wss://real.invalid"\n', encoding="utf-8")
    code = ('import os, sys, uvicorn; from tools.load import server; '
            f'os.environ.update(RF_LOAD_SERVER_CHECKOUT={str(root)!r}, DATA_DIR={str(tmp_path / "data")!r}, '
            'ELEVENLABS_WS_URL="wss://fake.invalid"); '
            'uvicorn.run=lambda *a, **kw: print(sys.modules["server.llm"].__file__, os.environ["ELEVENLABS_WS_URL"]); '
            'server.main()')
    result = subprocess.run([sys.executable, "-c", code], cwd=ROOT, capture_output=True, text=True, check=True)
    assert str(root / "server/llm.py") in result.stdout and "wss://fake.invalid" in result.stdout
    assert "wss://real.invalid" not in result.stdout


def test_latency_parser_seed_scenarios_and_safety(tmp_path):
    root = checkout(tmp_path)
    base = ["--server-checkout", str(root), "--report-dir", str(tmp_path / "report")]
    args = latency.parse_args(base + ["--allow-live"])
    assert args.participants == 1 and args.turns == 24 and args.seed == 20261112
    script = latency.sequence("S1A", 25, args.seed)
    assert script == latency.sequence("S1A", 25, args.seed)
    assert len([s for s in sequences.parse(script) if s[0] == "say"]) == 25
    for extra in [[], ["--allow-live", "--scenarios", "S3A"], ["--allow-live", "--participants", "21"]]:
        with pytest.raises(SystemExit): latency.parse_args(base + extra)
    with pytest.raises(ValueError): Runtime(tmp_path / "rt", server_checkout=root, backend="docker")


@pytest.mark.parametrize("missing", [None, "reply_trace", "commit_trace", "turn_timing"])
def test_latency_mode_archives_raw_traces_counts_gateway_and_keeps_tts_fake(monkeypatch, tmp_path, capsys, missing):
    root = checkout(tmp_path)
    observed = []
    monkeypatch.delenv("LITELLM_API_KEY", raising=False)
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    class Recorded(FakeRuntime):
        def __init__(self, directory, **kw):
            super().__init__(directory, **kw)
            observed.append((self, kw))
        async def start(self):
            await super().start()
            self.fake_connections = 0
            (self.data / "upstream-attempts.jsonl").write_text('{"upstream":"gateway","purpose":"preflight"}\n', encoding="utf-8")
        async def fake_stats(self):
            return {"errors": [], "counts": {"tts_ws": self.fake_connections}}
    async def step(rt, args, n, repeat, ceiling, **kw):
        assert not args.fail_fast and args.duration < args.encounter_seconds
        timelines = []
        for i in range(n):
            sid = f"s{repeat}_{i}"
            folder = rt.data / "sessions" / sid
            folder.mkdir(parents=True)
            rows = [{"type": kind, "schema_version": 1, "clock": "encounter_s", "units": "seconds",
                     "finalized_at": "encounter_teardown", "stages": {"create_entered": 1.25}}
                    for kind in ("reply_trace", "commit_trace", "turn_timing") if kind != missing]
            (folder / "events.jsonl").write_text("".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8")
            timelines.append({"session_id": sid, "scenario": args.scenarios[0], "lines": [{}] * 3})
        with (rt.data / "upstream-attempts.jsonl").open("a", encoding="utf-8") as fh:
            for _ in range(6): fh.write('{"upstream":"gateway","purpose":"actor"}\n')
            fh.write('{"upstream":"gateway","purpose":"actor","detail":"slot_wait"}\n')
            fh.write('{"upstream":"elevenlabs","purpose":"tts"}\n')
        rt.fake_connections += 1
        with (rt.data / "tts-destinations.jsonl").open("a", encoding="utf-8") as fh:
            fh.write('{"allowed":true,"destination":["wss","fake",443,"/tts"]}\n')
        return {"timelines": timelines, "metrics": {"settled_artifacts": True},
                "errors": [], "server_errors": [], "fake_errors": [], "live_stop": None}
    monkeypatch.setattr(run, "run_step", step)
    args = latency.parse_args(["--server-checkout", str(root), "--report-dir", str(tmp_path / "report"),
                              "--allow-live", "--turns", "3", "--participants", "2", "--tts-slots", "1"])
    report, directory = asyncio.run(latency.run_latency(args, runtime_factory=Recorded, key_loader=lambda: "mock-only-secret"))
    assert report["gateway_request_count"] == 13 and "Gateway requests: 13" in capsys.readouterr().out
    assert report["verdict"] == ("FAIL" if missing else "PASS") and not report["capacity_eligible"]
    assert report["configuration"]["live_guard_counted_purposes"] == ["preflight", "stt", "actor"]
    assert len(report["sessions"]) == 4 and observed[0][0].stopped
    options = observed[0][1]
    assert options["record_tts_destinations"] is True
    assert options["server_checkout"] == root and options["server_environment"]["ELEVENLABS_CONCURRENCY"] == "1"
    assert options["fake_config"].tts_profile == "calibrated" and options["live_key"] == "mock-only-secret"
    env = environment(gateway="live", fake_url="https://fake.invalid", data_dir="/data", key="synthetic",
                      live_key="mock-only-secret", live_url="https://gateway.invalid",
                      production={"REALTIME_MODEL": "cascade-argon-elevenlabs"})
    assert env["ELEVENLABS_WS_URL"].startswith("wss://fake.invalid/")
    assert env["ELEVENLABS_API_KEY"] == "load-only-fake-tts-key"
    assert "mock-only-secret" not in (directory / "report.json").read_text(encoding="utf-8")
    for session in report["sessions"]:
        target = directory / session["events"]
        assert target.read_bytes() == (observed[0][0].data / "sessions" / session["session_id"] / "events.jsonl").read_bytes()
