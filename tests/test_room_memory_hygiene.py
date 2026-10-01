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
import functools
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
    bridge, created, item_added, settle, tdelta, tdone,
    until,
)


def in_a_loop(fn):
    """test_bridge_correctness's, with a ceiling: a regression that leaves an
    await hanging (an insert sent before its deletes are acked, say) fails
    its test in 60 s instead of stalling the whole file."""
    @functools.wraps(fn)
    def wrapper(*a, **kw):
        async def bounded():
            return await asyncio.wait_for(fn(*a, **kw), 60)
        return asyncio.run(bounded())
    return wrapper

KNOBS = ("ROOM_CUT_MEMORY", "ROOM_TOLD_TEXT", "ROOM_NUDGE_ROLE")
# What 10-01a adds to a room's assistant_turn; absent where it does not run.
NEW_TURN_FIELDS = {"response_id", "memory", "told", "memory_text", "told_text",
                   "memory_estimate"}


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

    async def wait(self, type_, n=1, timeout=5.0):
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
            # Sent once more (a refusal can be transient), and refused again.
            await until(lambda: len(deletes(rt)) == 2)
            again = of(rt, "conversation.item.delete")[-1]
            assert again["item_id"] == "i1" and again["event_id"] != eid
            rt.ws.feed([err("item_not_found",
                            "Item with id 'i1' not found.",
                            event_id=again["event_id"] if by_id else None,
                            param="item_id")])
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
        assert e["code"] == "item_not_found" and e["recovery"] == "delete_resent"
        assert c.of("memory_op_error")[1]["recovery"] is None
        assert active is True, "the streaming reply was ended"
        assert op["action"] == "refused" and op["insert_skipped"] == "delete_refused"
        assert creates(rt) == [], "the heard words were put in beside the line"
        assert rt.memory_errors == 2

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
    # The measured frame: no id of ours (the mirror learns the gateway's).
    assert "id" not in again["item"] and again["event_id"].startswith("rfm_")
    assert e["op"] == "insert" and e["recovery"] == "inserted_at_end"
    assert e["placement"] == "end_after_error"
    assert e["retry_item_id"] in rt._conv


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


def told_frame(text):
    """28b's told-note frame, byte for byte: no id, no event_id."""
    return {"type": "conversation.item.create",
            "item": {"type": "message", "role": "user",
                     "content": [{"type": "input_text", "text": text}]}}


@in_a_loop
async def test_correct_item_rewrites_a_told_line_in_place():
    """A told note goes out as 28b's frame, with no id (it needs none; client ids are accepted, measured 2026-10-01);
    the gateway's id for it is learned from its conversation.item.added, and
    a correction deletes THAT item and puts the heard version where it was.
    Asked for before the gateway has named the note, it waits for the name."""
    rt = tracked()
    c = Consumer(rt)
    try:
        assert await rt.inject_text("full note", item_id="rf_t_x") == "rf_t_x"
        assert creates(rt)[-1] == told_frame("full note")
        rt.ws.feed([committed("p1", None)])
        await until(lambda: "p1" in rt._conv)
        assert await rt.correct_item("rf_t_x", role="user",
                                     text="heard note") == "deferred"
        assert deletes(rt) == [], "a delete went out under a name the gateway never saw"
        rt.ws.feed([added("item_t1", "p1", role="user", text="full note")])
        await until(lambda: deletes(rt) == ["item_t1"])
        assert rt._conv == ["p1", "item_t1"]
        rt.ws.feed([deleted("item_t1")])
        (op,) = await c.wait("memory_op")
        ins = creates(rt)[-1]
        assert ins["previous_item_id"] == "p1"
        assert ins["item"]["role"] == "user"
        assert ins["item"]["content"] == [{"type": "input_text",
                                           "text": "heard note"}]
        assert op["kind"] == "told_correction" and op["action"] == "corrected"
        assert op["deferred"] is True
        assert rt._conv == ["p1", ins["item"]["id"]]

        # Named already, nothing heard: the note is only deleted, at once.
        await rt.inject_text("other note", item_id="rf_t_y")
        rt.ws.feed([added("item_t2", ins["item"]["id"], role="user",
                          text="other note")])
        await until(lambda: rt._gid("rf_t_y") == "item_t2")
        n = len(creates(rt))
        assert await rt.correct_item("rf_t_y", role="user", text=None) == "sent"
        assert deletes(rt)[-1] == "item_t2"
        rt.ws.feed([deleted("item_t2")])
        await c.wait("memory_op", 2)
        assert len(creates(rt)) == n
        assert c.of("memory_op")[1]["action"] == "deleted"
    finally:
        await c.stop()


@in_a_loop
async def test_an_insert_the_gateway_renames_is_aliased():
    """Defensive (the gateway echoed our id on 2026-10-01): should the gateway put its own id on an item sent
    under ours, the item is matched by role and text, and a later delete of
    it names the gateway's id."""
    rt = tracked()
    c = Consumer(rt)
    try:
        iid = await rt.insert_item("assistant", "Heard words")
        assert creates(rt)[-1]["item"]["id"] == iid
        rt.ws.feed([added("item_srv1", None, text="Heard words")])
        await until(lambda: rt._id_alias.get(iid) == "item_srv1")
        assert "item_srv1" in rt._conv and iid not in rt._conv
        assert await rt.correct_item(iid, role="assistant", text="x") == "sent"
    finally:
        await c.stop()
    assert deletes(rt) == ["item_srv1"]


@in_a_loop
async def test_a_refused_insert_id_falls_back_to_the_measured_frame():
    """Defensive the other way: a gateway that refuses an id of ours
    gets the frame measured on 2026-09-30 instead (create with
    previous_item_id and no id), the mirror learns the gateway's id from its
    conversation.item.added, and no later insert on that socket names one."""
    rt = tracked()
    await rt.request_response()
    c = Consumer(rt)
    try:
        rt.ws.feed([committed("p1", None)]
                   + chain(reply_frames("R1", ["i1"]), "p1")
                   + [done_out("R1", ["i1"])])
        await c.wait("response_done")
        assert await rt.forget_reply("R1", keep_text="Heard") == "sent"
        rt.ws.feed([deleted("i1")])
        await c.wait("memory_op")
        (ins,) = creates(rt)
        assert ins["item"]["id"].startswith("rf_a_")
        rt.ws.feed([err("invalid_value", "Invalid 'item.id': not allowed.",
                        param="item.id")])
        (e,) = await c.wait("memory_op_error")
        _first, again = creates(rt)
        assert again["previous_item_id"] == "p1" and "id" not in again["item"]
        assert again["item"]["content"] == ins["item"]["content"]
        assert e["op"] == "insert" and e["recovery"] == "inserted_without_id"
        assert e["placement"] == "in_place"
        rt.ws.feed([added("item_g1", "p1", text="Heard")])
        await until(lambda: "item_g1" in rt._conv)
        assert rt._conv == ["p1", "item_g1"]
        later = await rt.insert_item("assistant", "More")
        assert "id" not in creates(rt)[-1]["item"] and later in rt._conv
    finally:
        await c.stop()
    assert not c.of("error")


@in_a_loop
async def test_a_dead_socket_is_said_not_reported_done():
    rt = tracked()
    rt._reply_items["R1"] = ["i1"]
    rt._reply_done_seen["R1"] = time.time()
    assert await rt.inject_text("note", item_id="rf_t_x") == "rf_t_x"
    rt.ws = None
    assert await rt.forget_reply("R1") == "closed"
    assert await rt.correct_item("rf_t_x", role="user", text="x") == "closed"
    assert await rt.correct_item("rf_t_never", role="user", text="x") == "unknown"
    assert await rt.inject_text("note", item_id="rf_t_y") is None


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
async def test_told_notes_are_todays_frames_and_remembered_only_where_tracked(
        monkeypatch):
    """Every told note is 28b's frame, to every member and under every knob:
    a told note is on every room turn, and 28b's frame needs no client id.
    Only a tracking member's note of a line with a reply id, under "heard",
    is remembered (by the bridge's name for it) for a later correction."""
    room = gpt_room()
    await room.tell("Dan", "We slipped the date.", exclude="dan", response_id="R1")
    told = room._told[("dan", "R1")]
    assert set(told) == {"priya", "chris"}
    for aid in ("priya", "chris"):
        (f,) = creates(room.sessions[aid])
        assert f == told_frame(gr.GroupRoom._told_note("Dan", "We slipped the date."))
        assert told[aid]["item_id"] in room.sessions[aid]._conv
        assert room._fanned_since_grant[aid] > 0
    assert creates(room.sessions["dan"]) == []

    for setup in ("no_response_id", "generated", "untracked"):
        room = gpt_room(make=(lambda: bridge(GPT)) if setup == "untracked" else tracked)
        if setup == "generated":
            monkeypatch.setenv("ROOM_TOLD_TEXT", "generated")
        await room.tell("Dan", "Hi.", exclude="dan",
                        **({} if setup == "no_response_id" else {"response_id": "R1"}))
        monkeypatch.delenv("ROOM_TOLD_TEXT", raising=False)
        for aid in ("priya", "chris"):
            (f,) = creates(room.sessions[aid])
            assert f == told_frame(gr.GroupRoom._told_note("Dan", "Hi.")), setup
            assert room._fanned_since_grant[aid] > 0
        assert not room._told

    # A note that never left (the member's socket is gone) is not
    # remembered: there is nothing in that conversation to correct.
    room = gpt_room()
    room.sessions["chris"].ws = None
    await room.tell("Dan", "Hi.", exclude="dan", response_id="R1")
    assert set(room._told[("dan", "R1")]) == {"priya"}


@in_a_loop
async def test_correct_told_rewrites_only_that_line_in_each_colleague():
    """Rule 3: in each colleague's conversation only the told note of the one
    line corrected is touched: not the same speaker's other line, not another
    colleague's line, not the participant's items; and the note put back is
    a user input_text item, where the old one was."""
    room = gpt_room()
    cons = {a: Consumer(room.sessions[a]) for a in CAST}
    try:
        for a in CAST:
            room.sessions[a].ws.feed([committed(f"p_{a}", None)])
        await until(lambda: all(f"p_{a}" in room.sessions[a]._conv for a in CAST))
        lines = [("Dan", "dan", "R1", "Our first line here."),
                 ("Dan", "dan", "R2", "We slipped the date by a week."),
                 ("Chris", "chris", "C1", "I can take the vendor call.")]
        for k, (name, sid, rid, text) in enumerate(lines):
            await room.tell(name, text, exclude=sid, response_id=rid)
            for a in CAST:
                if a != sid:
                    room.sessions[a].ws.feed([added(
                        f"g{k}_{a}", None, role="user",
                        text=gr.GroupRoom._told_note(name, text))])
        await until(lambda: all(not room.sessions[a]._pending_creates for a in CAST))

        def gid(a, sid, rid):
            return room.sessions[a]._gid(room._told[(sid, rid)][a]["item_id"])

        states = await room.correct_told("Dan", "dan", "R2", "We slipped…")
        assert states == {"chris": "sent", "priya": "sent"}
        for a in ("priya", "chris"):
            rt = room.sessions[a]
            assert deletes(rt) == [gid(a, "dan", "R2")]
            rt.ws.feed([deleted(gid(a, "dan", "R2"))])
            await cons[a].wait("memory_op")
            ins = creates(rt)[-1]
            assert ins["item"]["role"] == "user"
            assert ins["item"]["content"] == [{
                "type": "input_text",
                "text": gr.GroupRoom._told_note("Dan", "We slipped…")}]
            assert ins["previous_item_id"] == gid(a, "dan", "R1")
        fixed = creates(room.sessions["priya"])[-1]["item"]["id"]
        assert room.sessions["priya"]._conv == [
            "p_priya", gid("priya", "dan", "R1"), fixed, gid("priya", "chris", "C1")]
        assert deletes(room.sessions["dan"]) == []
        # Corrected once: a second call sends nothing.
        assert await room.correct_told("Dan", "dan", "R2", "x") == {
            "chris": "already", "priya": "already"}
        assert sum(len(deletes(room.sessions[a])) for a in CAST) == 2
    finally:
        for c in cons.values():
            await c.stop()

    # Nothing heard: deletes only. A member whose socket has gone, or who has
    # left the room, is said so, never a failure of the turn.
    room = gpt_room()
    cons = {a: Consumer(room.sessions[a]) for a in ("priya", "chris")}
    try:
        await room.tell("Dan", "Line.", exclude="dan", response_id="R2")
        room.sessions["priya"].ws.feed([added(
            "gp", None, role="user", text=gr.GroupRoom._told_note("Dan", "Line."))])
        await until(lambda: gid("priya", "dan", "R2") == "gp")
        room.sessions["chris"].ws = None
        assert await room.correct_told("Dan", "dan", "R2", "") == {
            "chris": "closed", "priya": "sent"}
        assert len(creates(room.sessions["priya"])) == 1          # the tell only
        assert deletes(room.sessions["priya"]) == ["gp"]
        room.sessions.pop("chris")
        assert (await room.correct_told("Dan", "dan", "R2", ""))["chris"] == "gone"
    finally:
        for c in cons.values():
            await c.stop()


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
        await until(lambda: len(deletes(rt)) >= n, 5.0)
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
    (pc,) = h.of("playback_cut")
    # Rule 4 on the playback clock: what the character keeps is read at the
    # instant the cut's playback_cut is, so the two agree to its rounding,
    # however long the barge-in took to detect (never a wall-clock bound).
    assert pc["agent_id"] == "dan" and pc["total_seconds"] == 6.0
    assert abs(mem["heard_ms"] - 1000 * pc["heard_seconds"]) <= 50, (mem, pc)
    assert mem["heard_ms"] >= 2000
    assert turn["memory_estimate"]["heard_ms"] == mem["heard_ms"]
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


async def _told_in_full_then_cut(h, total=6.0):
    st = await h.dan_speaks()
    (turn,) = h.of("assistant_turn")
    if h.rt("dan").track_items:
        assert turn["memory"] == "whole" and turn["told"] == "generated"
    else:
        assert not NEW_TURN_FIELDS & set(turn)
    if h.rt("priya").track_items and R.room_told_text() == "heard":
        # The finalize tells after it writes the turn: wait for the tell.
        await until(lambda: set(h.room._told.get(("dan", "R1")) or {})
                    >= {"priya", "chris"}, 5.0)
    told = h.room._told.get(("dan", "R1")) or {}
    for aid in ("priya", "chris"):
        rt = h.rt(aid)
        # The participant's item, then the told note as the gateway adds it:
        # under an id of its own, with the note's text (it was sent with none).
        rt.ws.feed([committed(f"p_{aid}", None),
                    added(f"n_{aid}", f"p_{aid}", role="user",
                          text=gr.GroupRoom._told_note("Dan", LINE))])
    await until(lambda: all(h.rt(a).ws.q.empty() for a in ("priya", "chris")))
    if h.rt("priya").track_items:
        await until(lambda: all(f"p_{a}" in h.rt(a)._conv
                                for a in ("priya", "chris")))
        await until(lambda: all(h.rt(a)._gid(told[a]["item_id"]) == f"n_{a}"
                                for a in told))
    h.room.speaking = None
    h.playing_since(st, ago=2.0, total=total)
    await h.barge_in()
    return told


@in_a_loop
async def test_a_cut_while_still_playing_corrects_the_told_line():
    h = RoomHarness()
    h.start()
    try:
        told = await _told_in_full_then_cut(h)
        assert set(told) == {"priya", "chris"}
        await h.ack_deletes("dan", 2)
        for aid in ("priya", "chris"):
            await h.ack_deletes(aid, 1)
        await until(lambda: len(h.of("told_line_corrected")) == 2
                    and h.of("member_memory_replaced"))
    finally:
        await h.stop()
    (cut,) = h.of("playback_cut")
    assert cut["agent_id"] == "dan" and cut["total_seconds"] == 6.0
    (mem,) = h.of("member_memory_replaced")
    assert mem["reason"] == "cut_still_playing" and mem["action"] == "replaced"
    assert mem["told_corrected"] == ["chris", "priya"]
    assert mem["heard_basis"] == "play_clock_share"
    # Rule 4: exactly the playback_cut's heard_text, the same formula on the
    # same unrounded reading, and the full line beside it.
    assert mem["heard_text"] == cut["heard_text"]
    assert mem["heard_text"].split()[:3] == LINE.split()[:3]
    assert mem["generated_text"] == LINE
    dan = h.rt("dan")
    assert deletes(dan) == ["i1", "i2"]
    (ins,) = creates(dan)
    assert ins["previous_item_id"] == "p1"
    assert ins["item"]["content"][0]["text"] == mem["heard_text"]
    for aid in ("priya", "chris"):
        rt = h.rt(aid)
        assert deletes(rt) == [f"n_{aid}"]
        full, fixed = creates(rt)
        assert full == told_frame(gr.GroupRoom._told_note("Dan", LINE))
        assert fixed["previous_item_id"] == f"p_{aid}"
        assert fixed["item"]["role"] == "user"
        assert fixed["item"]["content"] == [{
            "type": "input_text",
            "text": gr.GroupRoom._told_note("Dan", mem["heard_text"])}]
    assert sorted(e["agent_id"] for e in h.of("told_line_corrected")) == [
        "chris", "priya"]
    assert all(e["speaker_id"] == "dan" and e["action"] == "corrected"
               for e in h.of("told_line_corrected"))
    _assert_only_these_deleted(h, {"dan": ("i1", "i2"),
                                   "priya": ("n_priya",),
                                   "chris": ("n_chris",)})


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
    (mem,) = h.of("member_memory_replaced")
    assert mem["told_corrected"] == [] and mem["told"] == "generated"


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
async def test_a_cut_in_the_window_after_the_done_reaches_the_finalize_with_the_text(
        monkeypatch):
    """Review 4 (1b): the floor holder's response.done has passed and its
    finalize is still waiting for the transcript when the participant cuts
    in. The bridge has forgotten the reply, so the barge-in's own finalize
    names it from the playback clock, and whichever finalize takes the text
    applies the cut; the colleagues are told the heard words with the reply's
    name.

    The transcript grace is long here so the pump's finalize is certainly
    still waiting when the barge-in lands (it raced 0.2 s before)."""
    monkeypatch.setenv("TRANSCRIPT_GRACE_SECONDS", "6")
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
        assert note == told_frame(gr.GroupRoom._told_note("Dan", mem["heard_text"]))
    # Told with the reply's name: remembered for a later correction.
    assert set(h.room._told[("dan", "R1")]) == {"priya", "chris"}


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
            await until(lambda: h.rt("priya")._memory_ops.get("R0"))
            # R5, suppressed in turn, is open: the delete waits for its done
            # (the busy rule: no item op during an active response).
            await asyncio.sleep(0.05)
            assert deletes(h.rt("priya")) == []
            h.rt("priya").ws.feed([done_out("R5", ["h5"], "cancelled")])
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
    assert turn["text"] == "" and turn["memory"] == "deleted"
    assert turn["response_id"] == "R4" and turn["told"] == "none"


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
        await until(lambda: len(deletes(dan)) == 3)
        again = of(dan, "conversation.item.delete")[-1]
        assert again["item_id"] == "i1"
        dan.ws.feed([err("item_not_found", "gone", event_id=again["event_id"])])
        await until(lambda: len(h.of("member_memory_error")) == 2
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
    e1, e2 = h.of("member_memory_error")
    assert e1["op"] == "delete" and e1["item_id"] == "i1"
    assert e1["code"] == "item_not_found" and e1["recovery"] == "delete_resent"
    assert e2["item_id"] == "i1" and e2["recovery"] is None
    (mem,) = h.of("member_memory_replaced")
    # The line's first item survived two refusals: it already carries the
    # start of the line, so the heard words are not put in beside it.
    assert mem["action"] == "deleted" and mem["partial"] is True
    assert mem["items_deleted"] == ["i2"] and mem["items_refused"] == ["i1"]
    assert mem["insert_skipped"] == "first_item_kept"
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
    # 28b's record too, not only its frames: none of the 10-01a fields.
    assert not NEW_TURN_FIELDS & set(turn), turn
    for aid in ("priya", "chris"):
        (note,) = creates(h.rt(aid))
        assert note == told_frame(gr.GroupRoom._told_note("Dan", LINE))
    assert not [e for e in h.session.store.events
                if e["type"].startswith(("member_memory", "told_line"))]

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
    (retry,) = h.of("reply_retry")
    assert "nudge_role" not in retry


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
    assert not NEW_TURN_FIELDS & set(turn), turn
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
        "active": True, "effective": True,
        "heard_basis": {"cut_floor_holder": "play_clock_wpm",
                        "cut_still_playing": "play_clock_share",
                        "cut_queued": "play_clock_share"},
        "settle_s": R.MEMORY_SETTLE_S, "op_ttl_s": R.MEMORY_OP_TTL_S}
    native = llm.provenance(NATIVE)["room_memory"]
    assert native["active"] is False and native["effective"] is False
    for k, v in zip(KNOBS, ("keep", "generated", "user")):
        monkeypatch.setenv(k, v)
    rm = llm.provenance(GPT)["room_memory"]
    assert (rm["cut_memory"], rm["told_text"], rm["nudge_role"]) == (
        "keep", "generated", "user")
    # Nothing of 10-01a runs: the flag to select encounters by says so.
    assert rm["active"] is True and rm["effective"] is False
    monkeypatch.setenv("ROOM_NUDGE_ROLE", "system")
    assert llm.provenance(GPT)["room_memory"]["effective"] is True
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
    # ROOM_PACING_VERSION is deliberately not pinned here: whether the
    # ordering wait (up to REALTIME_MEMORY_SETTLE_S before a member's
    # reply-start) counts as a room pacing change is the researchers' call.
    assert llm.ROOM_PACING_VERSION >= "2026-09-29b"


# --------------------------------------------------------------------------
# 5. Review of the first cut: races, refusals, fallback frames, records
# --------------------------------------------------------------------------

class YieldingWS(WireWS):
    """A socket whose send yields, before the frame is written and after, as
    a real one can on a full buffer: other tasks run, and the events task
    reads frames, between two of our sends and inside one. With
    `ack_deletes` the gateway's ack of each delete is queued as the delete is
    written, before the send returns (the backpressured shape). An item
    create takes longer to go than anything else, so a reply-start let go
    before the heard words' create has gone out overtakes it."""

    def __init__(self, ack_deletes=False):
        super().__init__()
        # True: every delete; a tuple: only those item ids.
        self.ack_deletes = ack_deletes

    async def send(self, raw):
        m = json.loads(raw)
        await asyncio.sleep(0.06 if m.get("type") == "conversation.item.create"
                            else 0.01)
        self.sent.append(m)
        if (self.ack_deletes and m.get("type") == "conversation.item.delete"
                and (self.ack_deletes is True or m["item_id"] in self.ack_deletes)):
            self.feed([deleted(m["item_id"])])
        await asyncio.sleep(0.01)


def two_items_done(rid="R1"):
    return [committed("p1", None), created(rid),
            item_added(rid, "i1"), added("i1", "p1"),
            tdelta(rid, "i1", "One two."), adelta(rid, "i1"),
            item_added(rid, "i2"), added("i2", "i1"),
            tdelta(rid, "i2", "Three four."), adelta(rid, "i2"),
            done_out(rid, ["i1", "i2"],
                     texts={"i1": "One two.", "i2": "Three four."})]


def wire(rt, skip=("input_audio_buffer.append",)):
    return [(m["type"], m.get("item_id") or (m.get("item") or {}).get("id"))
            for m in rt.ws.sent if m["type"] not in skip]


@pytest.mark.parametrize("second", ["acked", "refused"])
@in_a_loop
async def test_an_ack_read_mid_send_never_completes_an_operation_early(second):
    """Every item is awaited before the first delete goes out: an ack read
    while the second delete is still being sent can neither put the heard
    words back nor let a waiting reply-start go before that delete is out
    and answered, and a refusal of it is the operation's (`items_refused`),
    never lost behind a record already written."""
    rt = tracked()
    rt.ws = YieldingWS(ack_deletes=True if second == "acked" else ("i1",))
    await rt.request_response()
    c = Consumer(rt)
    try:
        rt.ws.feed(two_items_done())
        await c.wait("response_done")

        async def grant():
            await asyncio.sleep(0.005)
            await rt.request_response()          # a reply-start, another task

        g = asyncio.ensure_future(grant())
        assert await rt.forget_reply("R1", keep_text="One") == "sent"
        if second == "refused":
            for k in (1, 2):                      # refused, resent, refused
                await until(lambda: deletes(rt).count("i2") == k)
                d2 = [m for m in of(rt, "conversation.item.delete")
                      if m["item_id"] == "i2"][-1]
                rt.ws.feed([err("invalid_request_error", "busy",
                                event_id=d2["event_id"], param="item_id")])
        await asyncio.wait_for(g, 3)
        (op,) = await c.wait("memory_op")
    finally:
        await c.stop()
    seq = wire(rt)
    d1 = seq.index(("conversation.item.delete", "i1"))
    d2 = seq.index(("conversation.item.delete", "i2"))
    ins = seq.index(("conversation.item.create", op["inserted_item_id"]))
    start = max(i for i, (t, _) in enumerate(seq) if t == "response.create")
    assert d1 < d2 < ins < start, seq
    assert op["action"] == "replaced"
    if second == "acked":
        assert op["items_deleted"] == ["i1", "i2"] and op["items_refused"] == []
    else:
        assert op["items_deleted"] == ["i1"] and op["items_refused"] == ["i2"]
        assert op["partial"] is True


@in_a_loop
async def test_partial_refusal_keeps_the_heard_words_unless_the_first_item_survives():
    for refused in ("i2", "i1"):
        rt = tracked()
        await rt.request_response()
        c = Consumer(rt)
        try:
            rt.ws.feed(two_items_done())
            await c.wait("response_done")
            await rt.forget_reply("R1", keep_text="One")
            ok = "i1" if refused == "i2" else "i2"
            bad = {m["item_id"]: m for m in of(rt, "conversation.item.delete")}[refused]
            rt.ws.feed([deleted(ok), err("invalid_request_error", "busy",
                                         event_id=bad["event_id"], param="item_id")])
            await until(lambda: len(deletes(rt)) == 3)
            rt.ws.feed([err("invalid_request_error", "busy",
                            event_id=of(rt, "conversation.item.delete")[-1]["event_id"],
                            param="item_id")])
            (op,) = await c.wait("memory_op")
        finally:
            await c.stop()
        assert op["partial"] is True and op["items_refused"] == [refused]
        if refused == "i2":
            # The heard words go back where the line began: kept out, the
            # character would remember only the unheard tail.
            (ins,) = creates(rt)
            assert op["action"] == "replaced" and ins["previous_item_id"] == "p1"
            assert rt._conv == ["p1", ins["item"]["id"], "i2"]
        else:
            # The first item already carries the start of the line.
            assert op["action"] == "deleted" and creates(rt) == []
            assert op["insert_skipped"] == "first_item_kept"
            assert rt._conv == ["p1", "i1"]


@pytest.mark.parametrize("resume", ["new_item", "deltas_only"])
@in_a_loop
async def test_a_resumed_reply_is_deleted_only_at_its_own_done(resume):
    """Rule 1 for a retry answered by the abandoned reply resuming under its
    own id: its first done no longer counts, read from either sign of the
    resume (a new output item of it, or the bridge re-binding its deltas),
    and every item it adds after the resume is deleted with the rest."""
    rt = tracked()
    await rt.request_response()
    c = Consumer(rt)
    try:
        rt.ws.feed([committed("p1", None), created("R1"),
                    item_added("R1", "i1"), added("i1", "p1"),
                    tdelta("R1", "i1", "Head."), adelta("R1", "i1"),
                    done_out("R1", ["i1"])])
        await c.wait("response_done")
        await rt.retry_response(nudge="again")
        if resume == "new_item":
            # Read before any of its output: the new item alone says it.
            rt.ws.feed([item_added("R1", "i2"), added("i2", "i1")])
            await until(lambda: "i2" in rt.reply_item_ids("R1"))
        else:
            rt.ws.feed([tdelta("R1", "i1", " Resumed"), adelta("R1", "i1")])
            await until(lambda: any(e.get("text") == " Resumed" for e in c.evs))
        assert not rt.reply_done_seen("R1")
        assert await rt.forget_reply("R1") == "deferred"
        rt.ws.feed([item_added("R1", "i3"), added("i3", "i2" if resume == "new_item"
                                                   else "i1"),
                    tdelta("R1", "i3", " tail.")])
        await until(lambda: "i3" in rt.reply_item_ids("R1"))
        assert deletes(rt) == []
        rt.ws.feed([done_out("R1", rt.reply_item_ids("R1"))])
        await until(lambda: deletes(rt) and len(deletes(rt)) == len(rt.reply_item_ids("R1")))
    finally:
        await c.stop()
    assert deletes(rt) == rt.reply_item_ids("R1") and "i3" in deletes(rt)


@in_a_loop
async def test_items_known_only_from_output_item_frames_are_deleted():
    """N-5 without the done's help: the cancelled reply's done names only its
    first item. The second is known from the response.output_item.added the
    cancelled-tail discard would have dropped, and a third, added after the
    forget was asked for and before the done (rule 1), is deleted too."""
    rt = tracked()
    await rt.request_response()
    c = Consumer(rt)
    try:
        rt.ws.feed([committed("p1", None), created("R1"), item_added("R1", "i1"),
                    added("i1", "p1"), tdelta("R1", "i1", "Okay,"),
                    adelta("R1", "i1")])
        await c.wait("agent_audio")
        await rt.cancel_response()                       # the barge-in
        rt.ws.feed([item_added("R1", "i2"), adelta("R1", "i2")])
        await until(lambda: rt.reply_item_ids("R1") == ["i1", "i2"])
        assert await rt.forget_reply("R1") == "deferred"
        rt.ws.feed([item_added("R1", "i3")])
        await until(lambda: "i3" in rt.reply_item_ids("R1"))
        assert deletes(rt) == []
        rt.ws.feed([done_out("R1", ["i1"], "cancelled")])
        await until(lambda: len(deletes(rt)) == 3)
        assert not c.of("agent_audio")[1:], "the tail was relayed"
    finally:
        await c.stop()
    assert deletes(rt) == ["i1", "i2", "i3"]


async def _a_pending_delete(rt, c):
    """R1 over, its delete out and unanswered; returns that delete."""
    rt.ws.feed([committed("p1", None)] + chain(reply_frames("R1", ["i1"]), "p1")
               + [done_out("R1", ["i1"])])
    await c.wait("response_done")
    assert await rt.forget_reply("R1") == "sent"
    return of(rt, "conversation.item.delete")[-1]


@pytest.mark.parametrize("shape", ["id_in_message", "param_only", "item_wording"])
@in_a_loop
async def test_an_error_without_our_event_id_is_still_recognised(shape):
    """Defensive (the gateway echoed event_id on 2026-10-01): a gateway may not echo event_id. Each fallback on
    its own: our id named in the message; an item parameter of ours; and an
    error that speaks of an item, with no frame but ours sent since."""
    rt = tracked()
    await rt.request_response()
    c = Consumer(rt)
    try:
        await _a_pending_delete(rt, c)
        if shape != "item_wording":
            # A plain item frame since ours: the last fallback is off, so the
            # one under test is the only one that can claim the error.
            await rt.inject_text("note")
        frame = {"id_in_message": err("x_code", "Nothing called 'i1' here."),
                 "param_only": err("x_code", "Not found.", param="item_id"),
                 "item_wording": err("conversation_item_missing",
                                     "That conversation item does not exist.")}[shape]
        rt.ws.feed([frame])
        (e,) = await c.wait("memory_op_error")
    finally:
        await c.stop()
    assert e["op"] == "delete" and e["item_id"] == "i1"
    assert not c.of("error")


@pytest.mark.parametrize("shape", ["tool_call", "stale_frame", "plain_frame_since",
                                   "reply_code"])
@in_a_loop
async def test_an_error_that_is_not_ours_takes_the_bridges_own_path(shape):
    rt = tracked()
    await rt.request_response()
    c = Consumer(rt)
    try:
        await _a_pending_delete(rt, c)
        if shape == "tool_call":
            frame = err("invalid_value", "No tool call found for item.",
                        param="item.call_id")
        elif shape == "stale_frame":
            for f in rt._memory_frames.values():
                f["at"] -= 11.0
            frame = err("invalid_value", "Bad.", param="item_id")
        elif shape == "plain_frame_since":
            # A told note (a plain item create) went out after the delete:
            # an item error with nothing of ours in it may be its.
            await rt.inject_text("note")
            frame = err("invalid_value", "Invalid item content.",
                        param="item.content")
        else:
            frame = err("conversation_already_has_active_response",
                        "An item is already being generated.")
        rt.ws.feed([frame])
        await c.wait("error")
    finally:
        await c.stop()
    assert not c.of("memory_op_error")
    assert rt._memory_frames, "the delete's own answer is still awaited"


@in_a_loop
async def test_the_anchor_is_the_nearest_live_item_before_the_reply():
    rt = tracked()
    await rt.request_response()
    c = Consumer(rt)
    try:
        rt.ws.feed([committed("p0", None), created("R0"), item_added("R0", "a0"),
                    added("a0", "p0"), done_out("R0", ["a0"]),
                    committed("p1", "a0")]
                   + chain(reply_frames("R1", ["i1", "i2"]), "p1")
                   + [done_out("R1", ["i1", "i2"]), committed("p2", "i2")])
        await until(lambda: rt._conv == ["p0", "a0", "p1", "i1", "i2", "p2"])
        assert await rt.forget_reply("R1", keep_text="Heard") == "sent"
        rt.ws.feed([deleted("i1"), deleted("i2")])
        await c.wait("memory_op")
    finally:
        await c.stop()
    (ins,) = creates(rt)
    assert ins["previous_item_id"] == "p1"
    assert rt._conv == ["p0", "a0", "p1", ins["item"]["id"], "p2"]


@pytest.mark.parametrize("first", ["note", "reply"])
@in_a_loop
async def test_a_reply_is_never_anchored_on_a_note_being_corrected_beside_it(first):
    """Chris's conversation: a told note N directly before his own cut reply,
    both put right at once. His replacement never anchors on N (being
    deleted), whichever goes first; in the order the deletes are sent, the
    two replacements land in the order of the items they replace."""
    rt = tracked()
    await rt.request_response()
    c = Consumer(rt)
    try:
        await rt.inject_text("the note", item_id="rf_t_n")
        rt.ws.feed([committed("p1", None),
                    added("n1", "p1", role="user", text="the note")]
                   + chain(reply_frames("R1", ["c1"]), "n1")
                   + [done_out("R1", ["c1"])])
        await until(lambda: rt._conv == ["p1", "n1", "c1"] and rt.reply_done_seen("R1"))
        calls = [rt.correct_item("rf_t_n", role="user", text="heard note"),
                 rt.forget_reply("R1", keep_text="chris heard")]
        if first == "reply":
            calls.reverse()
        for call in calls:
            assert await call == "sent"
        rt.ws.feed([deleted(i) for i in deletes(rt)])
        await c.wait("memory_op", 2)
    finally:
        await c.stop()
    by_text = {m["item"]["content"][0]["text"]: m for m in creates(rt)}
    note, line = by_text["heard note"], by_text["chris heard"]
    assert line["previous_item_id"] != "n1"
    # Whichever was asked first, the note's words go back first, where the
    # note was, and the reply's after them: the order the items had.
    assert note["previous_item_id"] == "p1"
    assert line["previous_item_id"] == note["item"]["id"]
    assert rt._conv == ["p1", note["item"]["id"], line["item"]["id"]]


@in_a_loop
async def test_a_reconnect_drops_every_operation_and_says_so():
    rt = tracked()
    await rt.request_response()
    c = Consumer(rt)
    rt.ws.feed([committed("p1", None)] + chain(reply_frames("R1", ["i1"]), "p1"))
    await c.wait("agent_transcript")
    await c.stop()
    assert rt.reserve_memory("R1") is True and rt._conv
    rt.api_key = ""                    # connect() resets, then stops short
    with pytest.raises(RuntimeError):
        await rt.connect()
    assert rt._conv == [] and not rt._memory_ops and rt._memory_idle.is_set()
    rt.ws = WireWS()
    c = Consumer(rt)
    try:
        (skip,) = await c.wait("memory_op_skipped")
        rt.ws.feed([done_out("R1", ["i1"], "cancelled")])
        await asyncio.sleep(0.05)
    finally:
        await c.stop()
    assert skip["why"] == "reconnected" and skip["response_id"] == "R1"
    assert deletes(rt) == []


@in_a_loop
async def test_a_replay_waits_for_a_pending_op():
    rt = tracked()
    await rt.request_response()
    c = Consumer(rt)
    try:
        rt.ws.feed([committed("p1", None)] + chain(reply_frames("R1", ["i1"]), "p1"))
        await c.wait("agent_transcript")
        await rt.cancel_response()
        assert await rt.forget_reply("R1") == "deferred"
        n = len(rt.ws.sent)
        rp = asyncio.ensure_future(rt.replay_input(b"\x01\x02" * 3200))
        await asyncio.sleep(0.05)
        assert rt.ws.types()[n:] == [], "the replay went out with a delete owed"
        rt.ws.feed([done_out("R1", ["i1"], "cancelled"), deleted("i1")])
        await asyncio.wait_for(rp, 2)
        await c.wait("memory_op_waited")
    finally:
        await c.stop()
    after = rt.ws.types()[n:]
    assert after[0] == "conversation.item.delete"
    assert after.index("conversation.item.delete") < after.index("input_audio_buffer.commit")


@in_a_loop
async def test_a_grant_waits_before_its_silence_pad_and_only_once(monkeypatch):
    monkeypatch.setattr(R, "room_grant_unanswered_s", lambda: 0.05)
    room = gpt_room()
    dan = room.sessions["dan"]
    await dan.request_response()
    c = Consumer(dan)
    try:
        dan.ws.feed([committed("p1", None)] + chain(reply_frames("R1", ["i1"]), "p1")
                    + [done_out("R1", ["i1"])])
        await c.wait("response_done")
        assert dan.reserve_memory("R1")
        n = len(dan.ws.sent)
        g = asyncio.ensure_future(room.give_floor("dan"))
        await asyncio.sleep(0.05)
        assert dan.ws.types()[n:] == [], "the pad went in with the decision owed"
        assert await dan.forget_reply("R1", keep_text="Ok") == "sent"
        dan.ws.feed([deleted("i1")])
        assert await asyncio.wait_for(g, 3) is dan
        await c.wait("memory_op_waited")
    finally:
        await c.stop()
    after = dan.ws.types()[n:]
    assert after[:3] == ["conversation.item.delete", "conversation.item.create",
                         "input_audio_buffer.append"], after
    assert after.index("input_audio_buffer.append") < after.index(
        "input_audio_buffer.commit")
    assert len(c.of("memory_op_waited")) == 1 and not c.of("memory_op_late")
    assert c.of("memory_op_waited")[0]["before"] == "commit"

    # Never decided: the grant goes after one settle time, written down once,
    # and its commit does not wait a second time.
    c = Consumer(dan)
    try:
        dan.ws.feed([created("R2"), tdelta("R2", "k1", "Hm"),
                     done_out("R2", ["k1"], "cancelled")])
        await until(lambda: dan.reply_done_seen("R2"))
        assert dan.reserve_memory("R2")
        assert await room.give_floor("dan") is dan
        await c.wait("memory_op_late")
        await asyncio.sleep(0.05)
    finally:
        await c.stop()
    # A second wait would be a second record.
    assert len(c.of("memory_op_late")) == 1
    assert c.of("memory_op_late")[0]["before"] == "commit"


# -- the runner ---------------------------------------------------------------

PRIYA_LINE = "Honestly the budget was never the real problem here at all."


async def told_and_named(h, speaker, rid, text, members=None):
    """Wait for `speaker`'s line `rid` to have been told (the finalize tells
    after it writes the turn), then have each colleague's gateway add the
    note under an id of its own, n_<rid>_<member>, and wait for the bridge
    to learn it."""
    members = set(members or (set(CAST) - {speaker}))
    await until(lambda: set(h.room._told.get((speaker, rid)) or {}) >= members, 5.0)
    told = h.room._told[(speaker, rid)]
    for aid in told:
        h.rt(aid).ws.feed([added(f"n_{rid}_{aid}", None, role="user",
                                 text=gr.GroupRoom._told_note(speaker.title(), text))])
    await until(lambda: all(h.rt(a)._gid(told[a]["item_id"]) == f"n_{rid}_{a}"
                            for a in told), 5.0)


@in_a_loop
async def test_a_finished_line_queued_behind_the_cut_line_is_forgotten():
    """Issue #48's shape: Dan's line is playing; Priya's whole line was
    generated, finalized and told in full while it played, and is queued on
    the page behind it; nobody holds the floor. The participant cuts in: the
    page drops Priya's queued audio, so nobody hears any of it. Her reply is
    deleted from her conversation and its told notes from her colleagues',
    and Dan's line keeps its heard words as before."""
    h = RoomHarness()
    h.start()
    try:
        dan_st = await h.dan_speaks()
        await told_and_named(h, "dan", "R1", LINE)
        # Long enough on the page that it is still playing however slowly
        # the steps below run.
        h.playing_since(dan_st, ago=2.0, total=20.0)
        pr = h.rt("priya")
        h.room.speaking = "priya"
        await pr.request_response()
        pr.ws.feed([committed("q1", None), created("R2"), item_added("R2", "j1"),
                    added("j1", "q1")]
                   + [adelta("R2", "j1") for _ in range(20)]
                   + [tdelta("R2", "j1", PRIYA_LINE), audio_done("R2", "j1"),
                      tdone("R2", "j1", PRIYA_LINE)])
        st = h.runner._member_states
        await until(lambda: "priya" in st and st["priya"].relayed_bytes >= 32000 * 4 - 6400)
        pr.ws.feed([done_out("R2", ["j1"])])
        await until(lambda: len(h.of("assistant_turn")) == 2)
        await told_and_named(h, "priya", "R2", PRIYA_LINE)
        assert h.runner._last_played["start"] > time.time()
        h.room.speaking = None
        await h.barge_in()
        await h.ack_deletes("priya", 2)
        await h.ack_deletes("dan", 3)
        await h.ack_deletes("chris", 2)
        await until(lambda: len(h.of("member_memory_replaced")) == 2
                    and len(h.of("told_line_corrected")) == 4, timeout=5.0)
    finally:
        await h.stop()
    assert [c["agent_id"] for c in h.of("playback_cut")] == ["dan"]
    mems = {m["agent_id"]: m for m in h.of("member_memory_replaced")}
    q = mems["priya"]
    assert q["reason"] == "cut_queued" and q["response_id"] == "R2"
    assert q["action"] == "deleted" and q["items_deleted"] == ["j1"]
    assert q["heard_text"] == "" and q["heard_ms"] == 0 and q["told"] == "none"
    assert q["told_corrected"] == ["chris", "dan"]
    assert q["generated_text"] == PRIYA_LINE
    assert mems["dan"]["reason"] == "cut_still_playing"
    assert mems["dan"]["action"] == "replaced"
    # Priya's told notes: deleted, nothing put back.
    for aid in ("dan", "chris"):
        assert f"n_R2_{aid}" in deletes(h.rt(aid))
        assert not [m for m in creates(h.rt(aid))
                    if PRIYA_LINE[:20] in m["item"]["content"][0]["text"]][1:]
    corr = [e for e in h.of("told_line_corrected") if e["speaker_id"] == "priya"]
    assert sorted(e["agent_id"] for e in corr) == ["chris", "dan"]
    assert all(e["action"] == "deleted" for e in corr)
    assert not [m for m in creates(h.rt("priya"))
                if m["item"].get("role") == "assistant"]
    _assert_only_these_deleted(h, {
        "dan": ("i1", "i2", "n_R2_dan"), "priya": ("j1", "n_R1_priya"),
        "chris": ("n_R1_chris", "n_R2_chris")})


@in_a_loop
async def test_the_next_grant_waits_for_a_floor_holders_cut_to_be_decided(monkeypatch):
    """Ordering rule 2 through the runner (review 2): Dan is cut on the floor,
    the cancelled done gives the floor back, and his next reply-start comes
    from another task BEFORE the turn's finalize has decided what he keeps.
    The barge-in's reservation holds it: on Dan's socket, the deletes, then
    the heard words, then the commit; and the wait is on the record. The
    socket's sends yield, as a real one's do."""
    monkeypatch.setattr(R, "MEMORY_SETTLE_S", 3.0)
    release = asyncio.Event()
    real_wait = rvs._await_transcript

    async def held(buf, grace, **kw):
        await release.wait()
        return await real_wait(buf, grace, **kw)

    monkeypatch.setattr(rvs, "_await_transcript", held)

    def make():
        rt = tracked()
        rt.ws = YieldingWS()
        return rt

    h = RoomHarness(make=make)
    h.start()
    dan = h.rt("dan")
    try:
        st = await h.dan_speaks(done=False)
        h.playing_since(st, ago=2.0)
        await h.barge_in()
        dan.ws.feed([done_out("R1", ["i1", "i2"], "cancelled")])
        await until(lambda: h.runner._response_done.is_set())
        n = len(dan.ws.sent)
        grant = asyncio.ensure_future(dan.commit_input())
        await asyncio.sleep(0.1)
        assert "input_audio_buffer.commit" not in dan.ws.types()[n:]
        release.set()
        await h.ack_deletes("dan", 2)
        await asyncio.wait_for(grant, 3)
        await until(lambda: h.of("member_memory_waited"))
    finally:
        release.set()
        await h.stop()
    seq = [m["type"] for m in dan.ws.sent[n:]
           if m["type"] != "input_audio_buffer.append"]
    assert seq[:4] == ["conversation.item.delete", "conversation.item.delete",
                       "conversation.item.create", "input_audio_buffer.commit"], seq
    (waited,) = h.of("member_memory_waited")
    assert waited["agent_id"] == "dan" and waited["before"] == "commit"
    assert not h.of("member_memory_late")


@in_a_loop
async def test_a_reply_start_that_cannot_wait_is_written_down_as_late():
    h = RoomHarness()
    h.start()
    dan = h.rt("dan")
    try:
        h.room.speaking = "dan"
        await dan.request_response()
        dan.ws.feed([committed("p1", None), created("R9"), tdelta("R9", "k1", "Hm")])
        await until(lambda: "R9" in dan._created_ids)
        assert dan.reserve_memory("R9")
        await dan.commit_input()
        await until(lambda: h.of("member_memory_late"))
    finally:
        await h.stop()
    (late,) = h.of("member_memory_late")
    assert late["agent_id"] == "dan" and late["before"] == "commit"
    assert late["pending"] == ["R9"] and late["own_task"] is False


def recording(h, aid):
    """A plain consumer of a member's events that writes its memory outcomes
    down as the member pump does (_record_memory_event)."""
    async def on(ev):
        h.runner._record_memory_event(aid, ev)
    return Consumer(h.rt(aid), on_event=on)


def _dan_in_the_window(h, *, text=LINE):
    """Dan's turn as the runner holds it in the window after his reply's
    response.done, before his pump has read it: announced, the whole line in
    the buffer, his audio on the playback clock. The bridge's own view (read
    by a plain consumer, not the pump) has the reply done and forgotten."""
    st = rvs._MemberState()
    st.line_rid, st.announced = "R1", True
    h.runner._member_states["dan"] = st
    settled = asyncio.Event()
    settled.set()
    h.runner._member_turns["dan"] = ([text], {
        "announced": True, "settled": settled, "audio_bytes": 6 * 32000})
    h.runner._last_played = {"agent_id": "dan", "response_id": "R1",
                             "start": 0.0, "end": 0.0, "text": ""}
    h.playing_since(st, ago=2.0)
    return st


@in_a_loop
async def test_the_barge_in_finalize_names_the_reply_from_the_playback_clock():
    """Review 4 (1b), the finalize that has the text being the barge-in's
    own: the bridge has forgotten the reply (cut_rid is None), so the turn,
    the cut and the reservation are named from the playback clock."""
    h = RoomHarness()
    dan = h.rt("dan")
    c = recording(h, "dan")
    try:
        await dan.request_response()
        dan.ws.feed([committed("p1", None)] + line_reply("R1")
                    + [done_out("R1", ["i1", "i2"])])
        await until(lambda: dan.reply_done_seen("R1"))
        assert dan._response_created_id is None
        h.room.speaking = "dan"
        _dan_in_the_window(h)
        await h.barge_in()
        await h.ack_deletes("dan", 2)
        await until(lambda: h.of("member_memory_replaced"))
    finally:
        await settle(h.runner)
        await c.stop()
    (turn,) = h.of("assistant_turn")
    assert turn["response_id"] == "R1" and turn["memory"] == "replaced"
    (mem,) = h.of("member_memory_replaced")
    assert mem["reason"] == "cut_floor_holder" and mem["response_id"] == "R1"
    assert set(h.room._told[("dan", "R1")]) == {"priya", "chris"}
    for aid in ("priya", "chris"):
        (note,) = creates(h.rt(aid))
        assert note == told_frame(gr.GroupRoom._told_note("Dan", mem["heard_text"]))


@in_a_loop
async def test_an_empty_finalize_leaves_the_cut_to_the_one_with_the_text():
    """Two finalizes on one buffer (the pump's and the barge-in's): the one
    that finds no text neither consumes the cut nor gives up its
    reservation; the one with the text applies it."""
    h = RoomHarness()
    dan = h.rt("dan")
    c = recording(h, "dan")
    agent = h.agents["dan"]
    try:
        await dan.request_response()
        dan.ws.feed([committed("p1", None)] + line_reply("R1")
                    + [done_out("R1", ["i1", "i2"])])
        await until(lambda: dan.reply_done_seen("R1"))
        h.room.speaking = "dan"
        await h.runner._memory_after_cut(
            {"agent_id": "dan", "response_id": "R1", "heard_s": 2.0,
             "total_s": 6.0}, reason="cut_still_playing")
        assert dan._memory_ops["R1"]["state"] == "reserved"
        await h.runner._finalize_member(agent, "", response_id="R1",
                                        audio_bytes=6 * 32000, interrupted=True)
        assert ("dan", "R1") in h.runner._line_cuts
        assert dan._memory_ops["R1"]["state"] == "reserved"
        assert not h.of("member_memory_skipped")
        h.room.speaking = "dan"
        await h.runner._finalize_member(agent, LINE, response_id="R1",
                                        audio_bytes=6 * 32000, interrupted=True)
        await h.ack_deletes("dan", 2)
        await until(lambda: h.of("member_memory_replaced"))
    finally:
        await settle(h.runner)
        await c.stop()
    turn = [t for t in h.of("assistant_turn") if t["text"]][0]
    assert turn["memory"] == "replaced" and turn["response_id"] == "R1"
    assert turn["memory_text"] == rvs._heard_share(LINE, 2.0, 6.0)[0]


@in_a_loop
async def test_a_cut_no_finalize_takes_lets_its_reservation_go(monkeypatch):
    """A line written without text, by every finalize it has: the cut and
    its reservation are let go within the grace and a margin, said so, and
    the member's next reply-start does not wait for them."""
    monkeypatch.setattr(rvs, "_CUT_EXPIRY_MARGIN_S", 0.1)
    h = RoomHarness()
    dan = h.rt("dan")
    c = Consumer(dan)
    try:
        await dan.request_response()
        dan.ws.feed([committed("p1", None)] + line_reply("R1")
                    + [done_out("R1", ["i1", "i2"])])
        await until(lambda: dan.reply_done_seen("R1"))
        await h.runner._memory_after_cut(
            {"agent_id": "dan", "response_id": "R1", "heard_s": 1.0,
             "total_s": 4.0}, reason="cut_still_playing")
        h.runner._memory_plan(h.agents["dan"], "", "R1", False)
        assert dan._memory_ops["R1"]["state"] == "reserved"
        await until(lambda: not dan._memory_ops, timeout=2.0)
    finally:
        await c.stop()
    assert ("dan", "R1") not in h.runner._line_cuts
    (skip,) = h.of("member_memory_skipped")
    assert skip["why"] == "no_text" and skip["reason"] == "cut_still_playing"
    assert deletes(dan) == []


@pytest.mark.parametrize("broken", ["forget_reply", "reserve_memory",
                                    "track_items", "correct_told"])
@in_a_loop
async def test_a_memory_failure_never_costs_the_turn_the_pump_or_the_room(broken):
    """"A dead member must not kill the turn": whatever raises inside the
    memory machinery, the turn is written, the floor comes back, the pump
    lives on to relay the next reply, and nothing reaches the page."""
    h = RoomHarness()

    def boom(*a, **kw):
        raise RuntimeError("broken on purpose")

    async def aboom(*a, **kw):
        raise RuntimeError("broken on purpose")

    for aid in CAST:
        rt = h.rt(aid)
        if broken == "forget_reply":
            rt.forget_reply = aboom
        elif broken == "reserve_memory":
            rt.reserve_memory = boom
        elif broken == "track_items":
            rt._track_items = boom
    if broken == "correct_told":
        h.room.correct_told = aboom
    h.start()
    dan = h.rt("dan")
    try:
        if broken == "correct_told":
            await _told_in_full_then_cut(h)
            # The colleagues' corrections failed; Dan's own line is still
            # put right, and the failure is written down.
            await h.ack_deletes("dan", 2)
            await until(lambda: h.of("member_memory_replaced"))
            assert [e for e in h.of("member_memory_skipped")
                    if e["reason"] == "told_correction" and e["why"] == "raised"]
        else:
            st = await h.dan_speaks(done=False)
            h.playing_since(st, ago=2.0)
            await h.barge_in()
            dan.ws.feed([done_out("R1", ["i1", "i2"], "cancelled")])
            await until(lambda: h.of("assistant_turn"))
        await until(lambda: h.runner._response_done.is_set())
        # A stale hold dropped, with the same failure inside.
        await _suppressed_hold(h)
        h.runner._speech_started_at = time.time()
        assert await h.runner.adopt_member("priya") is False
        # The pump lives on.
        h.room.speaking = "dan"
        await dan.request_response()
        dan.ws.feed([created("R2"), adelta("R2", "j1"),
                     tdelta("R2", "j1", "Anyway."), tdone("R2", "j1", "Anyway."),
                     done_out("R2", ["j1"])])
        await until(lambda: any(t["text"] == "Anyway." for t in h.of("assistant_turn")))
        assert all(not p.done() for p in h.pumps)
    finally:
        await h.stop()
    assert h.of("assistant_turn")[0]["text"] == LINE
    assert not h.of("voice_error")
    assert not h.page.frames("error") and not h.page.frames("voice_error")
    if broken == "track_items":
        assert h.of("member_memory_error")


@in_a_loop
async def test_a_cut_after_no_word_was_heard_deletes_only(monkeypatch):
    """Nothing heard but audio reached the page: the line is deleted, the
    colleagues are told nothing of it, and the turn says so."""
    monkeypatch.setattr(rvs.RealtimeVoiceSessionRunner, "_heard_wpm",
                        lambda self, aid: (1.0, "test"))
    h = RoomHarness()
    h.start()
    try:
        st = await h.dan_speaks(done=False)
        h.playing_since(st, ago=0.5)
        await h.barge_in()
        h.rt("dan").ws.feed([done_out("R1", ["i1", "i2"], "cancelled")])
        await h.ack_deletes("dan", 2)
        await until(lambda: h.of("member_memory_replaced"))
    finally:
        await h.stop()
    dan = h.rt("dan")
    assert deletes(dan) == ["i1", "i2"] and creates(dan) == []
    (mem,) = h.of("member_memory_replaced")
    assert mem["action"] == "deleted" and mem["heard_text"] == ""
    (turn,) = h.of("assistant_turn")
    assert turn["memory"] == "deleted" and turn["told"] == "none"
    assert "memory_text" not in turn and "told_text" not in turn
    for aid in ("priya", "chris"):
        assert creates(h.rt(aid)) == [], "a line nobody heard was told"

    # Still playing after its turn, nothing of it heard yet: the told notes
    # are deleted, and nothing is put back anywhere.
    h = RoomHarness()
    h.start()
    try:
        await _told_in_full_then_cut(h, total=600.0)
        await h.ack_deletes("dan", 2)
        for aid in ("priya", "chris"):
            await h.ack_deletes(aid, 1)
        await until(lambda: len(h.of("told_line_corrected")) == 2
                    and h.of("member_memory_replaced"))
    finally:
        await h.stop()
    (mem,) = h.of("member_memory_replaced")
    assert mem["action"] == "deleted" and mem["told"] == "none"
    assert mem["told_corrected"] == ["chris", "priya"]
    assert creates(h.rt("dan")) == []
    for aid in ("priya", "chris"):
        assert len(creates(h.rt(aid))) == 1          # the tell, never redone
    assert all(e["action"] == "deleted" for e in h.of("told_line_corrected"))


@in_a_loop
async def test_a_cut_during_the_tell_is_sent_once_the_tell_is_out():
    """The real path: the finalize's tell is still going out when the line,
    still playing, is cut; the correction is queued on the line and sent
    when the tell returns."""
    h = RoomHarness()
    release = asyncio.Event()
    for aid in ("priya", "chris"):
        rt = h.rt(aid)

        async def slow(text, role="user", *, item_id=None, _orig=rt.inject_text):
            await release.wait()
            return await _orig(text, role, item_id=item_id)
        rt.inject_text = slow
    h.start()
    try:
        st = await h.dan_speaks()
        line = h.runner._lines[("dan", "R1")]
        assert line["telling"] is True
        h.room.speaking = None
        h.playing_since(st, ago=2.0)
        await h.barge_in()
        assert "correct_after_tell" in line
        assert all(deletes(h.rt(a)) == [] for a in ("priya", "chris"))
        release.set()
        await until(lambda: all(h.room._told.get(("dan", "R1"), {}).get(a)
                                for a in ("priya", "chris")))
        await told_and_named(h, "dan", "R1", LINE)
        await h.ack_deletes("dan", 2)
        for aid in ("priya", "chris"):
            await h.ack_deletes(aid, 1)
        await until(lambda: len(h.of("told_line_corrected")) == 2)
    finally:
        release.set()
        await h.stop()
    (mem,) = h.of("member_memory_replaced")
    for aid in ("priya", "chris"):
        assert deletes(h.rt(aid)) == [f"n_R1_{aid}"]
        assert creates(h.rt(aid))[-1]["item"]["content"][0]["text"] == \
            gr.GroupRoom._told_note("Dan", mem["heard_text"])
    assert ("dan", mem["heard_text"]) in h.runner._recent_told


@in_a_loop
async def test_a_hold_left_beside_a_live_turn_is_deleted_and_only_it():
    """N-25's second half: Priya finalizes a live turn (R2) while she still
    holds a suppressed reply nobody adopted (R0): R0 is deleted, R2 is not."""
    h = RoomHarness()
    h.start()
    try:
        await _suppressed_hold(h)
        h.room.speaking = "priya"
        await h.runner._finalize_member(h.agents["priya"], "My live line.",
                                        response_id="R2", audio_bytes=32000)
        await h.ack_deletes("priya", 1)
        await until(lambda: h.of("member_memory_replaced"))
    finally:
        await h.stop()
    (mem,) = h.of("member_memory_replaced")
    assert mem["response_id"] == "R0" and mem["why"] == "turn_finalized"
    assert deletes(h.rt("priya")) == ["h1"]


@in_a_loop
async def test_room_retries_record_their_nudge_role_and_the_unhandled_head():
    h = RoomHarness()
    runner = h.runner
    runner._awaiting_participant = False
    h.room.speaking = "dan"
    ev = {"type": "response_done", "retry_reason": "absent", "retryable": True,
          "response_id": "R1", "text": "Lost words.", "words": 2, "audio_ms": 0}
    assert await runner._retry_reply(h.rt("dan"), "dan", ev, has_floor=True)
    (retry,) = h.of("audio_retry")
    assert retry["nudge_role"] == "system"
    (nudge,) = creates(h.rt("dan"))
    assert nudge["item"]["role"] == "system"
    (skip,) = h.of("member_memory_skipped")
    assert skip["reason"] == "retry_head" and skip["response_id"] == "R1"


def test_a_cut_no_finalize_ever_took_is_swept_after_a_minute():
    h = RoomHarness()
    h.runner._line_cuts[("dan", "R1")] = {"reason": "cut_floor_holder",
                                          "at": time.time() - 61}
    h.runner._sweep_line_cuts()
    assert not h.runner._line_cuts
    (skip,) = h.of("member_memory_skipped")
    assert skip["why"] == "never_finalized" and skip["response_id"] == "R1"


@in_a_loop
async def test_a_memory_event_reaching_a_hold_with_the_floor_is_only_recorded():
    """A memory outcome arriving while a suppressed hold has just been given
    the floor is written down and nothing else: it is not the end of the
    held reply, which is still spliced in when its output comes."""
    h = RoomHarness()
    h.start()
    pr = h.rt("priya")
    try:
        await _suppressed_hold(h, rid="R7", finish=False)
        h.room.speaking = "priya"
        pr._memory_outbox.append({
            "type": "memory_op_error", "op": "delete", "item_id": "x1",
            "response_id": "R0", "code": "item_not_found", "param": "item_id",
            "message": "gone", "context": {}, "recovery": None})
        pr.ws.feed([adelta("R7", "h1")])
        await until(lambda: h.of("member_memory_error"))
        pr.ws.feed([adelta("R7", "h1"), done_out("R7", ["h1"])])
        await until(lambda: h.of("assistant_turn"))
    finally:
        await h.stop()
    assert "R7" in h.runner._played_rids
    (turn,) = h.of("assistant_turn")
    assert turn["agent_id"] == "priya" and turn["text"] == "Well I think"


@in_a_loop
async def test_a_colleague_whose_note_cannot_be_corrected_is_written_down():
    """Every colleague of a corrected line is accounted for: one corrected
    is in told_corrected, one that cannot be (its socket gone, its session
    out of the room) is a member_memory_skipped saying why."""
    h = RoomHarness()
    h.start()
    try:
        await h.dan_speaks()
        await told_and_named(h, "dan", "R1", LINE)
        h.rt("chris").ws = None
        line = h.runner._lines[("dan", "R1")]
        corrected = await h.runner._correct_told("dan", "R1", line, "We slipped…",
                                                 reason="cut_still_playing")
    finally:
        await h.stop()
    assert corrected == ["priya"]
    (skip,) = [e for e in h.of("member_memory_skipped")
               if e["reason"] == "told_correction"]
    assert skip["agent_id"] == "chris" and skip["why"] == "closed"
    assert skip["speaker_id"] == "dan" and skip["cut_reason"] == "cut_still_playing"


@pytest.mark.parametrize("reason", ["cut_still_playing", "cut_queued"])
@in_a_loop
async def test_a_cut_before_the_finalize_with_nothing_heard_keeps_and_tells_nothing(reason):
    """A line cut (still playing, or queued behind the line cut) before its
    turn was written, with not one of its words heard: the finalize deletes
    it, tells the colleagues nothing, and the turn says so; never a bare
    ellipsis kept as the line."""
    h = RoomHarness()
    dan = h.rt("dan")
    c = recording(h, "dan")
    try:
        await dan.request_response()
        dan.ws.feed([committed("p1", None)] + line_reply("R1")
                    + [done_out("R1", ["i1", "i2"])])
        await until(lambda: dan.reply_done_seen("R1"))
        await h.runner._memory_after_cut(
            {"agent_id": "dan", "response_id": "R1",
             "heard_s": 0.0 if reason == "cut_queued" else 0.05, "total_s": 6.0},
            reason=reason)
        h.room.speaking = "dan"
        await h.runner._finalize_member(h.agents["dan"], LINE, response_id="R1",
                                        audio_bytes=6 * 32000)
        await h.ack_deletes("dan", 2)
        await until(lambda: h.of("member_memory_replaced"))
    finally:
        await settle(h.runner)
        await c.stop()
    (turn,) = h.of("assistant_turn")
    assert turn["memory"] == "deleted" and turn["told"] == "none"
    assert "memory_text" not in turn and turn["text"] == LINE
    (mem,) = h.of("member_memory_replaced")
    assert mem["reason"] == reason and mem["action"] == "deleted"
    assert mem["heard_text"] == "" and creates(dan) == []
    for aid in ("priya", "chris"):
        assert creates(h.rt(aid)) == [], "a line nobody heard was told"


@in_a_loop
async def test_a_told_correction_waits_for_the_reply_open_on_that_member():
    """By choice (both were accepted mid-reply on 2026-10-01): no item operation goes out while a reply is open
    on that member (the floor holder a case 2 correction lands on, just
    cancelled or still streaming); it goes at that reply's done."""
    rt = tracked()
    c = Consumer(rt)
    try:
        await rt.inject_text("full note", item_id="rf_t_x")
        rt.ws.feed([committed("p1", None),
                    added("item_t1", "p1", role="user", text="full note")])
        await until(lambda: rt._gid("rf_t_x") == "item_t1")
        await rt.request_response()
        rt.ws.feed([created("R5"), tdelta("R5", "k1", "Streaming")])
        await until(lambda: "R5" in rt._open_replies)
        assert await rt.correct_item("rf_t_x", role="user", text="heard") == "deferred"
        await asyncio.sleep(0.05)
        assert deletes(rt) == []
        rt.ws.feed([done_out("R5", ["k1"])])
        await until(lambda: deletes(rt) == ["item_t1"])
        rt.ws.feed([deleted("item_t1")])
        (op,) = await c.wait("memory_op")
    finally:
        await c.stop()
    assert op["action"] == "corrected" and op["deferred"] is True
