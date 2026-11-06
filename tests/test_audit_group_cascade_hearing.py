"""Audit 2026-11-04, step 1: does a room member on the cascade hear the
participant?

GroupRoom.hear fans the participant's PCM to every member as
input_audio_buffer.append. On the cascade that lands in CascadeSocket._audio.
give_floor on a text-grant family (cascade is one) injects a nudge and asks
for a reply, and never commits that buffer. CascadeSocket._messages builds
the model's input from committed items only. So the member's reply is
generated without the participant's words.

The two xfail(strict) tests below state the property a group scene needs.
They fail today, which is the finding. When the RF team changes the design,
they start passing and strict=True turns them red until the marker is
removed.

Everything is a fake: httpx.MockTransport stands in for the gateway and a
fake websocket for ElevenLabs.
"""
import asyncio
import base64
import json

import httpx
import pytest

from server import group_room as gr
from server.voice import cascade, realtime as rt

MODEL = "cascade-argon-elevenlabs"
# 2 s of loud participant audio at 16 kHz, in 100 ms frames as the page sends.
FRAME = b"\x00\x20" * 1600
FRAMES = 20
PARTICIPANT_WORDS = "PARTICIPANT_SAID_THE_DEADLINE_SLIPPED"


class _Agent:
    def __init__(self, aid, name, voice="Puck"):
        self.id, self.name, self.voice = aid, name, voice


class _FakeTTS:
    def __init__(self):
        self._out = asyncio.Queue()

    async def send(self, raw):
        m = json.loads(raw)
        for _ in m.get("inputs") or []:
            self._out.put_nowait(json.dumps(
                {"audio": base64.b64encode(b"\x01\x00" * 480).decode()}))
        if m.get("close_socket"):
            self._out.put_nowait(json.dumps({"is_final": True}))

    def __aiter__(self):
        return self

    async def __anext__(self):
        return await self._out.get()

    async def close(self):
        pass


async def _scene(monkeypatch):
    """Open a two-member cascade room, let the participant speak, close the
    participant's turn on the scribe, and grant the floor to one member.
    Returns what the member's model was sent and the member's socket."""
    chats = []

    def handle(req):
        body = json.loads(req.content)
        if body.get("stream"):
            chats.append(body)
            body = ("data: " + json.dumps({"choices": [{"delta": {
                "content": "Okay, go on."}}]}) + "\n\ndata: [DONE]\n\n")
            return httpx.Response(200, content=body.encode(),
                                  headers={"content-type": "text/event-stream"})
        # The STT step (the scribe's transcript of the participant).
        return httpx.Response(200, json={"choices": [{"message": {
            "content": PARTICIPANT_WORDS}}]})

    transport = httpx.MockTransport(handle)
    monkeypatch.setattr(cascade, "eleven_key", lambda: "k")
    # The room builds its sessions with no api_key, so connect() reads the
    # gateway key; CI has none (and .env wins over the environment in _cfg).
    monkeypatch.setattr(rt, "gateway_api_key", lambda: "k")

    async def opener():
        return _FakeTTS()
    monkeypatch.setattr(cascade, "open_tts", opener)
    real = cascade.CascadeSocket
    monkeypatch.setattr(rt, "CascadeSocket", lambda **kw: real(
        http=httpx.AsyncClient(transport=transport), **kw))

    agents = [_Agent("a", "Alex"), _Agent("b", "Sam", "Charon")]
    room = gr.GroupRoom(agents, instructions_for=lambda a: f"You are {a.name}.",
                        voice_for=lambda a: a.voice, model=MODEL)
    await room.open()
    for _ in range(FRAMES):
        await room.hear(FRAME)
    # The scribe's commit is what writes the participant transcript.
    await room.close_participant_turn()
    await asyncio.sleep(0.05)
    member = room.sessions["a"]
    buffered_before_grant = len(member.ws._audio)
    await room.give_floor("a")
    for _ in range(100):
        if chats:
            break
        await asyncio.sleep(0.02)
    await asyncio.sleep(0.05)
    result = {
        "chats": chats,
        "items": [dict(i, audio=bool(i.get("audio"))) for i in member.ws.items],
        "audio_before_grant": buffered_before_grant,
        "audio_after_grant": len(member.ws._audio),
        "other_member_audio": len(room.sessions["b"].ws._audio),
        "scribe_items": [i.get("text") for i in room.scribe.ws.items],
        "last_grant": room.last_grant,
    }
    await room.close()
    return result


def _has_participant(messages):
    for m in messages:
        c = m.get("content")
        if isinstance(c, str) and PARTICIPANT_WORDS in c:
            return True
        if isinstance(c, list) and any(
                isinstance(p, dict) and p.get("type") == "input_audio" for p in c):
            return True
    return False


def test_the_scene_runs_and_the_scribe_hears_the_participant(monkeypatch):
    """The fakes are wired right: the scribe transcribes, the grant goes out
    as a text prompt, and the member's model is called once."""
    got = asyncio.run(_scene(monkeypatch))
    assert got["last_grant"]["via"] == "text_prompt"
    assert len(got["chats"]) == 1
    assert PARTICIPANT_WORDS in got["scribe_items"]
    assert got["audio_before_grant"] == FRAMES * len(FRAME)


@pytest.mark.xfail(strict=True, raises=AssertionError, reason=(
    "AUDIT 2026-11-04: on the cascade a room member's reply is built without "
    "the participant's words. The fanned PCM is never committed on a member "
    "and nothing tells the member the transcript. Study-design matter for "
    "the RF team."))
def test_a_granted_member_is_given_what_the_participant_said(monkeypatch):
    got = asyncio.run(_scene(monkeypatch))
    messages = got["chats"][0]["messages"]
    assert _has_participant(messages), json.dumps(messages, indent=1)


@pytest.mark.xfail(strict=True, raises=AssertionError, reason=(
    "AUDIT 2026-11-04: CascadeSocket._audio on a room member is never "
    "committed or cleared, so it grows by 32 KB per second of participant "
    "audio for the whole encounter."))
def test_a_member_audio_buffer_does_not_outlive_the_turn(monkeypatch):
    got = asyncio.run(_scene(monkeypatch))
    assert got["audio_after_grant"] == 0
    assert got["other_member_audio"] == 0
