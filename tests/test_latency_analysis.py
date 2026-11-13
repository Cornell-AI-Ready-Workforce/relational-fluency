"""Latency analysis uses recorded boundaries, never a serial cascade model."""
import json
import subprocess
import sys
from types import SimpleNamespace
from pathlib import Path

import pytest

from server.encounter_record import build
from tools.sim import latency


@pytest.fixture
def writer_sessions(tmp_path, monkeypatch):
    """The reviewer's probe shape, written by SessionStore and TurnTimer."""
    from server import conversation_audio, storage, turn_timing

    base = 1_800_000_000
    clock = {"now": base}
    fake_time = SimpleNamespace(time=lambda: clock["now"])
    monkeypatch.setattr(storage, "time", fake_time)
    monkeypatch.setattr(turn_timing, "time", fake_time)
    monkeypatch.setattr(conversation_audio, "finish_later", lambda _: None)
    for name, value in (("DATA_DIR", tmp_path), ("SESSIONS_DIR", tmp_path / "sessions"),
                        ("PARTICIPANTS_DIR", tmp_path / "participants"), ("DB_PATH", tmp_path / "index.db")):
        monkeypatch.setattr(storage, name, value)

    def at(seconds):
        clock["now"] = base + seconds

    def make_session(sid, mode, scenario):
        at(0)
        store = storage.SessionStore(sid, scenario=scenario, model="m", participant_id=None, capture_audio=False)
        store.event("session_start", scenario=scenario, mode=mode)
        store.event("realtime_session_started", model="cascade-argon-elevenlabs")
        return store, turn_timing.TurnTimer(store)

    def reply(timer, agent, speech, commit, gateway, client, played, end, grant=None):
        if speech is not None:
            at(speech)
            timer.speech_end()
        if commit is not None:
            at(commit)
            timer.commit_sent()
        if grant is not None:
            at(grant)
            timer.grant_sent(agent)
        at(gateway - .01)
        turn = timer.started(agent)
        at(client)
        timer.audio_to_client(base + gateway)
        timer.done(agent, {"stt_ms": 700, "llm_first_token_ms": 900})
        if played is not None:
            at(played + .05)
            timer.ack({"phase": "start", "turn": turn, "lag_s": .05})
            at(end)
            timer.ack({"phase": "end", "turn": turn, "lag_s": 0})

    store, timer = make_session("s_1800000000_aaaaaa", "single", "S2A")
    store.event("awaiting_participant", reason="start")
    store.event("participant_opened", reason="start", first_of_encounter=True)
    reply(timer, "dan", 5, 5.1, 6.4, 7.2, 7.4, 12)
    reply(timer, "dan", 15, 15.1, 16.3, 16.31, 16.5, 20)
    reply(timer, "dan", None, None, 30, 30.01, 30.2, 33)
    store.event("segment_start", new_interaction=True)
    store.event("awaiting_participant", reason="handoff")
    store.event("participant_opened", reason="handoff", first_of_encounter=False)
    reply(timer, "sam", 40, 40.1, 41.4, 42.3, 42.5, 47)
    reply(timer, "sam", 50, 50.1, 51.3, 51.31, 51.5, 55)
    timer.flush()
    store.close(n_turns=5)
    one = store.dir

    store, timer = make_session("s_1800000001_bbbbbb", "group", "S3A")
    store.event("group_room_opened", agents=["a", "b"])
    reply(timer, "a", 5, 5.2, 7.5, 7.6, 7.8, 13, grant=6.4)
    reply(timer, "b", None, None, 14.6, 14.7, 14.9, 18, grant=13.5)
    reply(timer, "a", 25, 25.2, 27, 27.1, None, None, grant=26)
    at(40)
    timer.flush()
    store.close(n_turns=3)
    return one, store.dir


def test_handoff_first_reply_has_its_own_group_with_production_records(writer_sessions):
    one, _ = writer_sessions
    report = latency.analyze_paths([one])
    assert [row["encounter_reply"] for row in report["rows"]] == [
        "first", "later", "later", "first_after_handoff", "later"]
    groups = {g["encounter_reply"]: g for g in report["groups"] if g["participant_reply"] == "first"}
    assert groups["first_after_handoff"]["stages"]["gateway_to_client"]["p50_s"] == .9
    assert groups["later"]["stages"]["gateway_to_client"]["p90_s"] == .01


def test_production_writer_preserves_room_grants_and_missing_ack(writer_sessions):
    _, room = writer_sessions
    events, problems = latency.read_events(room / "events.jsonl")
    assert not problems
    timing_events = [e for e in events if e["type"] == "turn_timing"]
    assert all({"t", "wall", "written_at", "ack_lag_s", "play_end_interrupted"} <= e.keys() for e in timing_events)
    rows = latency.analyze_paths([room])["rows"]
    assert [row["participant_reply"] for row in rows] == ["first", "later", "first"]
    assert [row["boundaries_s"]["grant_sent"] for row in rows] == [6.4, 13.5, 26]
    assert rows[-1]["total_s"] is None
    assert rows[-1]["unexplained_spans"] == []


@pytest.mark.parametrize("keep_awaiting,keep_waited", [(True, True), (False, True), (False, False)])
def test_handoff_acceptance_can_follow_its_speech_end(writer_sessions, keep_awaiting, keep_waited):
    one, _ = writer_sessions
    events, _ = latency.read_events(one / "events.jsonl")
    for event in events:
        if event.get("type") == "participant_opened" and event.get("reason") == "handoff":
            event["t"] = 41.5  # VAD was 40, accepted transcript arrives later.
            if keep_waited:
                event["waited_s"] = 8.5
    if not keep_awaiting:
        events = [e for e in events if not (e.get("type") == "awaiting_participant" and e.get("reason") == "handoff")]
    rows = latency.session_rows({}, events, "probe")
    assert [r["turn"] for r in rows if r["encounter_reply"] == "first_after_handoff"] == [4]


def test_markdown_shows_reply_rows_and_missing_played_acks(writer_sessions):
    _, room = writer_sessions
    report = latency.analyze_paths([room])
    group = next(g for g in report["groups"] if g["participant_reply"] == "first" and g["encounter_reply"] == "later")
    assert group["reply_rows"] == 1 and group["missing_played_ack"] == 1
    assert group["stages"]["total"]["n"] == 0
    table = latency.markdown(report)
    assert "| Reply rows | No played ACK | n |" in table
    assert "| turn_timing | 1 | 1 | 0 |" in table


def timing(**changes):
    row = {"type": "turn_timing", "t": 20, "turn": 1, "agent_id": "dan",
           "reply_index": 0, "vad_speech_end": 1, "commit_sent": 2,
           "transcript_arrived": 4, "director_decided": None, "grant_sent": None,
           "first_audio_from_gateway": 6, "first_audio_to_client": 7,
           "first_audio_played": 9, "assistant_done": 12, "play_end": 19,
           "output_latency_s": 0.02, "cascade": {"stt_ms": 800, "llm_first_token_ms": 1200,
           "llm_total_ms": 9000, "tts_connect_ms": 500, "tts_slot_wait_ms": 10,
           "first_audio_ms": 2500}}
    return {**row, **changes}


def write_json(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data), encoding="utf-8", newline="")


def session(tmp_path, rows, *, mode="single", scenario="S1A", model="cascade-argon-elevenlabs"):
    directory = tmp_path / "sessions" / "s_1_abcdef"
    directory.mkdir(parents=True)
    events = [{"type": "session_start", "scenario": scenario, "mode": mode, "cast": []},
              {"type": "realtime_session_started", "model": model}, *rows]
    (directory / "events.jsonl").write_text("\n".join(json.dumps(e) for e in events) + "\n",
                                            encoding="utf-8", newline="")
    # Use the production builder to prove record.json's actual shape.
    write_json(directory / "record.json", build(directory))
    return directory


def test_all_intervals_residual_and_exact_endpoints():
    one = latency.turn_intervals(timing(), "1:1")
    assert one["total_s"] == 8
    assert one["intervals_s"] == {"speech_end_to_commit": 1, "commit_to_grant": None,
        "gateway_to_client": 1, "client_to_played": 2, "unexplained": 4}
    assert one["unexplained_spans"] == [{"start_field": "commit_sent", "end_field": "first_audio_from_gateway", "duration_s": 4}]
    room = latency.turn_intervals(timing(grant_sent=5, director_decided=4), "room")
    assert room["intervals_s"]["commit_to_grant"] == 3
    assert room["intervals_s"]["unexplained"] == 1
    assert room["unexplained_spans"][0]["start_field"] == "grant_sent"
    assert room["diagnostics_s"] == {"commit_to_transcript_arrival": 2, "speech_end_to_director_decision": 3}


def test_missing_stages_stay_unexplained():
    row = latency.turn_intervals(timing(commit_sent=None, first_audio_from_gateway=None), "1:1")
    assert row["intervals_s"]["speech_end_to_commit"] is None
    assert row["intervals_s"]["gateway_to_client"] is None
    assert row["intervals_s"]["unexplained"] == 6
    assert row["unexplained_spans"] == [{"start_field": "vad_speech_end", "end_field": "first_audio_to_client", "duration_s": 6}]
    row = latency.turn_intervals(timing(first_audio_to_client=None), "1:1")
    assert row["intervals_s"]["unexplained"] == 7


@pytest.mark.parametrize("value", [None, True, "9", float("inf"), float("nan"), -1, 0])
def test_missing_or_invalid_total_does_not_become_zero(value):
    row = latency.turn_intervals(timing(first_audio_played=value), "room")
    assert row["total_s"] is None
    assert row["issues"] == ["missing_or_invalid_total"]
    assert row["unexplained_spans"] == []


def test_bad_order_and_overlap_do_not_create_negative_residual():
    row = latency.turn_intervals(timing(grant_sent=8), "room")
    assert row["intervals_s"]["gateway_to_client"] is None
    assert row["intervals_s"]["client_to_played"] is None
    assert row["intervals_s"]["unexplained"] == 1
    assert len(row["issues"]) == 2
    reverse = latency.turn_intervals(timing(commit_sent=0, first_audio_from_gateway=8), "1:1")
    assert reverse["intervals_s"]["speech_end_to_commit"] is None
    assert reverse["intervals_s"]["gateway_to_client"] is None
    assert reverse["intervals_s"]["unexplained"] == 6


def test_stale_stt_and_overlapping_cascade_never_change_the_breakdown(tmp_path):
    a = timing()
    b = timing(turn=2, vad_speech_end=21, commit_sent=22, transcript_arrived=24,
               first_audio_from_gateway=26, first_audio_to_client=27, first_audio_played=29,
               cascade={"stt_ms": 800, "llm_first_token_ms": 90000, "llm_total_ms": 120000})
    directory = session(tmp_path, [b, a])
    report = latency.analyze_paths([directory / "record.json"])
    assert [r["encounter_reply"] for r in report["rows"]] == ["first", "later"]
    assert [r["intervals_s"]["unexplained"] for r in report["rows"]] == [4, 4]
    assert [r["cascade_diagnostics_ms"]["stt_ms"] for r in report["rows"]] == [800, 800]
    assert any("stale" in text for text in report["caveats"])
    assert any("scribe" in text for text in report["caveats"])
    assert any("not time to first token" in text for text in report["caveats"])


def test_grouping_keeps_room_followups_and_legacy_rows_separate(tmp_path):
    directory = session(tmp_path, [timing(turn=2, reply_index=1, grant_sent=5),
                                    timing(turn=1, reply_index=0, grant_sent=5)], mode="group", scenario="S3A")
    report = latency.analyze_paths([directory, directory / "record.json", directory / "events.jsonl"])
    assert len(report["rows"]) == 2
    assert {g["participant_reply"] for g in report["groups"]} == {"first", "later"}
    assert all(g["mode"] == "room" and g["family"] == "cascade" for g in report["groups"])
    assert sum(g["stages"]["total"]["n"] for g in report["groups"]) == 2
    rows = latency.session_rows({}, [timing(turn=3, reply_index=None), timing(turn=2, reply_index=None)], "legacy")
    assert [r["participant_reply"] for r in rows] == ["first", "later"]
    assert all(r["reply_index_inferred"] for r in rows)


def test_stats_use_paired_shares_and_each_stage_sample_count(tmp_path):
    directory = session(tmp_path, [timing(turn=1, vad_speech_end=None), timing(turn=2),
        timing(turn=3, vad_speech_end=20, commit_sent=22, first_audio_from_gateway=None,
               first_audio_to_client=26, first_audio_played=36)])
    group, = latency.analyze_paths([directory])["groups"]
    assert group["encounter_reply"] == "later"
    assert group["stages"]["total"] == {"n": 2, "p50_s": 12, "p90_s": 15.2, "mean_share": 1, "share_n": 2}
    assert group["stages"]["speech_end_to_commit"]["mean_share"] == 0.125
    assert group["stages"]["gateway_to_client"]["n"] == 1
    assert group["stages"]["gateway_to_client"]["share_n"] == 1


def test_missing_ack_is_counted_without_latency_and_openers_are_excluded(tmp_path):
    directory = session(tmp_path, [timing(turn=1, vad_speech_end=None), timing(turn=2, first_audio_played=None)])
    report = latency.analyze_paths([directory])
    assert len(report["rows"]) == 1
    assert report["counts"] == {"missing_or_invalid_total": 1}
    assert report["groups"][0]["stages"]["total"]["n"] == 0


def test_zero_total_has_no_share(tmp_path):
    directory = session(tmp_path, [timing(vad_speech_end=9, commit_sent=None,
        first_audio_from_gateway=None, first_audio_to_client=None)])
    stage = latency.analyze_paths([directory])["groups"][0]["stages"]["unexplained"]
    assert stage == {"n": 1, "p50_s": 0, "p90_s": 0, "mean_share": None, "share_n": 0}


def test_record_without_events_and_truncated_events_are_reported(tmp_path):
    directory = session(tmp_path, [timing()])
    events = directory / "events.jsonl"
    original = events.read_text(encoding="utf-8")
    events.write_text(original + "[]\n{broken", encoding="utf-8", newline="")
    report = latency.analyze_paths([directory])
    assert len(report["rows"]) == 1
    assert len(report["issues"]) == 2
    events.unlink()
    report = latency.analyze_paths([directory / "record.json"])
    assert not report["rows"]
    assert "does not retain stage timings" in report["issues"][0]
    assert report["groups"][0]["source_kind"] == "no_turn_timing"
    assert report["groups"][0]["stages"]["total"]["n"] == 0


def test_an_event_file_set_in_one_directory_keeps_each_session(tmp_path):
    for name, scenario in (("a", "S1A"), ("b", "S2A")):
        path = tmp_path / f"{name}.events.jsonl"
        events = [{"type": "session_start", "scenario": scenario, "mode": "single"}, timing()]
        path.write_text("\n".join(json.dumps(e) for e in events), encoding="utf-8", newline="")
    report = latency.analyze_paths(sorted(tmp_path.glob("*.jsonl")))
    assert len(report["rows"]) == 2
    assert {g["scenario"] for g in report["groups"]} == {"S1A", "S2A"}


def test_run_directory_uses_its_sim_timeline_for_missing_scenario(tmp_path):
    directory = session(tmp_path, [timing()], scenario=None)
    write_json(tmp_path / "S1A.timeline.json", {"session_id": directory.name, "scenario": "S1A"})
    report = latency.analyze_paths([tmp_path])
    assert report["rows"][0]["scenario"] == "S1A"


@pytest.mark.parametrize("model,family", [("gpt-realtime-2.1", "gpt-realtime"),
    ("gemini-live-2.5-flash", "gemini-live"), ("other", "unknown")])
def test_family_uses_the_repository_model_classifier(tmp_path, model, family):
    directory = session(tmp_path, [timing(cascade=None)], model=model)
    group, = latency.analyze_paths([directory])["groups"]
    assert group["family"] == family
    assert group["model"] == model


def test_saved_sim_report_preserves_quantiles_without_fabricating_stages():
    root = Path(__file__).resolve().parents[1]
    result = latency.analyze_paths([root / "tools/sim/reports/1469925.json"])
    assert [g["stages"]["total"]["n"] for g in result["groups"]] == [9, 25, 9, 32]
    assert all(g["encounter_reply"] == "unknown" for g in result["groups"])
    assert all(g["stages"]["unexplained"]["n"] == 0 for g in result["groups"])
    assert not result["rows"]
    assert "2.163 / 2.638" in latency.markdown(result)
    assert result["groups"][0]["completed"] is False
    assert "incomplete" in latency.markdown(result)


def test_saved_report_keeps_errored_and_incomplete_scenarios(tmp_path):
    path = tmp_path / "report.json"
    write_json(path, {"generated_at": "2026-11-12", "scenarios": {
        "S1A": {"metrics": {"completed": False, "speech_end_to_first_played_s": {"n": 2, "p50": 2, "p90": 3}}},
        "S3A": {"error": "RuntimeError: the encounter did not run"}}})
    report = latency.analyze_paths([path])
    incomplete, error = report["groups"]
    assert incomplete["completed"] is False and incomplete["status"] == "incomplete"
    assert error["scenario"] == "S3A" and error["status"] == "error"
    assert error["stages"]["total"]["n"] == 0
    assert any("S3A" in issue and "did not run" in issue for issue in report["issues"])
    assert "incomplete" in latency.markdown(report) and "S3A" in latency.markdown(report)


def test_directory_scan_skips_debug_and_other_jsonl_unless_explicit(tmp_path):
    directory = session(tmp_path, [timing()])
    for name in ("raw_scribe.jsonl", "raw_dan.jsonl", "other.jsonl"):
        (directory / name).write_text('{"type": "debug"}\n', encoding="utf-8", newline="")
    report = latency.analyze_paths([tmp_path])
    assert len(report["sources"]) == 1 and len(report["groups"]) == 1
    assert not report["issues"]
    assert len(latency.analyze_paths([directory / "other.jsonl"])["sources"]) == 1


def test_bad_json_and_wrong_timeline_shape_do_not_stop_valid_sessions(tmp_path):
    directory = session(tmp_path, [timing()])
    bad_report = tmp_path / "bad-report.json"
    bad_report.write_text("{truncated", encoding="utf-8", newline="")
    write_json(tmp_path / "S1A.timeline.json", [])
    (directory / "record.json").write_text("{truncated", encoding="utf-8", newline="")
    # Unrelated audio and manifest JSON are not analysis inputs.
    for name in ("manifest.json", "audio_timeline.json"):
        (directory / name).write_text("{truncated", encoding="utf-8", newline="")
    report = latency.analyze_paths([tmp_path])
    assert len(report["rows"]) == 1
    assert len(report["issues"]) == 3
    assert all(any(name in issue for issue in report["issues"]) for name in
               ("bad-report.json", "S1A.timeline.json", "record.json"))


def test_record_without_timings_does_not_suppress_saved_summary(tmp_path):
    directory = session(tmp_path, [timing()])
    (directory / "events.jsonl").unlink()
    write_json(tmp_path / "report.json", {"generated_at": "2026-11-12", "scenarios": {
        "S1A": {"metrics": {"session_id": directory.name, "speech_end_to_first_played_s": {"n": 1, "p50": 8, "p90": 8}}}}})
    report = latency.analyze_paths([tmp_path])
    assert {g["source_kind"] for g in report["groups"]} == {"no_turn_timing", "sim_summary"}
    assert "regardless of reply_index" in report["sampling"]["sim_summary_difference"]


def test_summary_does_not_double_count_sessions_with_raw_events(tmp_path):
    directory = session(tmp_path, [timing()])
    write_json(tmp_path / "report.json", {"generated_at": "2026-11-12", "scenarios": {
        "S1A": {"metrics": {"session_id": directory.name, "speech_end_to_first_played_s": {"n": 1, "p50": 8, "p90": 8}}}}})
    report = latency.analyze_paths([tmp_path])
    assert len(report["groups"]) == 1
    assert report["groups"][0]["source_kind"] == "turn_timing"


def test_cli_writes_json_and_markdown_offline(tmp_path):
    directory = session(tmp_path, [timing()])
    out = tmp_path / "out"
    run = subprocess.run([sys.executable, "-m", "tools.sim.latency", str(directory),
                          "--json-out", str(out / "summary.json"), "--markdown-out", str(out / "summary.md")],
                         capture_output=True, text=True, check=True)
    assert not run.stdout
    report = json.loads((out / "summary.json").read_text(encoding="utf-8"))
    assert report["groups"][0]["stages"]["unexplained"]["mean_share"] == 0.5
    assert report["definitions"]["gateway_to_client"] == {"start_field": "first_audio_from_gateway", "end_field": "first_audio_to_client"}
    assert "floor lock acquired" in (out / "summary.md").read_text(encoding="utf-8")
    assert "missing_boundaries" in report and "a6_missing_boundaries" not in report
