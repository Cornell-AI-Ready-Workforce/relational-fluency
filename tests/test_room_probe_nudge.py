"""Pipeline 2026-11-15b: a silence probe's grant on the cascade room route.

The probe's brief says "They have gone quiet"; the grant used to end the
request with _TEXT_GRANT_NUDGE ("The participant is waiting for you to answer
what they just said"), which contradicted it and pulled the reply toward the
newest line in the history (S4A 2026-11-15: t5 probes where Priya answered
Dan's date question). A probe grant, a keep-going grant included, now ends
with _PROBE_GRANT_NUDGE; a grant answering a participant line is unchanged.
Real room, runner and cascade; only HTTP and ElevenLabs are fake.
"""
from server import group_room as gr
from cascade_room_harness import (CASCADE, CASCADE_ROOM_GRANT_NUDGE, Transcript,
                                  wire, speak, finished, user_lines)
from test_room_member_reconnect import Room, in_a_loop, until  # noqa: F401
from test_keep_going import _exhaust

WAITING = "waiting for you to answer"


def _probe_request_ok(body):
    users = user_lines(body)
    assert users[-1] == gr._PROBE_GRANT_NUDGE
    assert WAITING not in users[-1]
    assert "gone quiet" in body["messages"][0]["content"]


@in_a_loop
async def test_a_planted_probe_grant_does_not_say_the_participant_is_waiting(monkeypatch):
    chats, _ = wire(monkeypatch, [Transcript()])
    h = await Room(monkeypatch, model=CASCADE).open()
    try:
        await speak(h)
        await until(lambda: len(chats) == 1)
        await finished(h)
        trig = h.runner._next_trigger()
        assert trig and trig.get("on_silence") and not trig.get("keep_going")
        await h.runner._probe_room()
        await until(lambda: len(chats) == 2)
        _probe_request_ok(chats[-1])
        assert h.of("trigger_fired")[-1]["probing"]
        assert h.room.last_grant["via"] == "text_prompt"
    finally:
        await h.close()


@in_a_loop
async def test_a_keep_going_grant_does_not_say_the_participant_is_waiting(monkeypatch):
    chats, _ = wire(monkeypatch, [Transcript()])
    h = await Room(monkeypatch, model=CASCADE).open()
    try:
        await speak(h)
        await until(lambda: len(chats) == 1)
        await finished(h)
        _exhaust(h.runner)
        await h.runner._probe_room()
        await until(lambda: len(chats) == 2)
        _probe_request_ok(chats[-1])
        (kg,) = h.of("keep_going_probe")
        assert kg["trigger_id"] == "keep_going_1"
    finally:
        await h.close()


@in_a_loop
async def test_a_grant_answering_the_participant_keeps_its_nudge(monkeypatch):
    chats, _ = wire(monkeypatch, [Transcript()])
    h = await Room(monkeypatch, model=CASCADE).open()
    try:
        await speak(h)
        await until(lambda: len(chats) == 1)
        await finished(h)
        assert user_lines(chats[0])[-1] == CASCADE_ROOM_GRANT_NUDGE
        assert gr._PROBE_GRANT_NUDGE not in user_lines(chats[0])
    finally:
        await h.close()


@in_a_loop
async def test_without_room_hearing_a_grant_keeps_the_old_text_nudge(monkeypatch):
    # The CASCADE_ROOM_TEXT_RELAY=0 rollback, and every other text-grant
    # family: no hearing, so no probe flag reaches the room, as before.
    wire(monkeypatch, [])
    monkeypatch.setenv("CASCADE_ROOM_TEXT_RELAY", "0")
    h = await Room(monkeypatch, model=CASCADE).open()
    try:
        assert getattr(h.room, "hearing", None) is None
        await h.room.give_floor("dan", probing=True)
        await until(lambda: bool(h.rt("dan").ws.items))
        texts = [i["text"] for i in h.rt("dan").ws.items]
        assert gr._TEXT_GRANT_NUDGE in texts and gr._PROBE_GRANT_NUDGE not in texts
    finally:
        await h.close()
