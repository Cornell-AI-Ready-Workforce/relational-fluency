"""The participant speech check (pipeline 2026-09-30a; gpt route).

S4A s_1790781273_b8b7cc (2026-09-30, build 55de607, a real microphone):
gpt-4o-transcribe wrote "լավ.", "Democrat", "Tuurlijk.", "The", "Good
afternoon.", "Hi.", "Post.", "Sexuality" over noise and over the characters'
own voices, the room routed on them, and the characters answered nobody.
Heard again from each commit's own audio, every one came back [no speech]
and every short real line was heard (server/speech_check.py has the
measurement). These tests hold the pieces: which lines are checked, the audio
the bridge keeps for them, what the runner does with each answer, and a room
that waits for a line still being checked instead of routing without it.

Everything is offline: the check's one network call (speech_check._post) is
answered here, and by tests/conftest.py as a failure everywhere else.
"""
from __future__ import annotations

import asyncio
import sys
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
for p in (ROOT, ROOT / "tests"):
    if str(p) not in sys.path:
        sys.path.insert(0, str(p))

from server import llm  # noqa: E402
from server import realtime_voice_session as rvs  # noqa: E402
from server import speech_check as sc  # noqa: E402
from server.voice import realtime as R  # noqa: E402
from test_participant_turn_integrity import (  # noqa: E402
    GEMINI, GPT, LOUD, NATIVE, QUIET, DeadMember, _room, bridge, in_a_loop,
    next_transcript, runner_for, send_ms,
)

PCM = LOUD * 50            # one second of voice at 16 kHz


class Answers:
    """speech_check._post, answered with `text` (or by raising it), and
    every payload it was sent kept."""

    def __init__(self, text="[no speech]", delay=0.0):
        self.text, self.delay, self.payloads = text, delay, []

    async def __call__(self, payload, timeout):
        self.payloads.append(payload)
        if self.delay:
            await asyncio.sleep(self.delay)
        if isinstance(self.text, BaseException):
            raise self.text
        return self.text


@pytest.fixture
def answers(monkeypatch):
    def install(text="[no speech]", delay=0.0):
        a = Answers(text, delay)
        monkeypatch.setattr(sc, "_post", a)
        return a
    return install


def gpt_runner(scenario_id="S2A"):
    runner, session, ws = runner_for(scenario_id)
    runner.rt = bridge(GPT)
    return runner, session, ws


# --------------------------------------------------------------------------
# 1. Which lines are checked
# --------------------------------------------------------------------------

@pytest.mark.parametrize("text", [
    "Democrat", "Tuurlijk.", "The", "Good afternoon.", "Hi.", "Post.",
    "Sexuality", "Ehh", "Right now.", "Hello,", "I totally understand."])
def test_every_short_line_of_the_session_is_checked(text):
    assert sc.check_reason(text) == "short"


@pytest.mark.parametrize("text", ["լավ.", "嗯。", "रिवर्स टीम is late on this one"])
def test_a_line_in_another_script_is_checked_at_any_length(text):
    assert sc.check_reason(text) == "non_latin"


@pytest.mark.parametrize("text", ["José can take it.", "Nº 5 goes first now",
                                  "Five µs is fine, Chloé."])
def test_latin_text_with_accents_and_signs_is_not_another_script(text):
    assert not sc.non_latin(text)


def test_a_longer_line_is_checked_only_over_a_characters_playback():
    line = "What do you mean by locked?"               # six words
    assert sc.check_reason(line) is None
    assert sc.check_reason(line, during_playback=True) == "during_playback"
    long = "Right, so the effective date will be next year."
    assert sc.check_reason(long, during_playback=True) is None


def test_the_knobs_move_the_bounds(monkeypatch):
    monkeypatch.setenv("SPEECH_CHECK_MAX_WORDS", "1")
    assert sc.check_reason("Good afternoon.") is None
    monkeypatch.setenv("SPEECH_CHECK_PLAYBACK_MAX_WORDS", "2")
    assert sc.check_reason("Good afternoon.", during_playback=True) == "during_playback"
    assert sc.check_reason("Hi there Dan.", during_playback=True) is None


# --------------------------------------------------------------------------
# 2. The check itself
# --------------------------------------------------------------------------

@pytest.mark.parametrize("said", ["[no speech]", "[No speech].", "no speech",
                                  "(silence)", "[inaudible]", '"[no speech]"'])
def test_nobody_speaking_is_no_speech(answers, said):
    answers(said)
    assert asyncio.run(sc.hear(PCM))["outcome"] == "no_speech"


def test_what_it_heard_comes_back_with_the_prompt_and_the_audio(answers):
    a = answers("Hi Morgan.")
    out = asyncio.run(sc.hear(PCM, names=["Morgan"]))
    assert out["outcome"] == "heard" and out["text"] == "Hi Morgan."
    assert out["audio_ms"] == 1000 and out["model"] == "nto.gemini-3.1-flash-lite"
    (payload,) = a.payloads
    assert payload["model"] == "nto.gemini-3.1-flash-lite"
    assert payload["temperature"] == 0
    prompt, audio = payload["messages"][0]["content"]
    assert "[no speech]" in prompt["text"] and "Morgan" in prompt["text"]
    assert audio["input_audio"]["format"] == "wav"


def test_the_model_is_a_knob(answers, monkeypatch):
    a = answers("Yes.")
    monkeypatch.setenv("SPEECH_CHECK_MODEL", "nto.gemini-3.5-flash-lite")
    assert asyncio.run(sc.hear(PCM))["model"] == "nto.gemini-3.5-flash-lite"
    assert a.payloads[0]["model"] == "nto.gemini-3.5-flash-lite"


def test_a_check_that_fails_says_why_and_never_raises(answers, monkeypatch):
    answers(RuntimeError("401 bad key sk-abcdefghijklmnopqrstuvwxyz0123"))
    out = asyncio.run(sc.hear(PCM))
    assert out["outcome"] == "error" and out["text"] is None
    assert "sk-abcdefghijklmnopqrstuvwxyz0123" not in out["error"]
    answers("")
    assert asyncio.run(sc.hear(PCM))["outcome"] == "empty"
    answers("Yes.", delay=0.5)
    monkeypatch.setenv("SPEECH_CHECK_TIMEOUT_S", "0.05")
    assert asyncio.run(sc.hear(PCM))["outcome"] == "timeout"


def test_too_little_audio_is_not_sent(answers):
    a = answers()
    assert asyncio.run(sc.hear(LOUD * 5))["outcome"] == "no_audio"     # 100 ms
    assert asyncio.run(sc.hear(b""))["outcome"] == "no_audio"
    assert a.payloads == []


# --------------------------------------------------------------------------
# 3. The audio a commit held
# --------------------------------------------------------------------------

@in_a_loop
async def test_a_commits_transcript_carries_exactly_the_audio_it_committed():
    rt = bridge(GPT)
    await send_ms(rt, QUIET, 400)
    await rt.clear_input()                    # the participant's turn begins
    await send_ms(rt, LOUD, 600)
    await send_ms(rt, QUIET, 200)
    await rt.commit_input()
    rt.ws.feed(type="input_audio_buffer.committed", item_id="item_A")
    rt.ws.feed(type="conversation.item.input_audio_transcription.completed",
               item_id="item_A", transcript="Democrat")
    ev = await next_transcript(rt)
    assert ev["pcm"] == LOUD * 30 + QUIET * 10
    # The next commit holds its own audio only.
    await send_ms(rt, LOUD, 200)
    await rt.commit_input()
    rt.ws.feed(type="input_audio_buffer.committed", item_id="item_B")
    rt.ws.feed(type="conversation.item.input_audio_transcription.completed",
               item_id="item_B", transcript="Hi.")
    assert (await next_transcript(rt))["pcm"] == LOUD * 10


@in_a_loop
async def test_the_audio_kept_is_bounded():
    rt = bridge(GPT)
    await send_ms(rt, QUIET, 1000)
    await send_ms(rt, LOUD, R.COMMIT_AUDIO_KEEP_S * 1000)
    assert len(rt._commit_audio) == R.COMMIT_AUDIO_KEEP_S * R.CLIENT_RATE * 2
    assert bytes(rt._commit_audio[:len(LOUD)]) == LOUD, "the latest, not the first"
    for _ in range(R.COMMIT_AUDIO_TAGS + 3):
        await send_ms(rt, LOUD, 100)
        await rt.commit_input()
    held = [t for t in rt._commit_tags if t.get("pcm") is not None]
    assert len(held) == R.COMMIT_AUDIO_TAGS


@in_a_loop
async def test_no_audio_is_kept_off_the_participants_own_gpt_channel():
    member = bridge(GPT, bar=None)            # a room member hears the room
    await send_ms(member, LOUD, 400)
    assert not member._commit_audio
    gemini = bridge(GEMINI)                   # the gateway commits its buffer
    await send_ms(gemini, LOUD, 400)
    assert not gemini._commit_audio


def test_only_the_gpt_row_asks_for_the_check():
    assert R.speech_check_family(GPT)
    assert not R.speech_check_family(GEMINI)
    assert not R.speech_check_family(NATIVE)
    assert "participant_speech_check" in llm.provenance(GPT)["turn_gate"]
    assert "participant_speech_check" not in llm.provenance(NATIVE)["turn_gate"]
    assert llm.PIPELINE_VERSION >= "2026-09-30a"


# --------------------------------------------------------------------------
# 4. What the runner does with the answer
# --------------------------------------------------------------------------

@in_a_loop
async def test_a_line_nobody_said_is_suppressed_and_its_reply_withdrawn(answers):
    answers("[no speech]")
    runner, session, ws = gpt_runner()
    withdrawn = []

    async def withdraw(text, reason, committed_at, voiced_ms):
        withdrawn.append((text, reason))
    runner._withdraw_reply = withdraw
    await runner._await_participant("start")
    await runner._record_user_turn("Democrat", item_id="i1", voiced_ms=2800,
                                   voiced_span_ms=3100, pcm=PCM)
    assert not session.store.of("user_turn")
    (sup,) = session.store.of("user_turn_suppressed")
    assert sup["reason"] == "no_speech" and sup["speech_check"] == "short"
    assert sup["text"] == "Democrat"
    (chk,) = session.store.of("speech_check")
    assert chk["outcome"] == "no_speech" and chk["reason"] == "short"
    assert chk["item_id"] == "i1" and chk["audio_ms"] == 1000
    assert withdrawn == [("Democrat", "no_speech")]
    assert not ws.frames("user_transcript"), "no caption"
    assert session.shared_history == [], "nothing for steering or the director"
    assert runner._awaiting_participant, "a line nobody said opens nothing"
    assert runner._speech_checks_pending == 0


@in_a_loop
async def test_a_line_it_hears_is_written_with_the_live_text(answers):
    answers("Hi, Morgan.")
    runner, session, ws = gpt_runner()
    await runner._await_participant("start")
    await runner._record_user_turn("Hi Morgan.", voiced_ms=800, pcm=PCM)
    (turn,) = session.store.of("user_turn")
    assert turn["text"] == "Hi Morgan." and turn["speech_check"] == "heard"
    assert "transcript_original" not in turn
    (chk,) = session.store.of("speech_check")
    assert chk["heard"] == "Hi, Morgan."
    assert [f["text"] for f in ws.frames("user_transcript")] == ["Hi Morgan."]
    assert not runner._awaiting_participant, "a line it heard opens the conversation"


@in_a_loop
async def test_a_line_in_another_script_is_written_in_the_english_it_heard(answers):
    answers("Rivera's team is late.")
    runner, session, ws = gpt_runner()
    await runner._record_user_turn("रिवर्स टीम इज़ लेट", voiced_ms=1500, pcm=PCM)
    (turn,) = session.store.of("user_turn")
    assert turn["text"] == "Rivera's team is late."
    assert turn["transcript_original"] == "रिवर्स टीम इज़ लेट"
    assert turn["script_mismatch"] is False
    (cap,) = ws.frames("user_transcript")
    assert cap["text"] == "Rivera's team is late." and cap["unclear"] is False


@pytest.mark.parametrize("answer,outcome", [
    (RuntimeError("gateway down"), "error"), ("", "empty")])
def test_a_check_that_fails_keeps_the_line(answers, answer, outcome):
    answers(answer)
    runner, session, _ = gpt_runner()
    asyncio.run(runner._record_user_turn("Yes.", voiced_ms=700, pcm=PCM))
    (turn,) = session.store.of("user_turn")
    assert turn["text"] == "Yes." and turn["speech_check"] == outcome


@in_a_loop
async def test_lines_that_are_not_checked_never_reach_the_model(answers, monkeypatch):
    a = answers()
    runner, session, _ = gpt_runner()
    await runner._record_user_turn(
        "Okay, that's fine. Can we go over the entire schedule?", pcm=PCM)
    await runner._record_user_turn("Yes.")                  # no audio kept
    monkeypatch.setenv("SPEECH_CHECK", "0")
    await runner._record_user_turn("No.", pcm=PCM)
    monkeypatch.delenv("SPEECH_CHECK")
    runner.rt = bridge(NATIVE)                              # another route
    await runner._record_user_turn("Maybe.", pcm=PCM)
    assert a.payloads == []
    assert not session.store.of("speech_check")
    assert len(session.store.of("user_turn")) == 4
    assert all("speech_check" not in t for t in session.store.of("user_turn"))


@in_a_loop
async def test_a_line_begun_over_a_characters_playback_is_checked(answers):
    answers("[no speech]")
    runner, session, _ = gpt_runner()
    now = time.time()
    runner._last_played = {"agent_id": "morgan", "start": now - 5, "end": now + 3,
                           "text": "More than they should right now."}
    await runner._record_user_turn("They should right now, I guess.",
                                   spoken_at=now - 1, pcm=PCM)
    (chk,) = session.store.of("speech_check")
    assert chk["reason"] == "during_playback" and chk["during_playback"] is True
    assert not session.store.of("user_turn")
    # The same line two seconds after the playback ended is left alone.
    runner._last_played = {"agent_id": "morgan", "start": now - 10, "end": now - 3,
                           "text": "..."}
    await runner._record_user_turn("They should right now, I guess.",
                                   spoken_at=now - 1, pcm=PCM)
    assert len(session.store.of("speech_check")) == 1
    assert len(session.store.of("user_turn")) == 1


@in_a_loop
async def test_a_line_being_checked_counts_as_pending(answers):
    answers("Yes.", delay=0.2)
    runner, session, _ = gpt_runner()
    task = asyncio.ensure_future(runner._record_user_turn("Yes.", pcm=PCM))
    await asyncio.sleep(0.05)
    assert runner._speech_checks_pending == 1
    await task
    assert runner._speech_checks_pending == 0
    (chk,) = session.store.of("speech_check")
    assert chk["held_ms"] >= 150


# --------------------------------------------------------------------------
# 5. A room routes on the line once it is decided
# --------------------------------------------------------------------------

def _gpt_room(runner, session):
    room = _room(GPT, runner)
    for a in runner._resolve_agents():
        room.sessions[a.id] = DeadMember(GPT)
    runner.room = room
    runner.director = session.director
    session.append_agent("dan", "The date is locked.")
    return room


def test_the_room_waits_for_a_line_still_being_checked(answers, monkeypatch):
    """Before 30a nothing could be pending once a transcript had arrived, and
    the routing wait broke as soon as it had: here that routed on "" while
    the line was still being heard again."""
    monkeypatch.setenv("ROUTE_TRANSCRIPT_WAIT", "3")
    answers("Yes, go on.", delay=0.4)

    async def go():
        runner, session, _ = runner_for("S4A")
        _gpt_room(runner, session)
        runner._turn_end_arrivals = runner._transcripts_arrived
        line = asyncio.ensure_future(
            runner._record_user_turn("Yes, go on.", voiced_ms=700, pcm=PCM))
        await asyncio.sleep(0.05)
        await asyncio.wait_for(runner._run_group_turn(), timeout=10)
        await line
        return session

    session = asyncio.run(go())
    (_, text) = session.director.calls[0]
    assert text == "Yes, go on."


def test_a_room_turn_on_a_line_nobody_said_is_skipped(answers, monkeypatch):
    monkeypatch.setenv("ROUTE_TRANSCRIPT_WAIT", "3")
    answers("[no speech]", delay=0.2)

    async def go():
        runner, session, _ = runner_for("S4A")
        _gpt_room(runner, session)
        runner._turn_end_arrivals = runner._transcripts_arrived
        line = asyncio.ensure_future(
            runner._record_user_turn("Sexuality", voiced_ms=400, pcm=PCM))
        await asyncio.sleep(0.05)
        await asyncio.wait_for(runner._run_group_turn(), timeout=10)
        await line
        return session

    session = asyncio.run(go())
    assert session.director.calls == [], "nobody is answered for nothing"
    assert not session.store.of("user_turn")
    (sup,) = session.store.of("user_turn_suppressed")
    assert sup["reason"] == "no_speech"


@in_a_loop
async def test_the_follow_up_gap_holds_while_a_line_is_being_checked():
    runner, session, _ = runner_for("S4A")
    room = _gpt_room(runner, session)
    runner._play_cursor = runner._heard_end_at = time.time() - 5
    runner._speech_checks_pending = 1
    gap = asyncio.ensure_future(runner._await_followup_gap(room))
    await asyncio.sleep(0.3)
    assert not gap.done(), "the follow-up was granted over a line still being checked"
    runner._speech_checks_pending = 0
    assert await asyncio.wait_for(gap, 2) is None
