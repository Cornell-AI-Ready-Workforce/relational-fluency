"""Latency analysis uses recorded boundaries, never a serial cascade model."""
import json
import subprocess
import sys
from pathlib import Path

import pytest

from server.encounter_record import build
from tools.sim import latency


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


def test_newest_sim_report_preserves_quantiles_without_fabricating_stages():
    root = Path(__file__).resolve().parents[1]
    reports = [(latency.read_json(p)["generated_at"], p) for p in (root / "tools/sim/reports").glob("*.json")]
    _, newest = max(reports)
    result = latency.analyze_paths([newest])
    assert newest.name == "1469925.json"
    assert [g["stages"]["total"]["n"] for g in result["groups"]] == [9, 25, 9, 32]
    assert all(g["encounter_reply"] == "unknown" for g in result["groups"])
    assert all(g["stages"]["unexplained"]["n"] == 0 for g in result["groups"])
    assert not result["rows"]
    assert "2.163 / 2.638" in latency.markdown(result)


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
