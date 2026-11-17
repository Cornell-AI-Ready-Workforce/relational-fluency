"""#297, cause 2: cascade rooms run 10-01a's room memory hygiene.

Ben's S3A cascade session s_1794766272_cff74e (pipeline 2026-11-16a): Alex's
line 10 was cut at 6.6 of its 6.8 s, and his line 11, finalized and told to
Jordan and Casey while line 10 played, was queued behind it and never played.
Both stayed whole in Alex's own conversation and in the others' told notes
(room_memory active False on cascade), Alex then said "I already answered
that", and Ben said "No, you didn't". Since 11-16b (CASCADE_ROOM_MEMORY,
default on) a cascade member's conversation keeps only what was heard.

Harness level: a real runner, GroupRoom, RealtimeVoiceSession bridges and
CascadeSocket stand-ins (tests/test_room_member_reconnect.Room); only the
gateway HTTP and ElevenLabs are fake. The tests marked "fails on origin/main"
were run against a git archive of origin/main.
"""
import asyncio
import json
import time

import httpx
import pytest

from server import llm
from server import realtime_voice_session as rvs
from server.voice import cascade, realtime as R, turn_audio
from cascade_room_harness import CASCADE
from test_cascade_voice import FakeTTS, _sse
from test_bridge_correctness import LOUD, settle
from test_room_member_reconnect import Room, in_a_loop, until

def wire(monkeypatch, replies, *, tts_gate=None, open_gate=None):
    """The gateway's chat (one scripted reply per request, the last one
    repeated) and ElevenLabs. `tts_gate`: the stream's end (is_final) waits
    for it, so a reply stays open while its audio plays; `open_gate`: the
    stream's open waits for it, so a reply has no audio yet."""
    chats = []

    async def handle(req):
        body = json.loads(req.content)
        if body.get("stream"):
            chats.append(body)
            reply = replies[min(len(chats), len(replies)) - 1]
            if isinstance(reply, dict):
                return _sse_call(**reply)
            return _sse(*reply)
        return httpx.Response(200, json={"choices": [{"finish_reason": "stop",
                                                      "message": {"content": "Mm."}}]})

    transport = httpx.MockTransport(handle)
    monkeypatch.setattr(cascade, "eleven_key", lambda: "k")
    monkeypatch.setattr(R, "gateway_api_key", lambda: "k")

    class HeldTTS(FakeTTS):
        async def send(self, raw):
            m = json.loads(raw)
            if m.get("close_socket") and tts_gate is not None:
                self.sent.append(m)

                async def later():
                    await tts_gate.wait()
                    self._out.put_nowait(json.dumps({"is_final": True}))
                asyncio.ensure_future(later())
                return
            await super().send(raw)

    async def opener():
        if open_gate is not None:
            await open_gate.wait()
        return HeldTTS({})
    monkeypatch.setattr(cascade, "open_tts", opener)
    real = cascade.CascadeSocket
    monkeypatch.setattr(R, "CascadeSocket", lambda **kw: real(
        http=httpx.AsyncClient(transport=transport), **kw))
    return chats


def _sse_call(name="end_segment", arguments="{}"):
    body = ("data: " + json.dumps({"choices": [{"delta": {"tool_calls": [{
        "id": "call_1", "function": {"name": name, "arguments": arguments}}]}}]})
        + "\n\n" + "data: [DONE]\n\n")
    return httpx.Response(200, content=body.encode(),
                          headers={"content-type": "text/event-stream"})


LINE10 = ("Forty open reviews. ", "Nadia left eleven of hers with me. ",
          "I cannot clear them all by Friday.")
LINE11 = ("I have my normal ten plus eleven of Nadia's. ",
          "How many reviews are you signing off on this week?")
NEXT = ("Fine, let me say it again.",)


def playing_since(h, aid, ago, total):
    st = h.runner._member_states[aid]
    now = time.time()
    st.play_start, st.play_end = now - ago, now - ago + total
    h.runner._play_cursor = st.play_end
    lp = h.runner._last_played
    lp["start"], lp["end"] = st.play_start, st.play_end


def place(h, lines):
    """Put lines on the playback clock as Ben's were, from now: each
    (response_id, seconds already played, seconds long); a line after the
    first starts where the one before it ends (queued behind it)."""
    now = time.time()
    by_rid = {l.get("response_id"): l for l in
              list(h.runner._played_lines) + [h.runner._last_played or {}]}
    at = None
    for rid, ago, total in lines:
        start = now - ago if at is None else at
        for line in {id(l): l for l in by_rid.values()
                     if l.get("response_id") == rid}.values():
            line["start"], line["end"] = start, start + total
        at = start + total
    h.runner._play_cursor = at


async def says(h, aid):
    n = len(h.of("assistant_turn"))
    h.room.speaking = aid
    await h.rt(aid).request_response()
    await until(lambda: len(h.of("assistant_turn")) > n, 8)
    await settle(h.runner)
    if h.room.speaking == aid:
        h.room.speaking = None
    # The reply's id from the playback clock: a 30a record has none.
    return {**h.of("assistant_turn")[-1],
            "rid": h.runner._last_played.get("response_id")}


async def barge_in(h):
    h.page._frames = [LOUD] * 25
    await h.runner._client_to_model()


def texts(rt):
    return [(i["role"], i["text"]) for i in rt.ws.items]


def joined(reply):
    return " ".join(p.strip() for p in reply)


def note_of(text):
    return f'Dan just said out loud to the group: "{text}'


def quiet(h):
    """No memory operation of any member went wrong or was skipped."""
    assert not h.of("member_memory_error")
    assert not h.of("member_memory_skipped")
    assert not [e for e in h.of("voice_error") if "memory" in json.dumps(e)]


async def bens_s2(monkeypatch, *, knob=None):
    """Line 10 playing (cut at 6.6 of 6.8 s), line 11 finalized, told and
    queued behind it, then the participant cuts in."""
    if knob is not None:
        monkeypatch.setenv("CASCADE_ROOM_MEMORY", knob)
    chats = wire(monkeypatch, [LINE10, LINE11, NEXT])
    h = await Room(monkeypatch, model=CASCADE).open()
    t10 = await says(h, "dan")
    playing_since(h, "dan", 6.6, 6.8)
    t11 = await says(h, "dan")
    # Re-placed just before the cut, so a slow step above cannot let line 10
    # end first: line 10 heard 6.6 of 6.8 s, line 11 (5.76 s) queued.
    place(h, [(t10["rid"], 6.6, 6.8), (t11["rid"], 0, 5.76)])
    assert h.runner._last_played["start"] > time.time()
    await barge_in(h)
    if R.room_memory_active(CASCADE):
        await until(lambda: len(h.of("member_memory_replaced")) == 2
                    and len(h.of("told_line_corrected")) == 4, 5)
    await settle(h.runner)
    return h, chats



@in_a_loop
async def test_bens_s2_a_queued_line_never_played_is_forgotten(monkeypatch):
    """Fails on origin/main. Alex's line 11 (here Dan's) is gone from his
    conversation and from the colleagues' told notes, line 10 keeps its heard
    words, and his next request (and theirs) holds neither line 11 nor its
    note: there is nothing for him to have "already answered"."""
    h, chats = await bens_s2(monkeypatch)
    try:
        mems = {m["reason"]: m for m in h.of("member_memory_replaced")}
        q = mems["cut_queued"]
        assert q["agent_id"] == "dan" and q["action"] == "deleted"
        assert q["heard_text"] == "" and q["told"] == "none"
        assert q["generated_text"] == joined(LINE11)
        assert q["told_corrected"] == ["chris", "priya"]
        cut = mems["cut_still_playing"]
        assert cut["action"] == "replaced" and cut["generated_text"] == joined(LINE10)
        (pc,) = h.of("playback_cut")
        assert cut["heard_text"] == pc["heard_text"] and pc["heard_text"].endswith("\u2026")
        assert cut["waited_s"] < 0.5 and q["waited_s"] < 0.5
        corr = sorted((e["agent_id"], e["action"]) for e in h.of("told_line_corrected")
                      if e["response_id"] == q["response_id"])
        assert corr == [("chris", "deleted"), ("priya", "deleted")]
        heard = pc["heard_text"].rstrip("\u2026")
        # Dan keeps the heard words, without the trailing ellipsis (amend 6).
        assert texts(h.rt("dan")) == [("assistant", heard)]
        for aid in ("priya", "chris"):
            (note,) = texts(h.rt(aid))
            assert note_of(pc["heard_text"]) in note[1]
        quiet(h)
        # The next requests: Dan's, then a colleague's.
        await says(h, "dan")
        body = json.dumps(chats[-1]["messages"])
        assert LINE11[1].strip() not in body and "Nadia's" not in body
        assert "Friday" not in body and heard in body
        await says(h, "priya")
        body = json.dumps(chats[-1]["messages"])
        assert LINE11[1].strip() not in body and "Nadia's" not in body
    finally:
        await h.close()


@in_a_loop
async def test_cascade_room_memory_off_is_the_old_cascade_room(monkeypatch):
    """Passes on origin/main too: CASCADE_ROOM_MEMORY=0 keeps both lines whole
    everywhere, sends no delete, and records none of 10-01a."""
    deletes = []
    real = cascade.CascadeSocket._on_conversation_item_delete

    async def counted(self, ev):
        deletes.append(ev)
        await real(self, ev)
    monkeypatch.setattr(cascade.CascadeSocket, "_on_conversation_item_delete", counted)
    h, chats = await bens_s2(monkeypatch, knob="0")
    try:
        assert deletes == []
        for aid in ("dan", "priya", "chris"):
            rt = h.rt(aid)
            assert rt.track_items is False and rt.nudge_role == "user"
        assert texts(h.rt("dan")) == [("assistant", joined(LINE10)),
                                      ("assistant", joined(LINE11))]
        for aid in ("priya", "chris"):
            notes = [t for _, t in texts(h.rt(aid))]
            assert note_of(joined(LINE10)) in notes[0]
            assert note_of(joined(LINE11)) in notes[1]
        for kind in ("member_memory_replaced", "told_line_corrected",
                     "member_memory_skipped", "member_memory_error"):
            assert not h.of(kind), kind
        assert "memory" not in h.of("assistant_turn")[0]
    finally:
        await h.close()


@in_a_loop
async def test_the_knob_is_read_once_per_member(monkeypatch):
    """Amend 5: a flip in the middle of an encounter changes nothing for a
    member already configured, and writes no 'untracked' skip."""
    monkeypatch.setenv("CASCADE_ROOM_MEMORY", "on")
    chats = wire(monkeypatch, [LINE10, LINE11, NEXT])
    h = await Room(monkeypatch, model=CASCADE).open()
    try:
        monkeypatch.setenv("CASCADE_ROOM_MEMORY", "0")
        t10 = await says(h, "dan")
        t11 = await says(h, "dan")
        place(h, [(t10["rid"], 6.6, 6.8), (t11["rid"], 0, 5.76)])
        await barge_in(h)
        await until(lambda: len(h.of("member_memory_replaced")) == 2, 5)
        await settle(h.runner)
        quiet(h)
    finally:
        await h.close()
    monkeypatch.setenv("CASCADE_ROOM_MEMORY", "0")
    chats = wire(monkeypatch, [LINE10, LINE11, NEXT])
    h = await Room(monkeypatch, model=CASCADE).open()
    try:
        monkeypatch.setenv("CASCADE_ROOM_MEMORY", "on")
        t10 = await says(h, "dan")
        t11 = await says(h, "dan")
        place(h, [(t10["rid"], 6.6, 6.8), (t11["rid"], 0, 5.76)])
        await barge_in(h)
        await settle(h.runner)
        assert not h.of("member_memory_replaced") and not h.of("member_memory_skipped")
        assert "memory" not in h.of("assistant_turn")[0]
    finally:
        await h.close()


@in_a_loop
async def test_a_floor_holders_cut_keeps_only_the_heard_words_on_cascade(monkeypatch):
    """Fails on origin/main. A 6 s line cut about 1 s in, while Dan holds the
    floor: his conversation keeps the play_clock_wpm estimate (a proper
    prefix, no ellipsis) and the colleagues are told those words only."""
    gate = asyncio.Event()
    wire(monkeypatch, [LINE10], tts_gate=gate)
    h = await Room(monkeypatch, model=CASCADE).open()
    try:
        h.room.speaking = "dan"
        await h.rt("dan").request_response()
        st = h.runner._member_states
        await until(lambda: "dan" in st and st["dan"].relayed_bytes > 0
                    and st["dan"].play_start is not None, 5)
        playing_since(h, "dan", 1.0, 6.0)
        await barge_in(h)
        await until(lambda: h.of("member_memory_replaced"), 5)
        await until(lambda: h.of("assistant_turn"), 5)
        await settle(h.runner)
        (mem,) = h.of("member_memory_replaced")
        (turn,) = h.of("assistant_turn")
        assert mem["reason"] == "cut_floor_holder" and mem["action"] == "replaced"
        expected = turn_audio.heard_estimate(joined(LINE10), mem["heard_ms"],
                                             mem["wpm"])["heard_text"]
        assert mem["heard_text"] == expected and expected.endswith("\u2026")
        kept = expected.rstrip("\u2026")
        assert joined(LINE10).startswith(kept) and kept != joined(LINE10)
        assert texts(h.rt("dan")) == [("assistant", kept)]
        for aid in ("priya", "chris"):
            (note,) = texts(h.rt(aid))
            assert note_of(expected) in note[1] and "Friday" not in note[1]
        assert turn["told"] == "heard" and turn["memory"] == "replaced"
        assert turn["text"] == joined(LINE10)
        quiet(h)
    finally:
        gate.set()
        await h.close()


@in_a_loop
async def test_a_line_cut_while_still_playing_corrects_the_told_note_in_place_on_cascade(
        monkeypatch):
    """Fails on origin/main. Ben s1, Alex at 187.9 s: cut at 4.9 of 6.6 s
    after its turn. Priya spoke first, so Dan's note is her second item; it
    is replaced by the heard share at that same index."""
    wire(monkeypatch, [("I can take the vendor call.",), LINE10])
    h = await Room(monkeypatch, model=CASCADE).open()
    try:
        await says(h, "priya")
        await says(h, "dan")
        before = {a: [i["id"] for i in h.rt(a).ws.items] for a in ("priya", "chris")}
        assert len(before["priya"]) == 2
        playing_since(h, "dan", 4.9, 6.6)
        place(h, [(h.of("assistant_turn")[-1]["response_id"], 4.9, 6.6)])
        await barge_in(h)
        await until(lambda: h.of("member_memory_replaced")
                    and len(h.of("told_line_corrected")) == 2, 5)
        await settle(h.runner)
        (mem,) = h.of("member_memory_replaced")
        (pc,) = h.of("playback_cut")
        assert mem["reason"] == "cut_still_playing"
        assert mem["heard_text"] == pc["heard_text"]
        for e in h.of("told_line_corrected"):
            assert e["action"] == "corrected"
        pr = texts(h.rt("priya"))
        assert pr[0] == ("assistant", "I can take the vendor call.")
        assert note_of(pc["heard_text"]) in pr[1][1] and "Friday" not in pr[1][1]
        assert len(pr) == 2
        quiet(h)
    finally:
        await h.close()


@in_a_loop
async def test_a_reply_cancelled_before_audio_is_forgotten_without_errors(monkeypatch):
    """Fails on origin/main (nothing is forgotten there). Dan's reply is cut
    on the floor before its ElevenLabs stream opened: the stand-in drops the
    empty item itself, and the runner's delete of it is acked (_dropped), so
    the record says deleted with no member_memory_error or _skipped."""
    held = asyncio.Event()
    wire(monkeypatch, [LINE10, NEXT], open_gate=held)
    h = await Room(monkeypatch, model=CASCADE).open()
    try:
        dan = h.rt("dan")
        h.room.speaking = "dan"
        await dan.request_response()
        await until(lambda: dan._reply_items, 5)
        await barge_in(h)
        await until(lambda: h.of("member_memory_replaced"), 5)
        await settle(h.runner)
        (mem,) = h.of("member_memory_replaced")
        assert mem["reason"] == "cut_floor_holder" and mem["action"] == "deleted"
        assert mem["items_refused"] == []
        assert texts(dan) == []
        quiet(h)
    finally:
        held.set()
        await h.close()


@in_a_loop
async def test_a_cascade_commit_with_no_reply_leaves_the_member_not_busy(monkeypatch):
    """Amend 3: on cascade a commit never starts a reply, so one with no
    create after it (here refused as empty) must not read the member busy
    for 2 s and hold a told correction back."""
    wire(monkeypatch, [LINE10])
    h = await Room(monkeypatch, model=CASCADE).open()
    try:
        await says(h, "dan")
        priya = h.rt("priya")
        assert priya.track_items
        await priya.commit_input()
        assert not priya._busy()
        rid = h.of("assistant_turn")[-1]["response_id"]
        state = await h.room.correct_told("Dan", "dan", rid, "Forty open\u2026")
        assert state["priya"] == "sent"
    finally:
        await h.close()


@in_a_loop
async def test_a_member_rebuilt_after_a_cut_is_seeded_with_the_heard_words(monkeypatch):
    """Fails on origin/main. After Ben's s2 cut, Dan rebuilt is given line 10's
    heard words (no ellipsis) and not line 11; Priya rebuilt is given the
    corrected note of line 10 and no note of line 11."""
    h, chats = await bens_s2(monkeypatch)
    try:
        (pc,) = h.of("playback_cut")
        monkeypatch.setattr(h.runner, "_member_stream_ended", lambda *a, **kw: None)
        for aid in ("dan", "priya"):
            old = h.room.sessions.pop(aid)
            await h.runner._rebuild_member(aid, old, "test")
        assert h.of("room_member_reconnected")[-1]["seed_route"] == "conversation_items"
        assert texts(h.rt("dan")) == [("assistant", pc["heard_text"].rstrip("\u2026"))]
        (note,) = texts(h.rt("priya"))
        assert note_of(pc["heard_text"]) in note[1]
        assert h.rt("dan").track_items and h.rt("dan").cascade_room_memory is True
    finally:
        await h.close()


@in_a_loop
async def test_the_adapter_acks_a_delete_of_an_item_it_dropped(monkeypatch):
    """Socket level, amend 4: a reply that was only a function call leaves
    its empty message item out of the conversation; a delete of that item is
    acked, not item_not_found. A delete of an id never seen is still refused."""
    wire(monkeypatch, [{"name": "end_segment", "arguments": "{}"}])
    ws = R.CascadeSocket(voice="will")
    await ws.send(json.dumps({"type": "conversation.item.create", "item": {
        "type": "message", "role": "user",
        "content": [{"type": "input_text", "text": "Hello there."}]}}))
    await ws.send(json.dumps({"type": "response.create"}))
    frames = []
    while not any(f["type"] == "response.done" for f in frames):
        frames.append(json.loads(await asyncio.wait_for(ws.recv(), 5)))
    (added,) = [f for f in frames if f["type"] == "response.output_item.added"]
    iid = added["item"]["id"]
    assert [i["type"] for i in ws.items] == ["message", "function_call"]
    await ws.send(json.dumps({"type": "conversation.item.delete", "item_id": iid,
                              "event_id": "e1"}))
    ack = json.loads(await asyncio.wait_for(ws.recv(), 5))
    assert ack == {"type": "conversation.item.deleted", "item_id": iid, "event_id": "e1"}
    await ws.send(json.dumps({"type": "conversation.item.delete", "item_id": iid,
                              "event_id": "e2"}))
    err = json.loads(await asyncio.wait_for(ws.recv(), 5))
    assert err["type"] == "error" and err["error"]["code"] == "item_not_found"
    await ws.close()


@in_a_loop
async def test_root_anchor_inserts_at_the_front(monkeypatch):
    """Socket level. Fails on origin/main (appended at the end there)."""
    wire(monkeypatch, [LINE10])
    ws = R.CascadeSocket(voice="will")
    for text in ("first", "second"):
        await ws.send(json.dumps({"type": "conversation.item.create", "item": {
            "type": "message", "role": "user",
            "content": [{"type": "input_text", "text": text}]}}))
    await ws.send(json.dumps({"type": "conversation.item.create",
                              "previous_item_id": "root", "item": {
                                  "id": "rf_a_1", "type": "message", "role": "assistant",
                                  "content": [{"type": "output_text", "text": "heard"}]}}))
    assert [i["text"] for i in ws.items] == ["heard", "first", "second"]
    await ws.close()


@in_a_loop
async def test_a_one_to_one_cascade_session_is_untouched(monkeypatch):
    """Passes on origin/main too: memory hygiene is room-only."""
    wire(monkeypatch, [LINE10])
    rt = R.RealtimeVoiceSession(api_key="k", instructions="", model=CASCADE)
    assert rt.track_items is False and rt.nudge_role == "user"
    runner = rvs.RealtimeVoiceSessionRunner.__new__(rvs.RealtimeVoiceSessionRunner)
    runner.room = None
    assert runner._memory_policy(rt) == ("keep", "generated")
    assert runner._memory_effective(rt) is False


def test_cascade_room_memory_knob_and_version():
    for v, on in (("on", True), ("1", True), ("anything", True), ("0", False),
                  ("off", False), ("False", False), ("no", False)):
        import os
        os.environ["CASCADE_ROOM_MEMORY"] = v
        try:
            assert R.cascade_room_memory() is on, v
            assert R.room_memory_active(CASCADE) is on
            assert R.room_memory_effective(CASCADE) is on
            assert R.room_memory_active("gpt-realtime-2.1") is True
        finally:
            del os.environ["CASCADE_ROOM_MEMORY"]
    assert R.room_memory_nudge_role(CASCADE) == "user"
    assert R.room_memory_nudge_role("gpt-realtime-2.1") == "system"
    assert llm.PIPELINE_VERSION >= "2026-11-16b"
    assert llm.ROOM_PACING_VERSION >= "2026-11-16b"
    src = open(llm.__file__, encoding="utf-8").read()
    assert src.count("(also) cascade room memory (#297)") == 1
    assert src.count("(also) Rooms (#297): cascade room memory") == 1
