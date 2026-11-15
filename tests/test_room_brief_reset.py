"""#216: a room member's DIRECTOR NOTE leaves its brief once the line it was
for is finalized; #221: the room wording names "the participant"."""
import asyncio

from server import realtime_voice_session as rvs
from cascade_room_harness import CASCADE, Transcript, wire, speak, finished
from test_room_member_reconnect import (Room, ScriptedDirector, in_a_loop,
                                        until, quick)  # noqa: F401
from test_bridge_correctness import GPT, settle

NOTE = "DIRECTOR NOTE"
CUE = "Say the write-up is yours and you will take it to leadership in person."
TO_DAN = "Dan, what do you make of the rollout timeline so far"


def _one_beat_for_dan(h):
    beat = {"id": "t_test", "agent": "dan", "cue": CUE, "esci": []}
    h.runner._triggers = lambda: [beat]


def _system(body):
    return next(m["content"] for m in body["messages"] if m["role"] == "system")


@in_a_loop
async def test_cascade_member_drops_the_note_after_its_directed_line(monkeypatch):
    monkeypatch.setenv("FOLLOWUP_GAP_S", "-1")
    chats, _ = wire(monkeypatch, [Transcript(), Transcript(TO_DAN)])
    h = Room(monkeypatch, model=CASCADE)
    h.runner.director = h.session.director = ScriptedDirector([])
    await h.open()
    _one_beat_for_dan(h)
    try:
        await speak(h)
        await until(lambda: len(chats) >= 1)
        await finished(h)
        assert h.of("director_route")[0]["speakers"][0] == "dan"
        assert NOTE in _system(chats[0]) and CUE in _system(chats[0])
        n = len(chats)
        # Dan again, by name, on a turn with no beat of his.
        await speak(h)
        await until(lambda: len(chats) > n)
        await finished(h)
        assert h.of("director_route")[1]["speakers"][0] == "dan"
        assert NOTE not in _system(chats[n])
        assert CUE not in _system(chats[n])
        assert [e["trigger_id"] for e in h.of("trigger_fired")] == ["t_test"]
        reset = h.of("brief_reset")[0]
        assert reset["agent_id"] == "dan" and reset["trigger_id"] == "t_test"
        assert reset["delivered"] is True and reset["skipped"] is None
        assert reset["acked"] is True
        # Its grant on the second turn did not have to wait for it.
        assert not h.of("brief_reset_awaited")
    finally:
        await h.close()


@in_a_loop
async def test_gpt_member_is_rebriefed_plain_after_the_directed_line(monkeypatch):
    h = await Room(monkeypatch, model=GPT).open()
    _one_beat_for_dan(h)
    try:
        await h.runner._brief_member("dan")
        assert NOTE in h.rt("dan").instructions
        await h.speaks("dan", "r1", "It's mine, I'll take it upstairs myself.")
        await until(lambda: bool(h.of("brief_reset")))
        assert NOTE not in h.rt("dan").instructions
        assert h.rt("dan").instructions == h.runner._instructions_for(h.agents["dan"])
        pair = [p for p in h.of("steering_pair") if p["actor"]["agent_id"] == "dan"][-1]
        assert pair["direction"]["trigger_id"] == "t_test"
        # A colleague's undirected line resets nobody.
        await h.speaks("priya", "r2", "Fine by me.")
        await settle(h.runner)
        assert [e["agent_id"] for e in h.of("brief_reset")] == ["dan"]
    finally:
        await h.close()


@in_a_loop
async def test_a_grant_waits_for_the_reset_and_a_new_beat_lands_after_it(monkeypatch):
    h = await Room(monkeypatch, model=GPT).open()
    _one_beat_for_dan(h)
    try:
        await h.runner._brief_member("dan")
        rt = h.rt("dan")
        gate, sends = asyncio.Event(), []
        real = rt.update_instructions

        async def slow(instructions):
            sends.append(instructions)
            if len(sends) == 1:
                await gate.wait()
            return await real(instructions)
        monkeypatch.setattr(rt, "update_instructions", slow)
        await h.speaks("dan", "r1", "It's mine, I'll take it upstairs myself.")
        await until(lambda: len(sends) == 1)
        assert NOTE not in sends[0]
        # A second beat for Dan, briefed while the reset is still going out:
        # it waits, and lands after it, so the reset cannot replace it.
        beat2 = {"id": "t_two", "agent": "dan", "cue": "Ask who owns the pilot.",
                 "esci": []}
        h.runner._triggers = lambda: [{"id": "t_test"}, beat2]
        brief = asyncio.ensure_future(h.runner._brief_member("dan"))
        await asyncio.sleep(0.05)
        assert not brief.done() and len(sends) == 1
        gate.set()
        await brief
        assert len(sends) == 2 and "Ask who owns the pilot." in sends[1]
        assert "Ask who owns the pilot." in rt.instructions
        assert h.of("brief_reset_awaited")[-1]["by"] == "brief_member"
        # Nothing outstanding: a grant does not wait at all.
        assert not h.runner._brief_resets
    finally:
        await h.close()


@in_a_loop
async def test_no_reset_frame_to_a_member_mid_reply_or_on_an_inert_family(monkeypatch):
    h = await Room(monkeypatch, model=GPT).open()
    try:
        agent = h.agents["dan"]
        rt = h.rt("dan")
        direction = {"agent_id": "dan", "stage_direction": "x", "trigger_id": "t"}
        monkeypatch.setattr(type(rt), "responding", property(lambda s: True))
        before = rt.instructions = "BRIEF" + NOTE
        await rvs.RealtimeVoiceSessionRunner._reset_member_brief(
            h.runner, h.room, agent, rt, direction)
        assert h.of("brief_reset")[-1]["skipped"] == "response_in_flight"
        assert rt.instructions == before
        monkeypatch.setattr(type(rt), "responding", property(lambda s: False))
        monkeypatch.setattr(type(h.room), "steering_is_real", property(lambda s: False))
        await rvs.RealtimeVoiceSessionRunner._reset_member_brief(
            h.runner, h.room, agent, rt, direction)
        assert h.of("brief_reset")[-1]["skipped"] == "steering_inert"
        assert rt.instructions == before
    finally:
        await h.close()


@in_a_loop
async def test_an_empty_reply_keeps_its_note_for_the_rerequest(monkeypatch):
    h = await Room(monkeypatch, model=GPT).open()
    _one_beat_for_dan(h)
    try:
        await h.runner._brief_member("dan")
        agent = h.agents["dan"]
        h.room.speaking = "dan"
        await h.runner._finalize_member_async(agent, [], False)
        await settle(h.runner)
        assert not h.of("brief_reset")
        assert NOTE in h.rt("dan").instructions
    finally:
        await h.close()


@in_a_loop
async def test_room_wording_names_the_participant(monkeypatch):
    h = await Room(monkeypatch, model=GPT).open()
    try:
        brief = h.runner._instructions_for(h.agents["priya"])
        assert "A voice that is not the participant is one of them" in brief
        assert "running the meeting" not in brief
        first = h.runner._first_room_reply_note("Dan opens the meeting.")
        assert "The participant speaks first." in first
        assert "meeting with" not in first
    finally:
        await h.close()
