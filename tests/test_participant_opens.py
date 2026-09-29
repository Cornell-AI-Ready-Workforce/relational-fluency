"""The participant opens every conversation (pipeline 2026-09-28b).

The researchers' rule of 2026-09-28, confirmed 2026-09-29: no character says
anything first. At the start of every encounter, 1:1 and rooms, and at S1's
hand-off to Sam or Drew, the characters stay silent until the participant has
said something the runner ACCEPTED (a user_turn; a line a gate suppressed, such
as "(laughter)", does not count). Nothing may make a character speak before
that: no room opener, no silence probe or hand-off line, no re-ask or replay,
no reply to a line nobody accepted. A beat written as the opener (S2A
t1_the_opening) becomes the reply to that first line. The S1 timebox and the
7:00 floor count from the participant's first line; the 12:00 ceiling does not
move. The page says "You start the conversation. Say hello when you're ready."
(and "You're now with Sam. You start." at the hand-off) until then.

What it replaces, on 2026-09-28a: a room's lead opened every scene
unprompted (_open_group_scene), the watchdog probed a silent participant from
12 s (S2A t1_the_opening's on_silence line), a 1:1 reply to a noise commit
could play before its transcript said it was noise, and the floor counted
from the socket opening. No network: the real gpt bridge over a fake wire.
"""
from __future__ import annotations

import asyncio
import json
import sys
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
for p in (ROOT, ROOT / "tests"):
    if str(p) not in sys.path:
        sys.path.insert(0, str(p))

from server import encounter_record, llm  # noqa: E402
from server import realtime_voice_session as rvs  # noqa: E402
from server.voice import realtime as R  # noqa: E402

import test_participant_turn_integrity as T  # noqa: E402
from test_bridge_correctness import adelta, created, done, tdelta, tdone  # noqa: E402

GPT = T.GPT
LOUD = T.LOUD


@pytest.fixture(autouse=True)
def quick(monkeypatch):
    monkeypatch.setattr(R, "RECV_POLL_S", 0.02)
    monkeypatch.setattr(R, "UPDATE_ACK_S", 0.05)
    monkeypatch.setenv("TRANSCRIPT_GRACE_SECONDS", "0.2")
    monkeypatch.setenv("AUTOFIRE_WAIT", "0.05")
    monkeypatch.setenv("PROBE_AFTER_SECONDS", "0.3")
    monkeypatch.setenv("PROBE_TICK_SECONDS", "0.05")
    monkeypatch.setattr(R, "MODEL", GPT)       # production's family


# --------------------------------------------------------------------------
# Fakes: one timeline for the record and the page, so "before" is checkable
# --------------------------------------------------------------------------

class Page:
    """The browser's end, writing every frame (and every audio chunk) onto
    the shared timeline."""

    def __init__(self, tl):
        self.tl = tl
        self.json = []

    async def receive(self):
        return {"type": "websocket.disconnect"}

    async def send_json(self, payload):
        self.json.append(payload)
        self.tl.append(("page", payload.get("type"), payload))

    async def send_bytes(self, payload):
        self.tl.append(("page", "audio", None))

    def frames(self, type_):
        return [f for f in self.json if f.get("type") == type_]


def make(scenario):
    session = T.FakeSession(scenario)
    tl: list = []
    record = session.store.event

    def event(type_, **fields):
        record(type_, **fields)
        tl.append(("event", type_, fields))
    session.store.event = event
    page = Page(tl)
    return rvs.RealtimeVoiceSessionRunner(session, page), session, page, tl


def first(tl, kind, name):
    return next((i for i, (k, n, _) in enumerate(tl) if k == kind and n == name), None)


async def until(pred, timeout=3.0):
    end = time.time() + timeout
    while time.time() < end:
        if pred():
            return True
        await asyncio.sleep(0.02)
    return False


def reply_head(rid, words=("Okay,", "go on.")):
    """A reply's opening frames: named, captioned and voiced."""
    out = [created(rid)]
    for w in words:
        out += [tdelta(rid, "it_" + rid, w + " "), adelta(rid, "it_" + rid)]
    return out


def reply_tail(rid, text="Okay, go on."):
    return [adelta(rid, "it_" + rid), tdone(rid, "it_" + rid, text), done(rid)]


def transcribed(item, text):
    return {"type": "conversation.item.input_audio_transcription.completed",
            "item_id": item, "transcript": text}


async def participant_says(runner, rt, ms=600):
    """A participant turn as the runner closes it: voice into the buffer, the
    turn end (brief, then the commit, which starts the reply on gpt)."""
    await T.send_ms(rt, LOUD, ms)
    await runner._on_turn_ended()


async def one_to_one(scenario="S2A"):
    runner, session, page, tl = make(scenario)
    rt = T.bridge(GPT)
    runner.rt = rt
    await runner._await_participant("start")
    pump = asyncio.ensure_future(runner._pump(rt))
    return runner, session, page, tl, rt, pump


def wire_types_before(rt, n):
    return [m["type"] for m in rt.ws.sent[:n]]


# --------------------------------------------------------------------------
# 1. 1:1: nothing is heard before the first accepted line
# --------------------------------------------------------------------------

@T.in_a_loop
async def test_1to1_nothing_reaches_the_page_before_the_first_accepted_line():
    """The gpt route's worst order: the reply's audio lands BEFORE the
    transcript of the line it answers. A cough transcribed "(laughter)" first,
    then a real hello. Nothing of the first reply is played, the beat it was
    briefed with (S2A t1_the_opening) is given back and performed by the
    reply to the hello, and no response.create or text prompt goes out."""
    runner, session, page, tl, rt, pump = await one_to_one("S2A")
    opener = runner._triggers()[0]["id"]
    assert opener == "t1_the_opening"

    await participant_says(runner, rt)
    rt.ws.feed(type="input_audio_buffer.committed", item_id="u1")
    for f in reply_head("r1"):
        rt.ws.feed(**f)
    assert await until(lambda: any(e["type"] == "trigger_fired" for e in session.store.events))
    await asyncio.sleep(0.2)
    rt.ws.feed(**transcribed("u1", "(laughter)"))
    assert await until(lambda: session.store.of("user_turn_suppressed"))
    for f in reply_tail("r1"):
        rt.ws.feed(**f)
    await asyncio.sleep(0.3)
    assert first(tl, "page", "audio") is None, "a reply to a cough was played"
    assert not page.frames("assistant_started")
    assert [e["trigger_id"] for e in session.store.of("trigger_undelivered")] == [opener]
    assert runner._awaiting_participant, "a sound tag is not the first line"

    await participant_says(runner, rt)
    rt.ws.feed(type="input_audio_buffer.committed", item_id="u2")
    for f in reply_head("r2"):
        rt.ws.feed(**f)
    await asyncio.sleep(0.3)
    assert first(tl, "page", "audio") is None, "played before its line was accepted"
    sent_before = len(rt.ws.sent)
    rt.ws.feed(**transcribed("u2", "Hi Morgan, thanks for making time."))
    for f in reply_tail("r2"):
        rt.ws.feed(**f)
    assert await until(lambda: session.store.of("steering_pair"), timeout=4)
    pump.cancel()

    turn, heard = first(tl, "event", "user_turn"), first(tl, "page", "audio")
    opened = first(tl, "event", "participant_opened")
    assert turn is not None and heard is not None and turn < opened < heard
    # Nothing asked the model to speak: on gpt the commit starts the reply.
    asked = wire_types_before(rt, sent_before)
    assert "response.create" not in asked and "conversation.item.create" not in asked
    # The opener's direction is the one the heard reply performed.
    fired = [e["trigger_id"] for e in session.store.of("trigger_fired")]
    assert fired == [opener, opener]
    (pair,) = session.store.of("steering_pair")
    assert pair["direction"]["trigger_id"] == opener
    # All of the hello's reply is played; what is dropped is the cancelled
    # cough reply's late done, which arrived before the hello's commit.
    (released,) = session.store.of("first_reply_released")
    assert released["released_frames"] > 0 and released["dropped_audio_ms"] == 0
    (op,) = session.store.of("participant_opened")
    assert op["reason"] == "start" and op["first_of_encounter"] is True
    assert page.frames("awaiting_participant")[0]["reason"] == "start"


@T.in_a_loop
async def test_a_reply_to_a_line_nobody_transcribed_is_not_played_after_the_next():
    """A commit with no transcript at all (the gateway sends none for an empty
    one): its reply is held, and when the next line is accepted only the reply
    that started after THAT line's commit is played."""
    runner, session, page, tl, rt, pump = await one_to_one("S2A")
    await participant_says(runner, rt)
    rt.ws.feed(type="input_audio_buffer.committed", item_id="u1")
    for f in reply_head("r1", ("Stale",)) + reply_tail("r1", "Stale"):
        rt.ws.feed(**f)
    assert await until(lambda: not rt.responding)
    await asyncio.sleep(0.1)
    await participant_says(runner, rt)
    rt.ws.feed(type="input_audio_buffer.committed", item_id="u2")
    for f in reply_head("r2"):
        rt.ws.feed(**f)
    await asyncio.sleep(0.2)
    rt.ws.feed(**transcribed("u2", "Morning. Can we talk about my pay?"))
    for f in reply_tail("r2"):
        rt.ws.feed(**f)
    assert await until(lambda: session.store.of("assistant_turn"), timeout=4)
    pump.cancel()
    (turn,) = session.store.of("assistant_turn")
    assert "Stale" not in turn["text"]
    (released,) = session.store.of("first_reply_released")
    assert released["dropped_frames"] > 0 and released["released_frames"] > 0


@T.in_a_loop
async def test_after_the_first_line_replies_are_not_held():
    runner, session, page, tl, rt, pump = await one_to_one("S2A")
    runner._awaiting_participant = False
    await participant_says(runner, rt)
    rt.ws.feed(type="input_audio_buffer.committed", item_id="u1")
    for f in reply_head("r1"):
        rt.ws.feed(**f)
    assert await until(lambda: first(tl, "page", "audio") is not None)
    pump.cancel()
    assert not session.store.of("user_turn"), "played before any transcript, as always"


# --------------------------------------------------------------------------
# 2. Probes, hand-off lines, re-asks and replays wait too
# --------------------------------------------------------------------------

class ProbeRT:
    """What the watchdog's probes reach for, recorded."""

    def __init__(self):
        self.ws = object()
        self.model = GPT
        self.voice = "coral"
        self.responding = False
        self.autofire_active = False
        self.probes = 0
        self.updates = []
        self.replays = []
        self.retries = []

    async def update_instructions(self, text):
        self.updates.append(text)
        return True

    async def commit_probe(self, pad):
        self.probes += 1
        return {"cleared": True}

    async def send_audio(self, pcm):
        pass

    async def replay_input(self, pcm):
        self.replays.append(pcm)
        return True

    async def retry_response(self, nudge=None):
        self.retries.append(nudge)
        return True


async def watch(runner, seconds):
    task = asyncio.ensure_future(runner._silence_watchdog())
    await asyncio.sleep(seconds)
    runner._closed = True
    await asyncio.wait_for(task, 2)
    runner._closed = False


@pytest.mark.parametrize("scenario", ["S2A", "S1A"])
@T.in_a_loop
async def test_the_watchdog_neither_probes_nor_hands_on_before_the_first_line(scenario):
    """S2A's t1_the_opening carries an on_silence line and fired at the 12 s
    probe; S1A's Riley would be handed on at 2:00 by a probe of her closing
    line. Before the participant has spoken, neither: however long the
    silence, and whatever the interaction's own clock says."""
    runner, session, page, tl = make(scenario)
    runner.rt = ProbeRT()
    await runner._await_participant("start")
    runner._last_activity = runner._play_cursor = time.time() - 300
    runner._interaction_started_at = runner._encounter_started_at = time.time() - 300
    await watch(runner, 0.5)
    assert runner.rt.probes == 0 and runner.rt.updates == []
    assert not session.store.of("trigger_fired")
    assert not session.store.of("handoff_briefed")

    # The same silence after they have spoken is probed as before: the rule
    # is what held it.
    runner._awaiting_participant = False
    runner._conversation_opened_at = time.time() - 300
    await watch(runner, 0.5)
    assert runner.rt.probes >= 1


@T.in_a_loop
async def test_a_reconnect_before_the_first_line_replays_nothing():
    runner, session, page, tl = make("S2A")
    old = ProbeRT()
    runner.rt = old
    await runner._await_participant("start")
    runner._replay_pcm += LOUD * 60
    built = []

    def new_session(**kw):
        rt = ProbeRT()

        async def connect():
            return None
        rt.connect = connect
        built.append(rt)
        return rt
    runner._new_session = new_session

    async def close():
        old.ws = None
    old.close = close
    monkey = getattr(R, "RECONNECT_LIMIT", 0)
    R.RECONNECT_LIMIT = 2
    try:
        assert await runner._reconnect_after_gateway_close(old) is True
    finally:
        R.RECONNECT_LIMIT = monkey
    assert built and built[0].replays == []
    (withheld,) = session.store.of("replay_withheld")
    assert withheld["reason"] == "awaiting_participant"
    assert page.frames("voice_notice")[-1]["replayed"] is False


@T.in_a_loop
async def test_a_reply_that_never_came_is_not_re_asked_before_the_first_line():
    runner, session, page, tl = make("S2A")
    runner.rt = ProbeRT()
    await runner._await_participant("start")
    runner._replay_pcm += LOUD * 60
    asked = await runner._reply_missing(runner.rt, runner.agent_id,
                                        {"retryable": True, "waited_s": 6})
    assert asked is False
    assert runner.rt.replays == [] and runner.rt.retries == []
    (retry,) = session.store.of("reply_retry")
    assert retry["why"] == "awaiting_participant"


# --------------------------------------------------------------------------
# 3. Rooms: no opener, no probe, no turn routed on nothing
# --------------------------------------------------------------------------

def room_runner(scenario="S3A"):
    runner, session, page, tl = make(scenario)
    room = T._room(GPT, runner)
    for a in runner._resolve_agents():
        room.sessions[a.id] = T.DeadMember(GPT)
    runner.room = room
    runner.director = session.director
    return runner, session, page, tl


def test_the_room_opener_is_gone():
    src = (ROOT / "server" / "realtime_voice_session.py").read_text(encoding="utf-8")
    assert "_open_group_scene" not in src.replace("(_open_group_scene", "")
    assert not hasattr(rvs.RealtimeVoiceSessionRunner, "_open_group_scene")
    assert not hasattr(R, "SCENE_OPEN_PROMPT")
    from server.group_room import GroupRoom
    assert not hasattr(GroupRoom, "open_scene")


@pytest.mark.parametrize("scenario", ["S3A", "S4A"])
@T.in_a_loop
async def test_a_room_says_nothing_before_the_participant(scenario, monkeypatch):
    monkeypatch.setenv("ROUTE_TRANSCRIPT_WAIT", "0.3")
    runner, session, page, tl = room_runner(scenario)
    spawned = []
    runner._spawn_group_turn = lambda coro: (spawned.append(coro), coro.close())
    await runner._await_participant("start")
    assert page.frames("awaiting_participant")[0]["names"] == [
        a.name for a in runner._resolve_agents()]
    # A silence with an on_silence beat next: no probe is spawned.
    assert runner._next_trigger().get("on_silence")
    runner._last_activity = runner._play_cursor = time.time() - 300
    await watch(runner, 0.4)
    assert spawned == []
    # A turn for which nothing arrived (the scribe-failure route) is skipped.
    runner._turn_end_arrivals = runner._transcripts_arrived
    await asyncio.wait_for(runner._run_group_turn(), 5)
    (skip,) = session.store.of("group_turn_skipped")
    assert skip["reason"] == "awaiting_participant"
    assert session.director.calls == [] and not session.store.of("floor_grant_failed")
    assert first(tl, "page", "audio") is None


@T.in_a_loop
async def test_the_rooms_first_reply_is_the_leads_and_carries_the_opening(monkeypatch):
    """What the lead used to open with is its answer to the participant: the
    first unnamed turn goes to the lead, whose brief carries the `opening:`
    as a first-reply note on every family (not only the inert one)."""
    monkeypatch.setenv("ROUTE_TRANSCRIPT_WAIT", "0.3")
    runner, session, page, tl = room_runner("S3A")
    lead = runner._resolve_agents()[0]
    assert runner._fold_opening(lead, group=True) is True
    (framing,) = session.store.of("opening_framing")
    assert framing["via"] == "first_reply_note" and framing["honours_session_update"] is True
    brief = runner._instructions_for(lead)
    assert "FIRST REPLY" in brief and "already convened" in brief
    assert "speak first and open the scene" not in brief
    other = runner._resolve_agents()[1]
    assert "already convened" not in runner._instructions_for(other)

    await runner._await_participant("start")
    runner._turn_end_arrivals = runner._transcripts_arrived
    await runner._record_user_turn("Hi everyone, thanks for coming.", voiced_ms=1500)
    assert not runner._awaiting_participant
    await asyncio.wait_for(runner._run_group_turn(), 5)
    assert session.director.calls == [], "the lead answers; the director is not asked"
    (failed,) = session.store.of("floor_grant_failed")
    assert failed["agent_id"] == lead.id


# --------------------------------------------------------------------------
# 4. S1's hand-off: the new character waits for the participant too
# --------------------------------------------------------------------------

@T.in_a_loop
async def test_the_s1_hand_off_waits_for_the_participant():
    runner, session, page, tl = make("S1A")
    runner.rt = ProbeRT()
    await runner._await_participant("start")
    await runner._record_user_turn("Hey Riley, what's up?", voiced_ms=1200)
    assert not runner._awaiting_participant

    nxt = runner.interactions[1]
    sam = next(a for a in runner.cast if a.id == nxt["agent"])

    async def switch(agent):
        return ProbeRT()
    runner._switch_character = switch
    runner.segment = 1
    runner._interaction_started_at = time.time()
    assert await runner._enter(sam, new_interaction=True) is True
    assert runner._awaiting_participant and runner._awaiting_reason == "handoff"
    order = [f["type"] for f in page.json]
    assert order.index("segment_start") < len(order) - 1 - order[::-1].index("awaiting_participant")
    cue = page.frames("awaiting_participant")[-1]
    assert cue["reason"] == "handoff" and cue["names"] == ["Sam"]
    # Sam's beats all carry an on_silence line; none of them is probed.
    runner._last_activity = runner._play_cursor = time.time() - 300
    await watch(runner, 0.4)
    assert runner.rt.probes == 0 and not session.store.of("trigger_fired")
    # The participant opens with Sam: a second participant_opened, not the
    # encounter's first, so the 7:00 floor keeps its clock.
    first_line = runner._first_line_at
    await runner._record_user_turn("Oh, hi Sam.", voiced_ms=900)
    opened = session.store.of("participant_opened")
    assert [o["reason"] for o in opened] == ["start", "handoff"]
    assert opened[1]["first_of_encounter"] is False
    assert runner._first_line_at == first_line


@T.in_a_loop
async def test_s2_i1_to_i2_is_the_same_conversation():
    runner, session, page, tl = make("S2A")
    rt = ProbeRT()

    async def cancel_response():
        return None
    rt.cancel_response = cancel_response
    runner.rt = rt
    await runner._await_participant("start")
    await runner._record_user_turn("Hi Morgan.", voiced_ms=900)
    runner.segment = 1
    assert await runner._enter(runner.agent, new_interaction=True) is True
    assert not runner._awaiting_participant
    assert len(page.frames("awaiting_participant")) == 1


# --------------------------------------------------------------------------
# 5. The clocks
# --------------------------------------------------------------------------

def test_the_timebox_counts_from_the_first_line_to_riley():
    runner, session, page, tl = make("S1A")
    asyncio.run(runner._await_participant("start"))
    now = time.time()
    runner._interaction_started_at = now - 300          # the banner, long ago
    assert runner._timebox_elapsed() == 0.0
    runner._awaiting_participant = False
    runner._conversation_opened_at = now - 60           # their first line
    assert runner._timebox_elapsed() == pytest.approx(60, abs=1)


@T.in_a_loop
async def test_the_floor_counts_from_the_first_line_and_the_ceiling_does_not_move():
    runner, session, page, tl = make("S2A")
    runner.rt = ProbeRT()
    await runner._await_participant("start")
    now = time.time()
    runner._encounter_started_at = now - 500             # 8:20 on the wall clock
    assert not runner._floor_open(), "nobody has spoken: no floor at all"
    await runner._encounter_clock_tick()
    assert not session.store.of("move_on_open")
    runner.segment = len(runner.interactions) - 1
    assert await runner._hold_at_floor("participant") is True

    runner._awaiting_participant = False
    runner._first_line_at = now - 100                   # they opened at 6:40
    assert await runner._hold_at_floor("participant") is True
    assert page.frames("floor_held")[-1]["seconds_left"] == pytest.approx(320, abs=2)
    await runner._encounter_clock_tick()
    assert not session.store.of("move_on_open")
    runner._first_line_at = now - 421
    await runner._encounter_clock_tick()
    (mo,) = session.store.of("move_on_open")
    assert mo["floor_elapsed_s"] >= 420 and mo["elapsed_s"] >= 500

    # The ceiling is the encounter's own: 12:00 from the socket opening,
    # whenever they first spoke.
    runner._first_line_at = now - 10
    runner._encounter_started_at = now - 721
    assert await runner._at_ceiling() is True
    assert page.frames("encounter_complete")


# --------------------------------------------------------------------------
# 6. On the record
# --------------------------------------------------------------------------

def test_the_policy_and_the_versions_are_on_the_record():
    prov = llm.provenance(GPT)
    assert prov["opening"]["policy"] == "participant_opens"
    assert prov["opening"]["clocks"] == {
        "floor": "first_participant_line",
        "timebox": "first_participant_line_in_conversation",
        "wrap": "encounter_start", "ceiling": "encounter_start"}
    assert llm.PIPELINE_VERSION == "2026-09-28b"
    assert llm.ROOM_PACING_VERSION == "2026-09-28b"
    src = (ROOT / "server" / "llm.py").read_text(encoding="utf-8")
    assert src.count("#   2026-09-28b") == 2, "a history line for each version"


def test_the_record_says_when_the_participant_opened(tmp_path):
    events = [
        {"type": "session_start", "t": 0.0},
        {"type": "realtime_session_started", "t": 0.2, "opening": {"policy": "participant_opens"}},
        {"type": "awaiting_participant", "t": 0.3, "reason": "start"},
        {"type": "participant_opened", "t": 9.4, "reason": "start", "first_of_encounter": True,
         "interaction": "i1", "agent_id": "riley", "waited_s": 9.1},
        {"type": "awaiting_participant", "t": 130.0, "reason": "handoff"},
        {"type": "participant_opened", "t": 136.5, "reason": "handoff", "first_of_encounter": False,
         "interaction": "i2", "agent_id": "sam", "waited_s": 6.5},
    ]
    (tmp_path / "events.jsonl").write_text(
        "\n".join(json.dumps(e) for e in events) + "\n", encoding="utf-8")
    rec = encounter_record.build(tmp_path)
    assert rec["opening"]["first_participant_line_s"] == 9.4
    assert [c["reason"] for c in rec["opening"]["conversations"]] == ["start", "handoff"]
    assert rec["opening"]["conversations"][1]["waited_s"] == 6.5
    assert rec["provenance"]["opening"]["policy"] == "participant_opens"
