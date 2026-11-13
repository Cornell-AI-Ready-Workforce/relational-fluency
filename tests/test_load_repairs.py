"""Round-1 regressions, labelled by the review finding they protect."""
import asyncio
import base64
from dataclasses import replace
import json
import logging
from pathlib import Path
import time

import httpx
import httpx2
import pytest
from fastapi.testclient import TestClient

from server import director, llm, scenarios, upstream_telemetry
from server.voice import cascade
from tools.load import calibrate, isolation, measure, observer, participant, relay, run, runtime
from tools.load.fake_upstream import Distribution, FakeConfig, StimulusMatcher, create_app
from tools.load.inspect import settled
from tools.load.server import RedactKey
from tools.sim import stim
from test_load_fake_upstream import fast_config, isolated_llm
from test_load_measure import REFERENCE, valid_metrics
from test_load_run import FakeRuntime


def step_result(n, repeat, *, slow=False):
    metrics = valid_metrics()
    metrics["achieved_concurrency"] = n
    if slow:
        metrics["speech_end_to_first_played_s"]["p90"] = 10
        metrics["latency_samples_s"] = [10]
    return {"concurrency": n, "repeat": repeat, "metrics": metrics, "errors": [],
            "server_errors": [], "fake_errors": [], "full_length": True, "live_stop": None,
            "profile": None}


def test_f1_relay_forwards_bidirectionally_to_only_server_8080(monkeypatch):
    destinations = []
    class Reader:
        def __init__(self, chunk): self.chunk = chunk
        async def read(self, size):
            if self.chunk:
                chunk, self.chunk = self.chunk, None
                return chunk
            await asyncio.sleep(0)
            return b""
    class Writer:
        def __init__(self): self.chunks = []; self.closed = False
        def write(self, chunk): self.chunks.append(chunk)
        async def drain(self): pass
        def close(self): self.closed = True
        async def wait_closed(self): pass
    client, server = Writer(), Writer()
    async def connect(host, port):
        destinations.append((host, port))
        return Reader(b"response"), server
    monkeypatch.setattr(relay.asyncio, "open_connection", connect)
    asyncio.run(relay.forward(Reader(b"request"), client))
    assert destinations == [("server", 8080)]
    assert client.chunks == [b"response"] and server.chunks == [b"request"]
    assert client.closed and server.closed


def test_f2_capacity_is_passing_prefix_even_if_later_n_passes():
    steps = [{"concurrency": n, "verdict": v} for n, v in
             [(1, "PASS"), (5, "PASS"), (10, "FAIL"), (20, "INVALID"), (30, "PASS")]]
    assert measure.capacity(steps, 1) == 5
    assert "non-monotonic" in measure.non_monotonic(steps)


@pytest.mark.parametrize("keep_going,expected", [(False, [1, 5]), (True, [1, 5, 10])])
def test_f2_ramp_stops_at_first_failed_n_unless_explicit(monkeypatch, tmp_path, keep_going, expected):
    calls = []
    async def step(rt, args, n, repeat, ceiling, **kwargs):
        calls.append(n)
        return step_result(n, repeat, slow=n == 5)
    monkeypatch.setattr(run, "run_step", step)
    args = run.parse_args(["--ramp", "1,5,10", "--scenarios", "S2A", "--report-dir", str(tmp_path)]
                          + (["--continue-after-fail"] if keep_going else []))
    report, _ = asyncio.run(run.run_experiment(args, runtime_factory=FakeRuntime))
    assert calls == expected and report["capacity"] == 1
    assert bool(report["ramps"][0]["non_monotonic_warning"]) == keep_going


@pytest.mark.parametrize("error", ["HTTP 503", "server-initiated close", "incomplete encounter", "close timeout", "video HTTP 409"])
def test_f3_server_failures_are_fail_even_when_they_prevent_measurements(error):
    m = valid_metrics()
    m.update(achieved_concurrency=0, encounter_count=2, close_burst_s=None)
    m["speech_end_to_first_played_s"]["p90"] = None
    m["generator_loop_lag_ms"]["p99"] = None
    result = measure.verdict(5, m, None, server_errors=[error])
    assert result["verdict"] == "FAIL" and "50.000 per 100 encounters" in result["reasons"][0]
    assert measure.verdict(5, m, None, server_errors=[error], errors=["generator OSError"])["verdict"] == "INVALID"
    m["generator_loop_lag_ms"]["p99"] = 51
    assert measure.verdict(5, m, None, server_errors=[error])["verdict"] == "INVALID"


def test_f3_http500_admission_is_server_error_not_generator(monkeypatch, tmp_path):
    real_client = httpx.AsyncClient
    monkeypatch.setattr(run.httpx, "AsyncClient", lambda **kw: real_client(
        transport=httpx.MockTransport(lambda req: httpx.Response(503)), **kw))
    async def exercise():
        rt = FakeRuntime(tmp_path)
        await rt.start()
        return await run.run_step(rt, run.parse_args(["--ramp", "1", "--scenarios", "S2A"]), 1, 1, 600)
    result = asyncio.run(exercise())
    assert not result["errors"] and "HTTPStatusError" in result["server_errors"][0]
    assert participant.error_side(OSError()) == "generator_errors"


def test_f4_cpu_calibration_runs_real_frame_path_and_reference_is_local(tmp_path):
    score = calibrate.calibrate(0.005)
    assert score["frames"] > 0 and score["checksum"] == score["frames"] * 3200
    assert score["version"] == calibrate.VERSION and score["frames_per_second"] > 0
    reference = {**score, "frames_per_second": score["frames_per_second"] / 2}
    path = tmp_path / "fargate.json"
    path.write_text(json.dumps(reference), encoding="utf-8")
    rt = runtime.Runtime(tmp_path, cpu_scale=0.75, fargate_reference=path)
    rt._calibration(score)
    assert rt.metadata["server_cpu_quota"] == 0.375 and rt.metadata["host_to_fargate_score_ratio"] == 2
    assert "this host" in rt.metadata["capacity_scope"]
    with pytest.raises(ValueError): calibrate.compare(score, {"version": "other", "frames_per_second": 1})


def test_f5_tts_factors_run_independent_baselines_and_report_capacity_range(monkeypatch, tmp_path):
    configs = []
    class Capture(FakeRuntime):
        def __init__(self, directory, **kwargs):
            super().__init__(directory, **kwargs)
            self.factor = kwargs["fake_config"].tts_realtime_factor
            configs.append(self.factor)
    async def step(rt, args, n, repeat, ceiling, **kwargs):
        return step_result(n, repeat, slow=n == 5 and rt.factor == 1)
    monkeypatch.setattr(run, "run_step", step)
    args = run.parse_args(["--ramp", "1,5", "--tts-factors", "1.0,3.0", "--scenarios", "S2A", "--report-dir", str(tmp_path)])
    report, _ = asyncio.run(run.run_experiment(args, runtime_factory=Capture))
    assert configs == [1, 3] and report["capacity_range"] == [1, 5]
    assert report["capacity_range_complete"] and len(report["ramps"]) == 2
    assert FakeConfig(tts_profile="legacy").tts_realtime_factor == 2 and FakeConfig().reply_words == Distribution(15, 35)


def window_summary():
    events = [{"type": "session_start", "wall": 100, "t": 0},
              {"type": "user_turn", "text": "warm", "wall": 102, "t": 2},
              {"type": "user_turn", "text": "measured", "wall": 115, "t": 15},
              {"type": "turn_timing", "wall": 190, "t": 90, "vad_speech_end": 2, "first_audio_played": 3},
              {"type": "turn_timing", "wall": 190, "t": 90, "vad_speech_end": 15, "first_audio_played": 25},
              {"type": "voice_error", "where": "warm", "wall": 105},
              {"type": "voice_error", "where": "measured", "wall": 115},
              {"type": "reply_missing", "wall": 115}, {"type": "reply_missing", "wall": 120}]
    return measure.summarize({"s": events}, [{"session_id": "s", "lines": []}],
        attempts=[{"purpose": "stt", "t": t, "outcome": "ok"} for t in [105, 115, 120]],
        resources=[{"t": 105, "cpu_percent": 999, "rss_bytes": 999999999},
                   {"t": 115, "cpu_percent": 5, "rss_bytes": 1000000}],
        concurrency=[{"t": 115, "active": 1}],
        generator_lag_ms=[{"t": 105, "lag_ms": 1000}, {"t": 115, "lag_ms": 1}, {"t": 120, "lag_ms": 1000}],
        server_lag=[{"t": 105, "p99_ms": 999, "max_ms": 999}, {"t": 115, "p99_ms": 1, "max_ms": 2}],
        measurement_start=110, measurement_end=120, close_burst_s=1, artifact_bytes={})


def test_f6_verdict_metrics_use_speech_end_window_with_whole_step_separate():
    m = window_summary()
    assert m["latency_samples_s"] == [10] and m["whole_step"]["latency_samples_s"] == [1, 10]
    assert m["participant_turns"] == m["reply_missing"] == m["upstream"]["stt"]["attempts"] == 1
    assert m["voice_error_kinds"] == {"measured": 1} and m["server_rss_bytes"]["max"] == 1000000
    assert m["whole_step"]["upstream"]["stt"]["attempts"] == 3


def test_f7_missing_playback_acks_fail_above_pooled_baseline_plus_one():
    m = valid_metrics()
    m["first_played_missing_per_100_turns"] = 1
    assert measure.verdict(5, m, REFERENCE)["verdict"] == "PASS"
    m["first_played_missing_per_100_turns"] = 1.01
    assert measure.verdict(5, m, REFERENCE)["verdict"] == "FAIL"
    m["speech_end_to_first_played_s"]["p90"] = None
    assert measure.verdict(5, m, REFERENCE)["verdict"] == "FAIL"
    m["first_played_missing"] = 2
    assert measure.baseline([{"metrics": m}, {"metrics": valid_metrics()}])["first_played_missing_per_100_turns"] == 1


def test_f8_active_starts_at_session_before_any_transcript():
    async def exercise():
        p = participant.Participant("http://test", "S2A", "tone:1", index=0)
        async def frames():
            yield '{"type":"session","session_id":"s_test"}'
            await asyncio.Event().wait()
        p.ws = frames()
        task = asyncio.create_task(p.read())
        await p.session_ready.wait()
        assert p.active and p.tl["conversation_started"] is None
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
    asyncio.run(exercise())


def test_f9_partial_audio_and_unmatched_speech_are_valid_stt_with_counter():
    matcher = StimulusMatcher()
    _, text, anchors = matcher.templates[0]
    assert matcher.transcribe(cascade.wav_bytes(anchors[0][1], 16000)) == text
    app = create_app(fast_config())
    with TestClient(app) as client:
        for seconds in [0.3, 1.5]:
            pcm = b"\0\x10" * round(16000 * seconds)
            body = {"messages": [{"content": [{"type": "input_audio", "input_audio": {
                "data": base64.b64encode(cascade.wav_bytes(pcm, 16000)).decode()}}]}]}
            response = client.post("/v1/chat/completions", json=body)
            assert response.status_code == 200 and response.json()["choices"][0]["message"]["content"]
        stats = client.get("/__load/stats").json()
        assert stats["unmatched_fragment"] == 2 and stats["errors"] == []


def test_f10_director_second_speaker_distribution_uses_distinct_real_cast_ids(isolated_llm):
    async def exercise(probability):
        app = create_app(fast_config(second_speaker_probability=probability))
        scene = scenarios.load_scenario("S3A")
        async with httpx2.AsyncClient(transport=httpx2.ASGITransport(app)) as http:
            route = director.Director(scene, client=llm.text_client().with_options(http_client=http))
            decisions = await route.route([{"speaker": "user", "text": "next?"}], "next?")
        ids = [row["agent_id"] for row in decisions]
        assert len(ids) == len(set(ids)) and set(ids) <= {actor.id for actor in scene.cast}
        return len(ids)
    assert asyncio.run(exercise(0)) == 1 and asyncio.run(exercise(1)) == 2
    assert FakeConfig().second_speaker_probability == 0.25


def test_f11_named_volume_results_are_copied_after_step_not_polled_from_host(monkeypatch, tmp_path):
    calls = []
    async def command(*args, **kwargs):
        calls.append(tuple(map(str, args)))
        return 0, '{"settled": true}', ''
    monkeypatch.setattr(runtime, "command", command)
    rt = runtime.Runtime(tmp_path)
    rt.observer_name = "observer"
    async def exercise():
        timelines = [{"session_id": "s1"}]
        assert await rt.settled(timelines)
        assert not (rt.data / "sessions").exists()
        rt.data.mkdir()
        await rt.export(timelines)
    asyncio.run(exercise())
    assert rt.metadata["storage_mount_type"] == "Docker named volume"
    assert calls[0][1:3] == ("exec", "observer") and calls[1][1] == "cp"


def test_f12_profile_uses_nonblocking_sidecar_and_is_not_inside_server(monkeypatch, tmp_path):
    calls = []
    async def command(*args, **kwargs):
        calls.append(tuple(map(str, args)))
        if args[1] == "cp": (tmp_path / "py-spy.raw").write_text("main;work 3\n", encoding="utf-8")
        return 0, '', ''
    monkeypatch.setattr(runtime, "command", command)
    rt = runtime.Runtime(tmp_path, profile_image="local-pyspy")
    result = asyncio.run(rt.profile(tmp_path, delay=0, seconds=1))
    plan = calls[0]
    assert plan[:2] == ("docker", "run") and plan[plan.index("--pid") + 1] == "container:" + rt.server_name
    assert "--nonblocking" in plan and "--native" not in plan and "SYS_PTRACE" in plan
    assert result["top20"] == [{"frame": "work", "self_samples": 3}]


def test_f12_only_one_extra_profile_repeat_and_excluded_from_capacity(monkeypatch, tmp_path):
    calls = []
    class ProfileRuntime(FakeRuntime):
        async def profile_available(self): return True
    async def step(rt, args, n, repeat, ceiling, **kwargs):
        calls.append((n, repeat, kwargs.get("profile_only", False)))
        result = step_result(n, repeat, slow=n > 1)
        if kwargs.get("profile_only"):
            result["profile"] = {"status": "sampled", "top20": [{"frame": "work", "self_samples": 10}]}
        return result
    monkeypatch.setattr(run, "run_step", step)
    args = run.parse_args(["--ramp", "1,5,10", "--continue-after-fail", "--scenarios", "S2A", "--report-dir", str(tmp_path)])
    report, _ = asyncio.run(run.run_experiment(args, runtime_factory=ProfileRuntime))
    assert calls == [(1, 1, False), (5, 1, False), (5, 0, True), (10, 1, False)]
    assert len(report["steps"]) == 3 and report["capacity"] == 1
    assert report["first_failure_profile"]["excluded_from_verdict"]


def test_f13_real_docker_test_does_not_touch_docker_without_opt_in(monkeypatch, tmp_path):
    from test_load_runtime import test_docker_isolation_blocks_public_egress as check
    monkeypatch.delenv("RF_LOAD_DOCKER_TESTS", raising=False)
    def forbidden(*args, **kwargs): raise AssertionError("must not run Docker")
    monkeypatch.setattr(runtime.shutil, "which", forbidden)
    with pytest.raises(pytest.skip.Exception, match="RF_LOAD_DOCKER_TESTS=1"):
        check(tmp_path)


def test_f15_live_rate_denominator_excludes_fake_tts_and_slot_wait():
    guard = measure.LiveGuard()
    for _ in range(100):
        assert guard.observe({"upstream": "elevenlabs", "outcome": "http_error"}, 1) is None
    assert guard.attempts == 0
    assert guard.observe({"upstream": "gateway", "detail": "slot_wait", "outcome": "cancelled"}, 1) is None
    assert guard.observe({"upstream": "gateway", "outcome": "http_error", "status": 503}, 1) is None
    for _ in range(18):
        assert guard.observe({"upstream": "gateway", "outcome": "ok", "status": 200}, 1) is None
    stop = guard.observe({"upstream": "gateway", "outcome": "http_error", "status": 503}, 1)
    assert stop["attempts"] == 20 and stop["errors"] == 2 and "gateway" in stop["reason"]


def test_f17_access_log_key_redacted_without_disabling_info_logging():
    record = logging.LogRecord("uvicorn.access", logging.INFO, "file", 1, '%s - "%s %s HTTP/%s" %d',
                               ("client", "GET", "/test?name=load&key=secret%20value&other=ok", "1.1", 307), None)
    assert RedactKey().filter(record)
    assert "secret" not in record.getMessage() and "key=[REDACTED]" in record.getMessage()


def test_f18_observer_reads_rss_and_cgroup_throttling_from_shared_pid(tmp_path):
    proc, root = tmp_path / "proc", tmp_path / "cgroup"
    proc.mkdir(); (root / "server").mkdir(parents=True)
    (proc / "status").write_text("VmRSS:\t1234 kB\n", encoding="utf-8")
    (proc / "cgroup").write_text("0::/server\n", encoding="utf-8")
    (root / "server" / "cpu.stat").write_text("usage_usec 900\nnr_throttled 5\nthrottled_usec 700\n", encoding="utf-8")
    sample = observer.resource_sample(proc, root)
    assert sample["rss_bytes"] == 1234 * 1024 and sample["throttled_usec"] == 700 and sample["nr_throttled"] == 5


def test_f19_webcam_absolute_ticks_queue_slow_requests_and_retry_failed_head(monkeypatch):
    monkeypatch.setattr(participant, "VIDEO_PERIOD_S", 0.02)
    async def exercise():
        p = participant.Participant("http://server", "S2A", "tone:1", index=0)
        p.tl.update(session_id="s", participant_id="p")
        seqs = []
        async def handle(request):
            seqs.append(int(request.url.params["seq"]))
            await asyncio.sleep(0.05)
            return httpx.Response(503 if len(seqs) == 1 else 200)
        async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as http:
            task = asyncio.create_task(p.video(http))
            await asyncio.sleep(0.19)
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        assert seqs[:2] == [0, 0] and p.tl["server_errors"]
        assert len(p.tl["video_ticks"]) >= 7 and p.tl["video_queue_max"] >= 4
    asyncio.run(exercise())


def test_f20_egress_probe_checks_dns_and_tcp_for_all_three_named_services():
    names, targets = [], []
    def resolve(host, port, **kwargs):
        names.append(host)
        return [(2, 1, 6, '', ("203.0.113.1", port))]
    def blocked(target, **kwargs):
        targets.append(target)
        raise OSError("blocked")
    proof = isolation.egress_probe(blocked, resolve, "gateway.example")
    assert proof["passed"] and set(names) == {"api.elevenlabs.io", "gateway.example", "s3.amazonaws.com"}
    assert len(targets) == 5 and all(row["resolved"] for row in proof["dns"])


def test_f21_close_poll_never_reads_events_and_lag_outside_window_is_excluded(tmp_path):
    directory = tmp_path / "sessions" / "s"
    directory.mkdir(parents=True)
    (directory / "events.jsonl").write_text("invalid json; must not parse while polling", encoding="utf-8")
    (directory / "manifest.json").write_text('{"status":"closed"}', encoding="utf-8")
    assert not settled(tmp_path, ["s"])
    (directory / "conversation_audio.wav").write_bytes(b"mix")
    assert settled(tmp_path, ["s"])
    m = window_summary()
    assert m["generator_loop_lag_ms"]["p99"] == 1


def test_f22_actor_uses_only_offered_scenario_tool_at_configured_rate():
    app = create_app(fast_config(tool_call_probability=1))
    with TestClient(app) as client:
        body = {"stream": True, "messages": [], "tools": [{"type": "function", "function": {
            "name": "end_conversation", "parameters": {"type": "object", "properties": {}, "required": []}}}]}
        replies = [client.post("/v1/chat/completions", json=body).text for _ in range(2)]
        assert all('"name": "end_conversation"' in text and 'data: [DONE]' in text for text in replies)
        assert 'call_fake_1' in replies[0] and 'call_fake_2' in replies[1]
        body["tools"] = []
        assert 'tool_calls' not in client.post("/v1/chat/completions", json=body).text
    assert FakeConfig().tool_call_probability == 0.01


def test_track0_slot_wait_only_contributes_queue_wait_not_sent_attempts():
    result = measure.upstream_summary([{"purpose": "tts", "outcome": "cancelled", "detail": "slot_wait",
                                        "duration_ms": 0, "queue_wait_ms": 400}])["tts"]
    assert result["attempts"] == result["errors"] == result["cancelled"] == 0
    assert result["slot_wait_only"] == 1 and result["queue_wait_ms"]["max"] == 400


def test_f16_fake_mounts_only_its_three_input_files_and_tools(monkeypatch, tmp_path):
    calls = []
    rt = runtime.Runtime(tmp_path, image="image")
    rt.bundle = tmp_path / "ca-bundle.pem"
    async def command(*args, **kwargs):
        calls.append(tuple(map(str, args)))
        if args[:2] == ("docker", "info"):
            return 0, '{"Architecture":"aarch64","NCPU":4}', ''
        if 'tools.load.calibrate' in args:
            return 0, '{"frames_per_second":100}', ''
        if 'tools.load.inspect' in args:
            return 0, '{"errors":[]}', ''
        if args[:2] == ("docker", "port"):
            assert args[2:] == (rt.relay_name, "8080/tcp")
            return 0, '127.0.0.1:1234', ''
        return 0, 'id', ''
    async def spawn(*args, **kwargs): pass
    monkeypatch.setattr(runtime, "command", command)
    monkeypatch.setattr(runtime.shutil, "which", lambda *args: '/bin/docker')
    monkeypatch.setattr(rt, '_spawn', spawn)
    asyncio.run(rt._docker_start())
    fake = next(c for c in calls if c[:2] == ('docker', 'run') and rt.fake_name in c)
    mounts = [fake[i + 1] for i, arg in enumerate(fake) if arg == '-v']
    assert set(m.split(':', 1)[1] for m in mounts) == {
        '/app/tools:ro', '/load/cert.pem:ro', '/load/key.pem:ro', '/load/fake-config.json:ro'}
    assert str(tmp_path) + ':/load:ro' not in mounts and not any('server.env' in m or ':/data' in m for m in mounts)
    assert f'{rt.directory / "code"}:/app/tools:ro' in mounts
    server = next(c for c in calls if c[:2] == ('docker', 'run') and rt.server_name in c)
    assert f'{rt.volume}:/data' in server and f'{rt.data}:/data' not in server
    observer_plan = next(c for c in calls if c[:2] == ('docker', 'run') and rt.observer_name in c)
    assert observer_plan[observer_plan.index('--network') + 1] == 'none'
    assert '--cgroupns' in observer_plan and '--cap-add' not in observer_plan
    assert rt.metadata['docker_vm_cpu_count'] == 4 and rt.metadata['docker_architecture'] == 'aarch64'


def test_f17_server_entry_uses_info_and_access_logging(monkeypatch, tmp_path):
    import dotenv
    import uvicorn
    from tools.load import server
    captured = {}
    monkeypatch.setenv('DATA_DIR', str(tmp_path))
    monkeypatch.setenv('LOAD_REQUIRE_ISOLATION', '0')
    monkeypatch.setattr(dotenv, 'load_dotenv', dotenv.load_dotenv)
    monkeypatch.setattr(llm, '_FILE', {})
    monkeypatch.setattr(upstream_telemetry, 'add_listener', lambda callback: None)
    monkeypatch.setattr(uvicorn, 'run', lambda app, **kwargs: captured.update(app=app, **kwargs))
    server.main()
    assert captured['app'] == 'server.app:app' and captured['access_log'] and captured['log_level'] == 'info'
    assert all(handler['filters'] == ['redact_key'] for handler in captured['log_config']['handlers'].values())


def test_f18_step_reports_throttled_time_and_count_delta():
    kwargs = dict(events_by_session={}, timelines=[], attempts=[], concurrency=[], generator_lag_ms=[],
                  server_lag=[], measurement_start=0, measurement_end=10, close_burst_s=1, artifact_bytes={})
    m = measure.summarize(**kwargs, resources=[{'t': 1, 'throttled_usec': 100, 'nr_throttled': 1},
                                             {'t': 5, 'throttled_usec': 2000100, 'nr_throttled': 5}])
    assert m['server_throttled_s'] == 2 and m['server_nr_throttled'] == 4


def test_f15_live_stop_kills_server_before_waiting_for_participant_cancellation(monkeypatch, tmp_path):
    order = []
    real_client = httpx.AsyncClient
    monkeypatch.setattr(run.httpx, 'AsyncClient', lambda **kw: real_client(transport=httpx.MockTransport(
        lambda request: httpx.Response(307, headers={'location':'/v2?participant_id=p&run=r'})), **kw))
    rt = FakeRuntime(tmp_path)
    async def kill(): order.append('kill')
    rt.kill_server = kill
    class Waiting:
        def __init__(self, *args, **kwargs):
            self.active = False
            self.tl = {'session_id': 's', 'scenario':'S2A', 'lines': [], 'hangup_requested': None}
        async def run(self):
            self.active = True
            (rt.data / 'upstream-attempts.jsonl').write_text(json.dumps({
                'upstream':'gateway', 'purpose':'actor', 'status':429, 'outcome':'http_error', 't':time.time()}) + '\n', encoding="utf-8")
            try: await asyncio.Event().wait()
            finally:
                order.append('cancel')
                self.tl['hangup_requested'] = time.time()
    async def exercise():
        await rt.start()
        args = run.parse_args(['--gateway','live','--allow-live','--ramp','1','--scenarios','S2A'])
        await asyncio.wait_for(run.run_step(rt, args, 1, 1, 600, participant_factory=Waiting), 2)
    asyncio.run(exercise())
    assert order == ['kill', 'cancel']


def test_f3_partial_close_artifacts_do_not_reclassify_server_timeout_as_generator_error(tmp_path):
    directory = tmp_path / 'sessions' / 's'
    directory.mkdir(parents=True)
    (directory / 'events.jsonl').write_text('{"type":"user_turn"}\n{"type":', encoding="utf-8")
    (directory / 'manifest.json').write_text('{"status":', encoding="utf-8")
    events, sizes, closed = runtime.read_encounters(tmp_path, [{'session_id':'s'}])
    assert events == {'s':[{'type':'user_turn'}]} and not closed and sizes['s'] > 0


@pytest.mark.parametrize('factor', [1.0, 3.0])
def test_f5_fake_tts_chunk_delay_uses_generation_factor(monkeypatch, factor):
    from types import SimpleNamespace
    from tools.load import fake_upstream
    sleeps = []
    real_sleep = asyncio.sleep
    async def paced(seconds):
        sleeps.append(seconds)
        await real_sleep(0)
    monkeypatch.setattr(fake_upstream, 'asyncio', SimpleNamespace(
        sleep=paced, Queue=asyncio.Queue, create_task=asyncio.create_task,
        gather=asyncio.gather, to_thread=asyncio.to_thread))
    config = fast_config(tts_realtime_factor=factor)
    config.tts_words_per_minute = 600  # one word = 100 ms = one PCM chunk
    with TestClient(create_app(config)) as client:
        with client.websocket_connect('/v1/text-to-dialogue/stream-input?output_format=pcm_24000&model_id=fake') as ws:
            ws.send_json({'voices':['voice']})
            ws.send_json({'inputs':[{'voice_id':'voice','text':'word'}], 'flush':True})
            ws.send_json({'close_socket':True})
            assert 'audio' in ws.receive_json()
            assert ws.receive_json()['is_final']
    assert max(sleeps) == pytest.approx(0.1 / factor)


def test_f3_missing_resources_after_server_failure_remain_fail(monkeypatch, tmp_path):
    real_client = httpx.AsyncClient
    monkeypatch.setattr(run.httpx, 'AsyncClient', lambda **kw: real_client(transport=httpx.MockTransport(
        lambda request: httpx.Response(307, headers={'location':'/v2?participant_id=p&run=r'})), **kw))
    rt = FakeRuntime(tmp_path)
    async def lost_resource(): raise RuntimeError('server PID vanished')
    rt.resources = lost_resource
    class Failed:
        def __init__(self, *args, **kwargs):
            self.active = True
            self.tl = {'session_id':'s', 'scenario':'S2A', 'lines':[], 'completed':False,
                       'server_errors':['server-initiated close'], 'hangup_requested':None}
        async def run(self):
            await asyncio.sleep(0.1)
            self.tl['hangup_requested'] = time.time()
            return self.tl
    async def exercise():
        await rt.start()
        args = run.parse_args(['--ramp','1','--scenarios','S2A','--warmup','0'])
        return await run.run_step(rt, args, 1, 1, 600, participant_factory=Failed)
    result = asyncio.run(exercise())
    assert result['measurement_errors'] and not result['errors']
    assert measure.verdict(1, result['metrics'], None, server_errors=result['server_errors'], errors=result['errors'])['verdict'] == 'FAIL'


def test_f15_live_guard_keeps_watching_during_close_settle(monkeypatch, tmp_path):
    real_client = httpx.AsyncClient
    monkeypatch.setattr(run.httpx, 'AsyncClient', lambda **kw: real_client(transport=httpx.MockTransport(
        lambda request: httpx.Response(307, headers={'location':'/v2?participant_id=p&run=r'})), **kw))
    rt = FakeRuntime(tmp_path)
    async def never_settled(session_ids): return set()
    rt.settled_sessions = never_settled
    class Finished:
        def __init__(self, *args, **kwargs):
            self.active = False
            self.tl = {'session_id':'s', 'scenario':'S2A', 'lines':[], 'completed':True, 'hangup_requested':None}
        async def run(self):
            self.tl['hangup_requested'] = time.time()
            return self.tl
    async def late_attempt():
        await asyncio.sleep(0.15)
        (rt.data / 'upstream-attempts.jsonl').write_text(json.dumps({
            'upstream':'gateway','purpose':'steering','outcome':'http_error','status':429,'t':time.time()})+'\n', encoding='utf-8')
    async def exercise():
        await rt.start()
        producer = asyncio.create_task(late_attempt())
        args = run.parse_args(['--gateway','live','--allow-live','--ramp','1','--scenarios','S2A','--close-timeout','120'])
        result = await asyncio.wait_for(run.run_step(rt, args, 1, 1, 600, participant_factory=Finished), 2)
        await producer
        return result
    result = asyncio.run(exercise())
    assert rt.killed and result['live_stop']['purpose'] == 'steering'
    assert 'session/mix close timeout' not in result['server_errors']


def test_f2_first_failed_repeat_stops_remaining_repeats(monkeypatch, tmp_path):
    calls = []
    async def step(rt, args, n, repeat, ceiling, **kwargs):
        calls.append((n, repeat))
        return step_result(n, repeat, slow=n == 5)
    monkeypatch.setattr(run, 'run_step', step)
    args = run.parse_args(['--ramp','1,5,10','--repeat','2','--scenarios','S2A','--report-dir',str(tmp_path)])
    report, _ = asyncio.run(run.run_experiment(args, runtime_factory=FakeRuntime))
    assert calls == [(1,1), (1,2), (5,1)] and report['capacity'] == 1


def test_f16_code_snapshot_excludes_run_secrets_and_results_even_inside_tools_reports(tmp_path):
    rt = runtime.Runtime(tmp_path / 'tools' / 'load' / 'reports' / 'run')
    rt.directory.mkdir(parents=True)
    (rt.directory / 'server.env').write_text('LITELLM_API_KEY=must-not-enter-code', encoding='utf-8')
    rt._prepare()
    code = rt.directory / 'code'
    assert (code / 'load' / 'fake_upstream.py').is_file() and (code / 'sim' / 'stim' / 'lines.txt').is_file()
    assert not any(path.name in {'server.env', 'fake-config.json', 'server-data', 'reports'} for path in code.rglob('*'))
    assert all(path.suffix in {'.py', '.ulaw', '.txt'} for path in code.rglob('*') if path.is_file())
