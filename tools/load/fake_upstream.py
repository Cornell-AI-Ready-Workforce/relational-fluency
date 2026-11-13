"""Gateway and ElevenLabs protocol doubles. No external service is called."""
from __future__ import annotations

import argparse
import asyncio
import base64
from dataclasses import asdict, dataclass, field
import io
import json
import math
from pathlib import Path
import random
import re
import struct
import time
import wave

from fastapi import FastAPI, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import JSONResponse, StreamingResponse

from tools.sim import stim


@dataclass
class Distribution:
    """Uniform bounds; equal bounds give a constant. All times are ms."""
    low: float
    high: float

    def __post_init__(self):
        if not (math.isfinite(self.low) and math.isfinite(self.high)
                and 0 <= self.low <= self.high):
            raise ValueError("distribution requires finite 0 <= low <= high")

    def draw(self, rng):
        return rng.uniform(self.low, self.high)


@dataclass
class FakeConfig:
    seed: int = 20261112
    # measured 2026-11-12: the Cornell gateway kept an idle keep-alive
    # connection open > 150 s; the server's pool expires at 55 s.
    http_keepalive_s: float = 160.0
    models_ms: Distribution = field(default_factory=lambda: Distribution(10, 30))
    stt_ms: Distribution = field(default_factory=lambda: Distribution(1000, 1500))
    actor_first_ms: Distribution = field(default_factory=lambda: Distribution(1400, 2600))
    token_ms: Distribution = field(default_factory=lambda: Distribution(25, 55))
    messages_ms: Distribution = field(default_factory=lambda: Distribution(300, 900))
    tts_first_ms: Distribution = field(default_factory=lambda: Distribution(150, 350))
    reply_words: Distribution = field(default_factory=lambda: Distribution(15, 35))
    tts_words_per_minute: float = 160.0
    tts_realtime_factor: float = 2.0
    second_speaker_probability: float = 0.25
    tool_call_probability: float = 0.01
    audio_chunk_ms: int = 100
    tool_call_every: int = 0
    model_ids: list[str] = field(default_factory=list)
    # Route -> statuses consumed in order, e.g. {"tts_ws": [409, 429]}.
    failures: dict[str, list[int]] = field(default_factory=dict)

    def __post_init__(self):
        if not math.isfinite(self.http_keepalive_s) or self.http_keepalive_s <= 0:
            raise ValueError("positive finite HTTP keep-alive timeout required")
        if (not math.isfinite(self.tts_words_per_minute) or self.tts_words_per_minute <= 0
                or not isinstance(self.audio_chunk_ms, int) or not 1 <= self.audio_chunk_ms <= 1000):
            raise ValueError("positive speech rate and 1..1000 ms chunks required")
        if (self.reply_words.low < 1 or not isinstance(self.tool_call_every, int)
                or self.tool_call_every < 0):
            raise ValueError("positive reply size and nonnegative tool frequency required")
        if not math.isfinite(self.tts_realtime_factor) or self.tts_realtime_factor <= 0:
            raise ValueError("positive finite TTS realtime factor required")
        if any(not math.isfinite(p) or not 0 <= p <= 1 for p in
               (self.second_speaker_probability, self.tool_call_probability)):
            raise ValueError("speaker/tool probabilities must be in 0..1")
        allowed = {"models", "stt", "actor", "messages", "tts_ws"}
        if set(self.failures) - allowed or any(
                not isinstance(status, int) or not 400 <= status <= 599
                for statuses in self.failures.values() for status in statuses):
            raise ValueError("failure injection requires a known route and HTTP 400..599")

    @classmethod
    def from_dict(cls, values):
        values = dict(values)
        unknown = set(values) - set(cls.__dataclass_fields__)
        if unknown:
            raise ValueError("unknown fake configuration keys: " + ", ".join(sorted(unknown)))
        for name in ("models_ms", "stt_ms", "actor_first_ms", "token_ms", "messages_ms",
                     "tts_first_ms", "reply_words"):
            if name in values:
                values[name] = Distribution(**values[name])
        return cls(**values)


class StimulusMatcher:
    """Exact waveform anchors for the committed synthetic clips, not an ASR.

    Multiple anchors tolerate the server's leading/trailing quiet and VAD cuts.
    Ordered occurrences also handle resume/burst steps and repeated lines.
    """
    def __init__(self):
        self.unmatched_fragments = 0
        self.templates = []
        for name, (_, text) in stim.lines().items():
            pcm = stim.load(name)
            anchors = []
            for fraction in (0.2, 0.4, 0.6, 0.8):
                offset = int(len(pcm) * fraction) // 2 * 2
                anchor = pcm[offset:offset + 512]
                if len(anchor) == 512 and stim.rms(anchor) > 300:
                    anchors.append((offset, anchor))
            if anchors:
                self.templates.append((name, text, anchors))

    def transcribe(self, wav):
        with wave.open(io.BytesIO(wav), "rb") as reader:
            if (reader.getframerate(), reader.getnchannels(), reader.getsampwidth()) != (16000, 1, 2):
                raise ValueError("STT requires mono PCM16 WAV at 16 kHz")
            pcm = reader.readframes(reader.getnframes())
        if not pcm or stim.rms(pcm) < 160:
            return ""
        hits = []
        for name, text, anchors in self.templates:
            origins = {}
            for offset, anchor in anchors:
                at = pcm.find(anchor)
                while at >= 0:
                    origin = at - offset
                    origins[origin] = origins.get(origin, 0) + 1
                    at = pcm.find(anchor, at + 2)
            for origin, count in origins.items():
                if count >= 1:
                    hits.append((origin, name, text))
        if not hits:
            self.unmatched_fragments += 1
            # VAD can cut any valid clip between anchors. Preserve approximate
            # workload size; an ASR miss is not malformed wire protocol.
            count = max(1, round(len(pcm) / 32000 * 160 / 60))
            words = "I would like us to agree on a practical next step".split()
            return " ".join((words * (count // len(words) + 1))[:count])
        return " ".join(text for _, _, text in sorted(hits))


REPLY = ("I hear what you are saying, and I want to understand the specific concern. "
         "Tell me what changed and what you would like us to do next. "
         "We still need a clear plan that accounts for the team's commitments and the time available. "
         "What would you suggest as a practical first step for us?")


class FakeState:
    def __init__(self, config, log_path=None):
        self.config = config
        self.rng = random.Random(config.seed)
        self.counts = {}
        self.errors = []
        self.protocol = {"registrations": 0, "inputs": 0, "flushes": 0,
                         "keepalives": 0, "close_socket": 0, "audio_chunks": 0}
        self.log_path = log_path
        self.matcher = None

    def log(self, kind, **fields):
        row = {"t": time.time(), "kind": kind, **fields}
        if kind == "protocol_error":
            self.errors.append(row)
            print(json.dumps(row), flush=True)
        if self.log_path:
            with Path(self.log_path).open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(row) + "\n")
        return row

    def attempt(self, route):
        self.counts[route] = self.counts.get(route, 0) + 1
        index = self.counts[route] - 1
        statuses = self.config.failures.get(route, [])
        status = statuses[index] if index < len(statuses) else None
        self.log("request", route=route, status=status or (101 if route == "tts_ws" else 200))
        return status

    async def delay(self, distribution):
        await asyncio.sleep(distribution.draw(self.rng) / 1000)

    def error(self, path, detail, status=422):
        self.log("protocol_error", path=path, detail=detail)
        return JSONResponse({"error": {"type": "invalid_request_error", "message": detail}},
                            status_code=status)


def speaker_ids(system):
    # Actual director: '- Name (agent_id): ...'; steering: '- agent_id (Name): warmth=...'.
    director = re.findall(r"(?m)^- [^\n(]+ \(([a-zA-Z0-9_-]+)\):", system)
    steering = re.findall(r"(?m)^- ([a-zA-Z0-9_-]+) \([^\n)]+\): warmth=", system)
    return list(dict.fromkeys(steering or director))


def create_app(config=None, *, log_path=None):
    config = config or FakeConfig()
    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)
    state = app.state.fake = FakeState(config, log_path)

    @app.get("/__load/stats")
    async def stats():
        return {"counts": state.counts, "errors": state.errors, "protocol": state.protocol,
                "unmatched_fragment": state.matcher.unmatched_fragments if state.matcher else 0,
                "config": asdict(config)}

    @app.get("/v1/models")
    async def models():
        failure = state.attempt("models")
        if failure:
            return JSONResponse({"error": "injected"}, status_code=failure)
        await state.delay(config.models_ms)
        ids = set(config.model_ids)
        ids.update({"nto.gemini-4-argon", "nto.gemini-3.5-flash-lite",
                    "nto.gemini-live-2.5-flash-native-audio"})
        return {"object": "list", "data": [{"id": name, "object": "model"} for name in sorted(ids)]}

    @app.post("/v1/chat/completions")
    async def chat(request: Request):
        body = await request.json()
        route = "actor" if body.get("stream") else "stt"
        failure = state.attempt(route)
        request_number = state.counts[route]
        if failure:
            return JSONResponse({"error": "injected"}, status_code=failure)
        if route == "stt":
            try:
                content = body["messages"][0]["content"]
                encoded = next(p["input_audio"]["data"] for p in content if p.get("type") == "input_audio")
                wav = base64.b64decode(encoded, validate=True)
                if state.matcher is None:
                    state.matcher = await asyncio.to_thread(StimulusMatcher)
                text = await asyncio.to_thread(state.matcher.transcribe, wav)
            except (KeyError, IndexError, ValueError, StopIteration, TypeError, wave.Error) as exc:
                return state.error(request.url.path, str(exc))
            await state.delay(config.stt_ms)
            return {"choices": [{"message": {"role": "assistant", "content": text}}]}

        async def stream():
            await state.delay(config.actor_first_ms)
            words = REPLY.split()
            count = round(config.reply_words.draw(state.rng))
            text = " ".join((words * (count // len(words) + 1))[:count]).rstrip(".,?") + "."
            for word in text.split():
                yield "data: " + json.dumps({"choices": [{"delta": {"content": word + " "}}]}) + "\n\n"
                await state.delay(config.token_ms)
            tool_due = (request_number % config.tool_call_every == 0 if config.tool_call_every
                        else state.rng.random() < config.tool_call_probability)
            if tool_due and body.get("tools"):
                tool = body["tools"][0]
                name = (tool.get("function") or tool)["name"]
                for delta in (
                    {"index": 0, "id": f"call_fake_{request_number}", "type": "function", "function": {"name": name, "arguments": "{"}},
                    {"index": 0, "function": {"arguments": "}"}},
                ):
                    yield "data: " + json.dumps({"choices": [{"delta": {"tool_calls": [delta]}}]}) + "\n\n"
            yield "data: [DONE]\n\n"
        return StreamingResponse(stream(), media_type="text/event-stream")

    @app.post("/v1/messages")
    async def messages(request: Request):
        body = await request.json()
        failure = state.attempt("messages")
        if failure:
            return JSONResponse({"error": {"type": "overloaded_error", "message": "injected"}}, status_code=failure)
        try:
            tool = body["tool_choice"]["name"]
            ids = speaker_ids(body["system"])
            if not ids or tool not in {"set_speakers", "adjust_persona"}:
                raise ValueError("messages requires actual cast IDs and a known tool")
            chosen = ids[(state.counts["messages"] - 1) % len(ids)]
            speakers = [{"agent_id": chosen, "intent": "Ask for the next concrete step."}]
            if tool == "set_speakers" and len(ids) > 1 and state.rng.random() < config.second_speaker_probability:
                second = ids[(ids.index(chosen) + 1) % len(ids)]
                speakers.append({"agent_id": second, "intent": "Follow up on the next step."})
            data = ({"speakers": speakers,
                     "rationale": "Continue the discussion."} if tool == "set_speakers" else
                    {"adjustments": ([{"agent_id": chosen, "knob": "warmth", "level": "mid",
                                      "reason": "The participant asked for a clear next step."}]
                                     if state.counts["messages"] % 4 == 0 else [])})
        except (KeyError, ValueError, TypeError) as exc:
            return state.error(request.url.path, str(exc))
        await state.delay(config.messages_ms)
        return {"id": "msg_fake", "type": "message", "role": "assistant", "model": body.get("model"),
                "content": [{"type": "tool_use", "id": "tool_fake", "name": tool, "input": data}],
                "stop_reason": "tool_use", "usage": {"input_tokens": 300, "output_tokens": 50}}

    @app.websocket("/v1/text-to-dialogue/stream-input")
    async def tts(ws: WebSocket):
        failure = state.attempt("tts_ws")
        if failure:
            await ws.send_denial_response(JSONResponse({"error": "injected"}, status_code=failure))
            return
        if ws.query_params.get("output_format") != "pcm_24000" or not ws.query_params.get("model_id"):
            state.error(ws.url.path, "TTS requires model_id and pcm_24000")
            await ws.close(code=1008)
            return
        await ws.accept()
        pending = asyncio.Queue()
        voices = set()

        async def audio():
            first = True
            while True:
                text = await pending.get()
                if text is None:
                    await ws.send_json({"is_final": True})
                    return
                if first:
                    await state.delay(config.tts_first_ms)
                    first = False
                samples = max(1, round(len(text.split()) * 60 / config.tts_words_per_minute * 24000))
                chunk_samples = max(1, config.audio_chunk_ms * 24)
                for offset in range(0, samples, chunk_samples):
                    n = min(chunk_samples, samples - offset)
                    # Synthetic PCM, not a voice recording. Keep sample rate,
                    # packet size, duration and wall-clock cadence realistic.
                    pcm = struct.pack("<" + "h" * n, *(
                        round(1200 * math.sin(2 * math.pi * 220 * (offset + i) / 24000)) for i in range(n)))
                    await ws.send_json({"audio": base64.b64encode(pcm).decode("ascii")})
                    state.protocol["audio_chunks"] += 1
                    await asyncio.sleep(n / 24000 / config.tts_realtime_factor)

        worker = asyncio.create_task(audio())
        try:
            while True:
                message = await ws.receive_json()
                if "voices" in message:
                    if not message["voices"] or not all(isinstance(v, str) and v for v in message["voices"]):
                        raise ValueError("invalid voice registration")
                    voices.update(message["voices"])
                    state.protocol["registrations"] += 1
                elif message.get("keep_alive"):
                    state.protocol["keepalives"] += 1
                elif message.get("close_socket"):
                    state.protocol["close_socket"] += 1
                    pending.put_nowait(None)
                    await worker
                    return
                elif "inputs" in message:
                    for item in message["inputs"]:
                        if item.get("voice_id") not in voices or not isinstance(item.get("text"), str):
                            raise ValueError("input needs registered voice_id and text")
                        pending.put_nowait(item["text"])
                        state.protocol["inputs"] += 1
                    if message.get("flush"):
                        state.protocol["flushes"] += 1
                else:
                    raise ValueError("unknown TTS message")
        except WebSocketDisconnect:
            pass
        except (ValueError, KeyError, TypeError) as exc:
            state.error(ws.url.path, str(exc))
            await ws.close(code=1008)
        finally:
            worker.cancel()
            await asyncio.gather(worker, return_exceptions=True)

    @app.api_route("/{path:path}", methods=["GET", "POST", "PUT", "DELETE", "PATCH", "HEAD", "OPTIONS"])
    async def unknown(request: Request, path: str):
        return state.error(request.url.path, "unknown route", status=404)

    @app.websocket("/{path:path}")
    async def unknown_ws(ws: WebSocket, path: str):
        state.error(ws.url.path, "unknown websocket route", status=404)
        await ws.close(code=1008)

    return app


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=8443)
    parser.add_argument("--config", type=Path)
    parser.add_argument("--log", type=Path)
    parser.add_argument("--ssl-cert", type=Path)
    parser.add_argument("--ssl-key", type=Path)
    args = parser.parse_args(argv)
    config = FakeConfig.from_dict(json.loads(args.config.read_text(encoding="utf-8"))) if args.config else FakeConfig()
    import uvicorn
    uvicorn.run(create_app(config, log_path=args.log), host="0.0.0.0", port=args.port,
                ssl_certfile=str(args.ssl_cert) if args.ssl_cert else None,
                ssl_keyfile=str(args.ssl_key) if args.ssl_key else None, access_log=False,
                timeout_keep_alive=config.http_keepalive_s)


if __name__ == "__main__":
    main()
