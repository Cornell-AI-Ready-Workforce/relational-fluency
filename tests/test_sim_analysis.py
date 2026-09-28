"""The pre-deploy sim check's offline half: analysis, comparison, stimuli.

tools/sim needs the gateway to run, so CI cannot drive an encounter. What CI
can hold is everything that decides pass or fail once the events exist: how a
turn is matched to a line said, what counts as a phantom, which voice_error is
benign, the percentiles, the tolerance arithmetic, and the report's verdict.
All of it is pinned here on small synthetic event lists. The stimuli and the
default sequences are held to each other (every line a sequence says exists,
and nothing is committed that no sequence says), and the sim server's
configuration to the task definition it copies.
"""
from __future__ import annotations

import json
import re
import subprocess
import sys
from pathlib import Path

import pytest

from tools.sim import analyze as A
from tools.sim import check, sequences, serve, stim

ROOT = Path(__file__).resolve().parent.parent
T0 = 1_790_000_000.0


def line(name, text, start, dur=3.0):
    return {"name": name, "text": text, "start": T0 + start, "end": T0 + start + dur}


def ev(type_, at, **kw):
    return {"type": type_, "t": at, "wall": T0 + at, **kw}


def _encounter():
    """Three lines said. The first is heard and answered; the second is heard
    (as a slightly different transcript) but never answered; the third is not
    heard at all. One turn nobody said lands in the room tone between them."""
    timeline = {"session_id": "s_x", "completed": True, "lines": [
        line("a1", "Hi Morgan, thanks for making time.", 3),
        line("a4", "I'm asking for a ten percent raise.", 30),
        line("a8", "Okay. Thank you, Morgan.", 60, 2),
    ]}
    events = [
        ev("session_start", 0.0, scenario="S2A"),
        ev("realtime_session_started", 0.1, realtime_model="gpt-realtime-2.1",
           pipeline_version="2026-09-24c", build="4798e64"),
        ev("user_turn", 7.2, text="Hi Morgan, thanks for making time."),
        ev("play_start", 9.0),
        ev("assistant_turn", 14.0, audio_ms=5000),
        ev("user_turn", 21.0, text="Bye-bye."),                      # phantom
        ev("user_turn", 34.5, text="Im asking for a 10% raise"),        # heard, different form
        ev("user_turn_suppressed", 50.0, reason="no_speech", text="..."),
        ev("voice_error", 40.0, where="model",
           message="the gateway had already started this reply; the extra response.create was refused"),
        ev("voice_error", 41.0, where="model",
           message="{'code': 'response_cancel_not_active'}"),
        ev("voice_error", 42.0, where="room:dan", message="socket closed 1011"),
        ev("reply_missing", 43.0),
        ev("trigger_fired", 8.0, trigger_id="t1"),
        ev("trigger_fired", 35.0, trigger_id="t2"),
        ev("turn_timing", 15.0, vad_speech_end=6.0, first_audio_played=8.0),
        ev("turn_timing", 25.0, vad_speech_end=33.5, first_audio_played=37.5),
        ev("turn_timing", 26.0, vad_speech_end=None, first_audio_played=3.0),  # opening line: not timed
        ev("session_end", 70.0),
    ]
    return events, timeline


# --- matching ------------------------------------------------------------------

def test_words_fold_case_punctuation_and_apostrophes():
    assert A.words("I'm here, Morgan!") == ["im", "here", "morgan"]
    assert A.words("I’m") == ["im"]
    assert A.words(None) == []


def test_percentile_interpolates_like_numpy():
    assert A.percentile([], 50) is None
    assert A.percentile([2.0], 90) == 2.0
    assert A.percentile([1, 2, 3, 4], 50) == 2.5
    assert A.percentile([1, 2, 3, 4, 5, 6, 7, 8, 9, 10], 90) == 9.1


def test_the_synthetic_encounter_summarizes_as_described():
    m = A.summarize(*_encounter())
    assert (m["lines_said"], m["lines_heard"], m["lines_answered"]) == (3, 2, 1)
    assert m["unheard_lines"] == ["a8"]
    assert m["phantom_turns"] == 1 and m["phantom_turn_texts"][0]["text"] == "Bye-bye."
    assert m["participant_turns"] == 3
    assert m["turns_suppressed"] == {"no_speech": 1}
    assert m["refused_creates"] == 1
    assert m["voice_error"] == 1 and m["voice_error_benign_cancel"] == 1
    assert "1011" in m["voice_error_messages"][0]
    assert m["reply_missing"] == 1
    assert m["triggers_fired"] == 2 and m["trigger_ids"] == ["t1", "t2"]
    assert m["speech_end_to_first_played_s"] == {"p50": 3.0, "p90": 3.8, "n": 2}
    assert m["provenance"]["build"] == "4798e64"
    assert m["completed"] is True


def test_a_turn_that_merges_two_lines_hears_both():
    """burst:b6 then say:b7 1.2 s later often comes back as one turn."""
    tl = {"lines": [line("b6", "Chris, quick question.", 0, 1.7),
                    line("b7", "Actually, Dan, you go first.", 2.9, 2.5)]}
    events = [ev("user_turn", 7.0, text="Chris, quick question. Actually, Dan, you go first.")]
    m = A.summarize(events, tl)
    assert m["lines_heard"] == 2 and m["phantom_turns"] == 0


def test_a_transcript_long_after_the_line_does_not_count_as_hearing_it():
    tl = {"lines": [line("s5", "I didn't say anything, yeah.", 0, 2)]}
    late = A.HEARD_WINDOW_S + 5
    m = A.summarize([ev("user_turn", late, text="I didn't say anything, yeah.")], tl)
    assert m["lines_heard"] == 0


def test_a_turn_before_any_line_is_a_phantom():
    """The room-tone opening of every sequence: a transcript there was never said."""
    tl = {"lines": [line("a1", "Hi Morgan, thanks for making time.", 10)]}
    m = A.summarize([ev("user_turn", 2.0, text="Thank you.")], tl)
    assert m["phantom_turns"] == 1


def test_a_reply_that_starts_after_the_next_line_does_not_answer_the_first():
    tl = {"lines": [line("a1", "Hi Morgan, thanks for making time.", 0),
                    line("a2", "Last year you told me we would revisit my salary.", 20)]}
    events = [ev("user_turn", 5.0, text="Hi Morgan, thanks for making time."),
              ev("user_turn", 25.0, text="Last year you told me we would revisit my salary."),
              ev("play_start", 26.0)]
    m = A.summarize(events, tl)
    assert (m["lines_heard"], m["lines_answered"]) == (2, 1)


def test_a_long_line_split_at_a_pause_is_heard_from_its_parts():
    """s1a_long is hesitant; the server ends the turn at a pause and the line
    comes back as two turns, neither holding half of it."""
    text = ("Well, Riley, thanks for, um, like, taking my side. Um, but I wasn't there, "
            "so could you explain the situation a bit more in detail?")
    tl = {"lines": [line("s1a_long", text, 0, 14)]}
    events = [ev("user_turn", 7.0, text="Well, Riley, thanks for, um, like, taking my side."),
              ev("user_turn", 15.0, text="Um, but I wasn't there")]
    m = A.summarize(events, tl)
    assert m["lines_heard"] == 1 and m["phantom_turns"] == 0


def test_a_neighbours_turn_cannot_complete_a_split_line():
    tl = {"lines": [line("a1", "Hi Morgan, thanks for making time to talk today.", 0),
                    line("a5", "What would you need from me to take this to leadership?", 20)]}
    events = [ev("user_turn", 4.0, text="Hi Morgan"),
              ev("user_turn", 24.0, text="What would you need from me to take this to leadership?")]
    m = A.summarize(events, tl)
    assert m["unheard_lines"] == ["a1"]


def test_only_the_first_reply_to_a_stretch_of_speech_is_a_wait():
    """A silence probe (reply_index 1) and a second character in a room share
    the speech end of the reply before them; neither is a wait anyone sat
    through, so they do not enter the percentiles."""
    events = [ev("turn_timing", 10, vad_speech_end=5.0, first_audio_played=7.0, reply_index=0),
              ev("turn_timing", 30, vad_speech_end=5.0, first_audio_played=26.0, reply_index=1),
              ev("turn_timing", 40, vad_speech_end=35.0, first_audio_played=38.0, reply_index=None),
              ev("turn_timing", 41, vad_speech_end=35.0, first_audio_played=36.5, reply_index=None)]
    m = A.summarize(events, {"lines": []})
    assert m["speech_end_to_first_played_s"] == {"p50": 1.75, "p90": 1.95, "n": 2}


def test_error_frames_the_page_would_show_are_counted_and_held():
    events, tl = _encounter()
    tl["error_frames"] = ["Something went wrong"]
    m = A.summarize(events, tl)
    assert m["error_frames"] == 1
    failed = [c["metric"] for c in A.compare(m, _base())["checks"] if not c["ok"]]
    assert failed == ["error_frames"]


def test_the_last_line_has_a_grace_window_to_be_answered():
    tl = {"lines": [line("a8", "Okay. Thank you, Morgan.", 0, 2)]}
    ok = A.summarize([ev("user_turn", 4.0, text="Okay, thank you Morgan."),
                      ev("play_start", 6.0)], tl)
    late = A.summarize([ev("user_turn", 4.0, text="Okay, thank you Morgan."),
                        ev("play_start", 2 + A.ANSWER_GRACE_S + 1)], tl)
    assert ok["lines_answered"] == 1 and late["lines_answered"] == 0


# --- comparison ----------------------------------------------------------------

def _base(**over):
    b = A.baseline_entry(A.summarize(*_encounter()))
    b.update(over)
    return b


def test_the_same_run_passes_its_own_baseline():
    m = A.summarize(*_encounter())
    v = A.compare(m, A.baseline_entry(m))
    assert v["passed"], v


def test_doing_better_than_the_baseline_never_fails():
    m = A.summarize(*_encounter())
    worse = _base(phantom_turns=5, voice_error=4, reply_missing=3, refused_creates=9,
                  lines_heard=1, lines_answered=0, triggers_fired=2,
                  speech_end_to_first_played_s={"p50": 9.0, "p90": 12.0, "n": 2})
    assert A.compare(m, worse)["passed"]


@pytest.mark.parametrize("field,value,metric", [
    ("phantom_turns", 3, "phantom_turns"),
    ("voice_error", 2, "voice_error"),
    ("reply_missing", 3, "reply_missing"),
    ("refused_creates", 5, "refused_creates"),
    ("triggers_fired", 0, "triggers_fired"),
])
def test_each_count_regression_fails_on_its_own_check(field, value, metric):
    m = A.summarize(*_encounter())
    m[field] = value
    v = A.compare(m, _base())
    failed = [c["metric"] for c in v["checks"] if not c["ok"]]
    assert not v["passed"] and failed == [metric], v


def test_a_heard_rate_drop_beyond_tolerance_fails():
    m = A.summarize(*_encounter())
    base = _base(lines_said=10, lines_heard=10, lines_answered=10)
    m.update(lines_said=10, lines_heard=8, lines_answered=8)   # 1.0 -> 0.8, tolerance 0.15
    failed = [c["metric"] for c in A.compare(m, base)["checks"] if not c["ok"]]
    assert failed == ["heard_rate"]


def test_latency_is_held_to_ratio_plus_slack():
    m = A.summarize(*_encounter())
    base = _base(speech_end_to_first_played_s={"p50": 2.0, "p90": 3.0, "n": 2})
    # p50 limit 2.0*1.5+0.5 = 3.5 (got 3.0); p90 limit 3.0*1.5+1.0 = 5.5 (got 3.8)
    assert A.compare(m, base)["passed"]
    m["speech_end_to_first_played_s"] = {"p50": 3.6, "p90": 3.8, "n": 2}
    failed = [c["metric"] for c in A.compare(m, base)["checks"] if not c["ok"]]
    assert failed == ["speech_end_to_first_played_p50_s"]


def test_no_timed_turns_fails_when_the_baseline_had_them():
    m = A.summarize(*_encounter())
    m["speech_end_to_first_played_s"] = {"p50": None, "p90": None, "n": 0}
    assert not A.compare(m, _base())["passed"]


def test_tolerances_can_be_widened_per_scenario_in_the_baseline():
    m = A.summarize(*_encounter())
    m["phantom_turns"] = 3
    assert not A.compare(m, _base())["passed"]
    assert A.compare(m, _base(tolerances={"phantom_turns_extra": 2}))["passed"]


def test_no_baseline_cannot_pass():
    v = A.compare(A.summarize(*_encounter()), None)
    assert not v["passed"] and v["checks"][0]["metric"] == "baseline"


# --- the report ------------------------------------------------------------------

HEALTH = {"build": "abc1234", "gateway": {"ok": True, "realtime_model": "gpt-realtime-2.1",
                                          "pipeline_version": "2026-09-24c",
                                          "room_pacing_version": "2026-09-24b"}}


def _baseline_file():
    m = A.summarize(*_encounter())
    return {"realtime_model": "gpt-realtime-2.1", "recorded": {"build": "x"},
            "tolerances": dict(A.DEFAULT_TOLERANCES),
            "scenarios": {"S2A": A.baseline_entry(m)}}


def test_a_report_passes_only_when_every_scenario_ran_and_passed():
    m = A.summarize(*_encounter())
    ok = check.build_report("abc1234", "--build", "http://127.0.0.1:1", HEALTH,
                            {"S2A": {"metrics": m}}, _baseline_file())
    assert ok["passed"] and ok["failures"] == [] and ok["build"] == "abc1234"
    broke = check.build_report("abc1234", "--build", "http://127.0.0.1:1", HEALTH,
                               {"S2A": {"metrics": m}, "S4A": {"error": "RuntimeError: x"}},
                               _baseline_file())
    assert not broke["passed"] and any("S4A: did not run" in f for f in broke["failures"])
    unbaselined = check.build_report("abc1234", "--build", "", HEALTH,
                                     {"S3A": {"metrics": m}}, _baseline_file())
    assert not unbaselined["passed"]
    assert not check.build_report("abc1234", "", "", HEALTH, {}, _baseline_file())["passed"]


def test_a_report_on_another_model_fails_rather_than_comparing():
    m = A.summarize(*_encounter())
    other = json.loads(json.dumps(HEALTH))
    other["gateway"]["realtime_model"] = "nto.gemini-live-2.5-flash-native-audio"
    r = check.build_report("abc1234", "", "", other, {"S2A": {"metrics": m}}, _baseline_file())
    assert not r["passed"] and "baseline was recorded on gpt-realtime-2.1" in r["failures"][0]


def test_committed_json_is_lf_sorted_and_utf8(tmp_path):
    p = tmp_path / "r.json"
    check.write_json(p, {"b": 1, "a": "’"})
    raw = p.read_bytes()
    assert b"\r" not in raw and raw.endswith(b"\n")
    assert raw.index(b'"a"') < raw.index(b'"b"') and "’".encode() in raw


# --- the committed baseline -------------------------------------------------------

def test_the_committed_baseline_covers_every_default_sequence():
    base = json.loads((ROOT / "tools" / "sim" / "baseline.json").read_text(encoding="utf-8"))
    assert base["realtime_model"] == serve.production_environment()["REALTIME_MODEL"], (
        "the baseline was recorded on a different model than production's actor_model: "
        "re-record it (tools/sim/README.md)")
    pending = base.get("pending") or {}
    for sid, steps in sequences.DEFAULT_SEQUENCES.items():
        entry = base["scenarios"].get(sid)
        if entry is None:
            # A scenario may wait for its first recording, but only by name and
            # with the reason and the command that records it: the check fails
            # for it until then, and says why.
            assert "--write-baseline" in pending.get(sid, ""), (
                f"no baseline for {sid}, and no `pending` entry saying why")
            continue
        assert sid not in pending, f"{sid} is both baselined and pending"
        assert set(A.BASELINE_KEYS) <= set(entry), sid
        # Offline, so CI catches it: a sequence or stimulus edited without
        # re-recording would make every later report compare against numbers
        # from a different experiment.
        assert entry["sequence_sha256"] == check.sequence_fingerprint(steps), (
            f"{sid}'s sequence or stimulus changed since its baseline was recorded; "
            f"re-record it with python -m tools.sim.check --write-baseline")
    assert set(A.DEFAULT_TOLERANCES) <= set(base["tolerances"])


# --- stimuli and sequences ----------------------------------------------------------

def test_every_default_sequence_parses_and_says_only_committed_lines():
    known = stim.lines()
    for sid, steps in sequences.DEFAULT_SEQUENCES.items():
        sequences.parse(steps)
        for name in sequences.lines_used(steps):
            assert name in known, f"{sid} says {name}, which lines.txt does not describe"
            assert stim.load(name), name


def test_nothing_is_committed_that_no_sequence_says():
    """The stimulus is binary in git history for good: keep it to what is used."""
    used = {n for steps in sequences.DEFAULT_SEQUENCES.values() for n in sequences.lines_used(steps)}
    files = {p.stem for p in stim.STIM_DIR.glob(f"*{stim.EXT}")}
    assert files == used, {"unused": sorted(files - used), "missing": sorted(used - files)}
    assert set(stim.lines()) == used


def test_the_stimulus_stays_small():
    total = sum(p.stat().st_size for p in stim.STIM_DIR.glob(f"*{stim.EXT}"))
    assert total < 3_000_000, f"{total} bytes of stimulus audio"


@pytest.mark.parametrize("bad", ["say", "shout:a1", "tone3", ""])
def test_a_malformed_step_is_named(bad):
    if not bad:
        assert sequences.parse(bad) == []
        return
    with pytest.raises(sequences.StepError, match="bad step"):
        sequences.parse(bad)


def test_ulaw_round_trip_is_close():
    from array import array
    vals = array("h", [0, 1, -1, 100, -100, 1000, -1000, 8000, -8000, 32767, -32768])
    if sys.byteorder == "big":
        vals.byteswap()
    back = array("h")
    back.frombytes(stim.ulaw_decode(stim.ulaw_encode(vals.tobytes())))
    if sys.byteorder == "big":
        back.byteswap()
        vals.byteswap()
    for a, b in zip(vals, back):
        assert abs(a - b) <= max(8, abs(a) // 16), (a, b)


def test_decoded_stimulus_is_speech_level_pcm():
    pcm = stim.load("a1")
    assert len(pcm) == 2 * (stim.STIM_DIR / "a1.ulaw").stat().st_size
    assert 1000 < stim.rms(pcm) < 20000


def test_room_tone_is_deterministic_and_under_the_speech_hint():
    """A quiet room, never speech: below server/voice/realtime.py VAD_HINT_RMS."""
    from server.voice.realtime import VAD_HINT_RMS

    a, b = stim.room_tone(2.5), stim.room_tone(2.5)
    assert a == b and len(a) == int(2.5 * stim.RATE) * 2
    assert 20 < stim.rms(a) < VAD_HINT_RMS / 2


# --- the sim server and the image ---------------------------------------------------

def test_image_paths_are_what_the_dockerfile_copies():
    text = (ROOT / "Dockerfile").read_text(encoding="utf-8")
    copied = {m.group(1).rstrip("/").lstrip("./") for m in
              re.finditer(r"(?m)^COPY\s+(\S+)\s", text)}
    assert copied | {"Dockerfile"} == set(check.IMAGE_PATHS)


def test_the_sim_server_takes_the_task_definitions_environment():
    env = serve.production_environment()
    tfvars = (ROOT / "infra" / "terraform" / "terraform.tfvars").read_text(encoding="utf-8")
    actor = re.search(r'(?m)^actor_model\s*=\s*"([^"]+)"', tfvars).group(1)
    assert env["REALTIME_MODEL"] == actor
    assert env["DIRECTOR_MODEL"] and env["CLAUDE_MODEL"]
    assert not set(env) & serve.NOT_LOCAL


def test_the_sim_server_reads_only_the_credentials_from_env(tmp_path):
    """Run in a child: configure() rewires server.llm's .env parse for the
    process, which must not leak into this test session. Prints key NAMES only."""
    code = (
        "import json, os, sys; from pathlib import Path\n"
        "from tools.sim import serve\n"
        f"applied = serve.configure(8123, Path({str(tmp_path)!r}), 'abc1234')\n"
        "from server import llm\n"
        "import dotenv\n"
        "print(json.dumps({'file': sorted(llm._FILE), 'archive': os.environ['ARCHIVE_SESSIONS_TO_S3'],\n"
        "  'build': os.environ['BUILD_SHA'], 'model': os.environ['REALTIME_MODEL'],\n"
        "  'aws': [k for k in ('AWS_ACCESS_KEY_ID','AWS_PROFILE') if k in os.environ],\n"
        "  'dotenv_noop': dotenv.load_dotenv() is False}))\n"
    )
    import os

    env = dict(os.environ, AWS_PROFILE="someone")
    out = subprocess.run([sys.executable, "-c", code], cwd=ROOT, capture_output=True,
                         text=True, encoding="utf-8", env=env, timeout=120)
    assert out.returncode == 0, out.stderr[-2000:]
    got = json.loads(out.stdout.strip().splitlines()[-1])
    assert set(got["file"]) <= set(serve.CREDENTIALS)
    assert got["archive"] == "0" and got["build"] == "abc1234" and got["aws"] == []
    assert got["model"] == serve.production_environment()["REALTIME_MODEL"]
    assert got["dotenv_noop"]


def test_a_changed_sequence_or_stimulus_invalidates_the_baseline():
    m = A.summarize(*_encounter())
    base = _baseline_file()
    steps = sequences.DEFAULT_SEQUENCES["S2A"]
    base["scenarios"]["S2A"]["sequence_sha256"] = check.sequence_fingerprint(steps)
    same = check.build_report("abc1234", "", "", HEALTH,
                              {"S2A": {"metrics": m, "sequence_sha256": check.sequence_fingerprint(steps)}},
                              base)
    assert same["passed"], same["failures"]
    moved = check.build_report("abc1234", "", "", HEALTH,
                               {"S2A": {"metrics": m,
                                        "sequence_sha256": check.sequence_fingerprint(steps + ",tone:1")}},
                               base)
    assert not moved["passed"] and "re-record" in moved["failures"][0]


def test_the_fingerprint_covers_the_stimulus_bytes(monkeypatch):
    steps = "say:a1"
    before = check.sequence_fingerprint(steps)
    monkeypatch.setattr(stim, "load", lambda name: b"\x00\x01")
    assert check.sequence_fingerprint(steps) != before


def test_a_pending_scenario_fails_and_says_why():
    m = A.summarize(*_encounter())
    base = _baseline_file()
    base["pending"] = {"S3A": "not recorded yet: the gateway was down. Record it with --write-baseline"}
    r = check.build_report("abc1234", "", "", HEALTH, {"S3A": {"metrics": m}}, base)
    assert not r["passed"] and "the gateway was down" in r["failures"][0]


def test_a_download_cut_short_while_the_session_closes_is_retried(monkeypatch):
    """The runner is still appending the last events when the check asks for
    events.jsonl, and the download route is a FileResponse: the file grows
    past the Content-Length it announced, the server aborts the body, and the
    client sees http.client.IncompleteRead, which is not an OSError. On the
    integrated 2026-09-28a tree that turned a finished S3A run into "did not
    run". A short body is one more reason to ask again, like a refused
    connection."""
    import http.client
    import io

    body = "\n".join(json.dumps(e) for e in (
        {"type": "user_turn", "t": 1.0}, {"type": "session_end", "t": 2.0})).encode()
    calls = {"n": 0}

    class _Resp(io.BytesIO):
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

    def fake_urlopen(req, timeout=None):
        calls["n"] += 1
        if calls["n"] == 1:
            raise http.client.IncompleteRead(b"", 51830)
        return _Resp(body)

    monkeypatch.setattr(check.urllib.request, "urlopen", fake_urlopen)
    monkeypatch.setattr(check.time, "sleep", lambda s: None)
    events = check.fetch_events("http://127.0.0.1:1", "s_1_abcdef", "", limit=30)
    assert calls["n"] == 2
    assert [e["type"] for e in events] == ["user_turn", "session_end"]
