"""Regression tests for the 2026-11-04 cascade room hearing audit."""
import asyncio

from cascade_room_harness import (CASCADE, WORDS, Transcript, wire, speak,
                                  finished, assert_heard)
from test_room_member_reconnect import Room, until, quick  # noqa: F401


async def _scene(monkeypatch):
    chats, stts = wire(monkeypatch, [Transcript()])
    h = await Room(monkeypatch, model=CASCADE).open()
    try:
        await speak(h)
        await until(lambda: bool(chats))
        await finished(h)
        return {
            "chats": chats, "stts": stts,
            "audio": [len(rt.ws._audio) for rt in h.room.sessions.values()],
            "scribe_items": [i["text"] for i in h.room.scribe.ws.items],
            "last_grant": h.room.last_grant,
        }
    finally:
        await h.close()


def test_the_scene_runs_and_the_scribe_hears_the_participant(monkeypatch):
    got = asyncio.run(_scene(monkeypatch))
    assert got["last_grant"]["via"] == "text_prompt"
    assert len(got["chats"]) == len(got["stts"]) == 1
    assert WORDS in got["scribe_items"]
    assert got["audio"] == [0, 0, 0]


def test_a_granted_member_is_given_what_the_participant_said(monkeypatch):
    got = asyncio.run(_scene(monkeypatch))
    assert_heard(got["chats"][0], [WORDS])


def test_a_member_audio_buffer_does_not_outlive_the_turn(monkeypatch):
    got = asyncio.run(_scene(monkeypatch))
    assert got["audio"] == [0, 0, 0]
