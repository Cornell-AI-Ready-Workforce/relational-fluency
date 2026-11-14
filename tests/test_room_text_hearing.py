"""Acceptance R: deterministic pauses through real cascade room requests."""
import asyncio
import hashlib
import json

import pytest

from server import group_room as gr
from server.voice import cascade, realtime as R
from cascade_room_harness import (CASCADE_ROOM_GRANT_NUDGE,
                                       CASCADE_ROOM_UNHEARD_NUDGE,
                                       ROOM_RULE_CASCADE)
from cascade_room_harness import (CASCADE, WORDS, WORDS2, QUIET, Transcript,
                                  wire, speak, finished, assert_heard, user_lines)
from test_room_member_reconnect import (Room, ScriptedDirector, in_a_loop,
                                        until, quick)  # noqa: F401
from test_bridge_correctness import GPT, NATIVE, LOUD, settle


@in_a_loop
async def test_unnamed_named_followup_and_two_turns(monkeypatch):
    # The fake page has no playback acks; the gap itself is covered elsewhere.
    monkeypatch.setenv("FOLLOWUP_GAP_S", "-1")
    chats, stts = wire(monkeypatch, [Transcript(), Transcript(WORDS2)])
    h = Room(monkeypatch, model=CASCADE)
    h.runner.director = h.session.director = ScriptedDirector([{"agent_id": "chris"}])
    await h.open()
    try:
        await speak(h)
        await until(lambda: len(chats) == 2)
        await finished(h)
        assert h.of("director_route")[0]["speakers"] == ["dan", "chris"]
        for body in chats:
            assert_heard(body, [WORDS])
        await speak(h)
        await until(lambda: len(chats) == 4)
        await finished(h)
        assert h.of("director_route")[1]["speakers"] == ["priya", "chris"]
        for body in chats[2:]:
            assert_heard(body, [WORDS, WORDS2])
        assert len(stts) == 2
        assert all(slot.event is None for slot in h.room.hearing.commits.values())
        for rt in h.room.sessions.values():
            assert [i["text"] for i in rt.ws.items].count(WORDS) == 1
            assert [i["text"] for i in rt.ws.items].count(WORDS2) == 1
            assert not rt.ws._audio
        assert all(ROOM_RULE_CASCADE in rt.ws.instructions
                   for rt in h.room.sessions.values())
    finally:
        await h.close()


@in_a_loop
async def test_reverse_stt_completion_releases_spoken_order(monkeypatch):
    gate = asyncio.Event()
    one = "Schedule the launch for Tuesday"
    two = "No, make it Wednesday instead"
    scripts = [Transcript(one, gate), Transcript(two)]
    chats, _ = wire(monkeypatch, scripts)
    monkeypatch.setenv("ROUTE_TRANSCRIPT_WAIT", "3")
    h = await Room(monkeypatch, model=CASCADE).open()
    try:
        await speak(h)
        await scripts[0].started.wait()
        await speak(h)
        await scripts[1].finished.wait()
        await settle(h.runner)
        assert not h.of("user_turn") and not chats
        gate.set()
        await until(lambda: bool(chats))
        await finished(h)
        assert [e["text"] for e in h.of("user_turn")] == [one, two]
        assert [e["text"] for e in h.runner._room_heard if e["speaker"] == "user"] == [one, two]
        assert h.room.hearing.active_turn.route_text == one + " " + two
        assert_heard(chats[0], [one, two])
        assert len(h.of("turn_end_withdrawn")) == 1
    finally:
        await h.close()


@in_a_loop
async def test_low_confidence_is_relayed_once_outside_director_queue(monkeypatch):
    chats, _ = wire(monkeypatch, [])
    h = await Room(monkeypatch, model=CASCADE).open()
    try:
        await h.runner._record_user_turn("No.", voiced_ms=200, voiced_span_ms=200)
        assert h.of("user_turn")[-1]["low_confidence"]
        assert not h.runner._unrouted_user_texts
        await h.room.give_floor("dan")
        await until(lambda: bool(chats))
        assert_heard(chats[0], ["No."])
        assert len(h.room.hearing.entries) == 1
    finally:
        await h.close()


@in_a_loop
async def test_cascade_does_not_drop_a_sentence_that_adds_words(monkeypatch):
    one, two = "I think the deadline slipped", "I think the deadline slipped a lot"
    chats, _ = wire(monkeypatch, [Transcript(one), Transcript(two)])
    h = await Room(monkeypatch, model=CASCADE).open()
    try:
        await speak(h)
        await until(lambda: len(chats) == 1)
        await finished(h)
        await speak(h)
        await until(lambda: len(chats) == 2)
        await finished(h)
        assert_heard(chats[1], [one, two])
        assert not h.of("user_transcript_duplicate_dropped")
        assert not R.room_has_second_transcriber(CASCADE)
        assert R.room_has_second_transcriber(NATIVE)
    finally:
        await h.close()


@pytest.mark.parametrize("outcome", ["suppressed", "failed", "empty", "timeout"])
@in_a_loop
async def test_terminal_slot_releases_the_next_commit(monkeypatch, outcome):
    gate = asyncio.Event() if outcome == "timeout" else None
    first = Transcript("." if outcome == "suppressed" else "", gate,
                       fail=outcome == "failed")
    chats, _ = wire(monkeypatch, [first, Transcript(WORDS2)])
    h = await Room(monkeypatch, model=CASCADE).open()
    try:
        # Keep both commits in the routing wait even for an immediate failure.
        async with h.runner._floor:
            await speak(h)
            await first.started.wait()
            await speak(h)
        await until(lambda: bool(chats))
        await finished(h)
        assert_heard(chats[0], [WORDS2])
        assert [e["text"] for e in h.of("user_turn")] == [WORDS2]
        assert len(chats) == 1
        assert CASCADE_ROOM_GRANT_NUDGE in user_lines(chats[0])
    finally:
        if gate:
            gate.set()
        await h.close()


@in_a_loop
async def test_acceptance_enqueues_before_caption_and_grant_waits_for_delivery(monkeypatch):
    chats, _ = wire(monkeypatch, [Transcript()])
    h = await Room(monkeypatch, model=CASCADE).open()
    await h.runner._await_participant("test")
    caption, delivery = asyncio.Event(), asyncio.Event()
    caption_seen, delivery_seen = asyncio.Event(), asyncio.Event()
    original_send = h.runner._send
    rt = h.rt("dan")
    inject = rt.inject_text

    async def send(payload):
        if payload.get("type") == "user_transcript":
            caption_seen.set()
            await caption.wait()
        await original_send(payload)

    async def paused(text, *a, **kw):
        if text == WORDS:
            delivery_seen.set()
            await delivery.wait()
        return await inject(text, *a, **kw)

    monkeypatch.setattr(h.runner, "_send", send)
    monkeypatch.setattr(rt, "inject_text", paused)
    try:
        await speak(h)
        await caption_seen.wait()
        await delivery_seen.wait()
        assert h.room.hearing.entries[0]["text"] == WORDS
        assert not h.runner._awaiting_participant
        assert not chats
        delivery.set()
        await until(lambda: bool(chats))
        assert not caption.is_set()
        assert_heard(chats[0], [WORDS])
        assert h.room.hearing.delivery(rt).cursor >= h.room.hearing.entries[0]["seq"]
        caption.set()
        await finished(h)
    finally:
        caption.set()
        delivery.set()
        await h.close()


@in_a_loop
async def test_late_named_line_during_briefing_changes_first_speaker_once(monkeypatch):
    gate, brief, in_brief = asyncio.Event(), asyncio.Event(), asyncio.Event()
    late = Transcript(WORDS2, gate)
    chats, _ = wire(monkeypatch, [Transcript(), late])
    h = await Room(monkeypatch, model=CASCADE).open()
    try:
        await speak(h)
        await until(lambda: len(chats) == 1)
        await finished(h)
        monkeypatch.setattr(h.runner, "_next_trigger", lambda: None)
        original = h.runner._brief_member
        paused = False

        async def briefing(aid):
            nonlocal paused
            if not paused:
                paused = True
                in_brief.set()
                await brief.wait()
            await original(aid)

        monkeypatch.setattr(h.runner, "_brief_member", briefing)
        await speak(h)
        await in_brief.wait()  # transcript deadline expired; Dan was selected
        gate.set()
        await until(lambda: len(h.of("user_turn")) == 2)
        brief.set()
        await until(lambda: len(chats) == 2)
        await finished(h)
        assert h.of("director_route")[-1]["speakers"] == ["priya"]
        assert_heard(chats[-1], [WORDS, WORDS2])
    finally:
        gate.set()
        brief.set()
        await h.close()


@in_a_loop
async def test_line_after_request_snapshot_is_context_for_next_request(monkeypatch):
    stt_gate, snapshot, resume = asyncio.Event(), asyncio.Event(), asyncio.Event()
    late = Transcript(WORDS2, stt_gate)
    next_line = "Dan, can you confirm the final plan for Thursday"
    chats, _ = wire(monkeypatch, [Transcript(), late, Transcript(next_line)])
    h = await Room(monkeypatch, model=CASCADE).open()
    try:
        await speak(h)
        await until(lambda: len(chats) == 1)
        await finished(h)
        original = cascade.CascadeSocket._stream_llm
        h.runner.director = ScriptedDirector([{"agent_id": "priya"}])
        paused = False

        async def stream(self, *a, **kw):
            nonlocal paused
            if not paused:
                paused = True
                snapshot.set()
                await resume.wait()
            return await original(self, *a, **kw)

        monkeypatch.setattr(cascade.CascadeSocket, "_stream_llm", stream)
        await speak(h)
        await snapshot.wait()
        stt_gate.set()
        await until(lambda: len(h.of("user_turn")) == 2)
        resume.set()
        await until(lambda: len(chats) == 2)
        await finished(h)
        assert WORDS2 not in json.dumps(chats[1])
        assert CASCADE_ROOM_UNHEARD_NUDGE in user_lines(chats[1])
        assert len(chats) == 2  # Dan/Priya late-routing double-answer probe
        h.runner.director = ScriptedDirector()
        await speak(h)
        await until(lambda: len(chats) == 3)
        await finished(h)
        assert_heard(chats[2], [WORDS, WORDS2, next_line])
    finally:
        stt_gate.set()
        resume.set()
        await h.close()


@pytest.mark.parametrize("late_suppression", [False, True])
@in_a_loop
async def test_missing_turn_clarifies_and_suppression_after_grant_adds_no_reply(monkeypatch, late_suppression):
    gate = asyncio.Event() if late_suppression else None
    second = Transcript("." if late_suppression else "", gate,
                        fail=not late_suppression)
    chats, _ = wire(monkeypatch, [Transcript(), second])
    h = await Room(monkeypatch, model=CASCADE).open()
    try:
        await speak(h)
        await until(lambda: len(chats) == 1)
        await finished(h)
        await speak(h)
        await until(lambda: len(chats) == 2)
        if gate:
            gate.set()
            await until(lambda: bool(h.of("user_turn_suppressed")))
        await finished(h)
        assert CASCADE_ROOM_UNHEARD_NUDGE in user_lines(chats[1])
        assert len(chats) == 2
        assert len(h.of("user_turn")) == 1
    finally:
        if gate:
            gate.set()
        await h.close()


@in_a_loop
async def test_first_missing_turn_waits_then_routes_first_accepted_line(monkeypatch):
    gate = asyncio.Event()
    chats, _ = wire(monkeypatch, [Transcript(WORDS2, gate)])
    h = await Room(monkeypatch, model=CASCADE).open()
    try:
        await h.runner._await_participant("test")
        await speak(h)
        await until(lambda: bool(h.of("group_turn_skipped")))
        assert not chats
        assert h.of("group_turn_skipped")[-1]["reason"] == "awaiting_participant"
        gate.set()
        await until(lambda: len(chats) == 1)
        await finished(h)
        assert_heard(chats[0], [WORDS2])
        assert CASCADE_ROOM_GRANT_NUDGE in user_lines(chats[0])
        assert h.of("director_route")[-1]["speakers"] == ["priya"]
    finally:
        gate.set()
        await h.close()


@in_a_loop
async def test_reconnect_catches_line_accepted_during_seed_once_and_keeps_memory(monkeypatch):
    chats, _ = wire(monkeypatch, [Transcript()])
    h = await Room(monkeypatch, model=CASCADE).open()
    seed_started, seed_resume = asyncio.Event(), asyncio.Event()
    try:
        await speak(h)
        await until(lambda: len(chats) == 1)
        await finished(h)
        original_seed = h.runner._seed_member
        old = h.rt("priya")
        monkeypatch.setattr(h.runner, "_member_stream_ended", lambda *a, **kw: None)
        paused = False

        async def seed(*a, **kw):
            nonlocal paused
            if not paused:
                paused = True
                seed_started.set()
                await seed_resume.wait()
            return await original_seed(*a, **kw)

        monkeypatch.setattr(h.runner, "_seed_member", seed)
        h.room.sessions.pop("priya")
        rebuilding = asyncio.create_task(h.runner._rebuild_member("priya", old, "test"))
        await seed_started.wait()
        await h.participant(WORDS2)
        seed_resume.set()
        await rebuilding
        new = h.rt("priya")
        assert new is not old
        texts = [i["text"] for i in new.ws.items]
        assert texts.count(WORDS) == texts.count(WORDS2) == 1
        assert any('Dan just said out loud' in t for t in texts)
        await h.runner._grant("priya")
        await until(lambda: len(chats) == 2)
        await settle(h.runner)
        h.runner._member_notes.pop("priya", None)
        await new.update_instructions(h.runner._instructions_for(h.agents["priya"]))
        await h.runner._grant("priya")
        await until(lambda: len(chats) == 3)
        for body in chats[1:]:
            assert_heard(body, [WORDS, WORDS2])
            assert sum('Dan just said out loud' in t for t in user_lines(body)) == 1
        assert h.of("room_member_reconnected")[-1]["seed_route"] == "conversation_items"
        assert not new.ws._audio
    finally:
        seed_resume.set()
        await h.close()


@in_a_loop
async def test_idle_pcm_has_no_member_growth_and_text_history_grows_separately(monkeypatch):
    wire(monkeypatch, [])
    h = await Room(monkeypatch, model=CASCADE).open()
    try:
        initial = {a: (len(rt.ws._audio), len(rt.ws.items)) for a, rt in h.room.sessions.items()}
        for _ in range(500):
            await h.room.hear(QUIET)
        assert initial == {a: (len(rt.ws._audio), len(rt.ws.items)) for a, rt in h.room.sessions.items()}
        assert len(h.room.scribe.ws._audio) == 500 * len(QUIET)
        await h.participant(WORDS)
        await until(lambda: all(any(i["text"] == WORDS for i in rt.ws.items)
                               for rt in h.room.sessions.values()))
        assert all(len(rt.ws.items) == initial[a][1] + 1 and not rt.ws._audio
                   for a, rt in h.room.sessions.items())
        assert sum(len(e["text"]) for e in h.room.hearing.entries) == len(WORDS)
    finally:
        await h.close()


@pytest.mark.parametrize("failure", ["exception", "counter", "closed"])
@in_a_loop
async def test_text_delivery_failure_uses_fanout_loss_policy(monkeypatch, failure):
    wire(monkeypatch, [])
    monkeypatch.setenv("ROOM_RECONNECT_LIMIT", "0")
    h = await Room(monkeypatch, model=CASCADE).open()
    bad = h.rt("dan")

    async def fail(text):
        if failure == "exception":
            raise RuntimeError("test delivery failure")
        if failure == "counter":
            bad.send_failures += 1
        else:
            bad.ws = None

    monkeypatch.setattr(bad, "inject_text", fail)
    try:
        await h.participant(WORDS)
        await until(lambda: "dan" in h.room.lost)
        assert h.room.session_for("dan") is None
        assert h.room.hearing.delivery(bad).cursor == 0
        assert await h.room.give_floor("dan") is None
    finally:
        await h.close()


@in_a_loop
async def test_rollback_freezes_all_room_behavior_until_next_room(monkeypatch):
    wire(monkeypatch, [])
    h = await Room(monkeypatch, model=CASCADE).open()
    try:
        monkeypatch.setenv("CASCADE_ROOM_TEXT_RELAY", "0")
        await h.room.hear(LOUD)
        assert h.room.hearing is not None
        assert all(not rt.ws._audio for rt in h.room.sessions.values())
        await h.runner._open_room()
        h.runner.rt = h.room.session_for("dan")
        assert getattr(h.room, "hearing", None) is None
        assert h.runner._second_transcript_source()
        await h.room.hear(LOUD)
        assert all(len(rt.ws._audio) == len(LOUD) for rt in h.room.sessions.values())
        assert all(ROOM_RULE_CASCADE not in rt.ws.instructions for rt in h.room.sessions.values())
        await h.participant(WORDS)
        assert all(WORDS not in [i["text"] for i in rt.ws.items] for rt in h.room.sessions.values())
        await h.room.give_floor("dan")
        await until(lambda: bool(h.rt("dan").ws.items))
        assert gr._TEXT_GRANT_NUDGE in [i["text"] for i in h.rt("dan").ws.items]
        # Rollback rebuilds use the old recap, not durable participant items.
        monkeypatch.setattr(h.runner, "_member_stream_ended", lambda *a, **kw: None)
        old = h.room.sessions.pop("priya")
        await h.runner._rebuild_member("priya", old, "test")
        assert h.of("room_member_reconnected")[-1]["seed_route"] == "brief_recap"
        assert WORDS not in [i["text"] for i in h.rt("priya").ws.items]
        assert WORDS in h.rt("priya").ws.instructions
    finally:
        await h.close()


@pytest.mark.parametrize("model", [GPT, NATIVE, "nto.gemini-live-2.5-flash"])
@in_a_loop
async def test_other_families_frames_are_unchanged_by_relay_switch(monkeypatch, model):
    snapshots = []
    monkeypatch.setenv("ROOM_GRANT_UNANSWERED_S", "0.01")
    monkeypatch.setenv("AUTOFIRE_WAIT", "0")
    for mode in ("0", "1"):
        monkeypatch.setenv("CASCADE_ROOM_TEXT_RELAY", mode)
        h = await Room(monkeypatch, model=model).open()
        try:
            assert getattr(h.room, "hearing", None) is None
            await h.room.hear(LOUD)
            await h.participant(WORDS)
            await h.room.tell("Priya", "The final plan is ready", exclude="priya")
            await h.room.give_floor("dan")
            frames = {a: rt.ws.sent for a, rt in h.room.sessions.items()}
            snapshots.append(json.dumps(frames, sort_keys=True))
        finally:
            await h.close()
    assert snapshots[0] == snapshots[1]
    # Full wire-frame snapshots taken on e949063, before the hearing fix.
    # Re-taken 2026-11-14 for the S4A brief changes (teams and dates, the
    # pilot beat): the room in this test is S4A, so its briefs are in the
    # frames. Both relay modes still produce identical frames, above.
    before = {
        GPT: "0c1fd597ca5f519a4e2a3cfb2ed9dd986ba13bb9234df01e8d5fe96c43a6c3c5",
        NATIVE: "57574818ea8945d2529a1c916e79cc47288c28a27d335c5c3415201f9c9105b1",
        "nto.gemini-live-2.5-flash": "e050c9d91d2725eb9d578df3f11585a6726d1815f25b4cbb8270b916145f0b42",
    }
    if R.audioop is None:
        # The pure-Python converter starts with one extra PCM sample. These
        # hashes are re-taken the same way, with the fallback forced.
        before.update({
            GPT: "03be87f05c31d0ec54e13e13035dee6285418e567a4242a2aaacbd2e3e6ca7af",
            NATIVE: "efb3b7647a99bf0b6471f5ec43af6192fa118442841cae50796f7c8de876e64a",
        })
    assert hashlib.sha256(snapshots[1].encode()).hexdigest() == before[model]


@in_a_loop
async def test_cascade_one_to_one_request_snapshot_is_unchanged(monkeypatch):
    from test_cascade_voice import _drive
    snapshots = []
    for mode in ("0", "1"):
        with monkeypatch.context() as patch:
            patch.setenv("CASCADE_ROOM_TEXT_RELAY", mode)
            seen, events = await _drive(patch)
            snapshots.append({"stt": seen["stt"], "chat": seen["chat"], "tts": seen["tts"]})
            assert any(e["type"] == "user_transcript" for e in events)
    assert snapshots[0] == snapshots[1]
    before = "fda965ca27308d39897bb553a7730a8527b65447731f649e4cd6d18358ecd873"
    assert hashlib.sha256(json.dumps(snapshots[1], sort_keys=True).encode()).hexdigest() == before


@in_a_loop
async def test_suppression_during_briefing_prevents_a_grant(monkeypatch):
    gate, brief, in_brief = asyncio.Event(), asyncio.Event(), asyncio.Event()
    chats, _ = wire(monkeypatch, [Transcript(), Transcript(".", gate)])
    h = await Room(monkeypatch, model=CASCADE).open()
    try:
        await speak(h)
        await until(lambda: len(chats) == 1)
        await finished(h)
        monkeypatch.setattr(h.runner, "_next_trigger", lambda: None)

        async def briefing(aid):
            in_brief.set()
            await brief.wait()

        monkeypatch.setattr(h.runner, "_brief_member", briefing)
        await speak(h)
        await in_brief.wait()
        gate.set()
        await until(lambda: bool(h.of("user_turn_suppressed")))
        brief.set()
        await finished(h)
        assert len(chats) == 1
        assert h.of("group_turn_skipped")[-1]["reason"] == "transcript_suppressed"
    finally:
        gate.set()
        brief.set()
        await h.close()


@in_a_loop
async def test_participant_barge_in_keeps_both_lines_in_next_request(monkeypatch):
    from test_cascade_voice import FakeTTS
    resume, closing = asyncio.Event(), asyncio.Event()
    chats, _ = wire(monkeypatch, [Transcript(), Transcript(WORDS2)])
    monkeypatch.setenv("ROOM_SPLIT_TURN_S", "0")
    opened = 0

    class PausedTTS(FakeTTS):
        async def send(self, raw):
            if json.loads(raw).get("close_socket"):
                closing.set()
                await resume.wait()
            await super().send(raw)

    async def opener():
        nonlocal opened
        opened += 1
        return PausedTTS({}) if opened == 1 else FakeTTS({})

    monkeypatch.setattr(cascade, "open_tts", opener)
    h = await Room(monkeypatch, model=CASCADE).open()
    try:
        await speak(h)
        await closing.wait()
        await until(lambda: h.runner._floor_reply_began)
        assert h.room.speaking == "dan"
        await speak(h)
        await until(lambda: len(chats) == 2)
        await finished(h)
        assert_heard(chats[0], [WORDS])
        assert_heard(chats[1], [WORDS, WORDS2])
        assert any(e.get("interrupted") for e in h.of("assistant_turn"))
        assert all(not rt.ws._audio for rt in h.room.sessions.values())
    finally:
        resume.set()
        await h.close()


@in_a_loop
async def test_lost_scribe_without_a_commit_requests_clarification(monkeypatch):
    chats, _ = wire(monkeypatch, [Transcript()])
    h = await Room(monkeypatch, model=CASCADE).open()
    try:
        await speak(h)
        await until(lambda: len(chats) == 1)
        await finished(h)
        scribe, h.room.scribe = h.room.scribe, None
        await speak(h)
        await until(lambda: len(chats) == 2)
        await finished(h)
        assert CASCADE_ROOM_UNHEARD_NUDGE in user_lines(chats[1])
        assert len(h.of("user_turn")) == 1
        await scribe.close()
    finally:
        await h.close()


@pytest.mark.parametrize("clarified", [False, True])
@in_a_loop
async def test_probe_grant_keeps_old_nudge_after_any_participant_turn(monkeypatch, clarified):
    scripts = [Transcript()] + ([Transcript("", fail=True)] if clarified else [])
    chats, _ = wire(monkeypatch, scripts)
    h = await Room(monkeypatch, model=CASCADE).open()
    try:
        for n in range(len(scripts)):
            await speak(h)
            await until(lambda: len(chats) == n + 1)
            await finished(h)
        turn = h.room.hearing.active_turn
        assert turn.clarification is clarified
        pending = h.runner._next_trigger()
        assert pending and pending.get("on_silence")
        await h.runner._probe_room()
        await until(lambda: len(chats) == len(scripts) + 1)
        assert user_lines(chats[-1])[-1] == gr._TEXT_GRANT_NUDGE
        assert "gone quiet" in chats[-1]["messages"][0]["content"]
        assert h.of("trigger_fired")[-1]["probing"]
        assert turn.clarification is clarified
    finally:
        await h.close()


@pytest.mark.parametrize("outcome", ["failed", "timeout"])
@in_a_loop
async def test_clarification_returns_beat_without_recording_it_fired(monkeypatch, outcome):
    gate = asyncio.Event() if outcome == "timeout" else None
    scripts = [Transcript(), Transcript("", gate, fail=outcome == "failed"),
               Transcript("What about the budget for the pilot")]
    chats, _ = wire(monkeypatch, scripts)
    h = await Room(monkeypatch, model=CASCADE).open()
    notices = []

    async def broadcast(payload):
        notices.append(payload)

    monkeypatch.setattr(h.session, "broadcast", broadcast)
    try:
        await speak(h)
        await until(lambda: len(chats) == 1)
        await finished(h)
        pending = h.runner._next_trigger()
        owner = h.runner._trigger_agent(pending)
        assert owner
        h.runner.director = ScriptedDirector([{"agent_id": owner}])
        h.runner._last_group_speaker = None
        before = list(h.of("trigger_fired"))
        index, fired = h.runner._trigger_idx, list(h.runner._fired)
        await speak(h)
        await until(lambda: len(chats) == 2)
        await finished(h)
        assert user_lines(chats[-1])[-1] == CASCADE_ROOM_UNHEARD_NUDGE
        assert h.of("trigger_fired") == before
        assert h.runner._trigger_idx == index and h.runner._fired == fired
        assert pending["cue"] not in chats[-1]["messages"][0]["content"]
        assert h.of("trigger_deferred")[-1]["reason"] == "transcript_missing"
        assert any(e.get("kind") == "trigger_undelivered" and e.get("severity") == "info"
                   and "transcript_missing" in (e.get("detail") or "") for e in notices)
        assert h.runner._pending_direction is None
        from server.verify_record import _net_fired
        assert _net_fired(h.session.store.events) == before
        h.runner._last_group_speaker = None
        await speak(h)
        await until(lambda: len(chats) == 3)
        await finished(h)
        assert user_lines(chats[-1])[-1] == CASCADE_ROOM_GRANT_NUDGE
        assert h.of("trigger_fired")[-1]["trigger_id"] == pending["id"]
        assert len(_net_fired(h.session.store.events)) == len(before) + 1
    finally:
        await h.close()


@in_a_loop
async def test_clarification_mode_stays_frozen_during_the_clean_rebrief(monkeypatch):
    stt, brief, in_brief = asyncio.Event(), asyncio.Event(), asyncio.Event()
    chats, _ = wire(monkeypatch, [Transcript(), Transcript(WORDS2, stt)])
    h = await Room(monkeypatch, model=CASCADE).open()
    try:
        await speak(h)
        await until(lambda: len(chats) == 1)
        await finished(h)
        original = h.runner._deliver_brief

        async def deliver(rt, instructions):
            if h.room.hearing.active_turn.clarification:
                in_brief.set()
                await brief.wait()
            return await original(rt, instructions)

        monkeypatch.setattr(h.runner, "_deliver_brief", deliver)
        await speak(h)
        await in_brief.wait()
        stt.set()
        await until(lambda: len(h.of("user_turn")) == 2)
        brief.set()
        await until(lambda: len(chats) == 2)
        await finished(h)
        assert h.room.hearing.active_turn.clarification
        assert user_lines(chats[-1])[-1] == CASCADE_ROOM_UNHEARD_NUDGE
    finally:
        stt.set()
        brief.set()
        await h.close()


@in_a_loop
async def test_later_deadline_releases_an_orphan_from_the_previous_turn(monkeypatch):
    first, orphan, paused, resume = (asyncio.Event() for _ in range(4))
    chats, _ = wire(monkeypatch, [Transcript(WORDS, first), Transcript(WORDS2, orphan),
                                  Transcript(WORDS2)])
    h = await Room(monkeypatch, model=CASCADE).open()
    hearing = h.room.hearing
    original = h.runner._release_room_transcripts

    async def release(room, **kw):
        if (hearing.commits.get(1) and hearing.commits[1].outcome == "missing"
                and hearing.released == 0 and not paused.is_set()):
            paused.set()
            await resume.wait()
        await original(room, **kw)

    monkeypatch.setattr(h.runner, "_release_room_transcripts", release)
    try:
        await speak(h)
        await paused.wait()
        await speak(h)
        assert len(h.of("turn_end_withdrawn")) == 1
        resume.set()
        await until(lambda: len(chats) == 1)
        await finished(h)
        first.set()
        await until(lambda: len(h.of("user_turn")) == 1)
        await speak(h)
        await until(lambda: len(chats) == 2)
        await finished(h)
        assert hearing.commits[2].outcome == "missing"
        assert hearing.commits[3].outcome == "accepted"
        assert hearing.released == 3
        assert [e["text"] for e in h.of("user_turn")] == [WORDS, WORDS2]
        assert user_lines(chats[-1])[-1] == CASCADE_ROOM_GRANT_NUDGE
    finally:
        resume.set()
        await h.close()


@pytest.mark.parametrize("closure", ["reopen", "deaf"])
@in_a_loop
async def test_scribe_closure_releases_its_inflight_slots(monkeypatch, closure):
    gate = asyncio.Event()
    scripts = [Transcript(WORDS, gate), Transcript(WORDS2)]
    chats, _ = wire(monkeypatch, scripts)
    h = await Room(monkeypatch, model=CASCADE).open()
    try:
        await speak(h)
        await scripts[0].started.wait()
        old = h.room.scribe
        assert h.room.hearing.commits[1].scribe == id(old)
        if closure == "deaf":
            await h.room._went_deaf(gr.SCRIBE_ID, old, "test closure")
            assert h.room.hearing.commits[1].outcome == "missing"
        new = await h.room.reopen_scribe()
        assert h.room.hearing.commits[1].outcome == "missing"
        if h.runner._scribe_pump is not None:
            h.runner._scribe_pump.cancel()
        h.runner._scribe_pump = asyncio.create_task(h.runner._pump_scribe(new))
        h.runner._pumps.append(h.runner._scribe_pump)
        await until(lambda: len(chats) == 1)
        await finished(h)
        await speak(h)
        await until(lambda: len(chats) == 2)
        await finished(h)
        assert h.room.hearing.commits[2].scribe == id(new)
        assert h.room.hearing.commits[2].outcome == "accepted"
        assert user_lines(chats[-1])[-1] == CASCADE_ROOM_GRANT_NUDGE
        assert_heard(chats[-1], [WORDS2])
    finally:
        await h.close()


@pytest.mark.parametrize("trailing", ["empty", "no_speech", "failed", "timeout"])
@in_a_loop
async def test_trailing_empty_keeps_accepted_turn_but_errors_still_clarify(monkeypatch, trailing):
    first = asyncio.Event()
    last = asyncio.Event() if trailing == "timeout" else None
    scripts = [Transcript(WORDS2, first), Transcript("", last, fail=trailing == "failed")]
    chats, _ = wire(monkeypatch, scripts)
    if trailing == "empty":
        # The 12a HTTP parser normalizes an empty hearing to [no speech].
        # Exercise a genuinely empty bridge event separately from that path.
        original_hear = cascade.CascadeSocket._hear

        async def empty_hearing(self, pcm):
            text = await original_hear(self, pcm)
            return "" if text == "[no speech]" else text

        monkeypatch.setattr(cascade.CascadeSocket, "_hear", empty_hearing)
    h = await Room(monkeypatch, model=CASCADE).open()
    try:
        await speak(h)
        await speak(h)
        first.set()
        await until(lambda: len(chats) == 1)
        await finished(h)
        assert [e["text"] for e in h.of("user_turn")] == [WORDS2]
        assert_heard(chats[0], [WORDS2])
        expected = (CASCADE_ROOM_GRANT_NUDGE if trailing in ("empty", "no_speech")
                    else CASCADE_ROOM_UNHEARD_NUDGE)
        assert user_lines(chats[0])[-1] == expected
        assert h.room.hearing.commits[2].outcome == {
            "empty": "empty", "no_speech": "suppressed", "failed": "failed", "timeout": "missing"}[trailing]
    finally:
        await h.close()


@pytest.mark.parametrize("next_line", ["What about the budget for the pilot", WORDS2])
@in_a_loop
async def test_pregrant_next_turn_is_merged_and_its_task_is_consumed(monkeypatch, next_line):
    chats, _ = wire(monkeypatch, [Transcript(), Transcript(next_line)])
    h = await Room(monkeypatch, model=CASCADE).open()
    in_brief, resume = asyncio.Event(), asyncio.Event()
    original = h.runner._brief_member
    paused = False

    async def brief(aid, **kw):
        nonlocal paused
        if not paused:
            paused = True
            in_brief.set()
            await resume.wait()
        await original(aid, **kw)

    monkeypatch.setattr(h.runner, "_brief_member", brief)
    try:
        await speak(h)
        await in_brief.wait()
        await speak(h)
        await until(lambda: len(h.of("user_turn")) == 2)
        newer = h.room.hearing.pending_turn
        assert newer is not h.room.hearing.active_turn
        resume.set()
        await until(lambda: len(chats) == 1)
        await finished(h)
        assert len(chats) == len(h.of("director_route")) == 1
        assert newer.consumed
        assert h.room.hearing.active_turn.commits == [1, 2]
        assert_heard(chats[0], [WORDS, next_line])
        assert user_lines(chats[0])[-1] == CASCADE_ROOM_GRANT_NUDGE
        if next_line == WORDS2:
            assert h.of("director_route")[-1]["speakers"] == ["priya"]
    finally:
        resume.set()
        await h.close()


def test_room_hearing_pipeline_version_follows_the_other_november_changes():
    import inspect
    from server import llm
    # At or after the version that introduced it: a later change (11-13a, the
    # steering budget) bumps the version without undoing this one.
    assert llm.PIPELINE_VERSION >= "2026-11-12d"
    source = inspect.getsource(llm)
    assert [source.index("#   2026-11-12" + suffix) for suffix in "abcd"] == sorted(
        source.index("#   2026-11-12" + suffix) for suffix in "abcd")


@pytest.mark.parametrize("relay", [False, True])
@in_a_loop
async def test_provenance_records_the_frozen_room_mode_across_switch_changes(monkeypatch, relay):
    from server import llm
    wire(monkeypatch, [])
    monkeypatch.setenv("CASCADE_ROOM_TEXT_RELAY", "1" if relay else "0")
    h = Room(monkeypatch, model=CASCADE)

    async def switch_after_open():
        monkeypatch.setenv("CASCADE_ROOM_TEXT_RELAY", "0" if relay else "1")

    monkeypatch.setattr(h.runner, "_announce_opening", switch_after_open)
    await h.runner.run()
    for gate in (h.of("group_room_opened")[-1]["turn_gate"],
                 h.of("realtime_session_started")[-1]["turn_gate"]):
        assert gate["room_participant_text_relay"] is relay
        assert gate["room_dedupe_second_source"] is not relay
    prov = llm.provenance(CASCADE, room=True, room_text_relay=relay)["turn_gate"]
    assert prov["room_dedupe_second_source"] is not relay


@pytest.mark.parametrize("phase", ["seed", "catchup"])
@pytest.mark.parametrize("all_gone", [False, True])
@in_a_loop
async def test_failed_relay_seed_uses_member_loss_records(monkeypatch, phase, all_gone):
    chats, _ = wire(monkeypatch, [Transcript()])
    h = await Room(monkeypatch, model=CASCADE).open()
    seeded, resume = asyncio.Event(), asyncio.Event()
    original_reopen, original_seed = h.room.reopen_member, h.runner._seed_member
    replacements = []

    async def reopen(*a, **kw):
        new = await original_reopen(*a, **kw)
        replacements.append(new)
        send = new.seed_item

        async def fail(role, text):
            if text == (WORDS if phase == "seed" else WORDS2):
                return None
            return await send(role, text)

        monkeypatch.setattr(new, "seed_item", fail)
        return new

    async def seed(*a, **kw):
        out = await original_seed(*a, **kw)
        if phase == "catchup" and not seeded.is_set():
            seeded.set()
            await resume.wait()
        return out

    monkeypatch.setattr(h.room, "reopen_member", reopen)
    monkeypatch.setattr(h.runner, "_seed_member", seed)
    monkeypatch.setattr(h.runner, "_member_stream_ended", lambda *a, **kw: None)
    try:
        await speak(h)
        await until(lambda: len(chats) == 1)
        await finished(h)
        old = h.room.sessions.pop("priya")
        if all_gone:
            for aid in list(h.room.sessions):
                await h.room.sessions.pop(aid).close()
        rebuild = asyncio.create_task(h.runner._rebuild_member("priya", old, "test"))
        if phase == "catchup":
            await seeded.wait()
            await h.participant(WORDS2)
            resume.set()
        await rebuild
        assert h.room.session_for("priya") is None
        assert replacements[0].ws is None
        assert h.of("room_member_lost")[-1]["reason"] == "seed_failed"
        assert not h.of("room_member_gone")
        assert not h.of("room_member_reconnected")
        assert bool(h.of("room_members_lost")) is all_gone
        assert await h.room.give_floor("priya") is None
    finally:
        resume.set()
        await h.close()


@pytest.mark.parametrize("closed", [False, True])
@in_a_loop
async def test_room_close_records_held_transcripts_in_spoken_order(monkeypatch, closed):
    first = asyncio.Event()
    scripts = [Transcript(WORDS, first), Transcript(WORDS), Transcript(WORDS2)]
    chats, _ = wire(monkeypatch, scripts)
    h = await Room(monkeypatch, model=CASCADE).open()
    try:
        await speak(h)
        await scripts[0].started.wait()
        await speak(h)
        await speak(h)
        await scripts[2].finished.wait()
        await until(lambda: h.room.hearing.commits[2].event is not None
                    and h.room.hearing.commits[3].event is not None)
        assert not h.of("user_turn")
        h.runner._closed = closed
        await h.runner._close_room()
        assert h.runner.room is None
        assert [e["text"] for e in h.of("user_turn")] == [WORDS, WORDS2]
        assert [e["text"] for e in h.runner._room_heard] == [WORDS, WORDS2]
        assert not chats
    finally:
        await h.close()


@in_a_loop
async def test_hearing_task_exception_is_observed_in_the_record(monkeypatch):
    wire(monkeypatch, [])
    h = await Room(monkeypatch, model=CASCADE).open()
    unhandled = []
    loop = asyncio.get_running_loop()
    handler = loop.get_exception_handler()
    loop.set_exception_handler(lambda loop, context: unhandled.append(context))

    async def fail():
        raise RuntimeError("test hearing task")

    try:
        h.room.hearing.spawn(fail())
        await until(lambda: any(e.get("where") == "room_hearing" for e in h.of("voice_error")))
        assert h.of("voice_error")[-1]["message"] == "RuntimeError: test hearing task"
        assert not h.room.hearing.tasks
        assert not unhandled
    finally:
        loop.set_exception_handler(handler)
        await h.close()


@in_a_loop
async def test_standalone_hearing_task_exception_is_logged(caplog):
    from server.room_hearing import RoomHearing
    hearing = RoomHearing()

    async def fail():
        raise RuntimeError("test standalone hearing task")

    hearing.spawn(fail())
    await until(lambda: not hearing.tasks)
    assert any(record.name == "server.room_hearing"
               and record.getMessage() == "Room hearing task failed"
               and record.exc_info[0] is RuntimeError for record in caplog.records)
    await hearing.close()


@in_a_loop
async def test_consumed_task_answers_a_line_during_the_merged_reply(monkeypatch):
    two = "What about the budget for the pilot"
    three = "And who owns the rollout after that"
    chats, _ = wire(monkeypatch, [Transcript(), Transcript(two), Transcript(three)])
    h = await Room(monkeypatch, model=CASCADE).open()
    in_brief, resume, in_grant, resume_grant = (asyncio.Event() for _ in range(4))
    original_brief, original_floor = h.runner._brief_member, h.room.give_floor
    brief_paused = grant_paused = False

    async def brief(aid, **kw):
        nonlocal brief_paused
        if not brief_paused:
            brief_paused = True
            in_brief.set()
            await resume.wait()
        await original_brief(aid, **kw)

    async def grant(aid, **kw):
        nonlocal grant_paused
        result = await original_floor(aid, **kw)
        if not grant_paused:
            grant_paused = True
            in_grant.set()
            await resume_grant.wait()
        return result

    monkeypatch.setattr(h.runner, "_brief_member", brief)
    monkeypatch.setattr(h.room, "give_floor", grant)
    try:
        await speak(h)
        await in_brief.wait()
        await speak(h)
        await until(lambda: len(h.of("user_turn")) == 2)
        consumed = h.room.hearing.pending_turn
        resume.set()
        await in_grant.wait()
        assert consumed.consumed
        await speak(h)
        await until(lambda: len(h.of("user_turn")) == 3)
        assert h.room.hearing.pending_turn.commits == [3]
        assert h.of("turn_end_withdrawn")
        resume_grant.set()
        await until(lambda: len(chats) == 2)
        await finished(h)
        assert len(chats) == 2
        assert_heard(chats[0], [WORDS, two])
        assert three not in user_lines(chats[0])
        assert_heard(chats[1], [WORDS, two, three])
        assert user_lines(chats[1])[-1] == CASCADE_ROOM_GRANT_NUDGE
    finally:
        resume.set()
        resume_grant.set()
        await h.close()


@pytest.mark.parametrize("colleague_before_grant", [False, True])
@in_a_loop
async def test_merge_leaves_the_untranscribed_tail_for_its_waiting_task(monkeypatch, colleague_before_grant):
    two = "What about the budget for the pilot"
    three = "And who owns the rollout after that"
    gate, in_brief, resume = (asyncio.Event() for _ in range(3))
    scripts = [Transcript(), Transcript(two), Transcript(three, gate)]
    chats, _ = wire(monkeypatch, scripts)
    h = await Room(monkeypatch, model=CASCADE).open()
    original = h.runner._brief_member
    paused = False
    give_floor = h.room.give_floor
    grants = 0
    colleague_line = "The budget estimate is ready."

    async def grant(aid, **kw):
        nonlocal grants
        grants += 1
        if grants == 2 and colleague_before_grant:
            await h.room.deliver_participant(aid, h.room.session_for(aid))
            await h.room.tell("Dan", colleague_line)
        return await give_floor(aid, **kw)

    async def brief(aid, **kw):
        nonlocal paused
        if not paused:
            paused = True
            in_brief.set()
            await resume.wait()
        await original(aid, **kw)

    monkeypatch.setattr(h.runner, "_brief_member", brief)
    monkeypatch.setattr(h.room, "give_floor", grant)
    monkeypatch.setenv("ROUTE_TRANSCRIPT_WAIT", "3")
    try:
        await speak(h)
        await in_brief.wait()
        await speak(h)
        await until(lambda: len(h.of("user_turn")) == 2)
        newer = h.room.hearing.pending_turn
        await speak(h)
        await scripts[2].started.wait()
        assert newer.commits == [2, 3]
        resume.set()
        await until(lambda: len(chats) == 1)
        first = h.room.hearing.commits[1].turn
        assert first.commits == [1, 2]
        assert not first.clarification
        assert newer.commits == [3] and not newer.consumed
        assert h.room.hearing.commits[3].turn is newer
        assert user_lines(chats[0])[-1] == CASCADE_ROOM_GRANT_NUDGE
        gate.set()
        await until(lambda: len(chats) == 2)
        await finished(h)
        assert len(chats) == 2
        assert_heard(chats[0], [WORDS, two])
        assert three not in user_lines(chats[0])
        assert_heard(chats[1], [WORDS, two, three])
        users = user_lines(chats[1])
        assert users[-1] == CASCADE_ROOM_GRANT_NUDGE
        if colleague_before_grant:
            note = h.room._told_note("Dan", colleague_line)
            assert users.count(note) == 1
            assert users.index(three) < users.index(note) < len(users) - 1
        assert not any(e.get("reason") == "transcript_missing" for e in h.of("trigger_deferred"))
    finally:
        gate.set()
        resume.set()
        await h.close()


@in_a_loop
async def test_request_snapshot_precedes_join_of_a_cancelling_reply(monkeypatch):
    chats, _ = wire(monkeypatch, [])
    h = await Room(monkeypatch, model=CASCADE).open()
    started, cancelling, release, snapshot = (asyncio.Event() for _ in range(4))
    creating = None
    old = None
    try:
        await h.participant(WORDS)
        rt = h.rt("dan")
        await h.room.deliver_participant("dan", rt)
        sock = rt.ws
        h.room.speaking = "dan"
        monkeypatch.setattr(cascade, "REPLY_CANCEL_TIMEOUT_S", 5)

        async def prior_reply():
            started.set()
            try:
                await asyncio.Event().wait()
            finally:
                cancelling.set()
                await release.wait()

        old = asyncio.create_task(prior_reply())
        sock._reply, sock._reply_id = old, "prior_cancelled_reply"
        await started.wait()
        await sock._on_response_cancel({})
        await cancelling.wait()
        original_messages = sock._messages

        def messages():
            captured = original_messages()
            snapshot.set()
            return captured

        monkeypatch.setattr(sock, "_messages", messages)
        # Drive the member bridge directly so the acceptance producer can
        # deliver context while create is blocked in the adapter's join.
        creating = asyncio.create_task(rt.request_response())
        await asyncio.wait_for(snapshot.wait(), 1)
        assert sock._create_lock.locked() and not creating.done()
        await h.participant(WORDS2)
        await until(lambda: any(i["text"] == WORDS2 for i in sock.items))
        release.set()
        await creating
        await until(lambda: len(chats) == 1 and sock._reply.done())
        assert_heard(chats[0], [WORDS])
        assert WORDS2 not in user_lines(chats[0])
        await settle(h.runner)
        rt.clear_response_state()
        await rt.request_response()
        await until(lambda: len(chats) == 2)
        assert_heard(chats[1], [WORDS, WORDS2])
        assert not sock._audio
    finally:
        release.set()
        if creating is not None:
            await asyncio.gather(creating, return_exceptions=True)
        if old is not None:
            await asyncio.gather(old, return_exceptions=True)
        await h.close()


@in_a_loop
async def test_cancelling_a_brief_clears_its_provisional_firing(monkeypatch):
    chats, _ = wire(monkeypatch, [Transcript()])
    h = await Room(monkeypatch, model=CASCADE).open()
    briefed, hold = asyncio.Event(), asyncio.Event()
    original = h.runner._brief_member

    async def brief(aid, **kw):
        await original(aid, **kw)
        briefed.set()
        await hold.wait()

    monkeypatch.setattr(h.runner, "_brief_member", brief)
    try:
        await speak(h)
        await briefed.wait()
        turn = h.room.hearing.active_turn
        assert turn.trigger_fired is not None and not h.of("trigger_fired")
        h.runner._cancel_group_turns()
        await settle(h.runner)
        assert turn.trigger_fired is None
        assert not chats
    finally:
        hold.set()
        await h.close()


@pytest.mark.parametrize("probing,offset", [(True, 0), (False, 1)])
@in_a_loop
async def test_a_stale_provisional_firing_cannot_hide_a_real_retraction(monkeypatch, probing, offset):
    from server.room_hearing import HearingTurn
    from server.verify_record import _net_fired
    wire(monkeypatch, [])
    h = await Room(monkeypatch, model=CASCADE).open()
    notices = []

    async def broadcast(payload):
        notices.append(payload)

    monkeypatch.setattr(h.session, "broadcast", broadcast)
    try:
        turn = h.room.hearing.active_turn = HearingTurn()
        index, count = h.runner._trigger_idx, len(h.runner._fired)
        async with h.runner._floor:
            await h.runner._brief_member("dan", probing=True)
            assert h.of("trigger_fired")
            turn.trigger_fired = {**h.of("trigger_fired")[-1], "index": index + offset}
            await h.runner._give_beat_back("dan", index, count,
                                          probing=probing, reason="member_lost")
        assert h.of("trigger_undelivered")[-1]["reason"] == "member_lost"
        assert not h.of("trigger_deferred")
        assert not _net_fired(h.session.store.events)
        assert turn.trigger_fired is None
        assert notices[-1]["severity"] == "error"
    finally:
        await h.close()


@pytest.mark.parametrize("duplicate_task", [False, True])
@in_a_loop
async def test_m4_one_turn_served_by_two_tasks(monkeypatch, duplicate_task):
    """A finishing task must not reopen the pending turn's spawn window.

    The second case also queues a stale capture explicitly, so the floor-entry
    guard is tested independently of the waiting-flag repair.
    """
    from collections import Counter
    two = "What about the budget for the pilot"
    three = "And who owns the rollout after that"
    gate, in_grant, resume_grant = (asyncio.Event() for _ in range(3))
    scripts = [Transcript(), Transcript(two, gate), Transcript(three)]
    chats, _ = wire(monkeypatch, scripts)
    h = await Room(monkeypatch, model=CASCADE).open()
    original_floor = h.room.give_floor
    grants = []

    async def give_floor(aid, **kw):
        turn = h.room.hearing.active_turn
        grants.append((aid, tuple(turn.commits)))
        result = await original_floor(aid, **kw)
        if len(grants) == 1:
            in_grant.set()
            await resume_grant.wait()
        return result

    monkeypatch.setattr(h.room, "give_floor", give_floor)
    try:
        await speak(h)
        await in_grant.wait()
        await speak(h)
        await scripts[1].started.wait()
        assert h.runner._group_turn_waiting
        assert len(h.runner._group_turn_tasks) == 2
        resume_grant.set()
        await until(lambda: len(h.runner._group_turn_tasks) == 1, 10)
        waiting = h.runner._group_turn_waiting
        pending = h.room.hearing.pending_turn
        if duplicate_task:
            h.runner._spawn_group_turn(h.runner._run_group_turn(text_turn=pending))
        await speak(h)
        await scripts[2].finished.wait()
        withdrawn = len(h.of("turn_end_withdrawn"))
        gate.set()
        await until(lambda: len(chats) >= 2)
        await finished(h)
        assert len(chats) == 2, (grants, waiting, withdrawn)
        assert Counter(turn for _, turn in grants) == {(1,): 1, (2, 3): 1}
        assert [route["speakers"] for route in h.of("director_route")] == [["dan"], ["priya"]]
        deferred = Counter((e["interaction"], e["index"], e["trigger_id"])
                           for e in h.of("trigger_deferred"))
        assert len(deferred) == 1 and list(deferred.values()) == [1]
        assert waiting and withdrawn == 1
        assert_heard(chats[0], [WORDS])
        assert_heard(chats[1], [WORDS, two, three])
        assert user_lines(chats[1])[-1] == CASCADE_ROOM_GRANT_NUDGE
    finally:
        resume_grant.set()
        gate.set()
        await h.close()
