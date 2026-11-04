"""Audit 2026-11-04: latency measurement and the fixes it measured.

All fakes: httpx.MockTransport for the gateway, a fake websocket for
ElevenLabs, temp directories for DATA_DIR.
"""
import asyncio
import base64
import json
import time

import httpx

from server import loop_lag
from server.turn_timing import TurnTimer
from server.voice import cascade


class _Store:
    started_at = 0.0

    def __init__(self):
        self.events = []

    def event(self, kind, **fields):
        self.events.append((kind, fields))


class FakeTTS:
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


def _sse_handler(first_token_delay_s=0.0):
    async def handle(req):
        body = json.loads(req.content)
        if body.get("stream"):
            async def gen():
                await asyncio.sleep(first_token_delay_s)
                for p in ("Sure, I hear you. ", "Go on."):
                    yield ("data: " + json.dumps(
                        {"choices": [{"delta": {"content": p}}]}) + "\n\n").encode()
                yield b"data: [DONE]\n\n"
            return httpx.Response(200, content=gen(),
                                  headers={"content-type": "text/event-stream"})
        return httpx.Response(200, json={"choices": [{"message": {"content": "hi"}}]})
    return handle


def _fake_tts(monkeypatch, open_delay_s=0.0):
    monkeypatch.setattr(cascade, "eleven_key", lambda: "k")

    async def opener():
        await asyncio.sleep(open_delay_s)
        return FakeTTS()
    monkeypatch.setattr(cascade, "open_tts", opener)


async def _one_reply(sock):
    await sock.send(json.dumps({"type": "response.create"}))
    while True:
        f = json.loads(await asyncio.wait_for(sock.recv(), 10))
        if f["type"] == "response.done":
            return f


def test_a_cascade_reply_reports_tts_slot_wait_and_connect(monkeypatch):
    _fake_tts(monkeypatch, open_delay_s=0.05)
    cascade._TTS_SLOTS.clear()

    async def go():
        sock = cascade.CascadeSocket(api_key="gw", http=httpx.AsyncClient(
            transport=httpx.MockTransport(_sse_handler())))
        done = await _one_reply(sock)
        await sock.close()
        return done["response"]["metadata"]["timings"]
    t = asyncio.run(go())
    for key in ("tts_slot_wait_ms", "tts_connect_ms", "llm_first_token_ms",
                "first_audio_ms", "llm_total_ms"):
        assert isinstance(t.get(key), int), (key, t)
    assert t["tts_connect_ms"] >= 40


def test_turn_timing_carries_the_cascade_step_latencies():
    store = _Store()
    timer = TurnTimer(store)
    timer.speech_end()
    timer.started("a")
    timer.done("a", {"stt_ms": 900, "tts_connect_ms": 310, "note": "x"})
    timer.flush()
    row = next(f for k, f in store.events if k == "turn_timing")
    assert row["cascade"] == {"stt_ms": 900, "tts_connect_ms": 310}


def test_turn_timing_cascade_is_null_off_the_cascade():
    store = _Store()
    timer = TurnTimer(store)
    timer.started("a")
    timer.done("a")
    timer.flush()
    row = next(f for k, f in store.events if k == "turn_timing")
    assert row["cascade"] is None


def test_the_loop_lag_probe_sees_a_blocking_call():
    async def go():
        async with loop_lag.LoopLagProbe() as probe:
            await asyncio.sleep(0.12)
            time.sleep(0.2)      # a blocking call on the loop
            await asyncio.sleep(0.12)
            return probe.snapshot()
    s = asyncio.run(go())
    assert s["n"] >= 3
    assert s["max_ms"] >= 150


def test_loop_lag_summary_of_nothing_is_nulls():
    assert loop_lag.summarize([]) == {"n": 0, "p50_ms": None,
                                      "p99_ms": None, "max_ms": None}


# --- finding 3: the LLM stream does not wait for the ElevenLabs connect ------

def _first_audio_ms(monkeypatch, *, open_delay_s, first_token_delay_s):
    _fake_tts(monkeypatch, open_delay_s=open_delay_s)
    cascade._TTS_SLOTS.clear()

    async def go():
        sock = cascade.CascadeSocket(api_key="gw", http=httpx.AsyncClient(
            transport=httpx.MockTransport(_sse_handler(first_token_delay_s))))
        done = await _one_reply(sock)
        await sock.close()
        return done["response"]["metadata"]["timings"]
    return asyncio.run(go())


def test_first_audio_overlaps_the_tts_connect_with_the_llm(monkeypatch):
    """300 ms ElevenLabs open, 300 ms to the first token. In series that is
    >= 600 ms to first audio; overlapped it is about 300 ms."""
    t = _first_audio_ms(monkeypatch, open_delay_s=0.3, first_token_delay_s=0.3)
    print("finding3 timings", t)
    assert t["tts_connect_ms"] >= 280
    assert t["first_audio_ms"] < 480, t


def test_an_llm_failure_closes_the_tts_stream_it_was_opening(monkeypatch):
    closed = []

    class Tracked(FakeTTS):
        async def close(self):
            closed.append(True)
    monkeypatch.setattr(cascade, "eleven_key", lambda: "k")

    async def opener():
        return Tracked()
    monkeypatch.setattr(cascade, "open_tts", opener)
    cascade._TTS_SLOTS.clear()

    async def fail(req):
        await asyncio.sleep(0.1)     # the stream has opened by now
        return httpx.Response(500, text="boom")

    async def go():
        sock = cascade.CascadeSocket(api_key="gw", http=httpx.AsyncClient(
            transport=httpx.MockTransport(fail)))
        await sock.send(json.dumps({"type": "response.create"}))
        frames = []
        while True:
            f = json.loads(await asyncio.wait_for(sock.recv(), 5))
            frames.append(f)
            if f["type"] == "response.done":
                break
        await asyncio.sleep(0.1)
        await sock.close()
        return frames
    frames = asyncio.run(go())
    assert any(f["type"] == "error" and "llm 500" in json.dumps(f) for f in frames)
    assert closed == [True]
