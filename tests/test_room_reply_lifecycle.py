"""Room reply lifecycle and clocks: issues #23 and #24, pipeline 2026-09-23e,
room pacing 2026-09-23c.

Four defects, each from the diagnosis (fix plan #23 (a) 5-6, #24 (a) 3 and 5):

  * THE DOUBLE REQUEST. On gpt-realtime-2.1 the commit itself starts the
    reply (response.created 0.41-0.88 s after it, diag track3
    probe_commit_create and review3 probe_silence_commit), and give_floor
    sent a response.create behind it anyway, because autofire_wait=0 ended
    its wait before created could arrive. Refused, that is "the extra
    response.create was refused" on nearly every grant; granted, a second
    reply. A grant there is now the commit alone, with one create only if
    nothing started within ROOM_GRANT_UNANSWERED_S.
  * THE ZERO-AUDIO ADOPTION. A suppressed hold whose cancel the gpt route
    honoured is a fragment, and one that finished with no audio is a line
    nobody heard; adopt_member played both as turns ("That works for",
    audio_ms 0, S4A 2026-09-23). Refused now, and written down with the text.
  * THE ACCUMULATING PLAYBACK CLOCK. st.play_start was reset only on the
    adopt path, so heard_seconds / total_seconds grew across a member's turns
    (159.5, 284.4 s). Reset at every announce now.
  * THE SPLIT TURN and THE PROBE CLOCK. A participant resuming just after
    their own commit cancelled the reply to their own words; and the silence
    probe counted from generation end and looked every 12 s, so a participant
    had about 4 s of real silence before a "12 s" probe.

Offline: the bridge sessions here are real RealtimeVoiceSession objects on a
fake socket (tests/test_bridge_correctness.py's WireWS); nothing connects.
"""
from __future__ import annotations

import asyncio
import sys
import time
import types
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from server import group_room as gr  # noqa: E402
from server import llm  # noqa: E402
from server import realtime_voice_session as rvs  # noqa: E402
from server.voice import realtime as R  # noqa: E402

from test_bridge_correctness import (  # noqa: E402
    GPT, LOUD, NATIVE, FakeSession, PageWS, ReplayRT, bridge, created,
    in_a_loop, settle, until,
)


@pytest.fixture(autouse=True)
def quick(monkeypatch):
    monkeypatch.setattr(R, "RECV_POLL_S", 0.02)
    monkeypatch.setenv("TRANSCRIPT_GRACE_SECONDS", "0.2")
    monkeypatch.delenv("AUTOFIRE_WAIT", raising=False)
    for knob in ("ROOM_COMMIT_ONLY_GRANT", "ROOM_GRANT_UNANSWERED_S",
                 "ROOM_ADOPT_GUARD", "ROOM_SPLIT_TURN_S", "PROBE_TICK_SECONDS",
                 "PROBE_IDLE_FROM_PLAYBACK", "PROBE_AFTER_SECONDS"):
        monkeypatch.delenv(knob, raising=False)


class Agent:
    def __init__(self, aid):
        self.id, self.name = aid, aid.title()


def gpt_room():
    room = gr.GroupRoom([Agent("dan")], instructions_for=lambda a: "",
                        voice_for=lambda a: "", model=GPT)
    rt = bridge(GPT)
    room.sessions["dan"] = rt
    return room, rt


async def committed(rt):
    await until(lambda: "input_audio_buffer.commit" in rt.ws.types())


# --------------------------------------------------------------------------
# 1. give_floor on gpt: the commit is the request
# --------------------------------------------------------------------------

@in_a_loop
async def test_a_gpt_grant_is_the_commit_alone_when_the_commit_starts_the_reply():
    room, rt = gpt_room()
    agen = rt.events()
    reader = asyncio.ensure_future(agen.__anext__())
    grant = asyncio.ensure_future(room.give_floor("dan"))
    await committed(rt)
    await asyncio.sleep(0.15)                   # the gateway's ~0.4 s, shortened
    rt.ws.feed([created("resp_1")])
    assert await asyncio.wait_for(grant, 2) is rt
    # Booked as a reply WE asked for, so response.created is not read as an
    # auto-fire and the bridge's own unanswered clock watches it. (Read before
    # the reader is stopped: stopping events() ends the reply.)
    assert rt._requested is True and rt.autofire_active is False
    assert rt._response_created_id == "resp_1"
    reader.cancel()
    await asyncio.gather(reader, return_exceptions=True)
    sent = rt.ws.types()
    assert sent[-1] == "input_audio_buffer.commit"
    assert "response.create" not in sent, (
        "a response.create went out behind the reply the commit started: "
        "refused on the gateway, or a second reply")
    assert room.last_grant["via"] == "commit"
    assert room.grant_fallback_creates == 0


@in_a_loop
async def test_a_commit_that_starts_nothing_gets_exactly_one_create(monkeypatch):
    monkeypatch.setenv("ROOM_GRANT_UNANSWERED_S", "0.2")
    room, rt = gpt_room()
    t0 = time.time()
    assert await room.give_floor("dan") is rt
    sent = rt.ws.types()
    assert sent.count("response.create") == 1
    assert sent.index("response.create") > sent.index("input_audio_buffer.commit")
    assert time.time() - t0 >= 0.2
    assert room.last_grant["via"] == "commit+create_fallback"
    assert room.grant_fallback_creates == 1


@in_a_loop
async def test_a_reply_that_finished_inside_the_wait_is_not_asked_for_again(monkeypatch):
    """response.done clears every in-flight flag; output after the commit is
    still evidence the commit was answered."""
    monkeypatch.setenv("ROOM_GRANT_UNANSWERED_S", "0.5")
    room, rt = gpt_room()

    async def whole_reply_then_done():
        await committed(rt)
        rt._last_output_at = time.time()
        rt._end_response()

    helper = asyncio.ensure_future(whole_reply_then_done())
    await room.give_floor("dan")
    await helper
    assert "response.create" not in rt.ws.types()
    assert room.last_grant["via"] == "commit"


@in_a_loop
async def test_a_reply_cut_off_during_the_wait_is_not_asked_for(monkeypatch):
    monkeypatch.setenv("ROOM_GRANT_UNANSWERED_S", "0.5")
    room, rt = gpt_room()

    async def participant_barges():
        await committed(rt)
        await rt.cancel_response()

    helper = asyncio.ensure_future(participant_barges())
    await room.give_floor("dan")
    await helper
    assert "response.create" not in rt.ws.types()
    assert room.last_grant["via"] == "commit_cancelled"


@in_a_loop
async def test_the_old_grant_is_one_knob_away(monkeypatch):
    monkeypatch.setenv("ROOM_COMMIT_ONLY_GRANT", "0")
    room, rt = gpt_room()
    await room.give_floor("dan")
    sent = rt.ws.types()
    assert sent[-2:] == ["input_audio_buffer.commit", "response.create"]
    assert room.last_grant["via"] == "commit+create"
    assert llm.provenance(GPT)["pacing"]["room_grant"] == "commit_and_create"


def test_the_room_grant_wait_is_bounded(monkeypatch):
    """6 s by default since room pacing 2026-09-24a (was 3, kept under the
    bridge's own 6 s; the grant now holds that bar off instead, see
    tests/test_voice_and_rate_gates.py), and never more than 15."""
    assert R.room_grant_unanswered_s() == 6.0
    monkeypatch.setenv("ROOM_GRANT_UNANSWERED_S", "30")
    assert R.room_grant_unanswered_s() == 15.0
    monkeypatch.setenv("ROOM_GRANT_UNANSWERED_S", "-1")
    assert R.room_grant_unanswered_s() == 0.0


@in_a_loop
async def test_a_fallback_create_is_on_the_record(monkeypatch):
    monkeypatch.setenv("ROOM_GRANT_UNANSWERED_S", "0.1")
    session = FakeSession("S4A")
    runner = rvs.RealtimeVoiceSessionRunner(session, PageWS())
    room, rt = gpt_room()
    runner.room = room
    assert await runner._grant("dan") is rt
    (ev,) = session.store.of("grant_fallback_create")
    assert ev["agent_id"] == "dan" and ev["waited_s"] >= 0.1


# --------------------------------------------------------------------------
# 2. adopt_member: never a fragment, never a line with no audio
# --------------------------------------------------------------------------

def runner_for(scenario="S4A"):
    session = FakeSession(scenario)
    runner = rvs.RealtimeVoiceSessionRunner(session, PageWS())
    runner._speech_started_at = time.time() - 5
    return runner, session


def hold(runner, agent_id, *, text="That works for", audio=0, done=True,
         cut=False):
    st = rvs._MemberState()
    st.begin_hold("resp_h")
    st.hold({"type": "agent_transcript_delta", "text": text})
    for _ in range(audio):
        st.hold({"type": "agent_audio", "pcm": b"\x00" * 3200})
    st.cut_by_suppression = cut
    if done:
        st.mode, st.done_at = "held_done", time.time()
    runner._member_states[agent_id] = st
    return st


@in_a_loop
async def test_a_completed_hold_with_no_audio_is_refused_and_kept():
    runner, session = runner_for()
    agent = runner._resolve_agents()[0].id
    hold(runner, agent)
    assert await runner.adopt_member(agent) is False
    assert not session.store.of("held_reply_adopted")
    assert not session.store.of("assistant_turn"), (
        "a line nobody heard was recorded as spoken")
    (ev,) = session.store.of("held_reply_refused")
    assert ev["reason"] == "no_audio" and ev["text"] == "That works for"
    assert ev["audio_ms"] == 0


@in_a_loop
async def test_a_hold_the_suppression_cut_short_is_refused_while_streaming():
    runner, session = runner_for()
    agent = runner._resolve_agents()[0].id
    st = hold(runner, agent, audio=2, done=False, cut=True)
    assert await runner.adopt_member(agent) is False
    (ev,) = session.store.of("held_reply_refused")
    assert ev["reason"] == "cancelled_by_suppression" and ev["mode"] == "holding"
    assert ev["audio_ms"] == 200
    # The pump keeps the rest of that reply suppressed (see below).
    assert st.refused is True and st.mode == "discarding"


@in_a_loop
async def test_a_streaming_gemini_hold_with_no_audio_yet_is_still_adopted():
    """Text leads audio, and on Gemini the cancel is inert: the hold is the
    whole reply, still arriving. Refusing it would leave that member silent
    (give_floor sees the reply already in flight and asks for nothing)."""
    runner, session = runner_for()
    agent = runner._resolve_agents()[0].id
    hold(runner, agent, done=False, cut=False)
    assert await runner.adopt_member(agent) is True
    assert session.store.of("held_reply_adopted")


@in_a_loop
async def test_the_adopt_guard_is_one_knob_away(monkeypatch):
    monkeypatch.setenv("ROOM_ADOPT_GUARD", "0")
    runner, session = runner_for()
    agent = runner._resolve_agents()[0].id
    hold(runner, agent)
    assert await runner.adopt_member(agent) is True
    assert not session.store.of("held_reply_refused")


@in_a_loop
async def test_an_adopted_completed_hold_records_the_audio_it_played():
    runner, session = runner_for()
    agent = runner._resolve_agents()[0].id
    hold(runner, agent, text="Fine, let's lock it.", audio=5)
    assert await runner.adopt_member(agent) is True
    (turn,) = session.store.of("assistant_turn")
    assert turn["text"] == "Fine, let's lock it."
    assert turn["audio_ms"] == 500, "the held_done path wrote audio_ms 0"


def test_a_whole_line_replaces_the_held_deltas_rather_than_following_them():
    st = rvs._MemberState()
    st.begin_hold("r")
    for w in ("That", " works", " for"):
        st.hold({"type": "agent_transcript_delta", "text": w})
    st.hold({"type": "agent_audio", "pcm": b"\x00" * 320})
    st.hold({"type": "agent_transcript", "text": "That works for me."})
    assert st.held_text() == "That works for me."
    assert st.held_seconds() == 0.01


@in_a_loop
async def test_the_pump_marks_a_gpt_hold_as_cut_and_keeps_a_refused_one_quiet():
    """Through _pump_member: a gpt member's unsolicited reply is suppressed and
    cancelled (so its hold is a fragment); once a grant has refused that hold,
    the rest of it is not spliced in as the head of the fresh reply, and the
    fresh reply, under its own id, is relayed as the turn."""
    session = FakeSession("S4A")
    ws = PageWS()
    runner = rvs.RealtimeVoiceSessionRunner(session, ws)
    agent = runner._resolve_agents()[0]
    room = types.SimpleNamespace(speaking="somebody_else")

    async def hear(pcm, exclude=None):
        pass
    room.hear = hear
    room.session_for = lambda aid: None
    runner.room = room
    gate = asyncio.Event()

    class RT(ReplayRT):
        async def events(self):
            yield {"type": "agent_transcript_delta", "text": "Then put",
                   "response_id": "resp_old"}
            await gate.wait()
            # the grant has happened: the old reply's tail, then the new one
            yield {"type": "agent_transcript_delta", "text": " it",
                   "response_id": "resp_old"}
            yield {"type": "response_done", "response_id": "resp_old"}
            yield {"type": "agent_audio", "pcm": b"\x00" * 16000,
                   "response_id": "resp_new"}
            yield {"type": "agent_transcript", "text": "Let's confirm owners first."}
            yield {"type": "response_done", "response_id": "resp_new"}

    pump = asyncio.ensure_future(runner._pump_member(agent, RT([])))
    st = None
    await until(lambda: agent.id in runner._member_states
                and runner._member_states[agent.id].mode == "holding")
    st = runner._member_states[agent.id]
    assert st.cut_by_suppression is True
    assert await runner.adopt_member(agent.id) is False
    room.speaking = agent.id
    gate.set()
    await asyncio.wait_for(pump, 2)
    await settle(runner)
    turns = session.store.of("assistant_turn")
    assert [t["text"] for t in turns] == ["Let's confirm owners first."]
    assert turns[0]["audio_ms"] == 500
    assert len(ws.frames("assistant_started")) == 1


# --------------------------------------------------------------------------
# 3. The room's playback clock is per turn
# --------------------------------------------------------------------------

def reply(rid, seconds, text):
    return [{"type": "agent_audio", "pcm": b"\x00" * int(32000 * seconds),
             "response_id": rid},
            {"type": "agent_transcript", "text": text},
            {"type": "response_done", "response_id": rid}]


@in_a_loop
async def test_each_turn_has_its_own_playback_span():
    session = FakeSession("S4A")
    runner = rvs.RealtimeVoiceSessionRunner(session, PageWS())
    agent = runner._resolve_agents()[0]
    room = types.SimpleNamespace(speaking=agent.id, session_for=lambda a: None)

    async def hear(pcm, exclude=None):
        pass
    room.hear = hear
    runner.room = room
    evs = reply("r1", 0.5, "First.") + reply("r2", 0.25, "Second.")
    await runner._pump_member(agent, ReplayRT(evs))
    await settle(runner)
    first, second = session.store.of("assistant_turn")
    span1 = first["play_clock_end"] - first["play_clock_start"]
    span2 = second["play_clock_end"] - second["play_clock_start"]
    assert span1 == pytest.approx(0.5, abs=0.01)
    assert span2 == pytest.approx(0.25, abs=0.01), (
        "the second turn's clock started at the first turn's start")
    assert second["play_clock_start"] >= first["play_clock_end"] - 0.01
    st = runner._member_states[agent.id]
    assert runner._heard_seconds(st) <= 0.26


def cut_runner(holder_announced):
    """Chris holds the floor as a follow-up; Dan's line is still playing;
    Chris's clock is from his previous turn. The participant cuts in."""
    runner, session, ws, speaker = room_runner(30, 0.2)
    runner.room.speaking = "chris"
    now = time.time()
    chris = rvs._MemberState()
    chris.play_start, chris.play_end = now - 20, now - 18.1   # 16 s ago, 1.9 s
    runner._member_states["chris"] = chris
    runner._member_turns["chris"] = ([], {"announced": holder_announced,
                                          "settled": asyncio.Event()})
    runner._play_cursor = now + 4
    runner._last_played = {"agent_id": "dan", "start": now - 2,
                           "end": now + 4, "text": "one two three four five six"}
    return runner, session


@in_a_loop
async def test_a_cut_is_written_for_the_line_playing_not_a_stale_clock():
    runner, session = cut_runner(holder_announced=False)
    await runner._client_to_model()
    (cut,) = session.store.of("playback_cut")
    assert cut["agent_id"] == "dan", (
        "the floor holder's previous turn was written as the line cut off")
    assert cut["total_seconds"] == 6.0
    assert 1.9 <= cut["heard_seconds"] <= cut["total_seconds"]


@in_a_loop
async def test_the_floor_holders_own_cut_is_its_current_turn():
    runner, session = cut_runner(holder_announced=True)
    chris = runner._member_states["chris"]
    chris.new_turn()                                   # as the pump does
    now = time.time()
    chris.play_start, chris.play_end = now - 1, now + 2
    await runner._client_to_model()
    (cut,) = session.store.of("playback_cut")
    assert cut["agent_id"] == "chris" and cut["total_seconds"] == 3.0
    assert cut["heard_seconds"] <= cut["total_seconds"]


# --------------------------------------------------------------------------
# 4. The split turn
# --------------------------------------------------------------------------

class Speaker:
    def __init__(self):
        self.cancels = 0

    async def cancel_response(self, **kw):
        self.cancels += 1


def room_runner(commit_ago, granted_ago):
    session = FakeSession("S4A")
    ws = PageWS([LOUD] * 25)
    runner = rvs.RealtimeVoiceSessionRunner(session, ws)
    speaker = Speaker()
    room = types.SimpleNamespace(speaking="dan", session_for=lambda a: speaker)

    async def hear(pcm, exclude=None):
        pass
    room.hear = hear
    runner.room = room
    now = time.time()
    runner._participant_commit_at = now - commit_ago
    runner._floor_granted_at = now - granted_ago
    return runner, session, ws, speaker


@in_a_loop
async def test_resuming_just_after_ones_own_commit_does_not_cancel_the_reply():
    runner, session, ws, speaker = room_runner(0.5, 0.2)
    await runner._client_to_model()
    assert speaker.cancels == 0, "the reply to the participant's own words was cut"
    assert not ws.frames("assistant_interrupted")
    (ev,) = session.store.of("split_turn_extended")
    assert ev["agent_id"] == "dan" and 0.4 <= ev["since_commit_s"] <= 1.5


@in_a_loop
async def test_a_barge_in_later_than_the_window_still_cancels():
    runner, session, ws, speaker = room_runner(3.0, 2.0)
    await runner._client_to_model()
    assert speaker.cancels == 1
    assert ws.frames("assistant_interrupted")
    assert not session.store.of("split_turn_extended")


@in_a_loop
async def test_a_reply_granted_before_the_commit_is_not_the_participants_own():
    runner, session, ws, speaker = room_runner(0.5, 0.8)
    await runner._client_to_model()
    assert speaker.cancels == 1


@in_a_loop
async def test_the_split_turn_window_is_one_knob_away(monkeypatch):
    monkeypatch.setenv("ROOM_SPLIT_TURN_S", "0")
    runner, session, ws, speaker = room_runner(0.5, 0.2)
    await runner._client_to_model()
    assert speaker.cancels == 1


# --------------------------------------------------------------------------
# 5. The silence probe counts from when the room went quiet
# --------------------------------------------------------------------------

def one_to_one_probe(monkeypatch, *, after="0.3", tick="0.02"):
    monkeypatch.setenv("PROBE_AFTER_SECONDS", after)
    monkeypatch.setenv("PROBE_TICK_SECONDS", tick)
    runner = rvs.RealtimeVoiceSessionRunner(FakeSession("S2A"), PageWS())
    runner.room = None
    fired = []

    async def brief(**kw):
        pass

    async def probe():
        fired.append(time.time())
        runner._closed = True
    runner._next_trigger = lambda: {"on_silence": True}
    runner._brief_next_beat = brief
    runner._probe_commit = probe
    return runner, fired


@in_a_loop
async def test_the_probe_waits_for_the_last_reply_to_finish_playing(monkeypatch):
    runner, fired = one_to_one_probe(monkeypatch)
    now = time.time()
    runner._last_activity = now - 5            # generation ended long ago...
    runner._play_cursor = now + 0.3            # ...and it is still playing
    await asyncio.wait_for(runner._silence_watchdog(), 3)
    assert fired, "the probe never fired"
    waited = fired[0] - runner._play_cursor
    assert 0.3 - 0.01 <= waited <= 0.3 + 0.15, (
        f"probe fired {waited:.2f}s after playback ended, not 0.3s")


@in_a_loop
async def test_the_old_probe_clock_is_one_knob_away(monkeypatch):
    monkeypatch.setenv("PROBE_IDLE_FROM_PLAYBACK", "0")
    runner, fired = one_to_one_probe(monkeypatch)
    now = time.time()
    runner._last_activity = now - 5
    runner._play_cursor = now + 5
    await asyncio.wait_for(runner._silence_watchdog(), 3)
    assert fired and fired[0] < runner._play_cursor - 4


@in_a_loop
async def test_a_one_to_one_reply_moves_the_playback_clock():
    runner = rvs.RealtimeVoiceSessionRunner(FakeSession("S2A"), PageWS())
    runner.room = None

    class RT:
        model = GPT
        discards_cancelled_output = True

        async def events(self):
            yield {"type": "agent_audio", "pcm": b"\x00" * 32000,
                   "response_id": "r"}

    t0 = time.time()
    await runner._pump_events(RT())
    assert runner._play_cursor == pytest.approx(t0 + 1.0, abs=0.05)
    assert runner._quiet_since() == pytest.approx(runner._play_cursor)


@in_a_loop
async def test_a_room_probe_is_not_spawned_while_the_floor_is_held(monkeypatch):
    monkeypatch.setenv("PROBE_AFTER_SECONDS", "0.05")
    monkeypatch.setenv("PROBE_TICK_SECONDS", "0.02")
    runner = rvs.RealtimeVoiceSessionRunner(FakeSession("S4A"), PageWS())
    runner.room = types.SimpleNamespace(speaking=None)
    runner._next_trigger = lambda: {"on_silence": True}
    runner._last_activity = time.time() - 5
    spawned = []
    runner._spawn_group_turn = lambda coro: (spawned.append(1), coro.close())
    async with runner._floor:
        task = asyncio.ensure_future(runner._silence_watchdog())
        await asyncio.sleep(0.2)
        assert spawned == []
    await asyncio.sleep(0.1)
    runner._closed = True
    await asyncio.wait_for(task, 1)
    assert spawned, "the probe was never spawned once the floor was free"


# --------------------------------------------------------------------------
# 6. Provenance
# --------------------------------------------------------------------------

def test_the_pacing_knobs_are_on_the_record():
    prov = llm.provenance(GPT)
    assert prov["pipeline_version"] >= "2026-09-23e"
    assert prov["room_pacing_version"] >= "2026-09-23c"
    assert prov["pacing"] == {
        "room_grant": "commit_only",
        "room_grant_unanswered_s": 6.0,
        "room_adopt_guard": True,
        "room_split_turn_s": 1.5,
        "room_play_clock": "per_turn",
        "probe_after_s": 12.0,
        "probe_tick_s": 1.0,
        "probe_idle_from": "playback",
        "handoff_idle_s": 3.0,
    }
    assert llm.provenance(NATIVE)["pacing"]["room_grant"] == "commit_only"
