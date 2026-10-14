"""Room members the gateway closes are rebuilt inside the encounter (pipeline
2026-10-14a).

The 2026-10-14 release sim check on main 01f10f0 (`python -m tools.sim.check`,
S4A, a three-character room on gpt-realtime-2.1): at t=154.7 s the gateway
closed all three members' sockets with WebSocket code 1012 (service restart),
`voice_error where=room:dan|room:chris|room:priya "realtime connection lost:
received 1012 (service restart)"`. The scribe survived. Every one of the next
39 participant turns was written as group_turn_no_members, and no character
answered for the remaining ~565 s (47 lines said, 5 answered). The same build's
S2A 1:1 encounter lost its connection at t=312.9 s and recovered: a 1:1 session
the gateway closes was already rebuilt (_reconnect_after_gateway_close), a room
member's was not.

Offline, as tests/test_room_memory_hygiene.py is: real RealtimeVoiceSession
bridges (connect() and all) on fake gateway sockets that close the way the live
one did, a real GroupRoom opened by the runner's own _open_room, a real runner.
The fake gateway answers what the real one was measured to answer
(session.updated, conversation.item.added / .deleted, input_audio_buffer
.committed); a reply is fed by each test, frame by frame.

Ordering is read off the frames each socket was sent, in the order it was sent
them, and off the order of the events in the record: never off two clock
readings, which a coarse clock makes equal (PR #84; PYTHONPATH=tests python -m
pytest -p coarse_clock).
"""
from __future__ import annotations

import asyncio
import functools
import itertools
import json
import sys
import time
from pathlib import Path

import pytest
from websockets.exceptions import ConnectionClosedError
from websockets.frames import Close

ROOT = Path(__file__).resolve().parent.parent
for p in (ROOT, ROOT / "tests"):
    if str(p) not in sys.path:
        sys.path.insert(0, str(p))

from server import encounter_record  # noqa: E402
from server import group_room as gr  # noqa: E402
from server import llm  # noqa: E402
from server import realtime_voice_session as rvs  # noqa: E402
from server.voice import realtime as R  # noqa: E402
from tools import encounter_health  # noqa: E402
from tools import load_analysis_db as L  # noqa: E402

from test_bridge_correctness import (  # noqa: E402
    GPT, LOUD, NATIVE, FakeSession, PageWS, WireWS, adelta, audio_done,
    created, item_added, settle, tdelta, tdone,
)
from test_bridge_correctness import until as _until  # noqa: E402
from test_room_memory_hygiene import LINE, done_out, line_reply  # noqa: E402

CAST = ("dan", "priya", "chris")


async def until(cond, timeout=5.0):
    """test_bridge_correctness's, with room for a slow CI runner."""
    await _until(cond, timeout)


def in_a_loop(fn):
    """Bounded, so a regression that leaves an await hanging (a rebuild nobody
    seats, a turn waiting on a floor nobody releases) fails in 60 s."""
    @functools.wraps(fn)
    def wrapper(*a, **kw):
        async def bounded():
            return await asyncio.wait_for(fn(*a, **kw), 60)
        return asyncio.run(bounded())
    return wrapper


@pytest.fixture(autouse=True)
def quick(monkeypatch):
    monkeypatch.setattr(R, "RECV_POLL_S", 0.02)
    monkeypatch.setattr(R, "AUDIO_RETRY_QUIET_S", 0.05, raising=False)
    monkeypatch.setattr(R, "MEMORY_SETTLE_S", 0.3)
    monkeypatch.setattr(R, "MEMORY_OP_TTL_S", 3.0)
    monkeypatch.setattr(rvs.RealtimeVoiceSessionRunner, "_REBUILD_BACKOFF_S", 0.02)
    monkeypatch.setattr(rvs.RealtimeVoiceSessionRunner, "_REBUILD_WAIT_S", 5.0)
    monkeypatch.setenv("TRANSCRIPT_GRACE_SECONDS", "0.2")
    monkeypatch.setenv("ROUTE_TRANSCRIPT_WAIT", "0.2")
    for knob in ("ROOM_CUT_MEMORY", "ROOM_TOLD_TEXT", "ROOM_NUDGE_ROLE",
                 "ROOM_RECONNECT_LIMIT", "AUTOFIRE_WAIT", "ROOM_SPLIT_TURN_S",
                 "ROOM_GRANT_UNANSWERED_S"):
        monkeypatch.delenv(knob, raising=False)


# --------------------------------------------------------------------------
# The gateway, as the 1012 found it
# --------------------------------------------------------------------------

def closed_1012():
    """What websockets raises for the live close: 1012 received, then sent."""
    return ConnectionClosedError(Close(1012, ""), Close(1012, ""), True)


_RESTART = object()


class Socket(WireWS):
    """The gateway's end of one realtime socket. It answers what the gateway
    answers, and restart() is the service restarting under the session: the
    read in flight raises the 1012 close, and so does every later send."""

    def __init__(self, gateway, url):
        super().__init__()
        self.gateway, self.url = gateway, url
        self.restarted = False
        self.closed = False
        self.items: list = []

    async def send(self, raw):
        if self.restarted:
            raise closed_1012()
        frame = json.loads(raw)
        self.sent.append(frame)
        self.feed(self.gateway.answer(self, frame))

    async def recv(self):
        item = await self.q.get()
        if item is _RESTART:
            raise closed_1012()
        return item

    async def close(self):
        self.closed = True

    def restart(self):
        self.restarted = True
        self.q.put_nowait(_RESTART)

    def creates(self):
        return [m for m in self.sent if m.get("type") == "conversation.item.create"]

    def instructions(self):
        return [m["session"].get("instructions") for m in self.sent
                if m.get("type") == "session.update"]


class Gateway:
    """Every socket the room opens, and how the gateway answers on each.
    `refuse` connects are refused (the gateway still restarting); `hold`, while
    set, holds every connect until it is released; `on_commit` is what a
    commit draws, beyond input_audio_buffer.committed (a reply's
    response.created, say)."""

    def __init__(self):
        self.sockets: list = []
        self.refuse = 0
        self.hold = None
        self.on_commit = None
        self._ids = itertools.count(1)

    async def connect(self, url, **kw):
        if self.hold is not None:
            await self.hold.wait()
        if self.refuse > 0:
            self.refuse -= 1
            raise ConnectionRefusedError("the gateway is still restarting")
        ws = Socket(self, url)
        self.sockets.append(ws)
        return ws

    def answer(self, ws, frame):
        t = frame.get("type")
        if t == "session.update":
            return [{"type": "session.updated", "session": {}}]
        if t == "conversation.item.create":
            item = dict(frame["item"])
            item.setdefault("id", f"gw_{next(self._ids)}")
            prev = frame.get("previous_item_id") or (ws.items[-1] if ws.items else None)
            ws.items.append(item["id"])
            return [{"type": "conversation.item.added", "previous_item_id": prev,
                     "item": item}]
        if t == "conversation.item.delete":
            return [{"type": "conversation.item.deleted",
                     "item_id": frame["item_id"]}]
        if t == "input_audio_buffer.commit":
            iid = f"in_{next(self._ids)}"
            prev = ws.items[-1] if ws.items else None
            ws.items.append(iid)
            out = [{"type": "input_audio_buffer.committed", "item_id": iid,
                    "previous_item_id": prev}]
            if self.on_commit is not None:
                out += self.on_commit(ws, frame) or []
            return out
        return []


class ScriptedDirector:
    model = "fake-director"

    def __init__(self, entries=()):
        self.entries = list(entries)

    async def route(self, history, text):
        return [dict(e) for e in self.entries]


class Room:
    """A real runner whose room it opened itself (_open_room), on the fake
    gateway: three members and the scribe, each with its pump."""

    def __init__(self, monkeypatch, *, model=GPT, scenario="S4A"):
        # A key the bridge will hand the fake gateway, and nothing else.
        monkeypatch.setenv("LITELLM_API_KEY", "sk-test-not-a-real-key")
        self.gateway = Gateway()
        monkeypatch.setattr(R.websockets, "connect", self.gateway.connect)
        monkeypatch.setattr(R, "MODEL", model)
        self.session = FakeSession(scenario)
        self.session.director = ScriptedDirector()
        self.page = PageWS()
        self.runner = rvs.RealtimeVoiceSessionRunner(self.session, self.page)
        self.agents = {a.id: a for a in self.runner._resolve_agents()}

        async def nothing():
            return None
        # What a room turn hands on afterwards (the steering review, the
        # pacing gate) is not under test here.
        self.runner._advance_when_spent = nothing
        self.runner._spawn_room_steer = lambda: None

    async def open(self):
        await self.runner._open_room()
        self.runner.rt = self.runner.room.session_for(self.runner.agent_id)
        return self

    @property
    def room(self):
        return self.runner.room

    def rt(self, aid):
        return self.room.sessions[aid]

    def of(self, type_):
        return self.session.store.of(type_)

    async def wait_for(self, type_, n=1, timeout=8.0):
        await until(lambda: len(self.of(type_)) >= n, timeout)
        return self.of(type_)

    async def close(self):
        await settle(self.runner)
        self.runner._closed = True
        await self.runner._close_room()

    async def participant(self, text):
        """An accepted participant line, as the scribe's transcript lands."""
        await self.runner._record_user_turn(text, voiced_ms=2400,
                                            voiced_span_ms=2400,
                                            spoken_at=time.time())

    async def speaks(self, aid, rid, text, *, item=None, hold_floor=True):
        """`aid`, holding the floor, says `text` as reply `rid`: streamed,
        relayed, done, written."""
        item = item or f"{rid}_i1"
        n = len([t for t in self.of("assistant_turn") if t["agent_id"] == aid])
        if hold_floor:
            self.room.speaking = aid
        self.rt(aid).ws.feed([created(rid), item_added(rid, item),
                              {"type": "conversation.item.added",
                               "previous_item_id": None,
                               "item": {"id": item, "type": "message",
                                        "role": "assistant"}},
                              adelta(rid, item), adelta(rid, item),
                              tdelta(rid, item, text), audio_done(rid, item),
                              tdone(rid, item, text),
                              done_out(rid, [item], texts={item: text})])
        await until(lambda: len([t for t in self.of("assistant_turn")
                                 if t["agent_id"] == aid]) > n)
        await settle(self.runner)
        if hold_floor and self.room.speaking == aid:
            self.room.speaking = None


def seeded(ws):
    """The conversation a rebuilt member was given: (role, text) per item."""
    return [(c["item"]["role"], c["item"]["content"][0]["text"])
            for c in ws.creates()]


def told(name, text):
    return gr.GroupRoom._told_note(name, text)


# --------------------------------------------------------------------------
# 1. One member, closed with 1012
# --------------------------------------------------------------------------

@in_a_loop
async def test_a_member_the_gateway_closes_is_rebuilt_as_the_same_character(monkeypatch):
    h = await Room(monkeypatch).open()
    try:
        room = h.room
        old = h.rt("dan")
        voice = old.voice
        scribe, scribe_ws = room.scribe, room.scribe.ws
        scribe_sent = len(scribe_ws.sent)
        others = {aid: h.rt(aid) for aid in ("priya", "chris")}
        await h.participant("We need a date for the rollout before we leave today.")
        await h.speaks("dan", "R1", "The date is in my draft, end of the month.")
        await h.speaks("priya", "R2", "Last year's rollout slipped twice, so I doubt it.")
        old_ws = old.ws
        old_ws.restart()                        # WebSocket 1012 under Dan
        (rec,) = await h.wait_for("room_member_reconnected")
        dan = h.rt("dan")
        assert dan is not old and dan.ws is not old_ws
        assert old.ws is None and old_ws.closed, "the dead socket was let go"
        # The same character: its voice, its brief, and a note for one reply
        # that the line dropped.
        assert dan.voice == voice == rec["voice"]
        (brief,) = dan.ws.instructions()
        assert brief.startswith("SYSTEM") and "The line dropped for a moment" in brief
        assert "do not greet anyone again" in brief
        assert dan.track_items is True and dan.nudge_role == "system"
        # Given the room's conversation as Dan holds it, in order: what the
        # participant said, his own line, Priya's line as he was told it.
        assert seeded(dan.ws) == [
            ("user", "We need a date for the rollout before we leave today."),
            ("assistant", "The date is in my draft, end of the month."),
            ("user", told("Priya", "Last year's rollout slipped twice, so I doubt it.")),
        ]
        assert rec["seeded"] == {"participant": 1, "own": 1, "told": 1}
        assert rec["seed_route"] == "conversation_items"
        assert rec["attempt"] == 1 and rec["limit"] == 2 and rec["floor"] is None
        assert "1012" in rec["why"]
        # His own line now stands for its reply in the new conversation, so a
        # cut that lands on it later can still be applied there.
        assert dan.reply_item_ids("R1") == [dan.ws.creates()[1]["item"]["id"]]
        # Back in the room, with a pump: he is heard again.
        assert room.session_for("dan") is dan and "dan" not in room.lost
        assert room.member_reopens == {"dan": 1}
        await h.speaks("dan", "R3", "Then end of the month it is, team by team.")
        (last,) = [t for t in h.of("assistant_turn") if t["text"].startswith("Then end")]
        assert last["agent_id"] == "dan"
        # Nobody else was touched: the other members and the scribe.
        for aid, rt in others.items():
            assert h.rt(aid) is rt and not rt.ws.closed
        assert room.scribe is scribe and scribe.ws is scribe_ws
        assert not scribe_ws.closed and len(scribe_ws.sent) == scribe_sent
        assert len(h.gateway.sockets) == 5, "one new socket, Dan's"
    finally:
        await h.close()
    (drop,) = h.of("room_member_dropped")
    assert drop["agent_id"] == "dan" and drop["floor"] is None
    assert drop["attempts"] == 0 and "1012" in drop["why"]
    (err,) = [e for e in h.of("voice_error") if e["where"] == "room:dan"]
    assert "received 1012 (service restart)" in err["message"]
    types = [e["type"] for e in h.session.store.events]
    assert types.index("room_member_dropped") < types.index("room_member_reconnected")
    # The 1:1 path is not what rebuilt him.
    assert not h.of("realtime_session_reconnected")
    assert not [f for f in h.page.json if f.get("type") in ("error", "voice_notice")]


# --------------------------------------------------------------------------
# 2. A gateway restart: every member at once (the S4A sim)
# --------------------------------------------------------------------------

def reply(rid, text, item=None):
    item = item or f"{rid}_i1"
    return [item_added(rid, item),
            {"type": "conversation.item.added", "previous_item_id": None,
             "item": {"id": item, "type": "message", "role": "assistant"}},
            adelta(rid, item), adelta(rid, item), tdelta(rid, item, text),
            audio_done(rid, item), tdone(rid, item, text),
            done_out(rid, [item], texts={item: text})]


@in_a_loop
async def test_a_gateway_restart_closes_every_member_and_the_room_answers_again(monkeypatch):
    h = await Room(monkeypatch).open()
    try:
        await h.participant("Let's agree the rollout order before we leave today.")
        old = {aid: h.rt(aid) for aid in CAST}
        for rt in old.values():
            rt.ws.restart()
        recs = await h.wait_for("room_member_reconnected", 3)
        assert sorted(r["agent_id"] for r in recs) == sorted(CAST)
        assert all(h.rt(aid) is not old[aid] for aid in CAST)
        # What the sim could not do any more: the participant speaks, the
        # room answers.
        commits = []

        def on_commit(ws, f):
            commits.append(ws)
            return [created("R9")]
        h.gateway.on_commit = on_commit
        await h.participant("Priya, I think that was your idea originally, wasn't it?")
        turn = asyncio.ensure_future(h.runner._run_group_turn())
        await until(lambda: commits)
        priya_ws = h.rt("priya").ws
        assert commits == [priya_ws]
        priya_ws.feed(reply("R9", "It was, and I still think one team goes first."))
        await asyncio.wait_for(turn, 10)
    finally:
        await h.close()
    assert not h.of("group_turn_no_members")
    (answer,) = h.of("assistant_turn")
    assert answer["agent_id"] == "priya"
    assert answer["text"] == "It was, and I still think one team goes first."
    # Her new conversation held the participant's earlier line before the
    # commit that answered the new one.
    sent = [m.get("type") for m in priya_ws.sent]
    first_line = seeded(priya_ws)[0]
    assert first_line == ("user", "Let's agree the rollout order before we leave today.")
    assert sent.index("conversation.item.create") < sent.index("input_audio_buffer.commit")


@in_a_loop
async def test_a_turn_spoken_while_the_room_is_rebuilt_waits_for_it(monkeypatch):
    """The S4A sim's next 39 turns found no member and said so at once. A
    turn arriving while the members are being rebuilt waits for them."""
    h = await Room(monkeypatch).open()
    try:
        h.gateway.hold = asyncio.Event()             # the gateway is slow to come back
        commits = []

        def on_commit(ws, f):
            commits.append(ws)
            return [created("R7")]
        h.gateway.on_commit = on_commit
        for aid in CAST:
            h.rt(aid).ws.restart()
        await until(lambda: len(h.runner._member_rebuilds) == 3)
        assert not h.room.sessions
        await h.participant("Chris, walk me through your pilot plan for the first team.")
        turn = asyncio.ensure_future(h.runner._run_group_turn())
        await asyncio.sleep(0.3)
        assert not turn.done() and not h.of("group_turn_no_members")
        h.gateway.hold.set()
        await until(lambda: commits)
        chris = h.rt("chris")
        assert commits == [chris.ws]
        chris.ws.feed(reply("R7", "Team one goes first, the others follow in a month."))
        await asyncio.wait_for(turn, 10)
    finally:
        await h.close()
    assert not h.of("group_turn_no_members")
    assert [t["agent_id"] for t in h.of("assistant_turn")] == ["chris"]
    assert len(h.of("room_member_reconnected")) == 3


# --------------------------------------------------------------------------
# 3. The floor holder
# --------------------------------------------------------------------------

@pytest.mark.parametrize("when", ["after_created", "before_created"])
@in_a_loop
async def test_a_floor_holder_lost_before_its_reply_is_heard_answers_once_back(
        monkeypatch, when):
    """The sim's shape: Priya was granted at 151.6 s and her socket went at
    154.7 s with nothing of her reply played. The holder is given the floor
    again on its new session, with the beat it was briefed with, and the
    participant is told it heard them. `after_created`: the gateway had begun
    the reply (the grant's wait was over); `before_created`: it had not, and
    the grant itself finds the socket gone."""
    h = await Room(monkeypatch).open()
    try:
        commits = []

        def on_commit(ws, f):
            commits.append(ws)
            if len(commits) == 1 and when == "before_created":
                return []
            return [created(f"R{len(commits)}")]
        h.gateway.on_commit = on_commit
        line = "Dan, can you walk me through the whole rollout plan, step by step?"
        await h.participant(line)
        old = h.rt("dan")
        turn = asyncio.ensure_future(h.runner._run_group_turn())
        await until(lambda: commits)
        if when == "after_created":
            await until(lambda: (h.room.last_grant or {}).get("via") == "commit")
        old.ws.restart()
        await until(lambda: len(commits) == 2)
        dan = h.rt("dan")
        dan_ws = dan.ws
        assert dan is not old and commits[1] is dan_ws
        dan_ws.feed(reply("R2", "Week one is the pilot team, then everyone else."))
        await asyncio.wait_for(turn, 10)
    finally:
        await h.close()
    (drop,) = h.of("room_member_dropped")
    assert drop["agent_id"] == "dan"
    (regrant,) = h.of("floor_regranted")
    assert regrant["agent_id"] == "dan"
    assert regrant["why"] == ("dropped_unanswered" if when == "after_created"
                              else "grant_failed")
    # Dan's beat (S4A's t1) went to the session that dropped, and to the new
    # one before the floor did.
    (fired,) = h.of("trigger_fired")
    (redo,) = h.of("stage_direction_redelivered")
    assert redo["trigger_id"] == fired["trigger_id"] == "t1_priya_interrupted"
    assert redo["acked"] is True and redo["reason"] == "member_reconnected"
    frames = [m.get("type") for m in dan_ws.sent]
    brief_at = max(i for i, m in enumerate(dan_ws.sent)
                   if m.get("type") == "session.update"
                   and "DIRECTOR NOTE" in m["session"]["instructions"])
    assert frames.index("conversation.item.create") < brief_at
    assert brief_at < frames.index("input_audio_buffer.commit")
    assert seeded(dan_ws)[-1] == ("user", line)
    # The line it answers, paired with the beat that produced it, once.
    (turn_ev,) = h.of("assistant_turn")
    assert turn_ev["agent_id"] == "dan" and turn_ev["text"].startswith("Week one")
    (pair,) = h.of("steering_pair")
    assert pair["direction"]["trigger_id"] == "t1_priya_interrupted"
    assert pair["direction"]["redelivered"] is True
    assert not h.of("trigger_undelivered") and not h.of("group_turn_timeout")
    (notice,) = [f for f in h.page.json if f.get("type") == "voice_notice"]
    assert notice["kind"] == "reconnected" and notice["agent_id"] == "dan"
    assert notice["replayed"] is True


@in_a_loop
async def test_a_floor_holder_lost_mid_reply_keeps_the_line_and_is_not_asked_again(monkeypatch):
    """Part of the reply had reached the page: the bridge's interrupted done
    closes that turn, as it always has, and the participant heard a line cut
    short, as a 1:1 reply cut by a drop. The rebuilt character keeps that line;
    nobody is asked again."""
    h = await Room(monkeypatch).open()
    try:
        h.gateway.on_commit = lambda ws, f: [created("R1")]
        await h.participant("Dan, can you walk me through the whole rollout plan, step by step?")
        old = h.rt("dan")
        turn = asyncio.ensure_future(h.runner._run_group_turn())
        await until(lambda: (h.room.last_grant or {}).get("via") == "commit")
        old.ws.feed([item_added("R1", "i1"), adelta("R1", "i1"), adelta("R1", "i1"),
                     tdelta("R1", "i1", "Week one is the pilot team,")])
        await until(lambda: [f for f in h.page.json
                             if f.get("type") == "assistant_started"])
        old.ws.restart()
        await asyncio.wait_for(turn, 10)
        (rec,) = await h.wait_for("room_member_reconnected")
        dan_ws = h.rt("dan").ws
    finally:
        await h.close()
    (turn_ev,) = h.of("assistant_turn")
    assert turn_ev["agent_id"] == "dan" and turn_ev["interrupted"] is True
    assert turn_ev["text"] == "Week one is the pilot team,"
    # Written with the room's memory policy although his session was gone,
    # and with the playback clock of the line it was (pump state kept for it).
    assert turn_ev["memory"] == "whole" and turn_ev["response_id"] == "R1"
    assert "play_clock_start" in turn_ev and "play_clock_end" in turn_ev
    (drop,) = h.of("room_member_dropped")
    assert drop["floor"] == "answered"
    assert not h.of("floor_regranted") and not h.of("trigger_undelivered")
    assert ("assistant", "Week one is the pilot team,") in seeded(dan_ws)
    assert not [m for m in dan_ws.sent if m.get("type") == "input_audio_buffer.commit"]
    (notice,) = [f for f in h.page.json if f.get("type") == "voice_notice"]
    assert notice["replayed"] is False


@in_a_loop
async def test_a_reply_held_by_a_member_that_drops_is_dropped_with_it(monkeypatch):
    """A suppressed reply (Priya answering while Dan has the floor) was never
    played and was in the conversation that went away: it is dropped, said
    so, and in nothing the new session is given."""
    h = await Room(monkeypatch).open()
    try:
        await h.participant("We need a date for the rollout before we leave today.")
        h.room.speaking = "dan"
        old = h.rt("priya")
        old.ws.feed([created("RP"), item_added("RP", "p1"), adelta("RP", "p1"),
                     tdelta("RP", "p1", "I was going to say the pilot")])
        await until(lambda: h.runner._member_states["priya"].mode == "holding")
        old.ws.restart()
        (rec,) = await h.wait_for("room_member_reconnected")
        priya_ws = h.rt("priya").ws
        assert h.runner._member_states["priya"].mode == "idle"
    finally:
        await h.close()
    (drop,) = h.of("room_member_dropped")
    assert drop["held_reply"] == {"response_id": "RP",
                                  "text": "I was going to say the pilot"}
    assert drop["floor"] is None
    assert not [t for t in seeded(priya_ws) if "going to say" in t[1]]
    assert not [f for f in h.page.json if f.get("type") == "assistant_started"]
    assert not h.of("assistant_turn")


# --------------------------------------------------------------------------
# 4. What a rebuilt character is given: only what was heard (10-01a)
# --------------------------------------------------------------------------

async def _dan_cut_on_the_floor(h):
    """Dan's 25-word line, relayed at once, playing for 2 s of its 6 when the
    participant cuts in; room memory keeps the heard words. Returns them."""
    dan = h.rt("dan")
    h.room.speaking = "dan"
    await dan.request_response()
    dan.ws.feed([{"type": "input_audio_buffer.committed", "item_id": "p1",
                  "previous_item_id": None}] + line_reply("R1"))
    st = h.runner._member_states
    await until(lambda: "dan" in st and st["dan"].relayed_bytes >= 32000 * 6 - 6400)
    await until(lambda: any(f.get("type") == "assistant_text_final"
                            and f.get("items") == 2 for f in h.page.json))
    now = time.time()
    st["dan"].play_start, st["dan"].play_end = now - 2.0, now + 4.0
    h.runner._play_cursor = st["dan"].play_end
    lp = h.runner._last_played
    lp["start"], lp["end"] = st["dan"].play_start, st["dan"].play_end
    h.page._frames = [LOUD] * 25
    await h.runner._client_to_model()                # the participant cuts in
    dan.ws.feed([done_out("R1", ["i1", "i2"], "cancelled")])
    (mem,) = await h.wait_for("member_memory_replaced")
    await settle(h.runner)
    h.room.speaking = None
    return mem["heard_text"]


@in_a_loop
async def test_a_rebuilt_member_keeps_only_the_heard_words_of_a_cut_line(monkeypatch):
    h = await Room(monkeypatch).open()
    try:
        heard = await _dan_cut_on_the_floor(h)
        assert heard and heard != LINE and heard.endswith("…")
        for aid in ("priya", "dan"):
            h.rt(aid).ws.restart()
        await h.wait_for("room_member_reconnected", 2)
        dan_ws, priya_ws = h.rt("dan").ws, h.rt("priya").ws
    finally:
        await h.close()
    (turn,) = h.of("assistant_turn")
    assert turn["text"] == LINE and turn["memory_text"] == heard
    # Dan is given the words heard of his line, never the line; Priya the
    # note she was told, of the heard words.
    assert ("assistant", heard) in seeded(dan_ws)
    assert ("user", told("Dan", heard)) in seeded(priya_ws)
    for ws in (dan_ws, priya_ws):
        assert not [t for t in seeded(ws) if "did not want to flag it" in t[1]]


@in_a_loop
async def test_a_line_nobody_heard_is_not_given_to_anyone(monkeypatch):
    """A line the participant cut before any of it reached the page leaves
    nothing in its character's conversation (10-01a), nor in a rebuilt one."""
    h = await Room(monkeypatch).open()
    try:
        await h.participant("We need a date for the rollout before we leave today.")
        heard = h.runner._room_heard
        plan = {"memory": "deleted", "mem_text": "", "told": "none", "told_text": ""}
        h.runner._note_room_line(h.agents["dan"], LINE, "R1", plan)
        assert heard[-1]["kept"] is None and heard[-1]["told"] is None
        for aid in ("dan", "chris"):
            h.rt(aid).ws.restart()
        await h.wait_for("room_member_reconnected", 2)
        dan_ws, chris_ws = h.rt("dan").ws, h.rt("chris").ws
    finally:
        await h.close()
    for ws in (dan_ws, chris_ws):
        assert seeded(ws) == [
            ("user", "We need a date for the rollout before we leave today.")]


@in_a_loop
async def test_a_cut_after_the_rebuild_reaches_the_seeded_line_in_place(monkeypatch):
    """Dan's line is still playing on the page when his socket, and Priya's,
    go: both are rebuilt with the whole line. The participant then cuts it.
    Dan's seeded line is replaced by the heard words where it is, and the note
    seeded in Priya is corrected in place, as on the sessions they replace."""
    h = await Room(monkeypatch).open()
    try:
        await h.participant("We need a date for the rollout before we leave today.")
        await h.speaks("dan", "R1", LINE, item="d1")
        st = h.runner._member_states["dan"]
        now = time.time()
        st.play_start, st.play_end = now - 1.0, now + 5.0
        h.runner._play_cursor = st.play_end
        lp = h.runner._last_played
        lp["start"], lp["end"] = st.play_start, st.play_end
        for aid in ("dan", "priya"):
            h.rt(aid).ws.restart()
        await h.wait_for("room_member_reconnected", 2)
        dan_ws, priya_ws = h.rt("dan").ws, h.rt("priya").ws
        own = dan_ws.creates()[1]["item"]["id"]
        note = priya_ws.creates()[1]["item"]["id"]
        assert seeded(dan_ws)[1] == ("assistant", LINE)
        assert seeded(priya_ws)[1] == ("user", told("Dan", LINE))
        h.page._frames = [LOUD] * 25
        await h.runner._client_to_model()            # the cut, after the rebuild
        await until(lambda: h.of("member_memory_replaced")
                    and len(h.of("told_line_corrected")) == 2)
        (pc,) = h.of("playback_cut")
    finally:
        await h.close()
    (mem,) = h.of("member_memory_replaced")
    assert mem["reason"] == "cut_still_playing" and mem["action"] == "replaced"
    assert mem["response_id"] == "R1" and mem["items_deleted"] == [own]
    heard = mem["heard_text"]
    assert heard and heard != LINE
    assert [m["item_id"] for m in dan_ws.sent
            if m.get("type") == "conversation.item.delete"] == [own]
    put_back = dan_ws.creates()[-1]
    assert put_back["item"]["content"][0]["text"] == heard
    assert put_back["previous_item_id"] == dan_ws.creates()[0]["item"]["id"]
    # Priya's seeded note, by the name the seed gave it; Chris, never
    # rebuilt, has the note he was told live corrected as before.
    fixes = {e["agent_id"]: e for e in h.of("told_line_corrected")}
    assert set(fixes) == {"priya", "chris"}
    assert fixes["priya"]["deleted_item_id"] == note
    assert fixes["priya"]["action"] == "corrected"
    assert priya_ws.creates()[-1]["item"]["content"][0]["text"] == told("Dan", heard)
    assert pc["agent_id"] == "dan"


# --------------------------------------------------------------------------
# 5. Bounded
# --------------------------------------------------------------------------

@in_a_loop
async def test_the_limit_is_per_member_and_past_it_the_member_leaves(monkeypatch):
    monkeypatch.setenv("ROOM_RECONNECT_LIMIT", "1")
    h = await Room(monkeypatch).open()
    try:
        h.rt("dan").ws.restart()
        await h.wait_for("room_member_reconnected")
        h.rt("dan").ws.restart()                     # a second time: past the limit
        (gone,) = await h.wait_for("room_member_lost")
        # Each member has its own budget: Priya is rebuilt once too.
        h.rt("priya").ws.restart()
        await h.wait_for("room_member_reconnected", 2)
        assert set(h.room.sessions) == {"priya", "chris"}
    finally:
        await h.close()
    assert gone["agent_id"] == "dan" and gone["reason"] == "limit"
    assert gone["attempts"] == 1 and gone["limit"] == 1
    assert len(h.gateway.sockets) == 4 + 2
    assert [e for e in h.of("voice_error") if e["where"] == "room_reconnect"]
    # Members are left, so the page is not told the conversation is lost.
    assert not [f for f in h.page.json if f.get("type") == "error"]
    assert not h.of("room_members_lost")


@in_a_loop
async def test_a_refused_reconnect_is_tried_again_and_counts(monkeypatch):
    h = await Room(monkeypatch).open()
    try:
        h.gateway.refuse = 1
        h.rt("chris").ws.restart()
        (rec,) = await h.wait_for("room_member_reconnected")
    finally:
        await h.close()
    (refused,) = [e for e in h.of("voice_error") if e["where"] == "room_reconnect"]
    assert refused["agent_id"] == "chris" and refused["attempt"] == 1
    assert "still restarting" in refused["message"]
    assert rec["agent_id"] == "chris" and rec["attempt"] == 2


@in_a_loop
async def test_a_member_the_gateway_keeps_refusing_leaves_the_room(monkeypatch):
    h = await Room(monkeypatch).open()
    try:
        h.gateway.refuse = 5
        h.rt("chris").ws.restart()
        (gone,) = await h.wait_for("room_member_lost")
    finally:
        await h.close()
    assert gone["reason"] == "connect_refused" and gone["attempts"] == 2
    assert len([e for e in h.of("voice_error") if e["where"] == "room_reconnect"
                and e.get("attempt")]) == 2
    assert not h.of("room_member_reconnected")
    assert not h.runner._member_notes


@in_a_loop
async def test_at_zero_a_closed_member_is_handled_as_on_10_01a(monkeypatch):
    """ROOM_RECONNECT_LIMIT=0 turns all of it off: a member whose socket
    went stays in the room until a send to it fails, then leaves it (10-01a's
    GroupRoom._went_deaf), and nothing of 10-14a is written or sent."""
    monkeypatch.setenv("ROOM_RECONNECT_LIMIT", "0")
    h = await Room(monkeypatch).open()
    try:
        for aid in CAST:
            h.rt(aid).ws.restart()
        await until(lambda: len(h.of("voice_error")) >= 3)
        await settle(h.runner)
        assert set(h.room.sessions) == set(CAST), "left in the room, as on 10-01a"
        await h.room.hear(LOUD)               # the participant's next frame
        assert not h.room.sessions and set(h.room.lost) == set(CAST)
        await h.participant("Is anybody still there? I can't hear any of you now.")
        await h.runner._run_group_turn()
    finally:
        await h.close()
    assert len(h.gateway.sockets) == 4
    for kind in ("room_member_dropped", "room_member_reconnected",
                 "room_member_lost", "room_members_lost", "floor_regranted"):
        assert not h.of(kind), kind
    assert not [f for f in h.page.json if f.get("type") in ("error", "voice_notice")]
    assert h.of("group_turn_no_members")


@in_a_loop
async def test_a_room_torn_down_mid_rebuild_seats_nothing(monkeypatch):
    h = await Room(monkeypatch).open()
    h.gateway.hold = asyncio.Event()
    h.rt("dan").ws.restart()
    await until(lambda: "dan" in h.runner._member_rebuilds)
    task = h.runner._member_rebuilds["dan"]
    await h.close()
    h.gateway.hold.set()
    await asyncio.gather(task, return_exceptions=True)
    assert task.cancelled()
    assert not h.of("room_member_reconnected") and not h.runner._member_notes
    assert h.runner.room is None and len(h.gateway.sockets) == 4


# --------------------------------------------------------------------------
# 6. The rest of the room's state
# --------------------------------------------------------------------------

@in_a_loop
async def test_a_line_said_while_a_member_was_being_rebuilt_is_told_to_it(monkeypatch):
    h = await Room(monkeypatch).open()
    try:
        h.rt("chris").ws.restart()
        await h.wait_for("room_member_reconnected")
        chris_ws = h.rt("chris").ws
        seated_at = h.runner._member_seated_at["chris"]
        n = len(chris_ws.creates())
        # Begun before Chris was back, written after he was seeded.
        await h.runner._record_user_turn(
            "So the pilot team goes first and everyone else follows after.",
            voiced_ms=2400, voiced_span_ms=2400, spoken_at=seated_at - 1.0)
        # Begun after: he hears its audio like everyone else.
        await h.runner._record_user_turn(
            "And the date stays where it is in the draft for now.",
            voiced_ms=2400, voiced_span_ms=2400, spoken_at=seated_at + 1.0)
    finally:
        await h.close()
    assert seeded(chris_ws)[n:] == [
        ("user", "So the pilot team goes first and everyone else follows after.")]
    (late,) = h.of("room_member_line_late")
    assert late["agent_id"] == "chris" and late["sent"] is True


@in_a_loop
async def test_off_the_gpt_route_the_rebuilt_member_gets_a_recap_in_its_brief(monkeypatch):
    """Native-audio and Gemini rooms run no item operations (10-01a is gpt
    only), so a rebuilt member there is recapped in its brief for one reply,
    the way a rebuilt 1:1 session is."""
    h = await Room(monkeypatch, model=NATIVE).open()
    try:
        await h.participant("We need a date for the rollout before we leave today.")
        whole = {"memory": "whole", "mem_text": None, "told": "generated",
                 "told_text": "Last year's rollout slipped twice."}
        h.runner._note_room_line(h.agents["priya"], "Last year's rollout slipped twice.",
                                 None, whole)
        h.rt("dan").ws.restart()
        (rec,) = await h.wait_for("room_member_reconnected")
        dan_ws = h.rt("dan").ws
    finally:
        await h.close()
    (brief,) = dan_ws.instructions()
    assert "What has been said so far, most recent last:" in brief
    assert "- Them: We need a date for the rollout before we leave today." in brief
    assert "- Priya: Last year's rollout slipped twice." in brief
    assert dan_ws.creates() == [] and rec["seed_route"] == "brief_recap"
    assert rec["seeded"] == {"participant": 0, "own": 0, "told": 0}


@in_a_loop
async def test_the_note_is_spent_once_the_rebuilt_member_has_spoken(monkeypatch):
    h = await Room(monkeypatch).open()
    try:
        h.rt("dan").ws.restart()
        await h.wait_for("room_member_reconnected")
        dan = h.agents["dan"]
        assert "The line dropped" in h.runner._instructions_for(dan)
        assert "The line dropped" not in h.runner._instructions_for(h.agents["priya"])
        await h.speaks("dan", "R5", "Fine, end of the month it is, then.")
        assert "The line dropped" not in h.runner._instructions_for(dan)
    finally:
        await h.close()


@in_a_loop
async def test_a_stale_loss_report_never_takes_the_rebuilt_member_out(monkeypatch):
    """A send gathered before the rebuild seated can fail after it; the report
    is about the old session and closes nothing but that."""
    h = await Room(monkeypatch).open()
    try:
        old = h.rt("dan")
        old.ws.restart()
        await h.wait_for("room_member_reconnected")
        new = h.rt("dan")
        await h.room._went_deaf("dan", old, "late report")
        assert h.room.session_for("dan") is new and "dan" not in h.room.lost
    finally:
        await h.close()
    assert len(h.of("room_member_dropped")) == 1


@in_a_loop
async def test_a_commit_only_grant_stops_waiting_on_a_socket_that_is_gone():
    class Gone:
        ws = None
        _cancelled_ids: set = set()
        _created_ids: set = set()
        _done_ids: set = set()
        _response_created_id = None
        _response_saw_output = False
        autofire_active = False
        _last_output_at = 0.0
        first_audio_at = None
        _cancelled_by_us = False

    room = gr.GroupRoom([], instructions_for=lambda a: "", voice_for=lambda a: "",
                        model=GPT)
    room.speaking = "dan"
    assert await room._commit_answered(Gone(), "dan", time.time(), 5.0) == "lost"


# --------------------------------------------------------------------------
# 7. On the record
# --------------------------------------------------------------------------

def test_the_version_and_its_history_line():
    assert llm.PIPELINE_VERSION == "2026-10-14a"
    src = (ROOT / "server" / "llm.py").read_text(encoding="utf-8")
    assert src.count("#   2026-10-14a") == 1


def test_the_reconnect_rules_are_on_the_record(monkeypatch, tmp_path):
    prov = llm.provenance(GPT)
    assert prov["reconnect"] == {"one_to_one_limit": R.RECONNECT_LIMIT,
                                 "room_member_limit": 2,
                                 "room_seed": "conversation_items"}
    assert llm.provenance(NATIVE)["reconnect"]["room_seed"] == "brief_recap"
    for raw, want in (("0", 0), ("5", 5), ("-3", 0), ("two", 2), ("", 2)):
        monkeypatch.setenv("ROOM_RECONNECT_LIMIT", raw)
        assert R.room_reconnect_limit() == want, raw
    monkeypatch.delenv("ROOM_RECONNECT_LIMIT")
    sdir = tmp_path / "s_1_ab"
    sdir.mkdir()
    (sdir / "events.jsonl").write_text(
        json.dumps({"t": 0.0, "type": "session_start"}) + "\n"
        + json.dumps({"t": 0.1, "type": "realtime_session_started",
                      "model": GPT, **prov}) + "\n", encoding="utf-8")
    assert encounter_record.build(sdir)["provenance"]["reconnect"] == prov["reconnect"]
    assert "reconnect" in L.PIPELINE_KEYS
    assert L.pipeline_provenance(prov, {})["reconnect"] == prov["reconnect"]


def test_encounter_health_says_what_happened_to_the_room():
    events = [{"type": "room_member_dropped"}] * 4 + [
        {"type": "room_member_reconnected"}] * 3 + [{"type": "room_member_lost"}]
    notes = "\n".join(encounter_health.pipeline_notes(events))
    assert ("gateway closed a room member's session 4 time(s); 3 rebuilt in "
            "the room, 1 left it") in notes
    assert "room member" not in "\n".join(encounter_health.pipeline_notes([]))


# --------------------------------------------------------------------------
# 8. The other ways a member holds the floor, and the interaction boundary
# --------------------------------------------------------------------------

@in_a_loop
async def test_a_probe_whose_speaker_drops_is_asked_again_once_back(monkeypatch):
    """The silence probe takes the floor the way a routed turn does, and is
    given it again the same way."""
    h = await Room(monkeypatch).open()
    try:
        commits = []

        def on_commit(ws, f):
            commits.append(ws)
            return [created(f"P{len(commits)}")]
        h.gateway.on_commit = on_commit
        await h.participant("We need a date for the rollout before we leave today.")
        # Routed by the turn it ended, long since: the probe is for the
        # silence after it.
        h.runner._take_unrouted()
        old = h.rt("dan")
        probe = asyncio.ensure_future(h.runner._probe_room())
        await until(lambda: (h.room.last_grant or {}).get("via") == "commit")
        old.ws.restart()
        await until(lambda: len(commits) == 2)
        dan_ws = h.rt("dan").ws
        assert commits[1] is dan_ws
        dan_ws.feed(reply("P2", "Silence means yes, then: one team goes first."))
        await asyncio.wait_for(probe, 10)
    finally:
        await h.close()
    (fired,) = h.of("trigger_fired")
    assert fired["probing"] is True
    (regrant,) = h.of("floor_regranted")
    assert regrant["agent_id"] == "dan" and regrant["why"] == "dropped_unanswered"
    (redo,) = h.of("stage_direction_redelivered")
    assert redo["trigger_id"] == fired["trigger_id"]
    (turn,) = h.of("assistant_turn")
    assert turn["agent_id"] == "dan" and turn["text"].startswith("Silence means yes")
    assert not h.of("trigger_undelivered")


def _unnamed_turn_room(h, entries):
    """A room whose lead has already opened, so an unnamed line goes to the
    director, and whose follow-ups take the floor without the page's gap."""
    h.runner._opening_agent, h.runner._opening_note = None, ""
    h.session.director.entries = entries


@in_a_loop
async def test_a_follow_up_whose_session_drops_unanswered_is_asked_again(monkeypatch):
    monkeypatch.setenv("FOLLOWUP_GAP_S", "-1")
    h = await Room(monkeypatch).open()
    try:
        _unnamed_turn_room(h, [{"agent_id": "chris", "intent": None},
                               {"agent_id": "priya", "intent": None}])
        commits = []

        def on_commit(ws, f):
            commits.append(ws)
            return [created(f"F{len(commits)}")]
        h.gateway.on_commit = on_commit
        await h.participant("So what does everyone think about the rollout order now?")
        turn = asyncio.ensure_future(h.runner._run_group_turn())
        await until(lambda: commits)
        assert commits[0] is h.rt("chris").ws
        commits[0].feed(reply("F1", "One team first, then the rest of us."))
        await until(lambda: len(commits) == 2)
        old = h.rt("priya")
        assert commits[1] is old.ws
        await until(lambda: (h.room.last_grant or {}).get("agent_id") == "priya"
                    and h.room.last_grant.get("via") == "commit")
        old.ws.restart()
        await until(lambda: len(commits) == 3)
        priya_ws = h.rt("priya").ws
        assert commits[2] is priya_ws
        priya_ws.feed(reply("F3", "And last year says we should leave slack."))
        await asyncio.wait_for(turn, 10)
    finally:
        await h.close()
    assert [t["agent_id"] for t in h.of("assistant_turn")] == ["chris", "priya"]
    (regrant,) = h.of("floor_regranted")
    assert regrant["agent_id"] == "priya" and regrant["why"] == "dropped_unanswered"
    # Priya's new conversation has Chris's line of this turn, as she was told
    # it, before the commit that asks her for hers.
    assert ("user", told("Chris", "One team first, then the rest of us.")) in seeded(priya_ws)


@in_a_loop
async def test_a_follow_up_whose_grant_fails_does_not_wait_out_the_timeout(monkeypatch):
    """Lost for good between the director's answer and its turn: nobody was
    asked to speak, and the turn goes on at once (it waited 45 s)."""
    monkeypatch.setenv("FOLLOWUP_GAP_S", "-1")
    monkeypatch.setenv("ROOM_RECONNECT_LIMIT", "1")
    h = await Room(monkeypatch).open()
    try:
        h.runner._member_reconnects["priya"] = 1      # her budget is spent
        _unnamed_turn_room(h, [{"agent_id": "chris", "intent": None},
                               {"agent_id": "priya", "intent": "Point back to last year."}])
        commits = []

        def on_commit(ws, f):
            commits.append(ws)
            return [created(f"G{len(commits)}")]
        h.gateway.on_commit = on_commit
        gap = h.runner._await_followup_gap

        async def priya_goes(room):
            h.rt("priya").ws.restart()
            await until(lambda: h.of("room_member_lost"))
            return await gap(room)
        h.runner._await_followup_gap = priya_goes
        await h.participant("So what does everyone think about the rollout order now?")
        turn = asyncio.ensure_future(h.runner._run_group_turn())
        await until(lambda: commits)
        commits[0].feed(reply("G1", "One team first, then the rest of us."))
        await asyncio.wait_for(turn, 5)
    finally:
        await h.close()
    assert [t["agent_id"] for t in h.of("assistant_turn")] == ["chris"]
    assert not h.of("group_turn_timeout") and not h.of("floor_regranted")
    assert h.room is None or "priya" not in h.room.sessions


@in_a_loop
async def test_an_interaction_boundary_waits_for_a_member_being_rebuilt(monkeypatch):
    """S4's working session and its close are one meeting: the room is kept
    across the boundary when its cast is all there. A member being rebuilt
    at that moment is waited for, or the whole room would be rebuilt with
    nobody in it who remembers the meeting."""
    h = await Room(monkeypatch).open()
    try:
        room = h.room
        h.gateway.hold = asyncio.Event()
        h.rt("dan").ws.restart()
        await until(lambda: "dan" in h.runner._member_rebuilds)
        advance = asyncio.ensure_future(h.runner._advance_segment())
        await asyncio.sleep(0.2)
        assert not advance.done()
        h.gateway.hold.set()
        assert await asyncio.wait_for(advance, 10) is True
        assert h.room is room and h.runner.segment == 1
    finally:
        await h.close()
    assert h.of("group_room_kept") and len(h.of("group_room_opened")) == 1
    assert h.of("room_member_reconnected")


@in_a_loop
async def test_a_session_the_gateway_refuses_outright_is_not_rebuilt(monkeypatch):
    """A voice the gateway does not know takes the whole session.update with
    it, and the bridge ends that session itself: a rebuild with the same
    voice would only be refused again."""
    h = await Room(monkeypatch).open()
    try:
        h.rt("chris").ws.feed([{"type": "error", "error": {
            "type": "invalid_request_error", "code": "invalid_value",
            "message": "Invalid value: 'Charon'.", "param": "session.voice"}}])
        (gone,) = await h.wait_for("room_member_lost")
    finally:
        await h.close()
    assert gone["agent_id"] == "chris" and gone["reason"] == "fatal"
    assert not h.of("room_member_reconnected") and len(h.gateway.sockets) == 4


# --------------------------------------------------------------------------
# 9. Found in review of the first cut
# --------------------------------------------------------------------------

@in_a_loop
async def test_a_drop_while_the_grant_is_decided_is_not_granted_twice(monkeypatch):
    """The holder's socket goes inside _grant's wait (a family that waits for
    the gateway's own reply first) and it is back before the grant asks: the
    grant goes to the new session, once. The drop's release of the floor and
    its mark belonged to a grant that never went to the old session."""
    monkeypatch.setenv("AUTOFIRE_WAIT", "1.0")
    h = await Room(monkeypatch, model=NATIVE).open()
    try:
        await h.participant("Dan, can you walk me through the whole rollout plan, step by step?")
        turn = asyncio.ensure_future(h.runner._run_group_turn())
        await until(lambda: h.room.speaking == "dan")   # inside _grant's wait
        h.rt("dan").ws.restart()
        await h.wait_for("room_member_reconnected")
        await until(lambda: h.of("fresh_reply_requested"))
        await asyncio.sleep(1.0)
        new_ws = h.rt("dan").ws
        asks = [m["type"] for m in new_ws.sent if m.get("type") == "response.create"]
        turn.cancel()
        await asyncio.gather(turn, return_exceptions=True)
    finally:
        await h.close()
    assert asks == ["response.create"], asks
    assert len(h.of("fresh_reply_requested")) == 1 and not h.of("floor_regranted")


@in_a_loop
async def test_a_drop_no_turn_was_waiting_on_is_not_granted_again_later(monkeypatch):
    """The floor still names a member nobody waits on (a turn's timeout leaves
    it named while its follow-ups run) when its socket goes. Its next ordinary
    grant is its next ordinary grant, once."""
    h = await Room(monkeypatch).open()
    try:
        h.room.speaking = "dan"
        h.runner._floor_reply_began = False
        h.rt("dan").ws.restart()
        await h.wait_for("room_member_reconnected")
        h.room.speaking = None
        commits = []

        def on_commit(ws, f):
            commits.append(ws)
            return [created(f"E{len(commits)}")]
        h.gateway.on_commit = on_commit
        await h.participant("Dan, can you walk me through the whole rollout plan, step by step?")
        turn = asyncio.ensure_future(h.runner._run_group_turn())
        await until(lambda: commits)
        commits[0].feed(reply("E1", "Week one is the pilot team, then everyone else."))
        await asyncio.wait_for(turn, 10)
    finally:
        await h.close()
    assert len(commits) == 1 and not h.of("floor_regranted")
    assert [t["agent_id"] for t in h.of("assistant_turn")] == ["dan"]
    assert not [f for f in h.page.json if f.get("type") == "voice_notice"]


@in_a_loop
async def test_the_reconnect_note_stays_with_its_room(monkeypatch):
    """A member rebuilt and not heard from before its room closes does not
    carry 'the line dropped' into a room of fresh sessions; nor, in a kept
    room, on top of the continuation note."""
    h = await Room(monkeypatch).open()
    try:
        h.rt("dan").ws.restart()
        await h.wait_for("room_member_reconnected")
        assert "The line dropped" in h.runner._instructions_for(h.agents["dan"])
        await h.runner._advance_segment()          # S4A i1 -> i2: the room is kept
        assert h.of("group_room_kept")
        assert "The line dropped" not in h.runner._instructions_for(h.agents["dan"])
        h.rt("priya").ws.restart()
        await h.wait_for("room_member_reconnected", 2)
        await h.runner._open_room()                # a room of fresh sessions
        (brief,) = h.rt("priya").ws.instructions()
    finally:
        await h.close()
    assert "The line dropped" not in brief and not h.runner._member_notes


@in_a_loop
async def test_the_old_sessions_last_line_does_not_spend_the_new_sessions_note(monkeypatch):
    h = await Room(monkeypatch).open()
    try:
        h.gateway.on_commit = lambda ws, f: [created("R1")]
        await h.participant("Dan, can you walk me through the whole rollout plan, step by step?")
        old = h.rt("dan")
        turn = asyncio.ensure_future(h.runner._run_group_turn())
        await until(lambda: (h.room.last_grant or {}).get("via") == "commit")
        old.ws.feed([item_added("R1", "i1"), adelta("R1", "i1"),
                     tdelta("R1", "i1", "Week one is the pilot team,")])
        await until(lambda: [f for f in h.page.json
                             if f.get("type") == "assistant_started"])
        old.ws.restart()
        await asyncio.wait_for(turn, 10)
        await h.wait_for("room_member_reconnected")
        note_left = "The line dropped" in h.runner._instructions_for(h.agents["dan"])
    finally:
        await h.close()
    (turn_ev,) = h.of("assistant_turn")
    assert turn_ev["interrupted"] is True
    assert note_left, "the interrupted line of the old session spent the note"


@in_a_loop
async def test_a_line_written_across_a_whole_rebuild_is_given_once(monkeypatch):
    """The page is slow for a moment while a participant line is being
    written, and a whole rebuild runs in that moment: it seeds the line, and
    the line's own write does not tell it to the member again."""
    h = await Room(monkeypatch).open()
    try:
        h.gateway.hold = asyncio.Event()
        h.rt("chris").ws.restart()
        await until(lambda: "chris" in h.runner._member_rebuilds)
        gate = asyncio.Event()
        send = h.page.send_json

        async def slow(payload):
            if payload.get("type") == "user_transcript":
                await gate.wait()
            return await send(payload)
        h.page.send_json = slow
        line = "So the pilot team goes first and everyone else follows after."
        writing = asyncio.ensure_future(h.runner._record_user_turn(
            line, voiced_ms=2400, voiced_span_ms=2400, spoken_at=time.time() - 1.0))
        await until(lambda: any(e.get("text") == line for e in h.runner._room_heard))
        h.gateway.hold.set()
        await h.wait_for("room_member_reconnected")
        gate.set()
        await writing
        chris_ws = h.rt("chris").ws
    finally:
        await h.close()
    assert [t for t in seeded(chris_ws) if t[1] == line] == [("user", line)]
    assert not h.of("room_member_line_late")


@in_a_loop
async def test_the_page_is_promised_an_answer_only_when_one_is_asked_for(monkeypatch):
    """Two drops of the holder in one turn: the first is granted again and
    the page says so; the second is not, and the page is not told the
    character heard them."""
    h = await Room(monkeypatch).open()
    try:
        commits = []

        def on_commit(ws, f):
            commits.append(ws)
            return [created(f"H{len(commits)}")]
        h.gateway.on_commit = on_commit
        await h.participant("Dan, can you walk me through the whole rollout plan, step by step?")
        turn = asyncio.ensure_future(h.runner._run_group_turn())
        await until(lambda: (h.room.last_grant or {}).get("via") == "commit")
        h.rt("dan").ws.restart()
        await until(lambda: len(commits) == 2)
        await until(lambda: (h.room.last_grant or {}).get("via") == "commit"
                    and h.rt("dan").ws is commits[1])
        h.rt("dan").ws.restart()
        await asyncio.wait_for(turn, 10)
        await h.wait_for("room_member_reconnected", 2)
    finally:
        await h.close()
    notices = [f for f in h.page.json if f.get("type") == "voice_notice"]
    assert [(n["agent_id"], n["replayed"]) for n in notices] == [("dan", True)]
    assert len(h.of("floor_regranted")) == 1
    assert [e["reason"] for e in h.of("trigger_undelivered")] == ["member_lost"]


@in_a_loop
async def test_off_the_gpt_route_a_line_finished_during_the_rebuild_is_told_after(monkeypatch):
    h = await Room(monkeypatch, model=NATIVE).open()
    try:
        await h.participant("We need a date for the rollout before we leave today.")
        h.gateway.hold = asyncio.Event()
        h.rt("dan").ws.restart()
        await until(lambda: "dan" in h.runner._member_rebuilds)
        await h.speaks("priya", "RP", "Last year's rollout slipped twice, so I doubt it.")
        h.gateway.hold.set()
        (rec,) = await h.wait_for("room_member_reconnected")
        dan_ws = h.rt("dan").ws
    finally:
        await h.close()
    (brief,) = dan_ws.instructions()
    assert "- Them: We need a date for the rollout before we leave today." in brief
    assert seeded(dan_ws) == [
        ("user", told("Priya", "Last year's rollout slipped twice, so I doubt it."))]
    assert rec["seeded"]["told"] == 1


@in_a_loop
async def test_a_cut_decided_while_the_seed_went_out_is_applied_after_the_seat(monkeypatch):
    """The cut cannot reach a member that is not in the room yet: once it is,
    the line it was just given whole is put right in its new conversation."""
    h = await Room(monkeypatch).open()
    try:
        await h.speaks("dan", "R1", LINE, item="d1")
        seed = h.runner._seed_member
        heard = "We slipped the date because the vendor…"

        async def seed_then_cut(aid, rt, entries):
            out = await seed(aid, rt, entries)
            if aid == "dan" and any(e.get("rid") == "R1" for e in entries):
                # The participant's cut, decided while the seed went out.
                h.runner._note_room_cut("dan", "R1", heard, cut_mem="replace",
                                        told_mode="heard")
            return out
        h.runner._seed_member = seed_then_cut
        h.rt("dan").ws.restart()
        await h.wait_for("room_member_reconnected")
        (mem,) = await h.wait_for("member_memory_replaced")
        dan_ws = h.rt("dan").ws
    finally:
        await h.close()
    own = dan_ws.creates()[0]["item"]["id"]
    assert seeded(dan_ws)[0] == ("assistant", LINE)
    assert [m["item_id"] for m in dan_ws.sent
            if m.get("type") == "conversation.item.delete"] == [own]
    assert dan_ws.creates()[-1]["item"]["content"][0]["text"] == heard
    assert mem["reason"] == "seed_reconciled" and mem["action"] == "replaced"


@in_a_loop
async def test_a_note_told_to_a_session_since_replaced_is_not_remembered():
    """A tell going out while its member is rebuilt reaches the old session;
    the room does not file that note under the member's new one."""
    from test_room_memory_hygiene import tracked
    room = gr.GroupRoom([type("A", (), {"id": a, "name": a.title()})() for a in CAST],
                        instructions_for=lambda a: "", voice_for=lambda a: "",
                        model=GPT)
    for aid in CAST:
        room.sessions[aid] = tracked()
    old, new = room.sessions["priya"], tracked()
    sent = old.inject_text

    async def swapped(text, **kw):
        out = await sent(text, **kw)
        room.sessions["priya"] = new                 # rebuilt meanwhile
        return out
    old.inject_text = swapped
    await room.tell("Dan", "We slipped the date.", exclude="dan", response_id="R1")
    told_to = room._told.get(("dan", "R1")) or {}
    assert "chris" in told_to and "priya" not in told_to
