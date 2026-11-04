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


# --- finding 4: the ElevenLabs slot covers the stream, not the LLM -----------

def _paced_handler(first_token_s, rest_s):
    async def handle(req):
        body = json.loads(req.content)
        if not body.get("stream"):
            return httpx.Response(200, json={"choices": [{"message": {"content": "hi"}}]})

        async def gen():
            await asyncio.sleep(first_token_s)
            yield ("data: " + json.dumps({"choices": [{"delta": {
                "content": "Sure, I hear you. "}}]}) + "\n\n").encode()
            await asyncio.sleep(rest_s)
            yield ("data: " + json.dumps({"choices": [{"delta": {
                "content": "Tell me more about it."}}]}) + "\n\n").encode()
            yield b"data: [DONE]\n\n"
        return httpx.Response(200, content=gen(),
                              headers={"content-type": "text/event-stream"})
    return handle


def _four_at_once(monkeypatch, *, first_token_s=1.0, rest_s=1.0, open_s=0.3):
    """Four sockets reply at once with ELEVENLABS_CONCURRENCY=3."""
    _fake_tts(monkeypatch, open_delay_s=open_s)
    monkeypatch.setenv("ELEVENLABS_CONCURRENCY", "3")
    cascade._TTS_SLOTS.clear()

    async def go():
        socks = [cascade.CascadeSocket(api_key="gw", http=httpx.AsyncClient(
            transport=httpx.MockTransport(_paced_handler(first_token_s, rest_s))))
            for _ in range(4)]
        dones = await asyncio.gather(*(_one_reply(s) for s in socks))
        for s in socks:
            await s.close()
        return [d["response"]["metadata"]["timings"] for d in dones]
    return asyncio.run(go())


def test_a_fourth_reply_thinks_while_it_waits_for_an_elevenlabs_slot(monkeypatch):
    got = _four_at_once(monkeypatch)
    print("finding4 timings", got)
    waited = max(got, key=lambda t: t["tts_slot_wait_ms"])
    assert waited["tts_slot_wait_ms"] >= 1500       # one reply had to wait
    # It still started thinking at once: its first token is not behind the
    # wait, and its first audio is the wait plus the connect, not the wait
    # plus the connect plus the first token.
    assert waited["llm_first_token_ms"] < 1300, waited
    assert waited["first_audio_ms"] < waited["tts_slot_wait_ms"] + 300 + 400, waited


def test_every_path_gives_the_elevenlabs_slot_back(monkeypatch):
    monkeypatch.setenv("ELEVENLABS_CONCURRENCY", "2")
    cascade._TTS_SLOTS.clear()
    monkeypatch.setattr(cascade, "eleven_key", lambda: "k")
    mode = {"open": "ok"}

    async def opener():
        await asyncio.sleep(0.02)
        if mode["open"] == "fail":
            raise RuntimeError("no stream")
        return FakeTTS()
    monkeypatch.setattr(cascade, "open_tts", opener)

    async def go():
        sock = cascade.CascadeSocket(api_key="gw", http=httpx.AsyncClient(
            transport=httpx.MockTransport(_paced_handler(0.2, 0.2))))
        await _one_reply(sock)                     # completed
        mode["open"] = "fail"
        await _one_reply(sock)                     # TTS open failed
        mode["open"] = "ok"
        await sock.send(json.dumps({"type": "response.create"}))
        await asyncio.sleep(0.1)                   # stream open, model thinking
        await sock.send(json.dumps({"type": "response.cancel"}))
        while json.loads(await asyncio.wait_for(sock.recv(), 5))["type"] != "response.done":
            pass
        await asyncio.sleep(0.05)
        value = cascade._tts_slot()._value
        await sock.close()
        return value
    assert asyncio.run(go()) == 2


# --- finding 5: one gateway client per loop, kept alive between turns --------

def test_cascade_sockets_on_one_loop_share_a_long_lived_gateway_client():
    async def go():
        a, b = cascade.CascadeSocket(api_key="gw"), cascade.CascadeSocket(api_key="gw")
        same = a._http is b._http
        pool = a._http._transport._pool
        await a.close()
        still_open = not b._http.is_closed
        await b.close()
        return same, pool._keepalive_expiry, still_open
    same, expiry, still_open = asyncio.run(go())
    assert same and still_open
    assert expiry == cascade.GATEWAY_KEEPALIVE_S >= 30


def test_the_drain_after_done_is_bounded():
    async def forever():
        while True:
            await asyncio.sleep(1)
            yield "x"

    async def go():
        t0 = time.perf_counter()
        await cascade._drain(forever(), 0.1)
        return time.perf_counter() - t0
    assert asyncio.run(go()) < 0.5


def test_elevenlabs_is_told_the_reply_is_over_before_the_drain(monkeypatch):
    """A gateway that holds its body open after [DONE] must not delay the
    reply's end: close_socket goes out at [DONE], the drain after it."""
    marks = {}

    class Timed(FakeTTS):
        async def send(self, raw):
            if json.loads(raw).get("close_socket"):
                marks["close_socket"] = time.perf_counter()
            await super().send(raw)
    monkeypatch.setattr(cascade, "eleven_key", lambda: "k")

    async def opener():
        return Timed()
    monkeypatch.setattr(cascade, "open_tts", opener)
    cascade._TTS_SLOTS.clear()

    async def handle(req):
        async def gen():
            yield ("data: " + json.dumps({"choices": [{"delta": {
                "content": "Sure, I hear you."}}]}) + "\n\n").encode()
            marks["done"] = time.perf_counter()
            yield b"data: [DONE]\n\n"
            await asyncio.sleep(1.0)          # the body's end comes late
        return httpx.Response(200, content=gen(),
                              headers={"content-type": "text/event-stream"})

    async def go():
        sock = cascade.CascadeSocket(api_key="gw", http=httpx.AsyncClient(
            transport=httpx.MockTransport(handle)))
        t0 = time.perf_counter()
        done = await _one_reply(sock)
        took = time.perf_counter() - t0
        await sock.close()
        return done, took
    done, took = asyncio.run(go())
    assert done["response"]["status"] == "completed"
    assert marks["close_socket"] - marks["done"] < 0.1
    assert took < 0.6, "the drain is bounded at 250 ms"


def test_the_shared_gateway_client_has_no_process_wide_connection_cap():
    async def go():
        sock = cascade.CascadeSocket(api_key="gw")
        pool = sock._http._transport._pool
        await sock.close()
        return pool._max_connections
    assert asyncio.run(go()) >= 10 ** 9      # httpcore stores "no cap" as sys.maxsize


def test_the_audit_pipeline_version_and_its_history_line():
    from pathlib import Path
    from server import llm
    assert llm.PIPELINE_VERSION == "2026-11-04a"
    src = (Path(__file__).resolve().parents[1] / "server" / "llm.py").read_text(encoding="utf-8")
    assert src.count("#   2026-11-04a") == 1
