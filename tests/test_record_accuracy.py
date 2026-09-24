"""Record accuracy odds and ends: issue #23, pipeline 2026-09-23f.

Two defects in what the record says an agent line was (fix plan #23 (a) 7
and (b) 6-7):

  * THE DEFERRAL FILTER BLANKED SPOKEN LINES. _DEFERRAL was compiled with
    re.I, so its name slots ([A-Z][a-z]+) matched any word, and "That's for
    you to set.", "Go ahead and tell me..." and "You asked me for a number..."
    were recorded as no reply (transcript_missing, "the character spoke and
    no transcript arrived") although the participant heard every word. The
    name slots are case-sensitive now, and a line that did match is blanked
    only when none of its audio was relayed; a spoken one keeps its text and
    is flagged `deferral`.
  * NO HEARD TEXT. An interrupted or cap-truncated turn records the model's
    whole line, and the text stream runs ahead of the audio. Those turns now
    also carry generated_text and heard_text (the words that fit in the audio
    RELAYED for the turn, not the playback clock), so the analysis can choose
    which column raters score.

Offline: the runner is real, its store and page socket are the fakes from
tests/test_turn_instrumentation.py; nothing connects.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from server import encounter_record, llm  # noqa: E402
from server import realtime_voice_session as rvs  # noqa: E402
from server.voice import realtime as R  # noqa: E402
from server.voice import turn_audio  # noqa: E402

from test_turn_instrumentation import (  # noqa: E402
    CAPPED_DONE, CL_AUDIO, FakeRoom, FakeSession, FakeWS, in_a_loop,
    one_to_one, settle,
)


@pytest.fixture(autouse=True)
def knobs(monkeypatch):
    for knob in ("DEFERRAL_BLANK_AUDIBLE", "HEARD_TEXT_WPM",
                 "HEARD_TEXT_CALIBRATE"):
        monkeypatch.delenv(knob, raising=False)


def pcm(seconds: float) -> int:
    """Bytes of client-rate PCM16 in `seconds`."""
    return int(turn_audio.CLIENT_BYTES_PER_S * seconds)


# --------------------------------------------------------------------------
# 1. The deferral match
# --------------------------------------------------------------------------

# Heard on 2026-09-23 and blanked by the old pattern (fix plan #23 (a) 7).
SPOKEN = [
    "That's for you to set.",
    "Go ahead and tell me what you need from me.",
    "You asked me for a number, so here it is: six months.",
]

# What the filter exists for (commit e6b2ec0 and its docstring).
DEFERRALS = [
    "I'll wait for Casey to answer.",
    "I'll wait and hear what Jordan says.",
    "I'm holding for Chris.",
    "I'll stay quiet and let Jordan answer that.",
    "Go ahead, Casey.",
    "That's for Casey.",
    "That's Jordan's call.",
    "Let me let Priya answer.",
]


@pytest.mark.parametrize("line", SPOKEN)
def test_a_line_with_no_name_in_the_slot_is_not_a_deferral(line):
    assert rvs._is_deferral(line) is False


@pytest.mark.parametrize("line", DEFERRALS)
def test_the_deferrals_the_filter_was_written_for_still_match(line):
    assert rvs._is_deferral(line) is True


def test_the_lead_words_are_still_case_insensitive():
    assert rvs._is_deferral("i'll wait for Casey to answer.")
    assert rvs._is_deferral("GO AHEAD, Casey.")
    assert not rvs._is_deferral("I'll wait for you to finish.")


# --------------------------------------------------------------------------
# 2. A deferral is blanked only when nobody heard it
# --------------------------------------------------------------------------

@in_a_loop
async def test_a_spoken_deferral_keeps_its_text_and_is_flagged(monkeypatch):
    runner, session, ws, rt = one_to_one(monkeypatch)
    await runner._finalize_turn(runner.agent_id, runner.agent, rt,
                                ["That's for Casey."], None,
                                audio_bytes=pcm(1.0))
    (turn,) = session.store.of("assistant_turn")
    assert turn["text"] == "That's for Casey."
    assert turn["deferral"] is True and turn["transcript_missing"] is False
    assert not session.store.of("transcript_missing")
    (ev,) = session.store.of("deferral_output")
    assert ev["kept"] is True and ev["audio_ms"] == 1000
    assert ev["text"] == "That's for Casey."
    (pair,) = session.store.of("steering_pair")
    assert pair["actor"]["deferral"] is True


@in_a_loop
async def test_a_silent_deferral_is_still_blanked_and_written_down(monkeypatch):
    runner, session, ws, rt = one_to_one(monkeypatch)
    await runner._finalize_turn(runner.agent_id, runner.agent, rt,
                                ["I'll wait for Casey to answer."], None,
                                audio_bytes=0)
    (turn,) = session.store.of("assistant_turn")
    assert turn["text"] == "" and turn["deferral"] is False
    (ev,) = session.store.of("deferral_output")
    assert ev["kept"] is False and ev["audio_ms"] == 0
    assert ev["text"] == "I'll wait for Casey to answer."


@in_a_loop
async def test_the_old_blanking_is_one_knob_away(monkeypatch):
    monkeypatch.setenv("DEFERRAL_BLANK_AUDIBLE", "1")
    runner, session, ws, rt = one_to_one(monkeypatch)
    await runner._finalize_turn(runner.agent_id, runner.agent, rt,
                                ["That's for Casey."], None,
                                audio_bytes=pcm(1.0))
    (turn,) = session.store.of("assistant_turn")
    assert turn["text"] == ""
    (ev,) = session.store.of("deferral_output")
    assert ev["kept"] is False and ev["text"] == "That's for Casey."


@in_a_loop
async def test_a_spoken_line_the_old_pattern_blanked_is_recorded(monkeypatch):
    runner, session, ws, rt = one_to_one(monkeypatch)
    await runner._finalize_turn(runner.agent_id, runner.agent, rt,
                                [SPOKEN[0]], None, audio_bytes=pcm(1.0))
    (turn,) = session.store.of("assistant_turn")
    assert turn["text"] == SPOKEN[0] and turn["deferral"] is False
    assert not session.store.of("deferral_output")


def room_runner():
    session = FakeSession("S4A")
    runner = rvs.RealtimeVoiceSessionRunner(session, FakeWS())
    agent = runner._resolve_agents()[0]
    runner.room = FakeRoom(speaking=agent.id)
    return runner, session, agent


@in_a_loop
async def test_the_room_applies_the_same_rule(monkeypatch):
    runner, session, agent = room_runner()
    await runner._finalize_member(agent, "I'm holding for Chris.",
                                  audio_bytes=pcm(0.8))
    await runner._finalize_member(agent, "I'll wait for Priya to answer.")
    kept, blanked = session.store.of("assistant_turn")
    assert kept["text"] == "I'm holding for Chris." and kept["deferral"] is True
    assert blanked["text"] == "" and blanked["deferral"] is False
    assert [e["kept"] for e in session.store.of("deferral_output")] == [True, False]


# --------------------------------------------------------------------------
# 3. heard_text beside the generated line
# --------------------------------------------------------------------------

LONG = ("We slipped the date and I think we have to say so plainly, because "
        "the alternative is telling them in December that we knew in "
        "September and chose not to mention it.")


def test_the_estimate_is_the_words_that_fit_in_the_relayed_audio():
    est = turn_audio.heard_estimate(LONG, 2000, 170)
    # 2 s at 170 wpm = 5.67 words -> 6.
    assert est["heard_words"] == 6 and est["generated_words"] == len(LONG.split())
    assert est["heard_text"] == "We slipped the date and I…"
    whole = turn_audio.heard_estimate("Fine, lock it.", 5000, 170)
    assert whole["heard_text"] == "Fine, lock it."
    assert turn_audio.heard_estimate(LONG, 0, 170)["heard_text"] == ""


@in_a_loop
async def test_an_interrupted_turn_records_both_lines(monkeypatch, tmp_path):
    runner, session, ws, rt = one_to_one(monkeypatch)
    await runner._finalize_turn(runner.agent_id, runner.agent, rt, [LONG],
                                None, interrupted=True, audio_bytes=pcm(2.0))
    (turn,) = session.store.of("assistant_turn")
    assert turn["text"] == LONG, "the generated line is still the turn's text"
    assert turn["generated_text"] == LONG
    assert turn["heard_text"] == "We slipped the date and I…"
    est = turn["heard_estimate"]
    assert est["basis"] == "relayed_audio" and est["audio_ms"] == 2000
    assert est["wpm"] == 170.0 and est["wpm_source"] == "default"
    (pair,) = session.store.of("steering_pair")
    assert pair["actor"]["heard_text"] == turn["heard_text"]
    # ...and both reach the artefact a rater is handed.
    lines = [json.dumps(e) for e in session.store.events]
    (tmp_path / "events.jsonl").write_text("\n".join(lines) + "\n",
                                           encoding="utf-8")
    agent_turns = [t for t in encounter_record.build(tmp_path)["transcript"]
                   if t["role"] == "agent"]
    assert agent_turns[0]["text"] == LONG
    assert agent_turns[0]["heard_text"] == turn["heard_text"]


@in_a_loop
async def test_a_cap_truncated_turn_records_both_lines(monkeypatch):
    runner, session, ws, rt = one_to_one(monkeypatch)
    rt.feed({"type": "agent_transcript_delta", "text": LONG})
    for _ in range(10):                      # 10 x 0.2 s relayed
        rt.feed({"type": "agent_audio", "pcm": CL_AUDIO})
    rt.feed(dict(CAPPED_DONE))
    rt.end()
    await runner._pump_events(rt)
    await settle(runner)
    (turn,) = session.store.of("assistant_turn")
    assert turn["cap_truncated"] is True and turn["interrupted"] is False
    assert turn["generated_text"] == turn["text"]
    assert turn["heard_estimate"]["audio_ms"] == 2000
    assert turn["heard_text"].endswith("…")
    assert rt.retries == 0, "a cap-cut line must not be re-spoken"


@in_a_loop
async def test_a_whole_turn_carries_no_heard_fields(monkeypatch):
    runner, session, ws, rt = one_to_one(monkeypatch)
    await runner._finalize_turn(runner.agent_id, runner.agent, rt, [LONG],
                                None, audio_bytes=pcm(9.0))
    (turn,) = session.store.of("assistant_turn")
    assert "heard_text" not in turn and "generated_text" not in turn
    assert session.store.of("steering_pair")[0]["actor"]["heard_text"] is None


@in_a_loop
async def test_the_rate_is_the_characters_own_once_measured(monkeypatch):
    runner, session, ws, rt = one_to_one(monkeypatch)
    thirty = " ".join(f"word{i}" for i in range(30)) + "."
    # Two whole turns: 60 words over 24 s is 150 wpm.
    for _ in range(2):
        await runner._finalize_turn(runner.agent_id, runner.agent, rt,
                                    [thirty], None, audio_bytes=pcm(12.0))
    await runner._finalize_turn(runner.agent_id, runner.agent, rt, [LONG],
                                None, interrupted=True, audio_bytes=pcm(4.0))
    est = session.store.of("assistant_turn")[-1]["heard_estimate"]
    assert est["wpm_source"] == "encounter" and est["wpm"] == 150.0
    assert est["heard_words"] == 10


@in_a_loop
async def test_calibration_and_the_default_rate_are_knobs(monkeypatch):
    monkeypatch.setenv("HEARD_TEXT_CALIBRATE", "0")
    monkeypatch.setenv("HEARD_TEXT_WPM", "120")
    runner, session, ws, rt = one_to_one(monkeypatch)
    thirty = " ".join(f"word{i}" for i in range(30)) + "."
    for _ in range(2):
        await runner._finalize_turn(runner.agent_id, runner.agent, rt,
                                    [thirty], None, audio_bytes=pcm(12.0))
    await runner._finalize_turn(runner.agent_id, runner.agent, rt, [LONG],
                                None, interrupted=True, audio_bytes=pcm(4.0))
    est = session.store.of("assistant_turn")[-1]["heard_estimate"]
    assert est["wpm_source"] == "default" and est["wpm"] == 120.0
    assert est["heard_words"] == 8


@in_a_loop
async def test_a_room_turn_uses_its_relayed_bytes_not_the_play_clock(monkeypatch):
    runner, session, agent = room_runner()
    await runner._finalize_member(agent, LONG, interrupted=True,
                                  audio_bytes=pcm(2.0))
    (turn,) = session.store.of("assistant_turn")
    assert turn["text"] == LONG and turn["generated_text"] == LONG
    assert turn["heard_text"] == "We slipped the date and I…"
    assert turn["heard_estimate"]["basis"] == "relayed_audio"


# --------------------------------------------------------------------------
# 4. On the record
# --------------------------------------------------------------------------

def test_the_record_rules_are_in_provenance(monkeypatch):
    prov = llm.provenance("gpt-realtime-2.1")
    assert prov["pipeline_version"] >= "2026-09-23f"
    assert prov["record"] == {
        "deferral_blank": "no_audio_only",
        "deferral_names": "case_sensitive",
        "heard_text": {"turns": "interrupted_or_cap_truncated",
                       "basis": "relayed_audio", "wpm": 170.0,
                       "calibrate": True},
    }
    monkeypatch.setenv("DEFERRAL_BLANK_AUDIBLE", "1")
    monkeypatch.setenv("HEARD_TEXT_WPM", "150")
    monkeypatch.setenv("HEARD_TEXT_CALIBRATE", "0")
    rec = R.record_provenance()
    assert rec["deferral_blank"] == "always"
    assert rec["heard_text"]["wpm"] == 150.0
    assert rec["heard_text"]["calibrate"] is False
