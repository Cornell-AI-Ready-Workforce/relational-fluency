"""Actual cascade/SDK clients against an in-process fake upstream."""
import asyncio
import base64
import json

import httpx
import httpx2
import pytest
from fastapi.testclient import TestClient
from starlette.testclient import WebSocketDenialResponse
from websockets.exceptions import InvalidStatus
from websockets.http11 import Response

from server import director, llm, scenarios, steering, upstream_telemetry
from server.voice import cascade
from tools.load.fake_upstream import Distribution, FakeConfig, StimulusMatcher, create_app, speaker_ids
from tools.sim import stim


def fast_config(**kwargs):
    values = {name: Distribution(0, 0) for name in (
        "models_ms", "stt_ms", "actor_first_ms", "token_ms", "messages_ms", "tts_first_ms")}
    values.update(reply_words=Distribution(30, 30), tts_words_per_minute=60000, **kwargs)
    return FakeConfig(**values)


class SocketAdapter:
    def __init__(self, client, url):
        self.context = client.websocket_connect(url)
        try:
            self.socket = self.context.__enter__()
        except WebSocketDenialResponse as exc:
            raise InvalidStatus(Response(exc.status_code, "injected", {}, body=exc.content)) from exc

    async def send(self, raw):
        await asyncio.to_thread(self.socket.send_text, raw)

    def __aiter__(self):
        return self

    async def __anext__(self):
        return await asyncio.to_thread(self.socket.receive_text)

    async def close(self):
        await asyncio.to_thread(self.context.__exit__, None, None, None)


@pytest.fixture
def isolated_llm(monkeypatch):
    monkeypatch.setattr(llm, "_FILE", {})
    monkeypatch.setenv("LLM_BASE_URL", "http://fake")
    monkeypatch.setenv("LITELLM_API_KEY", "test-only-key")
    monkeypatch.setenv("ELEVENLABS_API_KEY", "test-only-key")
    monkeypatch.setenv("ELEVENLABS_WS_URL", "ws://fake/v1/text-to-dialogue/stream-input")
    monkeypatch.setattr(upstream_telemetry, "_listeners", [])


def test_models_and_real_cascade_stt_actor_tool_and_tts_contract(isolated_llm, monkeypatch):
    app = create_app(fast_config(tool_call_every=1))
    seen = []
    upstream_telemetry.add_listener(seen.append)
    with TestClient(app) as client:
        monkeypatch.setattr(httpx, "get", lambda url, **kw: client.get("/v1/models"))
        result = llm.preflight()
        assert result["ok"], result

        async def connect(url, **kwargs):
            assert url.startswith("ws://fake/v1/text-to-dialogue/stream-input?")
            return await asyncio.to_thread(SocketAdapter, client, url)
        monkeypatch.setattr(cascade.websockets, "connect", connect)

        async def run():
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app)) as http:
                sock = cascade.CascadeSocket(http=http, api_key="test")
                assert await sock._hear(stim.load("a1")) == stim.text("a1")
                sock.tools = [{"type": "function", "function": {
                    "name": "end_conversation", "parameters": {"type": "object", "properties": {}}}}]
                await sock._respond("r")
                frames = []
                while not sock._out.empty():
                    frames.append(json.loads(sock._out.get_nowait()))
                assert not [frame for frame in frames if frame["type"] == "error"]
                assert any(frame["type"] == "response.output_audio.delta" for frame in frames)
                call = next(frame for frame in frames if frame["type"] == "response.function_call_arguments.done")
                assert call["name"] == "end_conversation" and call["arguments"] == "{}"
                text = next(frame["transcript"] for frame in frames if frame["type"] == "response.output_audio_transcript.done")
                assert len(text.split()) == 30
        asyncio.run(run())
        stats = client.get("/__load/stats").json()
    assert not stats["errors"]
    assert stats["counts"] == {"models": 1, "stt": 1, "actor": 1, "tts_ws": 1}
    assert stats["protocol"]["registrations"] == stats["protocol"]["close_socket"] == 1
    assert stats["protocol"]["inputs"] == stats["protocol"]["flushes"] >= 2
    assert stats["protocol"]["audio_chunks"] > 0
    assert len(seen) == 4 and all(row["outcome"] == "ok" for row in seen)


@pytest.mark.parametrize("scenario_id", ["S1A", "S2A", "S3A", "S4A"])
def test_actual_director_and_steering_prompts_have_valid_tool_responses(isolated_llm, scenario_id):
    app = create_app(fast_config())
    scenario = scenarios.load_scenario(scenario_id)
    valid = {agent.id for agent in scenario.cast}

    async def run():
        async def check_stop(response):
            await response.aread()
            assert response.json()["stop_reason"] == "tool_use"

        async with httpx2.AsyncClient(transport=httpx2.ASGITransport(app),
                                     event_hooks={"response": [check_stop]}) as http:
            client = llm.text_client().with_options(http_client=http)
            route = director.Director(scenario, client=client)
            control = steering.SteeringController(scenario, client=client)
            for _ in range(4):
                decisions = await route.route([{"speaker": "user", "text": "What next?"}], "What next?")
                assert decisions and {row["agent_id"] for row in decisions} <= valid
                adjustments = await control.review([{"speaker": "user", "text": "What next?"}],
                                                   scenario.initial_personas(), {})
                assert {row["agent_id"] for row in adjustments} <= valid
            assert not app.state.fake.errors
    asyncio.run(run())


def test_real_tts_handshake_retries_and_keepalive(isolated_llm, monkeypatch):
    app = create_app(fast_config(failures={"tts_ws": [409, 429]}))
    records = []
    upstream_telemetry.add_listener(records.append)
    with TestClient(app) as client:
        async def connect(url, **kwargs):
            return await asyncio.to_thread(SocketAdapter, client, url)
        monkeypatch.setattr(cascade.websockets, "connect", connect)
        async def run():
            sock = cascade.CascadeSocket()
            ws = await sock._connect_tts(queue_wait_ms=12)
            await ws.send('{"voices":["voice"]}')
            await ws.send('{"keep_alive":true}')
            await ws.send('{"inputs":[{"voice_id":"voice","text":"Hello there."}],"flush":true}')
            await ws.send('{"close_socket":true}')
            await sock._relay_audio(ws, "r", "i", 0)
            await ws.close()
        asyncio.run(run())
        stats = client.get("/__load/stats").json()
    assert [row["status"] for row in records] == [409, 429, 101]
    assert [row["attempt"] for row in records] == [1, 2, 3]
    assert stats["protocol"]["keepalives"] == 1
    assert not stats["errors"]


def test_unknown_routes_and_bad_audio_are_logged_errors():
    app = create_app(fast_config())
    with TestClient(app) as client:
        assert client.get("/v1/not-implemented").status_code == 404
        assert client.post("/v1/chat/completions", json={"messages": []}).status_code == 422
        assert len(client.get("/__load/stats").json()["errors"]) == 2


def test_matcher_handles_every_committed_clip_quiet_and_joined_turns():
    matcher = StimulusMatcher()
    for name in stim.lines():
        pcm = b"\0" * 3200 + stim.load(name) + b"\0" * 3200
        assert matcher.transcribe(cascade.wav_bytes(pcm, 16000)) == stim.text(name), name
    joined = stim.load("c5") + b"\0" * 32000 + stim.load("c6")
    assert matcher.transcribe(cascade.wav_bytes(joined, 16000)) == stim.text("c5") + " " + stim.text("c6")
    assert matcher.transcribe(cascade.wav_bytes(stim.room_tone(1), 16000)) == ""


def test_fake_distribution_validation_and_steering_ids():
    with pytest.raises(ValueError):
        Distribution(4, 3)
    with pytest.raises(ValueError):
        FakeConfig.from_dict({"unknown": 1})
    assert speaker_ids("- alex (Alex): warmth=mid, formality=low") == ["alex"]


def test_default_http_keepalive_exceeds_server_pool_expiry():
    from server.voice.cascade import GATEWAY_KEEPALIVE_S
    assert FakeConfig().http_keepalive_s > GATEWAY_KEEPALIVE_S


@pytest.mark.parametrize("override", [None, 180.5])
def test_http_keepalive_configuration_reaches_uvicorn(monkeypatch, tmp_path, override):
    import uvicorn
    from tools.load import fake_upstream
    calls = []
    monkeypatch.setattr(uvicorn, "run", lambda app, **kwargs: calls.append(kwargs))
    args = []
    if override is not None:
        config = tmp_path / "fake.json"
        config.write_text(json.dumps({"http_keepalive_s": override}), encoding="utf-8")
        args = ["--config", str(config)]
    fake_upstream.main(args)
    assert len(calls) == 1
    assert calls[0]["timeout_keep_alive"] == (FakeConfig().http_keepalive_s if override is None else override)


@pytest.mark.parametrize("value", [0, -1, float("inf"), float("nan")])
def test_http_keepalive_rejects_invalid_timeouts(value):
    with pytest.raises(ValueError, match="HTTP keep-alive"):
        FakeConfig.from_dict({"http_keepalive_s": value})


def d3_hearing(body):
    """Copy of CascadeSocket._hear validation at D3 48f14bc, with no branch import."""
    choices = body.get("choices") if isinstance(body, dict) else None
    if not isinstance(choices, list) or not choices:
        raise ValueError("STT response has no choices")
    choice = choices[0]
    if not isinstance(choice, dict) or choice.get("finish_reason") != "stop":
        raise ValueError("STT response did not finish with stop")
    message = choice.get("message")
    if not isinstance(message, dict) or "content" not in message:
        raise ValueError("STT response has no message content")
    if any("refusal" in part for part in (body, choice, message)):
        raise ValueError("STT response contains a refusal")
    content = message["content"]
    if content is not None and not isinstance(content, str):
        raise ValueError("STT response content is not text or null")
    return (content or "").strip() or "[no speech]"


@pytest.mark.parametrize("model", ["nto.gemini-3.5-transcribe", "nto.gemini-3.5-flash-lite"])
@pytest.mark.parametrize("stimulus", ["a1", "c5", "silence", "room-tone"])
def test_fake_stt_wire_shape_passes_d3_validation(model, stimulus):
    quiet = stimulus in {"silence", "room-tone"}
    pcm = (b"\0" * 32000 if stimulus == "silence" else stim.room_tone(1)
           if stimulus == "room-tone" else stim.load(stimulus))
    expected = ("[no speech]" if model.endswith("flash-lite") else None) if quiet else stim.text(stimulus)
    body = {"model": model, "messages": [{"role": "user", "content": [
        {"type": "text", "text": 'Write "[no speech]" if there is no speech.'},
        {"type": "input_audio", "input_audio": {
            "data": base64.b64encode(cascade.wav_bytes(pcm, 16000)).decode(), "format": "wav"}},
    ]}]}
    with TestClient(create_app(fast_config())) as client:
        assert model in {row["id"] for row in client.get("/v1/models").json()["data"]}
        response = client.post("/v1/chat/completions", json=body)
        assert response.status_code == 200
        result = response.json()
        assert result["choices"] == [{"index": 0, "finish_reason": "stop", "message": {
            "role": "assistant", "content": expected}}]
        assert d3_hearing(result) == ("[no speech]" if quiet else stim.text(stimulus))
        assert client.get("/__load/stats").json()["errors"] == []


@pytest.mark.parametrize("body", [
    {}, {"choices": []}, {"choices": [{"message": {"content": "hello"}}]},
    {"choices": [{"finish_reason": "length", "message": {"content": "hello"}}]},
    {"choices": [{"finish_reason": "stop", "message": {}}]},
    {"choices": [{"finish_reason": "stop", "message": {"content": []}}]},
    {"choices": [{"finish_reason": "stop", "message": {"content": None, "refusal": "no"}}]},
])
def test_d3_validation_rejects_incomplete_or_malformed_stt(body):
    with pytest.raises(ValueError):
        d3_hearing(body)


@pytest.mark.parametrize("model", ["nto.gemini-3.5-transcribe", "nto.gemini-3.5-flash-lite"])
def test_cascade_stt_model_selects_fake_quiet_shape(isolated_llm, monkeypatch, model):
    monkeypatch.setenv("CASCADE_STT_MODEL", model)
    app = create_app(fast_config())
    responses = []

    async def run():
        async def check(response):
            await response.aread()
            assert json.loads(response.request.content)["model"] == model
            responses.append(response.json())
            assert d3_hearing(responses[-1]) == "[no speech]"

        async with httpx.AsyncClient(transport=httpx.ASGITransport(app),
                                     event_hooks={"response": [check]}) as http:
            sock = cascade.CascadeSocket(http=http, api_key="test")
            await sock._hear(stim.room_tone(1))

    asyncio.run(run())
    assert len(responses) == 1
    assert responses[0]["choices"][0]["message"]["content"] == (
        "[no speech]" if model.endswith("flash-lite") else None)


@pytest.mark.parametrize("with_tool", [False, True])
def test_actor_sse_has_terminal_stop_before_done(with_tool):
    body = {"model": "nto.gemini-4-argon", "stream": True, "messages": []}
    if with_tool:
        body["tools"] = [{"type": "function", "function": {"name": "end_conversation"}}]
    with TestClient(create_app(fast_config(tool_call_every=1))) as client:
        response = client.post("/v1/chat/completions", json=body)
    assert response.status_code == 200
    frames = [line.removeprefix("data: ") for line in response.text.splitlines() if line.startswith("data: ")]
    assert frames[-1] == "[DONE]"
    choices = [json.loads(frame)["choices"][0] for frame in frames[:-1]]
    assert all(choice["index"] == 0 for choice in choices)
    assert choices[-1] == {"index": 0, "delta": {}, "finish_reason": "stop"}
    assert all(choice["finish_reason"] is None for choice in choices[:-1])
    assert any("content" in choice["delta"] for choice in choices)
    assert any("tool_calls" in choice["delta"] for choice in choices) == with_tool
