import copy
import json

import pytest

from tools.load import measure


def valid_metrics():
    return {"achieved_concurrency": 5.0, "generator_loop_lag_ms": {"p99": 2},
            "speech_end_to_first_played_s": {"p90": 1}, "latency_samples_s": [1],
            "first_played_missing": 0, "first_played_missing_per_100_turns": 0, "participant_turns": 100, "reply_missing": 0, "reply_missing_per_100_turns": 0,
            "server_loop_lag_ms": {"p99": 2}, "server_rss_bytes": {"max": 100_000_000},
            "voice_error_kinds": {}, "close_burst_s": 1}


REFERENCE = {"first_played_missing_per_100_turns": 0, "p90_s": 1, "reply_missing_per_100_turns": 0, "voice_error_kinds": []}


def test_first_played_deduplicates_members_but_not_sessions_and_counts_missing_acks():
    first = [
        {"type": "turn_timing", "vad_speech_end": 5, "first_audio_played": 9, "agent_id": "a"},
        {"type": "turn_timing", "vad_speech_end": 5, "first_audio_played": 7, "agent_id": "b"},
        {"type": "turn_timing", "vad_speech_end": 10, "first_audio_played": None},
        {"type": "turn_timing", "vad_speech_end": 10, "first_audio_played": None},
    ]
    latencies, missing = measure.first_played({"session1": first, "session2": first[:2]})
    assert latencies == [2, 2] and missing == 1


@pytest.mark.parametrize("path,value,expected", [
    (("speech_end_to_first_played_s", "p90"), 3, "PASS"),
    (("speech_end_to_first_played_s", "p90"), 3.001, "FAIL"),
    (("server_loop_lag_ms", "p99"), 250, "PASS"),
    (("server_loop_lag_ms", "p99"), 250.001, "FAIL"),
    (("server_rss_bytes", "max"), 900_000_000, "PASS"),
    (("server_rss_bytes", "max"), 900_000_001, "FAIL"),
    (("generator_loop_lag_ms", "p99"), 50, "PASS"),
    (("generator_loop_lag_ms", "p99"), 50.001, "INVALID"),
])
def test_exact_verdict_boundaries(path, value, expected):
    metrics = valid_metrics()
    metrics[path[0]][path[1]] = value
    assert measure.verdict(5, metrics, REFERENCE)["verdict"] == expected


def test_invalid_generator_takes_precedence_over_slow_server():
    metrics = valid_metrics()
    metrics["achieved_concurrency"] = 4.49
    metrics["speech_end_to_first_played_s"]["p90"] = 20
    assert measure.verdict(5, metrics, REFERENCE)["verdict"] == "INVALID"
    metrics["achieved_concurrency"] = 4.5
    assert measure.verdict(5, metrics, REFERENCE)["verdict"] == "FAIL"


def test_reply_missing_threshold_voice_kinds_and_required_measurements():
    metrics = valid_metrics()
    metrics["reply_missing_per_100_turns"] = 1
    metrics["voice_error_kinds"] = {"known": 4}
    reference = {**REFERENCE, "voice_error_kinds": ["known"]}
    assert measure.verdict(5, metrics, reference)["verdict"] == "PASS"
    metrics["reply_missing_per_100_turns"] = 1.01
    assert measure.verdict(5, metrics, reference)["verdict"] == "FAIL"
    metrics["reply_missing_per_100_turns"] = 0
    metrics["voice_error_kinds"]["new"] = 1
    assert measure.verdict(5, metrics, reference)["verdict"] == "FAIL"
    metrics["close_burst_s"] = None
    assert measure.verdict(5, metrics, reference)["verdict"] == "INVALID"
    assert measure.verdict(5, valid_metrics(), reference, full_length=False)["verdict"] == "INVALID"
    assert measure.verdict(5, valid_metrics(), None)["verdict"] == "INVALID"
    assert measure.verdict(5, valid_metrics(), reference, fake_errors=[{}])["verdict"] == "FAIL"
    assert measure.verdict(5, metrics, reference, fake_errors=[{}])["verdict"] == "FAIL"


def test_baseline_pools_raw_samples_and_capacity_requires_all_repeats():
    one = valid_metrics()
    two = copy.deepcopy(one)
    two["latency_samples_s"] = [3, 5, 7]
    ref = measure.baseline([{"metrics": one}, {"metrics": two}])
    assert ref["p90_s"] == 6.4 and ref["sample_count"] == 4
    steps = [{"concurrency": n, "verdict": state} for n, state in
             [(1, "PASS"), (1, "PASS"), (5, "PASS"), (5, "INVALID"), (10, "PASS")]]
    assert measure.capacity(steps, 2) == 1


def test_upstream_attempts_retries_queue_wait_and_cancellations():
    attempts = [dict(purpose="tts", route="tts_ws", outcome=outcome, duration_ms=duration,
                     queue_wait_ms=wait) for outcome, duration, wait in
                [("http_error", 10, 20), ("ok", 30, None), ("cancelled", 0, 100)]]
    summary = measure.upstream_summary(attempts)["tts"]
    assert (summary["attempts"], summary["errors"], summary["cancelled"]) == (3, 1, 1)
    assert summary["queue_wait_ms"]["n"] == 2 and summary["queue_wait_ms"]["max"] == 100
    assert summary["duration_ms"]["p50"] == 10


def test_live_guard_catches_recovered_429_and_stops_at_first_observed_error_rate():
    guard = measure.LiveGuard()
    event = {"upstream": "gateway", "purpose": "actor", "route": "chat_completions_stream",
             "outcome": "ok", "status": 200}
    for _ in range(100):
        assert guard.observe(event, 5) is None
    stop = guard.observe({**event, "status": 429, "outcome": "http_error"}, 5)
    assert stop["reason"] == "first gateway 429" and stop["concurrency"] == 5
    assert guard.observe(event, 20) is stop
    guard = measure.LiveGuard()
    for _ in range(19):
        assert guard.observe(event, 1) is None
    assert guard.observe({**event, "status": 502, "outcome": "http_error"}, 1) is None  # exactly 5%
    assert guard.observe({**event, "status": 502, "outcome": "http_error"}, 1)["reason"].endswith("> 5%")


def test_voice_benign_filters_reuse_sim_rules_and_no_recovery_decrements():
    assert measure.voice_error_kind({"message": "extra response.create was refused"}) is None
    assert measure.voice_error_kind({"message": "response_cancel_not_active"}) is None
    assert measure.voice_error_kind({"where": "tts", "message": "failure"}) == "tts"


def test_jsonl_tail_preserves_partial_lines_without_duplicate_attempts(tmp_path):
    path = tmp_path / "attempts.jsonl"
    reader = measure.JsonlTail(path)
    assert reader.read() == []
    path.write_bytes(b'{"a":1}\n{"a":')
    assert reader.read() == [{"a": 1}]
    assert reader.read() == []
    with path.open(mode="ab") as stream:
        stream.write(b'2}\n')
    assert reader.read() == [{"a": 2}]


def test_server_lag_uses_maximum_window_p99_with_bounds():
    rows = [{"t": t, "line": f"  loop_lag: n=100 p99={p99}ms max={p99 + 1}ms"}
            for t, p99 in [(1, 900), (4, 12), (6, 25)]]
    windows = measure.server_lag_windows(rows, 3, 8)
    assert max(window["p99_ms"] for window in windows) == 25


def test_summary_uses_actual_turns_attempts_resources_and_conversation_samples():
    rows = [{"type": "user_turn", "text": "hello Morgan", "wall": 100},
            {"type": "play_start", "wall": 102},
            {"type": "turn_timing", "vad_speech_end": 1, "first_audio_played": 3, "wall": 102, "t": 3},
            {"type": "reply_missing", "wall": 102},
            {"type": "voice_error", "message": "response_cancel_not_active"}]
    timelines = [{"session_id": sid, "completed": True, "websocket_disconnects": 1,
                  "lines": [{"name": "a1", "text": "hello Morgan", "start": 99, "end": 101}]}
                 for sid in ("s1", "s2")]
    summary = measure.summarize(
        {"s1": rows, "s2": rows}, timelines,
        attempts=[{"purpose": "tts", "route": "tts_ws", "outcome": "ok", "duration_ms": 10, "queue_wait_ms": 25, "t": 101}],
        resources=[{"t": 101, "cpu_percent": 12, "rss_bytes": 1024}],
        concurrency=[{"t": 0, "active": 0}, {"t": 101, "active": 2}, {"t": 102, "active": 1}],
        generator_lag_ms=[{"t": 101, "lag_ms": 1}, {"t": 102, "lag_ms": 3}], server_lag=[{"t": 101, "p99_ms": 10, "max_ms": 20}],
        measurement_start=99, measurement_end=103, close_burst_s=0.5, artifact_bytes={"s1": 100, "s2": 200})
    assert (summary["lines_said"], summary["lines_answered"], summary["participant_turns"]) == (2, 2, 2)
    assert summary["speech_end_to_first_played_s"] == {"p50": 2, "p90": 2, "p99": 2, "n": 2}
    assert summary["reply_missing"] == 2 and summary["reply_missing_per_100_turns"] == 100
    assert summary["voice_error"] == 0 and summary["achieved_concurrency"] == 1.5
    assert summary["upstream"]["tts"]["queue_wait_ms"]["max"] == 25
    assert summary["server_rss_bytes"]["max"] == 1024 and summary["server_cpu_percent"]["max"] == 12
    assert summary["close_burst_s"] == 0.5 and summary["websocket_disconnects"] == 2
