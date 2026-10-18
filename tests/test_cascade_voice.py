"""The cascade family: Argon hears and writes, ElevenLabs speaks.

Driven through the real RealtimeVoiceSession with httpx's MockTransport in
place of the gateway and a fake websocket in place of ElevenLabs, so every
frame the bridge reads is one the cascade socket really produced.
"""
import asyncio
import base64
import json

import httpx

from server.voice import cascade, realtime as rt

MODEL = "cascade-argon-elevenlabs"
PCM = b"\x10\x00" * 24000          # 1.5 s at 16 kHz


def _sse(*pieces):
    body = "".join(
        "data: " + json.dumps({"choices": [{"delta": {"content": p}}]}) + "\n\n"
        for p in pieces) + "data: [DONE]\n\n"
    return httpx.Response(200, content=body.encode(),
                          headers={"content-type": "text/event-stream"})


class FakeTTS:
    """ElevenLabs' dialogue stream: one audio message per input, is_final on
    close_socket. Everything sent is kept on `sent`."""

    def __init__(self, seen):
        self.sent = seen.setdefault("tts", [])
        self._out = asyncio.Queue()

    async def send(self, raw):
        m = json.loads(raw)
        self.sent.append(m)
        for _ in m.get("inputs") or []:
            self._out.put_nowait(json.dumps({"audio": base64.b64encode(b"\x01\x00" * 4800).decode()}))
        if m.get("close_socket"):
            self._out.put_nowait(json.dumps({"is_final": True}))

    def __aiter__(self):
        return self

    async def __anext__(self):
        return await self._out.get()

    async def close(self):
        pass


def _fake_tts(monkeypatch, seen, key="k"):
    monkeypatch.setattr(cascade, "eleven_key", lambda: key)

    async def opener():
        if not key:
            raise RuntimeError("ELEVENLABS_API_KEY is not set")
        return FakeTTS(seen)
    monkeypatch.setattr(cascade, "open_tts", opener)


def _handler(seen):
    def handle(req: httpx.Request) -> httpx.Response:
        body = json.loads(req.content)
        if body.get("stream"):
            seen["chat"] = body
            return _sse("Sure, I hear you. ", "Tell me more about ", "what happened.")
        seen["stt"] = body
        return httpx.Response(200, json={"choices": [{"message": {
            "content": "I missed the deadline."}}]})
    return handle


async def _drive(monkeypatch, *, key="k"):
    seen = {}
    _fake_tts(monkeypatch, seen, key)
    transport = httpx.MockTransport(_handler(seen))
    s = rt.RealtimeVoiceSession("You are Riley.", model=MODEL, voice="Puck",
                                api_key="gw")
    real = cascade.CascadeSocket
    monkeypatch.setattr(rt, "CascadeSocket", lambda **kw: real(
        http=httpx.AsyncClient(transport=transport), **kw))
    await s.connect()
    await s.send_audio(PCM)
    await s.commit_turn()
    events = []
    stream = s.events()
    while True:
        try:
            ev = await asyncio.wait_for(stream.__anext__(), 3)
        except (asyncio.TimeoutError, StopAsyncIteration):
            break
        events.append(ev)
        if ev["type"] == "error" or (ev["type"] == "response_done"
                                     and not ev.get("interrupted")):
            break
    await s.close()
    return seen, events


def test_family_voice_and_casting():
    assert rt.family_of(MODEL) == "cascade"
    assert rt.voice_for_model("Puck", MODEL) == "will"
    assert rt.capabilities_for(MODEL).casting_key == "cascade"
    assert not rt.is_openai_realtime(MODEL)


def test_sentence_splitting_keeps_short_first_and_merges_stragglers():
    got, rest = cascade.split_sentences("Hi there. How are you doing today? Fine", first=True)
    assert got == ["Hi there.", "How are you doing today?"] and rest == "Fine"
    got, rest = cascade.split_sentences("Fine", first=False, final=True)
    assert got == ["Fine"] and rest == ""


def test_a_turn_is_heard_answered_and_spoken(monkeypatch):
    seen, events = asyncio.run(_drive(monkeypatch))
    kinds = [e["type"] for e in events]
    user = next(e for e in events if e["type"] == "user_transcript")
    assert "missed the deadline" in user["text"]
    assert kinds.count("agent_audio") >= 1 and kinds[-1] == "response_done"
    said = [e for e in events if e["type"] == "agent_transcript"][-1]["text"]
    assert said == "Sure, I hear you. Tell me more about what happened."
    # Argon both heard (audio in) and replied, under the brief, with the
    # participant's words in its history.
    assert seen["stt"]["model"] == "nto.gemini-3.5-flash-lite"
    assert seen["stt"]["messages"][0]["content"][1]["type"] == "input_audio"
    assert seen["chat"]["model"] == "nto.gemini-4-argon"
    assert seen["chat"]["messages"][0] == {"role": "system", "content": "You are Riley."}
    # The reply does not wait for the transcript: the model is handed the
    # participant's words when they are in, else the audio itself.
    last = seen["chat"]["messages"][-1]
    assert last["role"] == "user" and (
        last["content"] == "I missed the deadline."
        or last["content"][0]["type"] == "input_audio")
    # ElevenLabs: ONE stream for the reply; the cast voice registered first,
    # each sentence flushed into it as it lands, closed at the end.
    sent = seen["tts"]
    assert sent[0] == {"voices": [cascade.PREMADE_VOICES["will"]]}
    inputs = [m["inputs"][0] for m in sent if m.get("inputs")]
    assert [i["text"].strip() for i in inputs] == ["Sure, I hear you.", "Tell me more about what happened."]
    assert all(i["voice_id"] == cascade.PREMADE_VOICES["will"] for i in inputs)
    assert all(m.get("flush") for m in sent if m.get("inputs"))
    assert sent[-1] == {"close_socket": True}


def test_a_missing_elevenlabs_key_is_an_error_not_silence(monkeypatch):
    seen, events = asyncio.run(_drive(monkeypatch, key=""))
    errors = [e for e in events if e["type"] == "error"]
    assert errors and "ELEVENLABS_API_KEY" in json.dumps(errors)


def test_cancel_stops_the_reply_and_says_so(monkeypatch):
    async def go():
        seen = {}
        _fake_tts(monkeypatch, seen)
        sock = cascade.CascadeSocket(api_key="gw", http=httpx.AsyncClient(
            transport=httpx.MockTransport(_handler(seen))))
        await sock.send(json.dumps({"type": "response.create"}))
        await sock.send(json.dumps({"type": "response.cancel"}))
        frames = []
        while True:
            f = json.loads(await asyncio.wait_for(sock.recv(), 5))
            frames.append(f["type"])
            if f["type"] == "response.done":
                return f, frames
    done, frames = asyncio.run(go())
    assert done["response"]["status"] == "cancelled"


def test_item_create_and_delete_are_acknowledged():
    async def go():
        sock = cascade.CascadeSocket(api_key="gw", http=httpx.AsyncClient(
            transport=httpx.MockTransport(_handler({}))))
        await sock.send(json.dumps({"type": "conversation.item.create", "event_id": "e1",
            "item": {"type": "message", "role": "user",
                     "content": [{"type": "input_text", "text": "note"}]}}))
        added = json.loads(await sock.recv())
        await sock.send(json.dumps({"type": "conversation.item.delete",
                                    "item_id": added["item"]["id"]}))
        return added, json.loads(await sock.recv())
    added, deleted = asyncio.run(go())
    assert added["type"] == "conversation.item.added"
    assert deleted["type"] == "conversation.item.deleted"
    assert deleted["item_id"] == added["item"]["id"]
