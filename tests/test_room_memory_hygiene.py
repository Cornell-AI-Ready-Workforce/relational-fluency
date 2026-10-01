"""Room memory hygiene: pipeline 2026-10-01a.

The researchers' decision of 2026-10-01: no generated-but-unheard reply
silently enters a room character's own conversation (its gpt realtime
session). Before 10-01a a character kept the whole of a line the participant
cut off after two seconds (generation runs about five times faster than
playback), every reply the room suppressed and then threw away, a reply a
re-brief cancelled, and the full text of a colleague's cut line as told to
it; and a retry's nudge sat in its memory as a line the participant said.

The gateway facts the mechanism rests on were measured live on 2026-09-30
(gpt-realtime-2.1 through the Cornell LiteLLM gateway): conversation.item
.delete is acked with conversation.item.deleted and forgotten (4/4); a reply
often has two output items (3 of 4), so every one is deleted; deleting them
and then creating the heard words with previous_item_id = the item before the
reply leaves exactly those words in place (3/3); conversation.item.truncate
drops the transcript (4/4) and is never sent.

Offline: real RealtimeVoiceSession objects on the fake socket of
tests/test_bridge_correctness.py, a real GroupRoom, a real runner. The frame
shapes below are the GA ones the probes saw; what the probes did NOT measure
(an echoed event_id, client item ids, "root") is marked where it is relied on.
"""
from __future__ import annotations

import asyncio
import json
import sys
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from server import encounter_record  # noqa: E402
from server import group_room as gr  # noqa: E402
from server import llm  # noqa: E402
from server import realtime_voice_session as rvs  # noqa: E402
from server.voice import realtime as R  # noqa: E402
from server.voice import turn_audio  # noqa: E402
from tools import load_analysis_db as L  # noqa: E402

from test_bridge_correctness import (  # noqa: E402
    GPT, LOUD, NATIVE, FakeSession, PageWS, WireWS, adelta, audio_done,
    bridge, created, in_a_loop, item_added, settle, tdelta, tdone,
    until,
)

KNOBS = ("ROOM_CUT_MEMORY", "ROOM_TOLD_TEXT", "ROOM_NUDGE_ROLE")


@pytest.fixture(autouse=True)
def quick(monkeypatch):
    monkeypatch.setattr(R, "RECV_POLL_S", 0.02)
    monkeypatch.setattr(R, "AUDIO_RETRY_QUIET_S", 0.05, raising=False)
    monkeypatch.setattr(R, "MEMORY_SETTLE_S", 0.3)
    monkeypatch.setattr(R, "MEMORY_OP_TTL_S", 0.6)
    monkeypatch.setenv("TRANSCRIPT_GRACE_SECONDS", "0.2")
    for knob in KNOBS + ("ROOM_ADOPT_GUARD", "HELD_REPLY_TTL",
                         "ROOM_SPLIT_TURN_S", "AUTOFIRE_WAIT"):
        monkeypatch.delenv(knob, raising=False)


# --------------------------------------------------------------------------
# Frames
# --------------------------------------------------------------------------

def added(item, prev, role="assistant", text=None, type_="message"):
    it = {"id": item, "type": type_, "role": role}
    if text is not None:
        part = "output_text" if role == "assistant" else "input_text"
        it["content"] = [{"type": part, "text": text}]
    return {"type": "conversation.item.added", "previous_item_id": prev,
            "item": it}


def committed(item, prev):
    return {"type": "input_audio_buffer.committed", "item_id": item,
            "previous_item_id": prev}


def deleted(item):
    return {"type": "conversation.item.deleted", "item_id": item}


def done_out(rid, items, status="completed", types_=None, texts=None):
    out = []
    for i, iid in enumerate(items):
        entry = {"id": iid, "type": (types_ or {}).get(iid, "message"),
                 "role": "assistant"}
        if texts and iid in texts:
            entry["content"] = [{"type": "output_audio",
                                 "transcript": texts[iid]}]
        out.append(entry)
    return {"type": "response.done",
            "response": {"id": rid, "status": status, "output": out}}


def err(code, message, event_id=None, param=None):
    return {"type": "error", "error": {"type": "invalid_request_error",
                                       "code": code, "message": message,
                                       "param": param, "event_id": event_id}}


def tracked(model=GPT, nudge_role="system"):
    rt = bridge(model)
    rt.track_items = True
    rt.nudge_role = nudge_role
    return rt


def of(rt, type_):
    return [m for m in rt.ws.sent if m.get("type") == type_]


def deletes(rt):
    return [m["item_id"] for m in of(rt, "conversation.item.delete")]


def creates(rt):
    return of(rt, "conversation.item.create")


class Consumer:
    """events() iterated by a task of its own, as a member pump does."""

    def __init__(self, rt, on_event=None):
        self.rt, self.evs, self._on = rt, [], on_event
        self.task = asyncio.ensure_future(self._run())

    async def _run(self):
        async for ev in self.rt.events():
            self.evs.append(ev)
            if self._on is not None:
                await self._on(ev)

    def of(self, type_):
        return [e for e in self.evs if e["type"] == type_]

    async def wait(self, type_, n=1, timeout=2.0):
        await until(lambda: len(self.of(type_)) >= n, timeout)
        return self.of(type_)

    async def stop(self):
        self.task.cancel()
        await asyncio.gather(self.task, return_exceptions=True)


def reply_frames(rid, items, words_per_item=3, deltas_per_item=2):
    """A reply streamed as the gateway sends one: each item added (both
    output_item.added and conversation.item.added, chained), its transcript
    and audio deltas, its .done frames. No response.done."""
    frames = [created(rid)]
    prev = None
    for n, iid in enumerate(items):
        frames.append(item_added(rid, iid))
        frames.append(added(iid, prev if n else "__prev__"))
        words = [f" w{n}{k}" for k in range(words_per_item)]
        for k in range(deltas_per_item):
            frames.append(adelta(rid, iid))
        for w in words:
            frames.append(tdelta(rid, iid, w))
        frames += [audio_done(rid, iid), tdone(rid, iid, "".join(words).strip())]
        prev = iid
    return frames


def chain(frames, first_prev):
    """reply_frames with the first item placed after `first_prev`."""
    out = []
    for f in frames:
        if f.get("previous_item_id") == "__prev__":
            f = dict(f, previous_item_id=first_prev)
        out.append(f)
    return out


# --------------------------------------------------------------------------
# 1. The bridge: mechanism
# --------------------------------------------------------------------------

@in_a_loop
async def test_untracked_bridge_sends_nothing_new():
    """Every 1:1 session and every test double: nothing tracked, no frame
    anywhere it was not before, inject_text's frame byte for byte 28b's."""
    rt = bridge(GPT)
    assert await rt.forget_reply("R1") == "untracked"
    assert await rt.correct_item("x", role="user", text="y") == "untracked"
    assert rt.reserve_memory("R1") is False
    assert rt.ws.sent == []
    assert await rt.inject_text("x") is None
    assert await rt.inject_text("x", item_id="rf_t_1") is None
    assert rt.ws.sent == [{"type": "conversation.item.create",
                           "item": {"type": "message", "role": "user",
                                    "content": [{"type": "input_text",
                                                 "text": "x"}]}}] * 2
    assert rt.nudge_role == "user" and rt.track_items is False


@in_a_loop
async def test_every_item_of_a_two_item_reply_is_deleted_only_after_its_done():
    rt = tracked()
    await rt.request_response()
    c = Consumer(rt)
    try:
        rt.ws.feed([committed("p1", None)]
                   + chain(reply_frames("R1", ["i1", "i2"]), "p1"))
        await c.wait("agent_transcript", 2)
        assert await rt.forget_reply("R1") == "deferred"
        assert deletes(rt) == [], "a delete went out before the reply's done"
        rt.ws.feed([done_out("R1", ["i1", "i2"])])
        await until(lambda: len(deletes(rt)) == 2)
        dels = of(rt, "conversation.item.delete")
        assert [d["item_id"] for d in dels] == ["i1", "i2"]
        assert all(d["event_id"].startswith("rfm_") for d in dels)
        assert of(rt, "conversation.item.truncate") == []
        assert creates(rt) == [], "nothing was to be put back"
        rt.ws.feed([deleted("i1"), deleted("i2")])
        (op,) = await c.wait("memory_op")
    finally:
        await c.stop()
    assert op["kind"] == "reply" and op["response_id"] == "R1"
    assert op["items_deleted"] == ["i1", "i2"] and op["action"] == "deleted"
    assert op["deferred"] is True and op["inserted_item_id"] is None
    assert rt.memory_done("R1") == "deleted"
    assert rt._memory_idle.is_set() and not rt._memory_ops


@in_a_loop
async def test_replace_reinserts_the_heard_words_in_place():
    rt = tracked()
    await rt.request_response()
    c = Consumer(rt)
    try:
        rt.ws.feed([committed("p1", None)]
                   + chain(reply_frames("R1", ["i1", "i2"]), "p1")
                   + [done_out("R1", ["i1", "i2"]), committed("p2", "i2")])
        await c.wait("response_done")
        await until(lambda: "p2" in rt._conv)
        assert await rt.forget_reply("R1", keep_text="Okay, thanks…") == "sent"
        assert deletes(rt) == ["i1", "i2"]
        assert creates(rt) == [], "inserted before the deletes were acked"
        rt.ws.feed([deleted("i1"), deleted("i2")])
        (op,) = await c.wait("memory_op")
    finally:
        await c.stop()
    (ins,) = creates(rt)
    assert ins["previous_item_id"] == "p1"
    assert ins["item"]["role"] == "assistant"
    assert ins["item"]["content"] == [{"type": "output_text",
                                       "text": "Okay, thanks…"}]
    assert ins["item"]["id"].startswith("rf_") and len(ins["item"]["id"]) <= 32
    assert set(deletes(rt)) == {"i1", "i2"}, "a participant item was deleted"
    assert op["action"] == "replaced" and op["placement"] == "in_place"
    assert op["inserted_item_id"] == ins["item"]["id"]
    assert op["deferred"] is False
    # In the mirror where it went: after p1, before the later participant item.
    assert rt._conv == ["p1", ins["item"]["id"], "p2"]


@in_a_loop
async def test_a_reply_whose_done_passed_is_handled_at_once_and_goes_first():
    """forget_reply in the caller's task: its deletes are on the wire before
    it returns, and a reply-start after it waits for the replacement."""
    rt = tracked()
    await rt.request_response()
    c = Consumer(rt)
    try:
        rt.ws.feed([committed("p1", None)]
                   + chain(reply_frames("R1", ["i1"]), "p1")
                   + [done_out("R1", ["i1"])])
        await c.wait("response_done")
        assert await rt.forget_reply("R1", keep_text="Okay…") == "sent"
        assert rt.ws.types()[-1] == "conversation.item.delete"
        rt.ws.feed([deleted("i1")])
        await rt.request_response()
        await c.wait("memory_op_waited")
    finally:
        await c.stop()
    tail = rt.ws.types()[-3:]
    assert tail == ["conversation.item.delete", "conversation.item.create",
                    "response.create"]


@in_a_loop
async def test_items_named_only_by_response_done_are_deleted():
    """A cut reply's tail is discarded before the bridge's bookkeeping
    (cancelled_output_discard), output_item frames included; the item mirror
    runs before that discard, and response.done's own output list names
    whatever was missed."""
    rt = tracked()
    await rt.request_response()
    c = Consumer(rt)
    try:
        rt.ws.feed([committed("p1", None), created("R1"), item_added("R1", "i1"),
                    added("i1", "p1"), tdelta("R1", "i1", "Okay,"),
                    adelta("R1", "i1")])
        await c.wait("agent_audio")
        await rt.cancel_response()                       # the barge-in
        rt.ws.feed([item_added("R1", "i2"), adelta("R1", "i2"),
                    done_out("R1", ["i1", "i2", "i3"], "cancelled")])
        await c.wait("cancelled_output")
        assert rt.reply_item_ids("R1") == ["i1", "i2", "i3"]
        assert await rt.forget_reply("R1") == "sent"
        assert deletes(rt) == ["i1", "i2", "i3"]
        assert not c.of("agent_audio")[1:], "the tail was relayed"
    finally:
        await c.stop()


@in_a_loop
async def test_function_call_items_are_kept():
    rt = tracked()
    await rt.request_response()
    c = Consumer(rt)
    try:
        rt.ws.feed([created("R1"), tdelta("R1", "i1", "Done."),
                    done_out("R1", ["i1", "fc1"],
                             types_={"fc1": "function_call"})])
        await c.wait("response_done")
        assert await rt.forget_reply("R1") == "sent"
        rt.ws.feed([deleted("i1")])
        (op,) = await c.wait("memory_op")
    finally:
        await c.stop()
    assert deletes(rt) == ["i1"]
    assert op["items_deleted"] == ["i1"] and op["items_kept"] == ["fc1"]


async def _streaming_after_a_delete(rt, c):
    """R1 is over and being deleted; R2 is streaming. Returns the delete's
    event_id."""
    rt.ws.feed([created("R1"), tdelta("R1", "i1", "One."), done_out("R1", ["i1"])])
    await c.wait("response_done")
    assert await rt.forget_reply("R1", keep_text="One") == "sent"
    eid = of(rt, "conversation.item.delete")[-1]["event_id"]
    await rt.request_response()
    rt.ws.feed([created("R2"), tdelta("R2", "j1", "Two")])
    await until(lambda: any(e.get("text") == "Two" for e in c.evs))
    assert rt._response_active and rt._response_saw_output
    return eid


@in_a_loop
async def test_a_delete_error_is_not_a_reply_error():
    for by_id in (True, False):
        rt = tracked()
        await rt.request_response()
        c = Consumer(rt)
        try:
            eid = await _streaming_after_a_delete(rt, c)
            n = len(c.evs)
            rt.ws.feed([err("item_not_found",
                            "Item with id 'i1' not found.",
                            event_id=eid if by_id else None, param="item_id")])
            (e,) = await c.wait("memory_op_error")
            (op,) = await c.wait("memory_op")
            rt.ws.feed([tdelta("R2", "j1", " three")])
            await until(lambda: any(x.get("text") == " three" for x in c.evs))
            # Read before the consumer stops: stopping events() ends a reply.
            active = rt._response_active
        finally:
            await c.stop()
        later = [x["type"] for x in c.evs[n:]]
        assert "error" not in later and "response_done" not in later, later
        assert e["op"] == "delete" and e["item_id"] == "i1"
        assert e["code"] == "item_not_found"
        assert active is True, "the streaming reply was ended"
        assert op["action"] == "refused" and op["insert_skipped"] == "delete_refused"
        assert creates(rt) == [], "the heard words were put in beside the line"
        assert rt.memory_errors == 1

    # And an error that is not ours still takes the bridge's own path.
    rt = tracked()
    await rt.request_response()
    c = Consumer(rt)
    try:
        await _streaming_after_a_delete(rt, c)
        rt.ws.feed([err("server_error", "boom")])
        await c.wait("error")
    finally:
        await c.stop()
    assert any(x["type"] == "response_done" and x.get("interrupted") for x in c.evs)
    assert not c.of("memory_op_error")


@in_a_loop
async def test_an_insert_error_on_a_vanished_anchor_retries_at_the_end():
    rt = tracked()
    await rt.request_response()
    c = Consumer(rt)
    try:
        rt.ws.feed([committed("p1", None)]
                   + chain(reply_frames("R1", ["i1"]), "p1")
                   + [done_out("R1", ["i1"])])
        await c.wait("response_done")
        await rt.forget_reply("R1", keep_text="Heard")
        rt.ws.feed([deleted("i1")])
        await c.wait("memory_op")
        (ins,) = creates(rt)
        rt.ws.feed([err("invalid_value",
                        "Item with id 'p1' not found.",
                        event_id=ins["event_id"], param="previous_item_id")])
        (e,) = await c.wait("memory_op_error")
    finally:
        await c.stop()
    first, again = creates(rt)
    assert first["previous_item_id"] == "p1" and "previous_item_id" not in again
    assert again["item"]["content"][0]["text"] == "Heard"
    assert e["op"] == "insert" and e["recovery"] == "inserted_at_end"
    assert e["placement"] == "end_after_error"
    assert e["retry_item_id"] == again["item"]["id"]


@in_a_loop
async def test_a_reply_start_waits_for_a_pending_op():
    rt = tracked()
    await rt.request_response()
    c = Consumer(rt)
    try:
        rt.ws.feed([committed("p1", None)] + chain(reply_frames("R1", ["i1"]), "p1"))
        await c.wait("agent_transcript")
        await rt.cancel_response()
        assert await rt.forget_reply("R1") == "deferred"
        n = len(rt.ws.sent)
        grant = asyncio.ensure_future(rt.commit_input())
        await asyncio.sleep(0.05)
        assert "input_audio_buffer.commit" not in rt.ws.types()[n:], (
            "a reply-start went out with a delete still owed")
        rt.ws.feed([done_out("R1", ["i1"], "cancelled"), deleted("i1")])
        await asyncio.wait_for(grant, 1)
        await c.wait("memory_op_waited")
    finally:
        await c.stop()
    assert rt.ws.types()[n:] == ["conversation.item.delete",
                                 "input_audio_buffer.commit"]
    assert rt.memory_late == 0

    # No done within REALTIME_MEMORY_SETTLE_S: the reply-start goes anyway,
    # and says so. A character is never muted by its own memory.
    rt = tracked()
    await rt.request_response()
    c = Consumer(rt)
    try:
        rt.ws.feed([created("R9"), tdelta("R9", "k1", "Hm")])
        await c.wait("agent_transcript_delta")
        await rt.cancel_response()
        assert await rt.forget_reply("R9") == "deferred"
        t0 = time.time()
        await rt.commit_input()
        assert time.time() - t0 >= R.MEMORY_SETTLE_S - 0.05
        (late,) = await c.wait("memory_op_late")
    finally:
        await c.stop()
    assert late["before"] == "commit" and late["pending"] == ["R9"]
    assert late["own_task"] is False and rt.memory_late == 1


@in_a_loop
async def test_the_gate_never_waits_inside_the_events_task():
    rt = tracked()
    await rt.request_response()
    took = []

    async def on_event(ev):
        if ev["type"] == "agent_transcript_delta" and ev["text"] == "Hm":
            t0 = time.time()
            await rt.prompt_response("n")
            took.append(time.time() - t0)

    rt.ws.feed([created("R9")])
    c = Consumer(rt)
    try:
        await until(lambda: "R9" in rt._created_ids)
        await rt.cancel_response()
        assert await rt.forget_reply("R9") == "deferred"
        c._on = on_event
        rt.ws.feed([tdelta("R8", "k1", "Hm")])
        (late,) = await c.wait("memory_op_late")
    finally:
        await c.stop()
    assert took and took[0] < 0.05, "the member pump's own retry waited on itself"
    assert late["own_task"] is True and late["before"] == "prompt"


@in_a_loop
async def test_an_op_without_a_done_expires_and_touches_nothing():
    rt = tracked()
    await rt.request_response()
    c = Consumer(rt)
    try:
        rt.ws.feed([created("R1"), tdelta("R1", "i1", "Hm")])
        await c.wait("agent_transcript_delta")
        assert await rt.forget_reply("R1") == "deferred"
        (skip,) = await c.wait("memory_op_skipped", timeout=3)
    finally:
        await c.stop()
    assert skip["why"] == "no_done" and skip["response_id"] == "R1"
    assert deletes(rt) == []
    assert rt._memory_idle.is_set() and not rt._memory_ops


@in_a_loop
async def test_a_reservation_holds_reply_starts_until_it_is_decided():
    """The barge-in reserves the cut reply before its finalize decides
    (review 2): the stale done that releases the floor comes first."""
    rt = tracked()
    await rt.request_response()
    c = Consumer(rt)
    try:
        rt.ws.feed([committed("p1", None)] + chain(reply_frames("R1", ["i1"]), "p1"))
        await c.wait("agent_transcript")
        await rt.cancel_response()
        assert rt.reserve_memory("R1") is True
        rt.ws.feed([done_out("R1", ["i1"], "cancelled")])
        await c.wait("response_done")
        assert deletes(rt) == [], "a reservation is not a decision"
        n = len(rt.ws.sent)
        grant = asyncio.ensure_future(rt.commit_input())
        await asyncio.sleep(0.05)
        assert rt.ws.types()[n:] == []
        assert await rt.forget_reply("R1", keep_text="Ok") == "sent"
        rt.ws.feed([deleted("i1")])
        await asyncio.wait_for(grant, 1)
    finally:
        await c.stop()
    assert rt.ws.types()[n:] == ["conversation.item.delete",
                                 "conversation.item.create",
                                 "input_audio_buffer.commit"]
    # Released, a reservation lets everything go at once.
    assert rt.reserve_memory("R2") is True
    assert rt.release_memory("R2") is True and rt._memory_idle.is_set()


@in_a_loop
async def test_correct_item_rewrites_a_told_line_in_place():
    rt = tracked()
    c = Consumer(rt)
    try:
        assert await rt.inject_text("full note", item_id="rf_t_x") == "rf_t_x"
        told = creates(rt)[-1]
        assert told["item"]["id"] == "rf_t_x" and "event_id" in told
        rt.ws.feed([committed("p1", None),
                    added("rf_t_x", "p1", role="user", text="full note")])
        await until(lambda: rt._conv == ["p1", "rf_t_x"])
        assert await rt.correct_item("rf_t_x", role="user", text="heard note") == "sent"
        assert deletes(rt) == ["rf_t_x"]
        rt.ws.feed([deleted("rf_t_x")])
        (op,) = await c.wait("memory_op")
        ins = creates(rt)[-1]
        assert ins["previous_item_id"] == "p1"
        assert ins["item"]["role"] == "user"
        assert ins["item"]["content"] == [{"type": "input_text",
                                           "text": "heard note"}]
        assert op["kind"] == "told_correction" and op["action"] == "corrected"

        # Nothing heard: the note is only deleted.
        await rt.inject_text("other note", item_id="rf_t_y")
        rt.ws.feed([added("rf_t_y", "p1", role="user", text="other note")])
        await until(lambda: "rf_t_y" in rt._conv)
        n = len(creates(rt))
        assert await rt.correct_item("rf_t_y", role="user", text=None) == "sent"
        rt.ws.feed([deleted("rf_t_y")])
        await c.wait("memory_op", 2)
        assert len(creates(rt)) == n
        assert c.of("memory_op")[1]["action"] == "deleted"
    finally:
        await c.stop()


@in_a_loop
async def test_a_renamed_create_is_aliased():
    """Unmeasured (P-M1): should the gateway put its own id on an item we
    named, the item is matched by role and text and corrected under its
    gateway name."""
    rt = tracked()
    c = Consumer(rt)
    try:
        await rt.inject_text("full note", item_id="rf_t_x")
        rt.ws.feed([added("item_srv1", None, role="user", text="full note")])
        await until(lambda: rt._id_alias.get("rf_t_x") == "item_srv1")
        assert await rt.correct_item("rf_t_x", role="user", text="heard") == "sent"
    finally:
        await c.stop()
    assert deletes(rt) == ["item_srv1"]


@in_a_loop
async def test_a_refused_tell_is_sent_again_as_todays_frame():
    """Unmeasured (P-M1) the other way: a gateway that refuses a client id
    must not leave a colleague untold (review 10)."""
    rt = tracked()
    c = Consumer(rt)
    try:
        await rt.inject_text("full note", item_id="rf_t_x")
        eid = creates(rt)[-1]["event_id"]
        rt.ws.feed([err("invalid_value", "Invalid 'item.id'.", event_id=eid,
                        param="item.id")])
        (e,) = await c.wait("memory_op_error")
    finally:
        await c.stop()
    assert e["op"] == "tell" and e["recovery"] == "told_without_id"
    assert creates(rt)[-1] == {"type": "conversation.item.create",
                               "item": {"type": "message", "role": "user",
                                        "content": [{"type": "input_text",
                                                     "text": "full note"}]}}
    assert await rt.correct_item("rf_t_x", role="user", text="x") == "refused"


@in_a_loop
async def test_a_dead_socket_is_said_not_reported_done():
    rt = tracked()
    rt._reply_items["R1"] = ["i1"]
    rt._reply_done_seen["R1"] = time.time()
    rt.ws = None
    assert await rt.forget_reply("R1") == "closed"
    assert await rt.correct_item("rf_t_x", role="user", text="x") == "closed"


@in_a_loop
async def test_nudge_role_is_the_sessions():
    rt = bridge(GPT)
    rt.nudge_role = "system"
    assert await rt.retry_response(nudge="N") is True
    assert rt.ws.types() == ["response.cancel", "conversation.item.create",
                             "response.create"]
    assert creates(rt)[0]["item"] == {"type": "message", "role": "system",
                                      "content": [{"type": "input_text",
                                                   "text": "N"}]}
    rt = bridge(GPT)
    assert await rt.retry_response(nudge="N") is True
    assert creates(rt)[0]["item"]["role"] == "user"


@in_a_loop
async def test_the_events_task_is_forgotten_only_by_its_own_events():
    rt = tracked()
    c = Consumer(rt)
    await until(lambda: rt._events_task is c.task)
    other = asyncio.ensure_future(asyncio.sleep(10))
    rt._events_task = other              # a respawned pump took over
    await c.stop()
    assert rt._events_task is other
    other.cancel()


# --------------------------------------------------------------------------
# 2. GroupRoom: told notes, corrections, configuration, re-brief
# --------------------------------------------------------------------------

class Agent:
    def __init__(self, aid):
        self.id, self.name = aid, aid.title()


CAST = ("dan", "priya", "chris")


def gpt_room(make=tracked):
    room = gr.GroupRoom([Agent(a) for a in CAST], instructions_for=lambda a: "",
                        voice_for=lambda a: "", model=GPT)
    for a in CAST:
        room.sessions[a] = make()
    return room


@in_a_loop
async def test_tell_supplies_ids_only_to_tracking_members_and_only_for_heard(monkeypatch):
    room = gpt_room()
    await room.tell("Dan", "We slipped the date.", exclude="dan", response_id="R1")
    told = room._told[("dan", "R1")]
    assert set(told) == {"priya", "chris"}
    for aid in ("priya", "chris"):
        (f,) = creates(room.sessions[aid])
        assert f["item"]["id"] == told[aid]["item_id"] and "event_id" in f
        assert f["item"]["content"][0]["text"] == gr.GroupRoom._told_note(
            "Dan", "We slipped the date.")
        assert room._fanned_since_grant[aid] > 0
    assert creates(room.sessions["dan"]) == []

    plain = {"type": "message", "role": "user", "content": [
        {"type": "input_text", "text": gr.GroupRoom._told_note("Dan", "Hi.")}]}
    for setup in ("no_response_id", "generated", "untracked"):
        room = gpt_room(make=(lambda: bridge(GPT)) if setup == "untracked" else tracked)
        if setup == "generated":
            monkeypatch.setenv("ROOM_TOLD_TEXT", "generated")
        await room.tell("Dan", "Hi.", exclude="dan",
                        **({} if setup == "no_response_id" else {"response_id": "R1"}))
        monkeypatch.delenv("ROOM_TOLD_TEXT", raising=False)
        for aid in ("priya", "chris"):
            (f,) = creates(room.sessions[aid])
            assert f == {"type": "conversation.item.create", "item": plain}, setup
            assert room._fanned_since_grant[aid] > 0
        assert not room._told


@in_a_loop
async def test_correct_told_rewrites_each_colleague():
    room = gpt_room()
    cons = {a: Consumer(room.sessions[a]) for a in CAST}
    try:
        for a in ("priya", "chris"):
            room.sessions[a].ws.feed([committed(f"p_{a}", None)])
        await until(lambda: all(f"p_{a}" in room.sessions[a]._conv
                                for a in ("priya", "chris")))
        await room.tell("Dan", "We slipped the date by a week.",
                        exclude="dan", response_id="R1")
        ids = {a: room._told[("dan", "R1")][a]["item_id"] for a in ("priya", "chris")}
        for a in ("priya", "chris"):
            room.sessions[a].ws.feed([added(ids[a], f"p_{a}", role="user")])
        done = await room.correct_told("Dan", "dan", "R1", "We slipped…")
        assert done == ["chris", "priya"]
        for a in ("priya", "chris"):
            rt = room.sessions[a]
            assert deletes(rt) == [ids[a]]
            rt.ws.feed([deleted(ids[a])])
            await cons[a].wait("memory_op")
            ins = creates(rt)[-1]
            assert ins["previous_item_id"] == f"p_{a}"
            assert ins["item"]["content"][0]["text"] == gr.GroupRoom._told_note(
                "Dan", "We slipped…")
        # Corrected once: a second call sends nothing.
        assert await room.correct_told("Dan", "dan", "R1", "x") == []
    finally:
        for c in cons.values():
            await c.stop()

    # Nothing heard: deletes only. A member whose socket has gone is skipped.
    room = gpt_room()
    await room.tell("Dan", "Line.", exclude="dan", response_id="R2")
    room.sessions["chris"].ws = None
    assert await room.correct_told("Dan", "dan", "R2", "") == ["priya"]
    assert len(creates(room.sessions["priya"])) == 1          # the tell only
    assert deletes(room.sessions["priya"]) == [
        room._told[("dan", "R2")]["priya"]["item_id"]]


def _patched_factory(monkeypatch):
    class Session(R.RealtimeVoiceSession):
        async def connect(self, **kw):
            self.ws = WireWS()
    monkeypatch.setattr(gr, "RealtimeVoiceSession",
                        lambda **kw: Session(api_key="dummy", **kw))


@in_a_loop
async def test_open_configures_gpt_members_only(monkeypatch):
    _patched_factory(monkeypatch)
    room = gr.GroupRoom([Agent(a) for a in CAST], instructions_for=lambda a: "",
                        voice_for=lambda a: "", model=GPT)
    await room.open()
    for rt in room.sessions.values():
        assert rt.track_items is True and rt.nudge_role == "system"
    assert room.scribe.track_items is False and room.scribe.nudge_role == "user"

    room = gr.GroupRoom([Agent(a) for a in CAST], instructions_for=lambda a: "",
                        voice_for=lambda a: "", model=NATIVE)
    await room.open()
    for rt in room.sessions.values():
        assert rt.track_items is False and rt.nudge_role == "user"

    for k, v in zip(KNOBS, ("keep", "generated", "user")):
        monkeypatch.setenv(k, v)
    room = gr.GroupRoom([Agent(a) for a in CAST], instructions_for=lambda a: "",
                        voice_for=lambda a: "", model=GPT)
    await room.open()
    for rt in room.sessions.values():
        assert rt.track_items is False and rt.nudge_role == "user"
    # Either memory knob alone keeps the mirror on.
    monkeypatch.setenv("ROOM_TOLD_TEXT", "heard")
    room = gr.GroupRoom([Agent(a) for a in CAST], instructions_for=lambda a: "",
                        voice_for=lambda a: "", model=GPT)
    await room.open()
    assert all(rt.track_items for rt in room.sessions.values())


@in_a_loop
async def test_rebrief_names_the_replies_it_cancelled():
    room = gpt_room()
    room.sessions["dan"]._response_created_id = "R1"
    await room.rebrief(lambda a: "brief")
    assert room.last_rebrief_cancelled == {"dan": "R1"}
    assert "response.cancel" in room.sessions["dan"].ws.types()
    await room.rebrief(lambda a: "brief")
    assert room.last_rebrief_cancelled == {}


# --------------------------------------------------------------------------
# 3. The runner: what was heard, and what each character keeps
# --------------------------------------------------------------------------

LINE = ("We slipped the date because the vendor missed two deliveries and "
        "I did not want to flag it until I had a new plan ready")
assert len(LINE.split()) == 25


def line_reply(rid, items=("i1", "i2"), seconds=6.0):
    """Dan's line: two items, the words split between them, `seconds` of
    page audio (16 kHz once resampled) relayed at once, as the gateway's
    ~5x delivery does."""
    words = LINE.split()
    half = len(words) // 2
    parts = [" ".join(words[:half]), " ".join(words[half:])]
    per = int(seconds / 0.2 / len(items))
    frames = [created(rid)]
    prev = "p1"
    for iid, text in zip(items, parts):
        frames += [item_added(rid, iid), added(iid, prev)]
        frames += [adelta(rid, iid) for _ in range(per)]
        frames.append(tdelta(rid, iid, text if iid == items[0] else " " + text))
        frames += [audio_done(rid, iid), tdone(rid, iid, text)]
        prev = iid
    return frames


class RoomHarness:
    """A real runner over a real GroupRoom of three tracked gpt bridges, each
    with its member pump running."""

    def __init__(self, *, make=tracked, page=()):
        self.session = FakeSession("S4A")
        self.page = PageWS(page)
        self.runner = rvs.RealtimeVoiceSessionRunner(self.session, self.page)
        self.room = gpt_room(make)
        self.runner.room = self.room
        self.agents = {a.id: a for a in self.runner._resolve_agents()}
        self.pumps = []

    def rt(self, aid):
        return self.room.sessions[aid]

    def start(self):
        for aid in CAST:
            self.pumps.append(asyncio.ensure_future(
                self.runner._pump_member(self.agents[aid], self.rt(aid))))

    async def stop(self):
        await settle(self.runner)
        for p in self.pumps:
            p.cancel()
        await asyncio.gather(*self.pumps, return_exceptions=True)

    def of(self, type_):
        return self.session.store.of(type_)

    async def dan_speaks(self, rid="R1", *, done=True):
        """Dan, holding the floor, relays his line; returns his state."""
        rt = self.rt("dan")
        self.room.speaking = "dan"
        await rt.request_response()
        rt.ws.feed([committed("p1", None)] + line_reply(rid))
        st = self.runner._member_states
        await until(lambda: "dan" in st and st["dan"].relayed_bytes >= 32000 * 6 - 6400)
        await until(lambda: any(f.get("type") == "assistant_text_final"
                                and f.get("items") == 2 for f in self.page.json))
        if done:
            rt.ws.feed([done_out(rid, ["i1", "i2"])])
            await until(lambda: self.of("assistant_turn"))
        return st["dan"]

    def playing_since(self, st, ago=2.0, total=6.0):
        now = time.time()
        st.play_start, st.play_end = now - ago, now - ago + total
        self.runner._play_cursor = st.play_end
        lp = self.runner._last_played
        lp["start"], lp["end"] = st.play_start, st.play_end

    async def barge_in(self):
        self.page._frames = [LOUD] * 25
        await self.runner._client_to_model()

    async def ack_deletes(self, aid, n):
        rt = self.rt(aid)
        await until(lambda: len(deletes(rt)) >= n)
        rt.ws.feed([deleted(i) for i in deletes(rt)[:n]])


def _assert_only_these_deleted(h, allowed):
    """N-21: no participant item and no other colleague's note is ever
    deleted, in any member's conversation."""
    for aid in CAST:
        for iid in deletes(h.rt(aid)):
            assert iid in allowed.get(aid, ()), (aid, iid)
            assert not iid.startswith("p"), "a participant item was deleted"


@in_a_loop
async def test_a_cut_on_the_floor_holder_keeps_only_the_heard_words():
    h = RoomHarness()
    h.start()
    try:
        st = await h.dan_speaks(done=False)
        h.playing_since(st, ago=2.0)
        await h.barge_in()
        dan = h.rt("dan")
        assert "response.cancel" in dan.ws.types()
        dan.ws.feed([done_out("R1", ["i1", "i2"], "cancelled")])
        await h.ack_deletes("dan", 2)
        await until(lambda: h.of("member_memory_replaced"))
    finally:
        await h.stop()
    (mem,) = h.of("member_memory_replaced")
    (turn,) = h.of("assistant_turn")
    assert 1700 <= mem["heard_ms"] <= 2300
    expected = turn_audio.heard_estimate(LINE, mem["heard_ms"], 170)["heard_text"]
    assert expected and expected != LINE and expected.endswith("…")
    # Dan's own conversation: the cancel, both items deleted, the heard words
    # put back after the participant item that came before the line.
    dan = h.rt("dan")
    assert deletes(dan) == ["i1", "i2"]
    (ins,) = creates(dan)
    assert ins["previous_item_id"] == "p1"
    assert ins["item"]["content"] == [{"type": "output_text", "text": expected}]
    # The colleagues are told what was heard, not the line.
    for aid in ("priya", "chris"):
        (note,) = creates(h.rt(aid))
        assert note["item"]["content"][0]["text"] == gr.GroupRoom._told_note(
            "Dan", expected)
    assert mem["reason"] == "cut_floor_holder" and mem["action"] == "replaced"
    assert mem["heard_text"] == expected and mem["told"] == "heard"
    assert mem["generated_text"] == LINE and mem["heard_basis"] == "play_clock_wpm"
    assert mem["wpm"] == 170 and mem["placement"] == "in_place"
    assert turn["memory"] == "replaced" and turn["told"] == "heard"
    assert turn["text"] == LINE and turn["memory_text"] == expected
    assert turn["told_text"] == expected
    assert turn["response_id"] == "R1" and turn["interrupted"] is True
    assert turn["memory_estimate"]["basis"] == "play_clock_wpm"
    # heard_text on the turn keeps its relayed-audio rule: the whole line was
    # relayed, so it is the upper bound it always was.
    assert turn["heard_estimate"]["basis"] == "relayed_audio"
    # The director's history keeps the line.
    assert {"speaker": "dan", "text": LINE} in h.session.shared_history
    _assert_only_these_deleted(h, {"dan": ("i1", "i2")})
    assert not h.of("voice_error")


async def _told_in_full_then_cut(h):
    st = await h.dan_speaks()
    (turn,) = h.of("assistant_turn")
    assert turn["memory"] == "whole" and turn["told"] == "generated"
    told = h.room._told.get(("dan", "R1")) or {}
    for aid in ("priya", "chris"):
        rt = h.rt(aid)
        rt.ws.feed([committed(f"p_{aid}", None)])
        if aid in told:
            rt.ws.feed([added(told[aid]["item_id"], f"p_{aid}", role="user")])
    await until(lambda: all(h.rt(a).ws.q.empty() for a in ("priya", "chris")))
    if h.rt("priya").track_items:
        await until(lambda: all(f"p_{a}" in h.rt(a)._conv
                                for a in ("priya", "chris")))
    h.room.speaking = None
    h.playing_since(st, ago=2.0)
    await h.barge_in()
    return told


@in_a_loop
async def test_a_cut_while_still_playing_corrects_the_told_line():
    h = RoomHarness()
    h.start()
    try:
        told = await _told_in_full_then_cut(h)
        await h.ack_deletes("dan", 2)
        for aid in ("priya", "chris"):
            await h.ack_deletes(aid, 1)
        await until(lambda: len(h.of("told_line_corrected")) == 2
                    and h.of("member_memory_replaced"))
    finally:
        await h.stop()
    (cut,) = h.of("playback_cut")
    assert cut["agent_id"] == "dan" and cut["total_seconds"] == 6.0
    heard_text, _, _ = rvs._heard_share(LINE, cut["heard_seconds"], 6.0)
    assert cut["heard_text"].split()[:3] == LINE.split()[:3]
    (mem,) = h.of("member_memory_replaced")
    assert mem["reason"] == "cut_still_playing" and mem["action"] == "replaced"
    assert mem["told_corrected"] == ["chris", "priya"]
    assert mem["heard_basis"] == "play_clock_share"
    assert mem["heard_text"] == rvs._heard_share(LINE, mem["heard_ms"] / 1000, 6.0)[0]
    dan = h.rt("dan")
    assert deletes(dan) == ["i1", "i2"]
    (ins,) = creates(dan)
    assert ins["previous_item_id"] == "p1"
    assert ins["item"]["content"][0]["text"] == mem["heard_text"]
    for aid in ("priya", "chris"):
        rt = h.rt(aid)
        assert deletes(rt) == [told[aid]["item_id"]]
        full, fixed = creates(rt)
        assert full["item"]["content"][0]["text"] == gr.GroupRoom._told_note("Dan", LINE)
        assert fixed["previous_item_id"] == f"p_{aid}"
        assert fixed["item"]["content"][0]["text"] == gr.GroupRoom._told_note(
            "Dan", mem["heard_text"])
    assert sorted(e["agent_id"] for e in h.of("told_line_corrected")) == [
        "chris", "priya"]
    assert all(e["speaker_id"] == "dan" and e["action"] == "corrected"
               for e in h.of("told_line_corrected"))
    _assert_only_these_deleted(h, {"dan": ("i1", "i2"),
                                   "priya": (told["priya"]["item_id"],),
                                   "chris": (told["chris"]["item_id"],)})


@in_a_loop
async def test_told_generated_tells_and_keeps_the_full_text(monkeypatch):
    monkeypatch.setenv("ROOM_TOLD_TEXT", "generated")
    h = RoomHarness()
    h.start()
    try:
        st = await h.dan_speaks(done=False)
        h.playing_since(st)
        await h.barge_in()
        h.rt("dan").ws.feed([done_out("R1", ["i1", "i2"], "cancelled")])
        await h.ack_deletes("dan", 2)
        await until(lambda: h.of("member_memory_replaced"))
    finally:
        await h.stop()
    for aid in ("priya", "chris"):
        (note,) = creates(h.rt(aid))
        assert note == {"type": "conversation.item.create", "item": {
            "type": "message", "role": "user", "content": [
                {"type": "input_text",
                 "text": gr.GroupRoom._told_note("Dan", LINE)}]}}
    (turn,) = h.of("assistant_turn")
    assert turn["told"] == "generated" and turn["memory"] == "replaced"

    h = RoomHarness()
    h.start()
    try:
        await _told_in_full_then_cut(h)
        await h.ack_deletes("dan", 2)
        await until(lambda: h.of("member_memory_replaced"))
    finally:
        await h.stop()
    assert not h.of("told_line_corrected")
    for aid in ("priya", "chris"):
        assert deletes(h.rt(aid)) == []
    assert h.of("member_memory_replaced")[0]["told_corrected"] == []


@in_a_loop
async def test_cut_memory_keep_sends_no_item_operation(monkeypatch):
    monkeypatch.setenv("ROOM_CUT_MEMORY", "keep")
    h = RoomHarness()
    h.start()
    try:
        st = await h.dan_speaks(done=False)
        h.playing_since(st)
        await h.barge_in()
        h.rt("dan").ws.feed([done_out("R1", ["i1", "i2"], "cancelled")])
        await until(lambda: h.of("assistant_turn"))
        await asyncio.sleep(0.1)
    finally:
        await h.stop()
    dan = h.rt("dan")
    assert deletes(dan) == [] and creates(dan) == []
    (turn,) = h.of("assistant_turn")
    assert turn["memory"] == "whole" and turn["told"] == "heard"
    assert "memory_text" not in turn and turn["told_text"].endswith("…")
    assert turn["memory_estimate"]["basis"] == "play_clock_wpm"
    note = creates(h.rt("priya"))[0]["item"]["content"][0]["text"]
    assert LINE not in note and "…" in note
    assert not h.of("member_memory_replaced")

    h = RoomHarness()
    h.start()
    try:
        await _told_in_full_then_cut(h)
        for aid in ("priya", "chris"):
            await h.ack_deletes(aid, 1)
        await until(lambda: len(h.of("told_line_corrected")) == 2)
    finally:
        await h.stop()
    assert deletes(h.rt("dan")) == [] and len(creates(h.rt("dan"))) == 0


@in_a_loop
async def test_a_cut_before_any_audio_reached_the_page_deletes_the_reply():
    """Review 3: the floor holder's reply was named and nothing of it had
    been relayed (grant to first audio reached 5.09 s on gpt): no finalize
    reaches _finalize_member_inner, so the barge-in deletes it itself."""
    h = RoomHarness()
    h.start()
    try:
        h.room.speaking = "dan"
        dan = h.rt("dan")
        await dan.request_response()
        dan.ws.feed([committed("p1", None), created("R1"), item_added("R1", "i1"),
                     added("i1", "p1")])
        await until(lambda: dan._response_created_id == "R1")
        await h.barge_in()
        dan.ws.feed([adelta("R1", "i1"), done_out("R1", ["i1"], "cancelled")])
        await h.ack_deletes("dan", 1)
        await until(lambda: h.of("member_memory_replaced"))
    finally:
        await h.stop()
    (mem,) = h.of("member_memory_replaced")
    assert mem["reason"] == "cut_floor_holder" and mem["why"] == "not_relayed"
    assert mem["action"] == "deleted" and mem["heard_ms"] == 0
    assert creates(h.rt("dan")) == []
    assert h.of("empty_response") and not h.of("assistant_turn")


@in_a_loop
async def test_a_cut_in_the_window_after_the_done_reaches_the_finalize_with_the_text():
    """Review 4 (1b): the floor holder's response.done has passed and its
    finalize is still waiting for the transcript when the participant cuts
    in. The bridge has forgotten the reply, so the barge-in's own finalize
    names it from the playback clock, and whichever finalize takes the text
    applies the cut; the colleagues are told the heard words with the reply's
    name."""
    h = RoomHarness()
    h.start()
    try:
        h.room.speaking = "dan"
        dan = h.rt("dan")
        await dan.request_response()
        frames = [f for f in line_reply("R1")
                  if f["type"] not in ("response.output_audio_transcript.done",
                                       "response.output_audio_transcript.delta")]
        dan.ws.feed([committed("p1", None)] + frames + [done_out("R1", ["i1", "i2"])])
        st = h.runner._member_states
        await until(lambda: "dan" in st and st["dan"].relayed_bytes >= 32000 * 6 - 6400)
        await until(lambda: dan._response_created_id is None)
        await asyncio.sleep(0.05)
        h.playing_since(st["dan"], ago=2.0)
        await h.barge_in()
        dan.ws.feed([tdone("R1", "i1", LINE)])
        await h.ack_deletes("dan", 2)
        await until(lambda: h.of("member_memory_replaced"))
    finally:
        await h.stop()
    (mem,) = h.of("member_memory_replaced")
    assert mem["reason"] == "cut_still_playing" and mem["action"] == "replaced"
    turns = [t for t in h.of("assistant_turn") if t["text"]]
    assert len(turns) == 1 and turns[0]["response_id"] == "R1"
    assert turns[0]["memory"] == "replaced"
    for aid in ("priya", "chris"):
        (note,) = creates(h.rt(aid))
        assert note["item"]["content"][0]["text"] == gr.GroupRoom._told_note(
            "Dan", mem["heard_text"])
        assert note["item"]["id"].startswith("rf_t_")


@in_a_loop
async def test_a_cut_during_the_tell_is_corrected_once_the_tell_is_out():
    """A line cut while its finalize is still telling it: the correction is
    queued on the line and sent when the tell returns, never before it."""
    h = RoomHarness()
    key = ("dan", "R1")
    h.runner._lines[key] = {"text": LINE, "told": "generated",
                            "memory": "whole", "agent_name": "Dan",
                            "telling": True}
    await h.runner._memory_after_cut(
        {"agent_id": "dan", "response_id": "R1", "heard_s": 2.0,
         "total_s": 6.0}, reason="cut_still_playing")
    line = h.runner._lines[key]
    assert line["correct_after_tell"] == rvs._heard_share(LINE, 2.0, 6.0)[0]
    assert line["told"] == "heard"
    for aid in ("priya", "chris"):
        assert deletes(h.rt(aid)) == []


@in_a_loop
async def test_a_cut_of_a_line_written_without_text_keeps_nothing_back():
    h = RoomHarness()
    agent = h.agents["dan"]
    h.room.speaking = "dan"
    await h.runner._finalize_member(agent, "", response_id="R1")
    assert h.runner._lines[("dan", "R1")]["text"] == ""
    await h.runner._memory_after_cut(
        {"agent_id": "dan", "response_id": "R1", "heard_s": 2.0,
         "total_s": 6.0}, reason="cut_still_playing")
    (skip,) = h.of("member_memory_skipped")
    assert skip["why"] == "no_text"
    assert not h.rt("dan")._memory_ops, "a reservation was left for nothing"


async def _suppressed_hold(h, aid="priya", rid="R0", *, finish=True):
    """`aid` replies while Dan holds the floor: the pump suppresses it."""
    h.room.speaking = "dan"
    rt = h.rt(aid)
    rt.ws.feed([committed(f"p_{aid}", None), created(rid), item_added(rid, "h1"),
                added("h1", f"p_{aid}"), tdelta(rid, "h1", "Well I think"),
                adelta(rid, "h1"), adelta(rid, "h1")])
    st = h.runner._member_states
    await until(lambda: aid in st and st[aid].mode == "holding")
    if finish:
        rt.ws.feed([done_out(rid, ["h1"], "cancelled")])
        await until(lambda: st[aid].mode == "held_done")
    return st[aid]


@pytest.mark.parametrize("how", ["stale", "expired", "refused_holding",
                                 "refused_held_done", "participant_speaking",
                                 "participant_speaking_holding", "superseded"])
@in_a_loop
async def test_a_hold_never_played_is_deleted(how, monkeypatch):
    h = RoomHarness()
    h.start()
    runner = h.runner
    try:
        st = await _suppressed_hold(h, finish=how not in (
            "refused_holding", "participant_speaking_holding"))
        if how == "stale":
            runner._speech_started_at = time.time()
            assert await runner.adopt_member("priya") is False
        elif how == "expired":
            monkeypatch.setenv("HELD_REPLY_TTL", "0")
            monkeypatch.setenv("ROOM_ADOPT_GUARD", "0")
            await asyncio.sleep(0.01)
            assert await runner.adopt_member("priya") is False
        elif how.startswith("refused"):
            assert st.cut_by_suppression
            assert await runner.adopt_member("priya") is False
            if how == "refused_holding":
                assert deletes(h.rt("priya")) == [], "deleted before its done"
                h.rt("priya").ws.feed([done_out("R0", ["h1"], "cancelled")])
        elif how.startswith("participant_speaking"):
            await runner._cancel_stale_holds()
            if how.endswith("holding"):
                h.rt("priya").ws.feed([done_out("R0", ["h1"], "cancelled")])
        elif how == "superseded":
            h.rt("priya").ws.feed([created("R5"), tdelta("R5", "h5", "Also")])
        await h.ack_deletes("priya", 1)
        await until(lambda: h.of("member_memory_replaced"))
    finally:
        await h.stop()
    (mem,) = h.of("member_memory_replaced")
    why = {"refused_holding": "refused", "refused_held_done": "refused",
           "participant_speaking_holding": "participant_speaking"}.get(how, how)
    assert mem["agent_id"] == "priya" and mem["response_id"] == "R0"
    assert mem["reason"] == "hold_dropped" and mem["why"] == why
    assert mem["action"] == "deleted" and mem["items_deleted"] == ["h1"]
    assert mem["generated_text"] == "Well I think"
    assert creates(h.rt("priya")) == []
    _assert_only_these_deleted(h, {"priya": ("h1",)})


@in_a_loop
async def test_an_adopted_hold_is_never_deleted(monkeypatch):
    monkeypatch.setenv("ROOM_ADOPT_GUARD", "0")
    h = RoomHarness()
    h.start()
    try:
        await _suppressed_hold(h)
        h.room.speaking = "priya"
        assert await h.runner.adopt_member("priya") is True
        await until(lambda: h.of("assistant_turn"))
        assert "R0" in h.runner._played_rids
        # The floor reaching a hold mid-reply: the pump splices it in.
        await _suppressed_hold(h, aid="chris", rid="R7", finish=False)
        h.room.speaking = "chris"
        h.rt("chris").ws.feed([adelta("R7", "h1"), done_out("R7", ["h1"])])
        await until(lambda: len(h.of("assistant_turn")) == 2)
        assert "R7" in h.runner._played_rids
        await asyncio.sleep(0.1)
    finally:
        await h.stop()
    assert deletes(h.rt("priya")) == [] and deletes(h.rt("chris")) == []
    assert not h.of("member_memory_replaced")


@in_a_loop
async def test_a_stale_hold_spliced_in_after_all_keeps_its_items():
    """Review 7: the stale drop leaves a streaming hold to the pump, which
    splices it in when the floor reaches it; the deletion waiting for its
    done is withdrawn the moment it plays."""
    h = RoomHarness()
    h.start()
    try:
        await _suppressed_hold(h, finish=False)
        h.runner._speech_started_at = time.time()
        h.room.speaking = "priya"                   # _grant sets it first
        assert await h.runner.adopt_member("priya") is False
        assert h.rt("priya")._memory_ops, "no deletion was requested"
        h.rt("priya").ws.feed([adelta("R0", "h1"), done_out("R0", ["h1"])])
        await until(lambda: h.of("assistant_turn"))
        await asyncio.sleep(0.1)
    finally:
        await h.stop()
    assert deletes(h.rt("priya")) == []
    (skip,) = [e for e in h.of("member_memory_skipped")
               if e["why"] == "played_after_request"]
    assert skip["response_id"] == "R0" and skip["reason"] == "hold_dropped"


@in_a_loop
async def test_a_suppressed_reply_the_floor_reaches_only_at_its_end_is_deleted():
    """Review 6 (a): a grant that does not come through adopt_member (the
    silence probe's give_floor) reaching a suppressed reply at its done:
    nothing of it is relayed, nor kept."""
    h = RoomHarness()
    h.start()
    try:
        await _suppressed_hold(h, finish=False)
        h.room.speaking = "priya"
        h.rt("priya").ws.feed([done_out("R0", ["h1"], "cancelled")])
        await h.ack_deletes("priya", 1)
        await until(lambda: h.of("member_memory_replaced"))
    finally:
        await h.stop()
    (mem,) = h.of("member_memory_replaced")
    assert mem["why"] == "floor_at_end" and mem["action"] == "deleted"


@in_a_loop
async def test_a_reply_cut_before_it_was_named_is_deleted_when_it_is():
    """Review 6 (d): a cut in a retry's window marks the next reply for
    discard; it is never relayed and never a hold, so the bridge names it."""
    h = RoomHarness()
    h.start()
    try:
        h.room.speaking = "dan"
        dan = h.rt("dan")
        await dan.retry_response(nudge="N")
        await dan.cancel_response()
        assert dan._discard_next_created
        dan.ws.feed([created("R3"), item_added("R3", "x1"), adelta("R3", "x1"),
                     done_out("R3", ["x1"], "cancelled")])
        await h.ack_deletes("dan", 1)
        await until(lambda: h.of("member_memory_replaced"))
    finally:
        await h.stop()
    (mem,) = h.of("member_memory_replaced")
    assert mem["response_id"] == "R3" and mem["why"] == "cut_before_named"
    assert mem["action"] == "deleted"


@in_a_loop
async def test_a_line_blanked_with_no_audio_is_deleted():
    """Review 6 (c): a deferral nobody heard is blanked from the record; it
    is deleted from the character's conversation too."""
    h = RoomHarness()
    h.start()
    try:
        h.room.speaking = "dan"
        dan = h.rt("dan")
        await dan.request_response()
        dan.ws.feed([committed("p1", None), created("R4"), item_added("R4", "d1"),
                     added("d1", "p1"),
                     tdelta("R4", "d1", "I'll wait for Priya to answer."),
                     tdone("R4", "d1", "I'll wait for Priya to answer."),
                     done_out("R4", ["d1"])])
        await h.ack_deletes("dan", 1)
        await until(lambda: h.of("member_memory_replaced"))
    finally:
        await h.stop()
    (mem,) = h.of("member_memory_replaced")
    assert mem["reason"] == "unvoiced" and mem["why"] == "deferral"
    (turn,) = h.of("assistant_turn")
    assert turn["text"] == "" and turn["memory"] == "whole"


@in_a_loop
async def test_a_rebrief_cancel_is_deleted_unless_it_was_played():
    for played in (False, True):
        h = RoomHarness()
        runner = h.runner
        runner.director = h.session.director

        async def nothing():
            return None
        runner._advance_when_spent = nothing
        dan = h.rt("dan")
        dan._response_created_id = "R1"
        dan._created_ids.add("R1")
        if played:
            runner._played_rids["R1"] = time.time()
        runner.segment = 1
        lead = runner._resolve_agents()[0]
        assert await runner._enter(lead, new_interaction=True)
        assert h.of("group_room_kept")
        if not played:
            assert dan._memory_ops.get("R1", {}).get("state") == "pending"
            dan.ws.feed([done_out("R1", ["i1"], "cancelled")])
            await h.ack_deletes("dan", 1)
            await until(lambda: h.of("member_memory_replaced"))
            (mem,) = h.of("member_memory_replaced")
            assert mem["reason"] == "rebrief_cancel" and mem["action"] == "deleted"
        else:
            (skip,) = h.of("member_memory_skipped")
            assert skip["why"] == "played" and skip["reason"] == "rebrief_cancel"
            assert deletes(dan) == [] and not dan._memory_ops
        for t in list(runner._pumps):
            t.cancel()
        await asyncio.gather(*runner._pumps, return_exceptions=True)


@in_a_loop
async def test_room_nudges_are_system_and_one_to_one_nudges_are_user(monkeypatch):
    h = RoomHarness()
    runner = h.runner
    runner._awaiting_participant = False
    h.room.speaking = "dan"
    ev = {"type": "reply_missing", "retryable": True, "waited_s": 6}
    assert await runner._reply_missing(h.rt("dan"), "dan", ev, has_floor=True)
    (nudge,) = creates(h.rt("dan"))
    assert nudge["item"]["role"] == "system"
    assert nudge["item"]["content"][0]["text"] == R.UNANSWERED_NUDGE
    (retry,) = h.of("reply_retry")
    assert retry["nudge_role"] == "system"

    from test_bridge_correctness import one_to_one
    runner, session, ws, rt = one_to_one()
    runner._awaiting_participant = False
    assert await runner._reply_missing(rt, runner.agent_id, ev)
    (nudge,) = creates(rt)
    assert nudge["item"]["role"] == "user"
    assert "nudge_role" not in session.store.of("reply_retry")[0]

    monkeypatch.setenv("ROOM_NUDGE_ROLE", "user")
    room = gr.GroupRoom([Agent("dan")], instructions_for=lambda a: "",
                        voice_for=lambda a: "", model=GPT)
    rt = bridge(GPT)
    room._configure_memory(rt)
    assert rt.nudge_role == "user"


@in_a_loop
async def test_a_gateway_error_on_delete_is_recorded_and_the_room_goes_on():
    h = RoomHarness()
    h.start()
    try:
        st = await h.dan_speaks(done=False)
        h.playing_since(st)
        await h.barge_in()
        dan = h.rt("dan")
        dan.ws.feed([done_out("R1", ["i1", "i2"], "cancelled")])
        await until(lambda: len(deletes(dan)) == 2)
        bad = of(dan, "conversation.item.delete")[0]["event_id"]
        dan.ws.feed([err("item_not_found", "gone", event_id=bad),
                     deleted("i2")])
        await until(lambda: h.of("member_memory_error")
                    and h.of("member_memory_replaced"))
        assert h.runner._response_done.is_set(), "the floor was not released"
        # The pump lives on: Dan's next reply is relayed and written.
        h.room.speaking = "dan"
        await dan.request_response()
        dan.ws.feed([created("R2"), adelta("R2", "j1"),
                     tdelta("R2", "j1", "Anyway."), tdone("R2", "j1", "Anyway."),
                     done_out("R2", ["j1"])])
        await until(lambda: any(t["text"] == "Anyway." for t in h.of("assistant_turn")))
    finally:
        await h.stop()
    (e,) = h.of("member_memory_error")
    assert e["op"] == "delete" and e["item_id"] == "i1" and e["code"] == "item_not_found"
    (mem,) = h.of("member_memory_replaced")
    assert mem["action"] == "deleted" and mem["insert_skipped"] == "delete_refused"
    assert creates(dan) == [], "the heard words went in beside the line"
    assert not h.of("voice_error")
    assert not h.page.frames("error") and not h.page.frames("voice_error")


@in_a_loop
async def test_knobs_at_todays_values_send_todays_frames(monkeypatch):
    for k, v in zip(KNOBS, ("keep", "generated", "user")):
        monkeypatch.setenv(k, v)
    _patched_factory(monkeypatch)

    async def harness():
        h = RoomHarness()
        room = gr.GroupRoom([Agent(a) for a in CAST],
                            instructions_for=lambda a: "", voice_for=lambda a: "",
                            model=GPT)
        await room.open()
        h.room = room
        h.runner.room = room
        assert not any(rt.track_items for rt in room.sessions.values())
        return h

    def todays(h):
        for aid in CAST:
            rt = h.rt(aid)
            for m in rt.ws.sent:
                assert m.get("type") not in ("conversation.item.delete",
                                             "conversation.item.truncate")
                assert "event_id" not in m
                if m.get("type") == "conversation.item.create":
                    assert "id" not in m["item"] and "previous_item_id" not in m
                    assert m["item"]["role"] == "user"

    # N-19's cut on the floor holder.
    h = await harness()
    h.start()
    try:
        st = await h.dan_speaks(done=False)
        h.playing_since(st)
        n = len(h.rt("dan").ws.sent)
        await h.barge_in()
        h.rt("dan").ws.feed([done_out("R1", ["i1", "i2"], "cancelled")])
        await until(lambda: h.of("assistant_turn"))
        await asyncio.sleep(0.1)
    finally:
        await h.stop()
    todays(h)
    assert [m["type"] for m in h.rt("dan").ws.sent[n:]
            if m["type"] != "input_audio_buffer.append"] == ["response.cancel"]
    (turn,) = h.of("assistant_turn")
    assert (turn["memory"], turn["told"], turn["response_id"]) == (
        "whole", "generated", "R1")
    for aid in ("priya", "chris"):
        (note,) = creates(h.rt(aid))
        assert note["item"]["content"][0]["text"] == gr.GroupRoom._told_note("Dan", LINE)

    # N-20's cut while still playing.
    h = await harness()
    h.start()
    try:
        await _told_in_full_then_cut(h)
        await asyncio.sleep(0.1)
    finally:
        await h.stop()
    todays(h)
    assert not h.of("member_memory_replaced") and not h.of("told_line_corrected")

    # N-24's stale hold.
    h = await harness()
    h.start()
    try:
        await _suppressed_hold(h)
        h.runner._speech_started_at = time.time()
        assert await h.runner.adopt_member("priya") is False
        await asyncio.sleep(0.05)
    finally:
        await h.stop()
    todays(h)

    # N-27's nudge.
    h = await harness()
    h.runner._awaiting_participant = False
    h.room.speaking = "dan"
    await h.runner._reply_missing(h.rt("dan"), "dan",
                                  {"retryable": True, "waited_s": 6}, has_floor=True)
    todays(h)
    assert creates(h.rt("dan"))[0]["item"]["role"] == "user"


@in_a_loop
async def test_the_native_route_is_untouched(monkeypatch):
    _patched_factory(monkeypatch)
    room = gr.GroupRoom([Agent(a) for a in CAST], instructions_for=lambda a: "",
                        voice_for=lambda a: "", model=NATIVE)
    await room.open()
    assert not any(rt.track_items for rt in room.sessions.values())
    assert all(rt.nudge_role == "user" for rt in room.sessions.values())
    h = RoomHarness()
    h.room = room
    h.runner.room = room
    assert h.runner._memory_policy(room.sessions["dan"]) == ("keep", "generated")
    agent = h.agents["dan"]
    room.speaking = "dan"
    await h.runner._finalize_member(agent, "Fine.", audio_bytes=64000,
                                    response_id="R1")
    (turn,) = h.of("assistant_turn")
    assert (turn["memory"], turn["told"], turn["response_id"]) == (
        "whole", "generated", "R1")
    assert "memory_text" not in turn
    for aid in ("priya", "chris"):
        for m in room.sessions[aid].ws.sent:
            assert "event_id" not in m and "id" not in m.get("item", {})


# --------------------------------------------------------------------------
# 4. On the record
# --------------------------------------------------------------------------

def test_room_memory_is_on_the_record(monkeypatch, tmp_path):
    prov = llm.provenance(GPT)
    assert prov["room_memory"] == {
        "cut_memory": "replace", "told_text": "heard", "nudge_role": "system",
        "active": True,
        "heard_basis": {"cut_floor_holder": "play_clock_wpm",
                        "cut_still_playing": "play_clock_share"},
        "settle_s": R.MEMORY_SETTLE_S, "op_ttl_s": R.MEMORY_OP_TTL_S}
    assert llm.provenance(NATIVE)["room_memory"]["active"] is False
    for k, v in zip(KNOBS, ("keep", "generated", "user")):
        monkeypatch.setenv(k, v)
    rm = llm.provenance(GPT)["room_memory"]
    assert (rm["cut_memory"], rm["told_text"], rm["nudge_role"]) == (
        "keep", "generated", "user")
    for k in KNOBS:
        monkeypatch.setenv(k, "nonsense")
    rm = llm.provenance(GPT)["room_memory"]
    assert (rm["cut_memory"], rm["told_text"], rm["nudge_role"]) == (
        "replace", "heard", "system")
    monkeypatch.delenv("ROOM_CUT_MEMORY")
    monkeypatch.delenv("ROOM_TOLD_TEXT")
    monkeypatch.delenv("ROOM_NUDGE_ROLE")

    sdir = tmp_path / "s_1_ab"
    sdir.mkdir()
    prov = llm.provenance(GPT)
    (sdir / "events.jsonl").write_text(
        json.dumps({"t": 0.0, "type": "session_start"}) + "\n"
        + json.dumps({"t": 0.1, "type": "realtime_session_started",
                      "model": GPT, **prov}) + "\n", encoding="utf-8")
    assert encounter_record.build(sdir)["provenance"]["room_memory"] == prov["room_memory"]
    assert "room_memory" in L.PIPELINE_KEYS
    assert L.pipeline_provenance(prov, {})["room_memory"] == prov["room_memory"]


def test_the_version_and_its_history_line():
    assert llm.PIPELINE_VERSION == "2026-10-01a"
    src = (ROOT / "server" / "llm.py").read_text(encoding="utf-8")
    assert src.count("#   2026-10-01a") == 1
    # A memory change is not a change to when the next character speaks.
    assert llm.ROOM_PACING_VERSION == "2026-09-29b"
