"""Offline tests driven through the real gateway and websocket call sites."""
import asyncio
import base64
import json
import time
from types import SimpleNamespace

import httpx
import httpx2
import pytest
from anthropic import AsyncAnthropic
from websockets.exceptions import InvalidStatus, ConnectionClosedError
from websockets.http11 import Response

from server import director, llm, scenarios, steering, upstream_telemetry as telemetry, video
from server.voice import cascade
from test_upstream_telemetry import records

# conftest replaces the boot probes after collection when server.app is loaded.
storage_preflight = video.storage_preflight


@pytest.fixture(autouse=True)
def configuration(monkeypatch):
    monkeypatch.setattr(llm, "_FILE", {})
    for key in ("LLM_BASE_URL", "ELEVENLABS_WS_URL", "ELEVENLABS_TTS_ID",
                "STORAGE_PREFLIGHT"):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("LITELLM_API_KEY", "test-gateway-key")
    monkeypatch.setenv("ELEVENLABS_API_KEY", "test-tts-key")


class FakeTTS:
    def __init__(self, *, messages=None, send_error=None):
        self.sent = []
        self.out = asyncio.Queue()
        self.messages = messages
        self.send_error = send_error

    async def send(self, raw):
        if self.send_error is not None:
            raise self.send_error
        message = json.loads(raw)
        self.sent.append(message)
        for _ in message.get("inputs", []):
            self.out.put_nowait(json.dumps({"audio": base64.b64encode(b"\1\0" * 10).decode()}))
        if message.get("close_socket"):
            for reply in self.messages or [{"is_final": True}]:
                self.out.put_nowait(json.dumps(reply))

    def __aiter__(self):
        return self

    async def __anext__(self):
        return await self.out.get()

    async def close(self):
        pass


def sse():
    delta = {"choices": [{"delta": {"content": "I hear you. "}}]}
    return httpx.Response(200, content=("data: " + json.dumps(delta) + "\n\n"
                                       "data: [DONE]\n\n").encode())


@pytest.mark.parametrize("override", [False, True])
def test_all_gateway_routes_and_tts_use_default_or_override(records, monkeypatch, override):
    base = "http://fake-gateway:8000" if override else "https://api.ai.it.cornell.edu"
    tts_base = ("ws://fake-tts:8001/dialogue" if override else
                "wss://api.elevenlabs.io/v1/text-to-dialogue/stream-input")
    if override:
        monkeypatch.setenv("LLM_BASE_URL", base + "/")
        monkeypatch.setenv("ELEVENLABS_WS_URL", tts_base)
    seen = []
    tts = FakeTTS()

    def preflight_get(url, **kwargs):
        seen.append(("models", url, kwargs))
        return httpx.Response(200, json={"data": []})

    def gateway(request):
        body = json.loads(request.content)
        kind = "actor" if body.get("stream") else "stt"
        seen.append((kind, str(request.url), body))
        return sse() if kind == "actor" else httpx.Response(
            200, json={"choices": [{"message": {"content": "Hello."}}]})

    def sdk(request):
        seen.append(("messages", str(request.url), json.loads(request.content)))
        return httpx2.Response(200, json=tool_response("adjust_persona", {"adjustments": []}))

    async def connect(url, **kwargs):
        seen.append(("tts", url, kwargs))
        return tts

    monkeypatch.setattr(httpx, "get", preflight_get)
    monkeypatch.setattr(cascade.websockets, "connect", connect)
    llm.preflight()

    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(gateway)) as http:
            sock = cascade.CascadeSocket(api_key="gw", http=http, session_id="test-session")
            assert await sock._hear(b"\0\0" * 20) == "Hello."
            await sock._respond("r")
        async with httpx2.AsyncClient(transport=httpx2.MockTransport(sdk)) as http:
            client = llm.text_client().with_options(http_client=http)
            await llm.message_create(client, purpose="steering", model="test",
                                     max_tokens=10, messages=[{"role": "user", "content": "Hi"}])

    asyncio.run(run())
    assert [(kind, url) for kind, url, _ in seen] == [
        ("models", base + "/v1/models"),
        ("stt", base + "/v1/chat/completions"),
        ("actor", base + "/v1/chat/completions"),
        ("tts", tts_base + "?model_id=eleven_v4&output_format=pcm_24000"),
        ("messages", base + "/v1/messages")]
    assert seen[0][2] == {"headers": {"Authorization": "Bearer test-gateway-key"}, "timeout": 10}
    assert seen[1][2]["messages"][0]["content"][1]["input_audio"]["format"] == "wav"
    assert tts.sent == [
        {"voices": [cascade.voice_id_for("")]},
        {"inputs": [{"text": "I hear you. ", "voice_id": cascade.voice_id_for("")}], "flush": True},
        {"close_socket": True}]
    assert {(r["route"], r["purpose"], r["outcome"], r["status"]) for r in records} == {
        ("models", "preflight", "ok", 200), ("chat_completions", "stt", "ok", 200),
        ("chat_completions_stream", "actor", "ok", 200), ("tts_ws", "tts", "ok", 101),
        ("messages", "steering", "ok", 200)}
    assert len(records) == 5
    assert all(r["session_id"] == "test-session" for r in records if r["purpose"] in {"stt", "actor", "tts"})


def tool_response(name, data):
    return dict(id="msg_test", type="message", role="assistant", model="test",
                content=[dict(type="tool_use", id="tool_test", name=name, input=data)],
                stop_reason="tool_use", usage=dict(input_tokens=1, output_tokens=1))


def test_text_client_matches_all_stock_request_headers(records):
    requests = []

    def handler(request):
        requests.append(request)
        return httpx2.Response(200, json=tool_response("adjust_persona", {"adjustments": []}))

    async def run():
        async with httpx2.AsyncClient(transport=httpx2.MockTransport(handler)) as http:
            stock = AsyncAnthropic(base_url=llm.gateway_base_url(),
                                   api_key=llm.gateway_api_key(), http_client=http)
            client = llm.text_client().with_options(http_client=http)
            kwargs = dict(model="test", max_tokens=10,
                          messages=[{"role": "user", "content": "Hi"}])
            await stock.messages.create(**kwargs)
            await llm.message_create(client, purpose="steering", **kwargs)

    asyncio.run(run())
    assert len(requests) == 2
    assert requests[0].headers["user-agent"].startswith("AsyncAnthropic/Python ")
    assert [(key, value) for key, value in requests[0].headers.multi_items()
            if key != "idempotency-key"] == [
                (key, value) for key, value in requests[1].headers.multi_items()
                if key != "idempotency-key"]
    assert requests[0].content == requests[1].content
    assert len(records) == 1 and records[0]["outcome"] == "ok"


@pytest.mark.parametrize("context", [None, ("director", "encounter")])
@pytest.mark.parametrize("retries", [None, 3])
def test_sdk_attempt_hook_forwards_all_arguments(records, monkeypatch, context, retries):
    args = (object(), object())
    kwargs = {"stream": object(), "future_option": object()}
    if retries is not None:
        kwargs["retries_taken"] = retries
    result = (SimpleNamespace(status_code=200), object())

    async def stock_attempt(self, *received_args, **received_kwargs):
        assert len(received_args) == len(args)
        assert all(left is right for left, right in zip(received_args, args))
        assert received_kwargs.keys() == kwargs.keys()
        assert all(received_kwargs[key] is value for key, value in kwargs.items())
        return result

    monkeypatch.setattr(AsyncAnthropic, "_attempt_request", stock_attempt)

    async def run():
        async with llm.text_client() as client:
            token = llm._message_context.set(context)
            try:
                assert await client._attempt_request(*args, **kwargs) == result
            finally:
                llm._message_context.reset(token)

    asyncio.run(run())
    if context is None:
        assert records == []
    else:
        assert len(records) == 1
        assert records[0]["attempt"] == (retries or 0) + 1


@pytest.mark.parametrize("status", [200, 302, 401, 429, 502])
def test_preflight_status_per_request(records, monkeypatch, status):
    monkeypatch.setattr(httpx, "get", lambda *a, **kw: httpx.Response(status, json={"data": []}))
    llm.preflight()
    assert len(records) == 1
    assert records[0]["status"] == status
    assert records[0]["outcome"] == ("ok" if status == 200 else "http_error")


@pytest.mark.parametrize("error,outcome", [
    (httpx.ReadTimeout("test"), "timeout"), (httpx.ConnectError("test"), "exception")])
def test_preflight_failures(records, monkeypatch, error, outcome):
    def fail(*a, **kw):
        raise error
    monkeypatch.setattr(httpx, "get", fail)
    assert not llm.preflight()["ok"]
    assert len(records) == 1 and records[0]["outcome"] == outcome


@pytest.mark.parametrize("purpose", ["stt", "actor"])
@pytest.mark.parametrize("failure,outcome,status", [
    (429, "http_error", 429), (httpx.ReadTimeout("test"), "timeout", None),
    (httpx.ConnectError("test"), "exception", None),
    (asyncio.CancelledError(), "cancelled", None)])
def test_cascade_gateway_failures(records, purpose, failure, outcome, status):
    async def handler(request):
        if isinstance(failure, BaseException):
            raise failure
        return httpx.Response(failure, text="failed")

    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
            sock = cascade.CascadeSocket(http=http)
            with pytest.raises(BaseException):
                if purpose == "stt":
                    await sock._hear(b"\0\0")
                else:
                    tts = asyncio.get_running_loop().create_future()
                    tts.set_result((FakeTTS(), None, None))
                    await sock._stream_llm(tts, "v", [], "r", "i", time.time())
    asyncio.run(run())
    assert len(records) == 1
    assert (records[0]["purpose"], records[0]["outcome"], records[0]["status"]) == (purpose, outcome, status)


@pytest.mark.parametrize("purpose", ["director", "steering"])
@pytest.mark.parametrize("first", [429, 502, "timeout"])
def test_sdk_recovered_attempts_at_real_controller_sites(records, monkeypatch, purpose, first):
    scenario = scenarios.load_scenario("S3A")
    requests = []
    async def handler(request):
        requests.append(request)
        if len(requests) == 1:
            if first == "timeout":
                raise httpx2.ReadTimeout("test", request=request)
            return httpx2.Response(first, json={"error": {"type": "test", "message": "retry"}},
                                   headers={"retry-after-ms": "1"})
        if purpose == "director":
            body = tool_response("set_speakers", {"speakers": [{
                "agent_id": scenario.cast[0].id, "intent": "Ask about the deadline."}]})
        else:
            body = tool_response("adjust_persona", {"adjustments": []})
        return httpx2.Response(200, json=body)

    async def run():
        async with httpx2.AsyncClient(transport=httpx2.MockTransport(handler)) as http:
            client = llm.text_client().with_options(http_client=http)
            if purpose == "director":
                controller = director.Director(scenario, client=client, session_id="encounter")
                assert await controller.route([{"speaker": "user", "text": "Hello"}], "Hello")
            else:
                controller = steering.SteeringController(scenario, client=client, session_id="encounter")
                assert await controller.review([{"speaker": "user", "text": "Hello"}],
                                               scenario.initial_personas(), {}) == []

    asyncio.run(run())
    assert len(requests) == len(records) == 2
    assert [(r["attempt"], r["outcome"], r["status"]) for r in records] == [
        (1, "timeout" if first == "timeout" else "http_error", None if first == "timeout" else first),
        (2, "ok", 200)]
    assert all(r["route"] == "messages" and r["purpose"] == purpose
               and r["session_id"] == "encounter" for r in records)


@pytest.mark.parametrize("statuses", [[409, 429], [429, 429, 429, 429], [401]])
def test_tts_records_every_handshake_attempt(records, monkeypatch, statuses):
    calls = []
    pauses = []
    now = [0.0]
    monkeypatch.setattr(telemetry.time, "perf_counter", lambda: now[0])

    async def open_tts():
        now[0] += 0.01
        calls.append(1)
        if len(calls) <= len(statuses):
            raise InvalidStatus(Response(statuses[len(calls) - 1], "test", {}, body=b"failure"))
        return FakeTTS()

    async def sleep(delay):
        pauses.append(delay)
        now[0] += delay

    monkeypatch.setattr(cascade, "open_tts", open_tts)
    monkeypatch.setattr(cascade.asyncio, "sleep", sleep)

    async def run():
        sock = cascade.CascadeSocket()
        if len(statuses) == 2:
            ws = await sock._connect_tts(queue_wait_ms=125.0)
            ws.ws.out.put_nowait('{"is_final":true}')
            await sock._relay_audio(ws, "r", "i", time.time())
            await ws.close()
        else:
            with pytest.raises(RuntimeError):
                await sock._connect_tts(queue_wait_ms=125.0)
    asyncio.run(run())
    expected = statuses + ([101] if len(statuses) == 2 else [])
    assert [r["status"] for r in records] == expected
    assert [r["attempt"] for r in records] == list(range(1, len(expected) + 1))
    assert [r["outcome"] for r in records] == ["http_error"] * len(statuses) + (["ok"] if len(statuses) == 2 else [])
    assert [r["queue_wait_ms"] for r in records] == [125.0] + [None] * (len(expected) - 1)
    assert all(r["duration_ms"] == pytest.approx(10.0) for r in records)
    assert pauses == ([0.4, 0.8] if len(statuses) == 2 else [0.4, 0.8, 1.6] if len(statuses) == 4 else [])


@pytest.mark.parametrize("error,outcome", [
    (TimeoutError(), "timeout"), (RuntimeError("test"), "exception"),
    (ConnectionClosedError(None, None), "closed"), (asyncio.CancelledError(), "cancelled")])
def test_tts_connection_failures(records, monkeypatch, error, outcome):
    async def fail():
        raise error
    monkeypatch.setattr(cascade, "open_tts", fail)
    async def run():
        with pytest.raises(BaseException):
            await cascade.CascadeSocket()._connect_tts()
    asyncio.run(run())
    assert len(records) == 1 and records[0]["outcome"] == outcome


def test_tts_stream_error_is_one_attempt(records, monkeypatch):
    async def open_tts():
        return FakeTTS(messages=[{"error": "test", "message": "failed"}])
    monkeypatch.setattr(cascade, "open_tts", open_tts)
    async def run():
        sock = cascade.CascadeSocket()
        ws = await sock._connect_tts()
        await ws.send('{"close_socket":true}')
        with pytest.raises(RuntimeError):
            await sock._relay_audio(ws, "r", "i", time.time())
        await ws.close()
    asyncio.run(run())
    assert len(records) == 1 and records[0]["outcome"] == "exception"


def test_tts_queue_wait_is_separate_from_request_and_cancelled_wait_is_recorded(records, monkeypatch):
    now = [0.0]
    monkeypatch.setattr(telemetry.time, "perf_counter", lambda: now[0])
    async def open_tts():
        now[0] += 0.025
        return FakeTTS()
    monkeypatch.setattr(cascade, "open_tts", open_tts)

    async def run():
        sem = asyncio.Semaphore(0)
        monkeypatch.setattr(cascade, "_tts_slot", lambda: sem)
        sock = cascade.CascadeSocket()
        task = asyncio.create_task(sock._open_reply_tts("v", "r", "i", time.time()))
        await asyncio.sleep(0)
        now[0] += 2.0
        sem.release()
        ws, reader, slot = await task
        ws.ws.out.put_nowait('{"is_final":true}')
        await reader
        await sock._close_reply_tts(task)
        assert not slot.locked()
        await sem.acquire()
        waiting = asyncio.create_task(sock._open_reply_tts("v", "r2", "i2", time.time()))
        await asyncio.sleep(0)
        now[0] += 1.0
        waiting.cancel()
        with pytest.raises(asyncio.CancelledError):
            await waiting
        assert sem.locked()
    asyncio.run(run())
    assert [(r["outcome"], r["duration_ms"], r["queue_wait_ms"]) for r in records] == [
        ("ok", pytest.approx(25.0), 2000.0), ("cancelled", 0.0, 1000.0)]
    assert [r["detail"] for r in records] == [None, "slot_wait"]


def test_actor_duration_excludes_tts_wait_and_sends(records, monkeypatch):
    now = [0.0]
    monkeypatch.setattr(telemetry.time, "perf_counter", lambda: now[0])
    class SlowTTS(FakeTTS):
        async def send(self, raw):
            now[0] += 3.0
            await super().send(raw)

    async def run():
        requested = asyncio.Event()

        async def handler(request):
            now[0] += 0.05
            requested.set()
            return sse()

        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
            sock = cascade.CascadeSocket(http=http)
            tts = asyncio.get_running_loop().create_future()
            async def release():
                await requested.wait()
                assert not records
                now[0] += 2.0
                tts.set_result((SlowTTS(), None, None))
            waiter = asyncio.create_task(release())
            await asyncio.wait_for(sock._stream_llm(tts, "v", [], "r", "i", time.time()), 1)
            await waiter
    asyncio.run(run())
    assert len(records) == 1
    assert records[0]["duration_ms"] == pytest.approx(50.0)


def test_actor_duration_stops_at_done_before_final_tts_and_drain(records, monkeypatch):
    now = [0.0]
    monkeypatch.setattr(telemetry.time, "perf_counter", lambda: now[0])

    async def run():
        class PacedStream(httpx.AsyncByteStream):
            async def __aiter__(self):
                now[0] += 0.3
                yield b'data: {"choices":[{"delta":{"content":"Hi"}}]}\n\n'
                now[0] += 0.2
                yield b'data: [DONE]\n\n'
                now[0] += 5.0

        class SlowTTS(FakeTTS):
            async def send(self, raw):
                now[0] += 3.0
                await super().send(raw)

        async with httpx.AsyncClient(transport=httpx.MockTransport(
                lambda req: httpx.Response(200, stream=PacedStream()))) as http:
            sock = cascade.CascadeSocket(http=http)
            tts = asyncio.get_running_loop().create_future()
            task = asyncio.create_task(sock._stream_llm(tts, "v", [], "r", "i", time.time()))
            await asyncio.sleep(0)
            assert not tts.done() and not task.done()
            assert records[0]["duration_ms"] == pytest.approx(500.0)
            now[0] += 2.0
            tts.set_result((SlowTTS(), None, None))
            await task
    asyncio.run(run())
    assert len(records) == 1 and records[0]["outcome"] == "ok"
    assert records[0]["duration_ms"] == pytest.approx(500.0)


def test_slow_tts_preserves_origin_main_frame_order_and_body_reads(records, monkeypatch):
    async def run():
        first_read = asyncio.Event()
        ready = asyncio.Event()
        reads = []

        class PacedStream(httpx.AsyncByteStream):
            async def __aiter__(self):
                for text in ("Hello there. ", "How are you today? "):
                    await asyncio.sleep(0)
                    reads.append(text)
                    first_read.set()
                    yield ("data: " + json.dumps({"choices": [{"delta": {"content": text}}]})
                           + "\n\n").encode()
                yield b"data: [DONE]\n\n"

        async def open_tts():
            await ready.wait()
            return FakeTTS()

        monkeypatch.setattr(cascade, "open_tts", open_tts)
        async with httpx.AsyncClient(transport=httpx.MockTransport(
                lambda req: httpx.Response(200, stream=PacedStream()))) as http:
            sock = cascade.CascadeSocket(http=http)
            spawned = []
            spawn = sock._spawn

            def track_spawn(coro):
                spawned.append(coro.cr_code.co_name)
                return spawn(coro)

            monkeypatch.setattr(sock, "_spawn", track_spawn)
            task = asyncio.create_task(sock._respond("r"))
            await first_read.wait()
            for _ in range(4):
                await asyncio.sleep(0)
            assert reads == ["Hello there. "]
            assert not records
            ready.set()
            await asyncio.wait_for(task, 1)
            frames = []
            while not sock._out.empty():
                frame = json.loads(sock._out.get_nowait())
                if frame["type"].endswith(".delta"):
                    frames.append(frame["type"])
            # origin/main e949063 emits each sentence before its audio, then
            # reads the next gateway chunk. A pump reorders the middle pair.
            assert frames == ["response.output_audio_transcript.delta",
                              "response.output_audio.delta",
                              "response.output_audio_transcript.delta",
                              "response.output_audio.delta"]
            assert spawned == ["_open_reply_tts", "keep_alive", "_relay_audio"]

    asyncio.run(run())


def test_tts_reader_cancelled_before_start_records_cancelled(records):
    async def run():
        sock = cascade.CascadeSocket()
        attempt = telemetry._Attempt(upstream="elevenlabs", route="tts_ws", purpose="tts")
        ws = cascade._TtsSocket(FakeTTS(), attempt)
        reader = asyncio.create_task(sock._relay_audio(ws, "r", "i", time.time()))
        reader.cancel()
        with pytest.raises(asyncio.CancelledError):
            await reader
        assert reader.done() and reader.cancelled() and not attempt.finished
        slot = asyncio.Semaphore(1)
        await slot.acquire()
        tts = asyncio.get_running_loop().create_future()
        tts.set_result((ws, reader, slot))
        await sock._close_reply_tts(tts)
        assert not slot.locked()

    asyncio.run(run())
    assert len(records) == 1 and records[0]["outcome"] == "cancelled"


@pytest.mark.parametrize("purpose", ["director", "steering"])
@pytest.mark.parametrize("failure", [401, "cancelled"])
def test_sdk_terminal_failure_and_cancellation(records, purpose, failure):
    scenario = scenarios.load_scenario("S3A")

    async def handler(request):
        if failure == "cancelled":
            raise asyncio.CancelledError()
        return httpx2.Response(401, json={"error": {"type": "authentication_error", "message": "test"}})

    async def run():
        async with httpx2.AsyncClient(transport=httpx2.MockTransport(handler)) as http:
            client = llm.text_client().with_options(http_client=http)
            if purpose == "director":
                controller = director.Director(scenario, client=client)
                call = controller.route([{"speaker": "user", "text": "Hello"}], "Hello")
            else:
                controller = steering.SteeringController(scenario, client=client)
                call = controller.review([{"speaker": "user", "text": "Hello"}],
                                         scenario.initial_personas(), {})
            if failure == "cancelled":
                with pytest.raises(asyncio.CancelledError):
                    await call
            elif purpose == "director":
                assert await call  # The existing liveness fallback remains.
            else:
                with pytest.raises(Exception):
                    await call
    asyncio.run(run())
    assert len(records) == 1
    assert records[0]["outcome"] == ("cancelled" if failure == "cancelled" else "http_error")
    assert records[0]["status"] == (None if failure == "cancelled" else 401)


def test_actor_and_tts_early_eof_are_closed(records, monkeypatch):
    class ClosedTTS(FakeTTS):
        async def __anext__(self):
            raise StopAsyncIteration

    async def open_tts():
        return ClosedTTS()

    monkeypatch.setattr(cascade, "open_tts", open_tts)
    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(
                lambda req: httpx.Response(200, content=b""))) as http:
            await cascade.CascadeSocket(http=http)._respond("r")
    asyncio.run(run())
    assert len(records) == 2
    assert {r["purpose"] for r in records} == {"actor", "tts"}
    assert all(r["outcome"] == "closed" for r in records)


def test_tts_registration_failure_releases_slot_and_records_once(records, monkeypatch):
    async def open_tts():
        return FakeTTS(send_error=RuntimeError("test"))
    monkeypatch.setattr(cascade, "open_tts", open_tts)
    async def run():
        sem = asyncio.Semaphore(1)
        monkeypatch.setattr(cascade, "_tts_slot", lambda: sem)
        with pytest.raises(RuntimeError):
            await cascade.CascadeSocket()._open_reply_tts("v", "r", "i", time.time())
        assert not sem.locked()
    asyncio.run(run())
    assert len(records) == 1 and records[0]["outcome"] == "exception"


@pytest.mark.parametrize("flag", [None, "1", "false", "0"])
def test_storage_preflight_default_and_explicit_skip(monkeypatch, capsys, flag):
    calls = []
    client = SimpleNamespace(
        head_bucket=lambda **kw: calls.append(("HEAD", kw)),
        put_object=lambda **kw: calls.append(("PUT", kw)))
    if flag is not None:
        monkeypatch.setenv("STORAGE_PREFLIGHT", flag)
    def make_client(*args, **kwargs):
        calls.append(("client", args))
        return client
    monkeypatch.setattr(video.boto3, "client", make_client)
    result = storage_preflight()
    if flag == "0":
        assert result["ok"] is None
        assert result["skipped"] is True and calls == []
        assert capsys.readouterr().out.splitlines() == [
            "  Storage preflight skipped (STORAGE_PREFLIGHT=0)."]
        assert storage_preflight(client=client) == result
        assert calls == []
    else:
        assert result["ok"] is True
        assert capsys.readouterr().out == ""
        assert "skipped" not in result
        assert calls == [("client", ("s3",)), ("HEAD", {"Bucket": video.BUCKET}),
                         ("PUT", {"Bucket": video.BUCKET, "Key": video.PREFLIGHT_KEY,
                                  "Body": b"relational-fluency storage preflight\n"})]


def test_storage_preflight_skip_uses_file_setting_before_environment(monkeypatch, capsys):
    monkeypatch.setattr(llm, "_FILE", {"STORAGE_PREFLIGHT": " 0 "})
    monkeypatch.setenv("STORAGE_PREFLIGHT", "1")

    def forbidden_client(*args, **kwargs):
        pytest.fail("skipped preflight must not construct an S3 client")

    monkeypatch.setattr(video.boto3, "client", forbidden_client)
    result = storage_preflight()
    assert result["ok"] is None and result["skipped"] is True
    assert capsys.readouterr().out.splitlines() == [
        "  Storage preflight skipped (STORAGE_PREFLIGHT=0)."]
