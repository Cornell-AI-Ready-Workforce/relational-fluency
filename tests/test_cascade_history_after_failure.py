"""A failed cascade turn must not leave the conversation out of shape.

Live (S2A, build 885a862, 2026-11-14): two turns whose transcription and
reply both failed (ReadError, ConnectTimeout) left their AUDIO in the
conversation, so every later request carried three audio-only user turns in a
row and the reply model spoke the participant's next scripted line from then
on. Replayed against the gateway with the reconstructed history: 10/12 replies
were a participant line with the stale audio, 0/12 without it.

Driven at the CascadeSocket's own surface with httpx's MockTransport for the
gateway and an in-memory ElevenLabs stream.
"""
import asyncio
import base64
import json

import httpx

from server.voice import cascade
from test_cascade_voice import FakeTTS, _sse

PCM = b"\x10\x00" * 16000          # 1 s at 16 kHz


class Wire(cascade.CascadeSocket):
    def __init__(self, **kw):
        super().__init__(**kw)
        self.frames = []

    def _emit(self, ev):
        self.frames.append(ev)
        super()._emit(ev)


def _gateway(script):
    """`script` is a list of steps, popped per request: an exception to raise,
    a string to stream as the reply, or ("stt", text, gate) for a hearing that
    waits on `gate` (an asyncio.Event or None)."""
    chats = []

    async def handle(req):
        body = json.loads(req.content)
        if body.get("stream"):
            chats.append(body)
            step = script["reply"].pop(0)
            if isinstance(step, Exception):
                raise step
            return _sse(step)
        step = script["stt"].pop(0)
        if isinstance(step, Exception):
            raise step
        text, gate = step
        if gate is not None:
            await gate.wait()
        return httpx.Response(200, json={"choices": [{
            "finish_reason": "stop", "message": {"content": text}}]})
    return handle, chats


async def _turn(sock, rid_count):
    await sock.send(json.dumps({"type": "input_audio_buffer.append",
                                "audio": base64.b64encode(PCM).decode()}))
    await sock.send(json.dumps({"type": "input_audio_buffer.commit"}))
    await sock.send(json.dumps({"type": "response.create"}))

    async def done():
        while sum(1 for f in sock.frames if f["type"] == "response.done") < rid_count:
            await asyncio.sleep(0.005)
    await asyncio.wait_for(done(), 3)
    # The hearing may still be running: let it settle before the next turn,
    # as the participant's next line comes seconds later.
    pending = [t for t in sock._pending_stt if not t.done()]
    return pending


def _audio_parts(messages):
    return sum(1 for m in messages if isinstance(m.get("content"), list)
               for p in m["content"] if p.get("type") == "input_audio")


def test_failed_turns_leave_no_stale_audio_and_roles_stay_in_order(monkeypatch):
    async def go():
        seen = {}
        monkeypatch.setattr(cascade, "eleven_key", lambda: "k")

        async def opener():
            return FakeTTS(seen)
        monkeypatch.setattr(cascade, "open_tts", opener)
        last_gate = asyncio.Event()
        script = {
            "stt": [("Hi Morgan, I wanted to talk about my pay.", None),
                    httpx.ReadError("read"),            # turn 2: STT fails
                    httpx.ConnectTimeout("connect"),    # turn 3: STT fails
                    ("What would you need from me?", last_gate),
                    ("Thanks.", None)],
            "reply": ["I hear you. What did you want to ask?",
                      httpx.ReadError("read"),          # turn 2: reply fails
                      httpx.ConnectTimeout("connect"),  # turn 3: reply fails
                      "A written case by Friday.",
                      "Good."],
        }
        handle, chats = _gateway(script)
        async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
            sock = Wire(api_key="k", http=client)
            sock.instructions = "You are Morgan."
            for n in (1, 2, 3):
                for t in await _turn(sock, n):
                    await t
            errors = [f["error"]["code"] for f in sock.frames if f["type"] == "error"]
            assert errors.count("cascade_failed") == 2
            assert sum(1 for f in sock.frames if f["type"].endswith(
                "input_audio_transcription.failed")) == 2

            # Turn 4 is asked while its own hearing is still out, as live:
            # its audio is the one audio in the request.
            pending = await _turn(sock, 4)
            last_gate.set()
            for t in pending:
                await t
            msgs = chats[3]["messages"]
            assert [m["role"] for m in msgs] == ["system", "user", "assistant", "user"]
            assert msgs[1]["content"] == "Hi Morgan, I wanted to talk about my pay."
            assert msgs[2]["content"] == "I hear you. What did you want to ask?"
            assert msgs[3]["content"][0]["type"] == "input_audio"
            assert _audio_parts(msgs) == 1

            for t in await _turn(sock, 5):
                await t
            msgs = chats[4]["messages"]
            assert [m["role"] for m in msgs] == [
                "system", "user", "assistant", "user", "assistant", "user"]
            assert msgs[3]["content"] == "What would you need from me?"
            assert msgs[4]["content"] == "A written case by Friday."
            # Every assistant item holds what the character said, none the
            # participant's words.
            said = {"I hear you. What did you want to ask?", "A written case by Friday."}
            assert {m["content"] for m in msgs if m["role"] == "assistant"} == said
            assert _audio_parts(msgs) <= 1        # at most turn 5's own audio
            await sock.close()
    asyncio.run(go())
