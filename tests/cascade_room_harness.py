"""Real room, runner and cascade; only HTTP and ElevenLabs are fake."""
import asyncio
import json
from dataclasses import dataclass, field
from pathlib import Path

import httpx

from server.voice import cascade, realtime as R
from test_cascade_voice import FakeTTS, _sse
from test_bridge_correctness import LOUD, FRAME, settle
from test_room_member_reconnect import Room, until

_COPY = json.loads((Path(__file__).parent / "cascade_room_texts.json").read_text(encoding="utf-8"))
CASCADE_ROOM_GRANT_NUDGE = _COPY["CASCADE_ROOM_GRANT_NUDGE"]
CASCADE_ROOM_UNHEARD_NUDGE = _COPY["CASCADE_ROOM_UNHEARD_NUDGE"]
ROOM_RULE_CASCADE = _COPY["ROOM_RULE_CASCADE"]

CASCADE = "cascade-argon-elevenlabs"
WORDS = "My name is Quillon and I park on level nine"
WORDS2 = "Priya, Quillon here again, what about the blue zebra plan"
QUIET = b"\x00\x00" * FRAME


@dataclass
class Transcript:
    text: str = WORDS
    gate: asyncio.Event | None = None
    fail: bool = False
    started: asyncio.Event = field(default_factory=asyncio.Event)
    finished: asyncio.Event = field(default_factory=asyncio.Event)


def wire(monkeypatch, transcripts):
    chats, stts = [], []

    async def handle(req):
        body = json.loads(req.content)
        if body.get("stream"):
            chats.append(body)
            return _sse("Okay, sure thing, go on.")
        stts.append(body)
        transcript = transcripts[len(stts) - 1]
        transcript.started.set()
        if transcript.gate is not None:
            await transcript.gate.wait()
        transcript.finished.set()
        if transcript.fail:
            return httpx.Response(500, json={"error": "STT failed"})
        return httpx.Response(200, json={"choices": [{"finish_reason": "stop", "message": {
            "content": transcript.text}}]})

    transport = httpx.MockTransport(handle)
    monkeypatch.setattr(cascade, "eleven_key", lambda: "k")
    monkeypatch.setattr(R, "gateway_api_key", lambda: "k")

    async def opener():
        return FakeTTS({})
    monkeypatch.setattr(cascade, "open_tts", opener)
    real = cascade.CascadeSocket
    monkeypatch.setattr(R, "CascadeSocket", lambda **kw: real(
        http=httpx.AsyncClient(transport=transport), **kw))
    return chats, stts


async def speak(h):
    h.page._frames = [LOUD] * 100 + [QUIET] * 120
    await h.runner._client_to_model()


async def finished(h):
    await until(lambda: not h.runner._group_turn_tasks, 10)
    await settle(h.runner)
    errors = [e for e in h.of("voice_error") if e.get("where") == "group_turn"]
    assert not errors


def user_lines(body):
    return [m["content"] for m in body["messages"] if m["role"] == "user"]


def assert_heard(body, lines):
    users = user_lines(body)
    positions = []
    for line in lines:
        assert users.count(line) == 1, users
        positions.append(users.index(line))
    assert positions == sorted(positions)
    assert "input_audio" not in json.dumps(body)
