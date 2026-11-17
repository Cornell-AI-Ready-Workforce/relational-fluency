"""Cascaded voice: Argon hears and thinks, ElevenLabs speaks, a fast Gemini
writes the transcript.

The study's voice used to be ONE speech-to-speech model behind the gateway's
realtime socket. This module replaces that model with three steps, and does it
without touching the 11,000 lines that drive a conversation: it is a stand-in
for the WEBSOCKET, not for RealtimeVoiceSession. RealtimeVoiceSession.connect()
builds a CascadeSocket instead of dialling the gateway when the model's family
is `cascade`, and from there every frame the session sends (session.update,
input_audio_buffer.*, conversation.item.*, response.create/cancel) is answered
with the frames a realtime server would send, so the bridge's turn handling,
audio-recovery, cancellation and the runner and room above them all run as
they did.

    participant PCM  -->  Argon (chat input_audio)  -->  user transcript
    history + brief  -->  Argon (streamed text)     -->  sentences
    each sentence    -->  ElevenLabs (one websocket per reply) --> agent audio

Both model ids are settings (CASCADE_LLM_MODEL, ELEVENLABS_TTS_ID), and the
ElevenLabs key is ELEVENLABS_API_KEY. What this stand-in does NOT have, because
a cascade has no server-side conversation to edit:

  * Server-side turn detection. The runner's own VAD owns end-of-turn, as on the
    gpt route (needs_turn_detection_null).
  * Its own idea of what was heard. Cancelling stops the reply
    mid-generation; with no audio emitted its assistant item is removed (and
    a delete of it later is acked), but after audio starts the item keeps the
    generated text, because the TTS stream has no sentence alignment or
    page-playback acknowledgement.

In a ROOM that is put right from outside (pipeline 2026-11-16b, #297): a
cascade room runs 10-01a's room memory hygiene like a gpt room. The bridge
mirrors these items, and the runner deletes a reply nobody heard and puts
back the heard words of one cut off, read from its own playback clock; a
colleague's told note is corrected the same way. The deletes and inserts are
applied to `items` at once (previous_item_id "root" is the front). Retry and
unanswered nudges stay user items. CASCADE_ROOM_MEMORY=0 restores the
11-16a cascade room, which kept every generated line whole.
"""
import asyncio
import base64
import io
import json
import logging
import os
import re
import time
import uuid
import wave
import weakref
from typing import Optional

import httpx
import websockets

from ..llm import gateway_api_key, gateway_base_url, setting
from ..reply_trace import observe

log = logging.getLogger(__name__)
from ..upstream_telemetry import _Attempt, record

# What the cascade calls, read at use so a test or an operator can change them.
DEFAULT_TTS_MODEL = "eleven_v4"
# One websocket per reply. The text-to-dialogue stream is the one that takes
# eleven_v3/v4 (the text-to-speech websocket refuses them, measured
# 2026-10-15); sentences sent into one stream keep one delivery, where one
# HTTP request per sentence re-decided pitch and tone every sentence (the same
# reply came back at 273, 180, 174 and 226 Hz).
TTS_WS = "wss://api.elevenlabs.io/v1/text-to-dialogue/stream-input"

# ElevenLabs returns raw PCM at the rate named in output_format. 24 kHz is the
# rate the rest of the bridge already treats the gateway's audio as
# (GATEWAY_OUTPUT_RATE), so nothing downstream resamples differently.
OUTPUT_RATE = 24000

# The roster. These names are what a scenario's gemini casting maps onto (see
# the `cascade` row's voice_aliases); the ids are ElevenLabs' current default
# (premade) voices, listed from the account on 2026-10-15.
# ELEVENLABS_VOICE_IDS (a JSON object, name -> voice id) overrides or extends
# the table, which is how a researcher casts a cloned or library voice.
PREMADE_VOICES = {
    "will": "bIHbv24MWmeRgasZH58o",       # relaxed optimist, young male
    "chris": "iP95p4xoKVk53GoZ742B",      # charming, down-to-earth
    "eric": "cjVigY5qzO86Huf0OWal",       # smooth, trustworthy
    "brian": "nPczCjzI2devNBz1zQrb",      # deep, resonant, comforting
    "roger": "CwhRBWXzGAHq8TQ4Fs17",      # laid-back, casual, resonant
    "liam": "TX3LPaxmHKxFdv7VOQHJ",       # energetic, confident
    "charlie": "IKne3meq5aSn9XLyUdCD",    # deep, confident, energetic
    "river": "SAz9YHcvj6GT2YYXdXww",      # relaxed, neutral
    "sarah": "EXAVITQu4vr4xnSDxMaL",      # mature, reassuring, confident
    "jessica": "cgSgspJ2msm6clMCkdW9",    # playful, bright, warm
    "laura": "FGY2WhTYpPnrIDTdsKH5",      # quirky, sassy
    "matilda": "XrExE9yKIg1WjnnlVkGX",    # knowledgeable, upbeat
    "bella": "hpp4J3VqNfWAUOO0d1Us",      # professional, bright, warm
    "lily": "pFZP5JQG7iQjIQuC4Bku",       # velvety, confident (British)
    "alice": "Xb7hH8MSUJpSbSDYk0k2",      # clear, engaging (British)
    "george": "JBFqnCBsd6RMkjVDRZzb",     # warm (British)
}
# Voice Library clones the study casts (2026-10-15, chosen by ear from the
# most-used conversational clones; see scenarios/v3/*.yaml `cascade:`). A
# library voice speaks on this account without being added to it (measured).
LIBRARY_VOICES = {
    "hope": "uYXf8XasLslADfZ2MB4u",         # Hope - Bubbly, Gossipy and Girly
    "joseph": "J5iaaqzR5zn6HFG4jV3b",       # Joseph - Nasally and Steady
    "armando": "TWUKKXAylkYxxlPe4gx0",      # Armando - Forthright and Stentorian
    "zuri": "C3x1TEM7scV4p2AXJyrp",         # Zuri - Casual, Calm and Clear
    "eryn": "kdnRe2koJdOK4Ovxn2DI",         # Eryn - Genuine, Friendly and Natural
    "hale": "wWWn96OtTHu1sn8SRGEr",         # Hale - Expressive, Deep and Emotive
    "lucius": "HaUDdkOAoitiVjpiet1i",       # Lucius - Calm, Pleasant and Reassuring
    "callie": "7YaUDeaStRuoYg3FKsmU",       # Callie - Playful, Inviting and Friendly
    "jessica_anne": "g6xIsTj2HwM6VR4iXFCw", # Jessica Anne Bogart - Chatty and Friendly
    "alexandra": "kdmDKE6EkgrWrrykO9Qt",    # Alexandra - Conversational and Natural
    "ivanna": "yM93hbw8Qtvdma2wCnJG",       # Ivanna - Young, Versatile and Casual
    "mark": "1SM7GgM6IMuvQlz2BwM3",         # Mark - Casual, Relaxed and Light
    "burt": "kdVjFjOXaqExaDvXZECX",         # Burt - Calm, Steady and Conversational
    "jessica_playful": "r1KmysJdVYZjJCm4mL3b",  # Jessica - Playful, Bright, Warm (library)
}
PREMADE_VOICES.update(LIBRARY_VOICES)
VOICE_ROSTER = tuple(PREMADE_VOICES)

# Cut a streamed reply into speakable pieces at sentence ends. The first piece
# is allowed to be short (it is the one that decides time-to-first-audio); later
# ones are held to a few words so a stray "Mr." does not become its own request.
_SENTENCE_END = re.compile(r"(?<=[.!?…])[\"')\]]*\s+")
_FIRST_MIN_CHARS = 8
_LATER_MIN_CHARS = 24

# Participants heard eleven_v4's pitch jump at ", and" inside a reply: the
# comma seems to be where ElevenLabs cuts a long sentence and starts its
# delivery afresh. What it is sent drops that comma; the transcript keeps it.
_COMMA_AND = re.compile(r",\s+(and)\b", re.IGNORECASE)


def llm_model() -> str:
    return setting("CASCADE_LLM_MODEL", "nto.gemini-4-argon")


def tts_model() -> str:
    return setting("ELEVENLABS_TTS_ID", DEFAULT_TTS_MODEL) or DEFAULT_TTS_MODEL


def stt_model() -> str:
    """The model that writes the participant's transcript. Measured on the
    gateway 2026-11-12: transcribe's speech requests were about 300 ms faster
    at p50 than flash-lite's, with fewer word errors and better names. This
    excludes encoding, slot waits and backoff; room-turn latency and capacity
    were not measured. Silence/room tone returned content:null with stop;
    transcribe ignores the text prompt (including language and name hints).
    _hear validates that response before mapping it to [no speech]. The reply
    model still hears the audio itself; a room waits for this transcript."""
    return setting("CASCADE_STT_MODEL", "nto.gemini-3.5-transcribe")


def reasoning_effort() -> str:
    """Argon's reasoning_effort for a reply. Blank (the default) sends no such
    field, so the model thinks as it would by default; "none" switches
    thinking off and was measured 2026-10-15 to cut first-token latency from
    1.4-2.6 s to a steady 1.4 s."""
    return setting("CASCADE_REASONING_EFFORT", "")


def eleven_key() -> str:
    return setting("ELEVENLABS_API_KEY", "")


def tts_concurrency() -> int:
    """How many ElevenLabs requests this process keeps in flight at once.
    The account's limit is per plan (a 429 `concurrent_limit_exceeded` came
    back live on 2026-10-15 with one sentence per request fanned out as fast
    as the reply streamed); a sentence waits for a slot rather than failing."""
    try:
        return max(1, int(setting("ELEVENLABS_CONCURRENCY", "3")))
    except ValueError:
        return 3


_TTS_SLOTS: dict = {}


def _tts_slot() -> asyncio.Semaphore:
    """One semaphore per event loop (a Semaphore belongs to the loop it was
    first used on; tests and workers run their own)."""
    loop = asyncio.get_running_loop()
    sem = _TTS_SLOTS.get(loop)
    if sem is None:
        sem = _TTS_SLOTS[loop] = asyncio.Semaphore(tts_concurrency())
    return sem


# Keep a gateway connection open between turns. httpx's default is 5 s, which
# is shorter than the gap between two turns of a conversation, so every turn
# paid a new TCP and TLS handshake. Kept under the 60 s idle timeout common on
# load balancers, so a pooled connection is not one the far end has dropped.
GATEWAY_KEEPALIVE_S = 55.0

_HTTP_CLIENTS: "weakref.WeakKeyDictionary" = weakref.WeakKeyDictionary()


def _shared_http() -> httpx.AsyncClient:
    """One gateway client per event loop, shared by every CascadeSocket on
    it (a connection pool belongs to the loop it was opened on; tests and
    workers run their own). Never closed by a socket."""
    loop = asyncio.get_running_loop()
    client = _HTTP_CLIENTS.get(loop)
    if client is None or client.is_closed:
        client = _HTTP_CLIENTS[loop] = httpx.AsyncClient(
            timeout=httpx.Timeout(60.0, connect=10.0),
            # No cap on open connections: each socket's own client had a
            # cap of 100, so a process-wide 100 would be a new queue.
            limits=httpx.Limits(max_connections=None,
                                max_keepalive_connections=20,
                                keepalive_expiry=GATEWAY_KEEPALIVE_S))
    return client


# How long a reply's stream is read past [DONE] for the body's end.
DRAIN_AFTER_DONE_S = 0.25

# websockets.connect defaults close_timeout to 10 s (local 16.1.1 client).
# The TTS slot stays held through close, and the next turn may already be
# waiting. Half a second bounds that handshake; the cancel join leaves another
# half second for the HTTP drain (at most 0.25 s) and task cleanup.
TTS_CLOSE_TIMEOUT_S = 0.5
REPLY_CANCEL_TIMEOUT_S = 1.0


async def _drain(lines, within_s: float) -> None:
    """Consume what is left of a streamed body, for `within_s` at most.
    Never raises: an unread end only costs the pooled connection."""
    async def rest():
        async for _ in lines:
            pass
    try:
        # Keep cancellation in this task when EOF and cancel arrive together.
        # Python 3.11's wait_for can otherwise return the finished child and
        # leave the reply waiting for TTS after cancellation was requested.
        async with asyncio.timeout(within_s):
            await rest()
    except asyncio.CancelledError:
        raise
    except Exception:  # noqa: BLE001 - see the docstring
        pass


def voice_ids() -> dict:
    ids = dict(PREMADE_VOICES)
    extra = setting("ELEVENLABS_VOICE_IDS", "")
    if extra:
        try:
            ids.update({str(k).lower(): str(v)
                        for k, v in json.loads(extra).items()})
        except (ValueError, AttributeError):
            pass
    return ids


def voice_id_for(name: str) -> str:
    ids = voice_ids()
    return ids.get((name or "").lower()) or ids[VOICE_ROSTER[0]]


def open_tts():
    """Connect one ElevenLabs stream. Looked up on the module at call time so
    a test can stand in a fake."""
    key = eleven_key()
    if not key:
        raise RuntimeError("ELEVENLABS_API_KEY is not set")
    url = (f"{setting('ELEVENLABS_WS_URL', TTS_WS)}"
           f"?model_id={tts_model()}&output_format=pcm_{OUTPUT_RATE}")
    return websockets.connect(url, additional_headers={"xi-api-key": key},
                              max_size=None, close_timeout=TTS_CLOSE_TIMEOUT_S)


async def _close_tts(ws) -> None:
    """Bound teardown even for an injected stream; a close is not reply work."""
    closed = False
    try:
        await asyncio.wait_for(ws.close(), TTS_CLOSE_TIMEOUT_S)
        closed = True
    except asyncio.TimeoutError:
        log.warning("Cascade TTS close exceeded %.1f s", TTS_CLOSE_TIMEOUT_S)
    except Exception as exc:  # noqa: BLE001 - closing must not lose response.done
        log.warning("Cascade TTS close failed: %s", type(exc).__name__)
    finally:
        # Our deadline or a second reply cancel can interrupt close() before
        # websockets reaches its own transport.abort(). Do not release a TTS
        # slot with that connection still open; cancellation still propagates.
        if not closed:
            transport = getattr(ws, "transport", None)
            if transport is not None:
                transport.abort()


def wav_bytes(pcm16: bytes, rate: int) -> bytes:
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(pcm16)
    return buf.getvalue()


def tts_text(sentence: str) -> str:
    """The words as ElevenLabs is sent them: `sentence` minus the comma
    before "and" (see _COMMA_AND). Nothing else changes."""
    return _COMMA_AND.sub(r" \1", sentence)


def split_sentences(buffer: str, *, first: bool, final: bool = False):
    """Peel complete sentences off the front of `buffer`.

    Returns (sentences, rest). With `final` everything left is a sentence."""
    out = []
    while True:
        m = _SENTENCE_END.search(buffer)
        floor = _FIRST_MIN_CHARS if (first and not out) else _LATER_MIN_CHARS
        while m and m.end() < floor and _SENTENCE_END.search(buffer, m.end()):
            m = _SENTENCE_END.search(buffer, m.end())
        if not m or m.end() < floor:
            break
        out.append(buffer[:m.end()].strip())
        buffer = buffer[m.end():]
    if final and buffer.strip():
        out.append(buffer.strip())
        buffer = ""
    return out, buffer


def _rt_tools(tools: list) -> list:
    """The realtime tool shape ({"type":"function","name",...}) as chat's."""
    out = []
    for t in tools or []:
        if not isinstance(t, dict):
            continue
        if "function" in t:
            out.append(t)
        else:
            out.append({"type": "function", "function": {
                k: t[k] for k in ("name", "description", "parameters") if k in t}})
    return out


class _GatewayStream:
    """Observe gateway completion without changing when the body is read."""

    def __init__(self, lines, attempt):
        self.lines, self.attempt = lines, attempt
        self.ended = False

    def __aiter__(self):
        return self

    def _observe(self, line):
        if line.startswith("data:") and line[5:].strip() == "[DONE]":
            self.ended = True
            self.attempt.finish()

    async def __anext__(self):
        try:
            line = await self.lines.__anext__()
        except StopAsyncIteration:
            self.attempt.finish(outcome="closed")
            raise
        except BaseException as exc:
            self.attempt.finish(exc=exc)
            raise
        self._observe(line)
        return line

    async def close(self):
        # TTS or the caller stopped the request before the gateway finished.
        self.attempt.finish(outcome="cancelled")


class _TtsSocket:
    """Keep one attempt open from the handshake through the final audio."""

    def __init__(self, ws, attempt):
        self.ws = ws
        self.attempt = attempt

    def __aiter__(self):
        return self.ws.__aiter__()

    @property
    def transport(self):
        # _close_tts aborts this when a close overruns its deadline.
        return getattr(self.ws, "transport", None)

    async def send(self, raw):
        try:
            await self.ws.send(raw)
        except BaseException as exc:
            self.attempt.finish(exc=exc)
            raise

    async def close(self):
        try:
            await self.ws.close()
        except BaseException as exc:
            self.attempt.finish(exc=exc)
            raise
        finally:
            self.attempt.finish(outcome="closed")


class CascadeSocket:
    """The slice of a websockets client the bridge uses (send, recv, close),
    answering as a realtime server would. One socket is one conversation."""

    def __init__(self, *, voice: str = "", api_key: str = "",
                 http: Optional[httpx.AsyncClient] = None,
                 session_id: Optional[str] = None) -> None:
        self.api_key = api_key or gateway_api_key()
        self.voice = voice
        self.session_id = session_id
        self.instructions = ""
        self.tools: list = []
        # The process's shared gateway client (_shared_http) unless one is
        # handed in. Taken lazily: a socket may be built off the loop.
        self._given_http = http
        self._out: asyncio.Queue = asyncio.Queue()
        self._closed = False
        self._audio = bytearray()
        self._rate = 16000
        # Conversation: [{"id", "role", "text"}] and function calls/outputs.
        self.items: list = []
        self._tasks: set = set()
        self._reply: Optional[asyncio.Task] = None
        self._reply_id: Optional[str] = None
        self._create_lock = asyncio.Lock()
        self._stt: dict = {}
        # Pending transcription tasks, awaited before a reply is generated so
        # the model never answers a turn it has not yet read.
        self._pending_stt: list = []
        self._finished: set = set()
        # Reply message items this socket removed itself (_respond: nothing
        # of the reply reached the bridge, or only a function call did), by
        # id, bounded. The bridge learned them from output_item.added; a room
        # member's memory delete of one is acked, not item_not_found (11-16b).
        self._dropped: dict = {}
        # Latencies of the latest turn, for the record and the benchmark.
        self.timings: dict = {}
        self._reply_trace = None

    @property
    def _http(self) -> httpx.AsyncClient:
        return self._given_http or _shared_http()

    # -- the websockets surface ------------------------------------------------
    async def send(self, raw) -> None:
        if self._closed:
            raise websockets.ConnectionClosedOK(None, None)
        try:
            ev = json.loads(raw)
        except (ValueError, TypeError):
            return
        handler = getattr(self, "_on_" + ev.get("type", "").replace(".", "_"), None)
        if handler is not None:
            await handler(ev)

    async def recv(self) -> str:
        item = await self._out.get()
        if item is None:
            raise websockets.ConnectionClosedOK(None, None)
        return item

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        observe(self, "socket_closed", id(self))
        for t in list(self._tasks):
            t.cancel()
        self._out.put_nowait(None)

    # -- plumbing --------------------------------------------------------------
    def _emit(self, ev: dict) -> None:
        if not self._closed:
            self._out.put_nowait(json.dumps(ev))

    def _spawn(self, coro) -> asyncio.Task:
        task = asyncio.ensure_future(coro)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        return task

    def _error(self, code: str, message: str, *, event_id: str = "") -> None:
        self._emit({"type": "error", "event_id": event_id, "error": {
            "type": "server_error", "code": code, "message": message,
            "event_id": event_id}})

    @staticmethod
    def _id(kind: str) -> str:
        return f"{kind}_{uuid.uuid4().hex[:12]}"

    def _add_item(self, role: str, text: str, *, iid: Optional[str] = None,
                  prev: Optional[str] = None, kind: str = "message",
                  **extra) -> dict:
        item = {"id": iid or self._id("item"), "role": role, "text": text,
                "type": kind, **extra}
        if prev == "root":
            # The front of the conversation, as on the gateway: the heard
            # words of a room member's first line go back where it was
            # (room memory, 11-16b), not after everything said since.
            self.items.insert(0, item)
        elif prev and any(i["id"] == prev for i in self.items):
            at = next(n for n, i in enumerate(self.items) if i["id"] == prev)
            self.items.insert(at + 1, item)
        else:
            self.items.append(item)
        return item

    @staticmethod
    def _wire_item(item: dict) -> dict:
        if item["type"] == "function_call_output":
            return {"id": item["id"], "type": "function_call_output",
                    "call_id": item.get("call_id"), "output": item["text"]}
        kind = "output_text" if item["role"] == "assistant" else "input_text"
        return {"id": item["id"], "type": "message", "role": item["role"],
                "content": [{"type": kind, "text": item["text"]}]}

    # -- client events ---------------------------------------------------------
    async def _on_session_update(self, ev: dict) -> None:
        s = ev.get("session") or {}
        if "instructions" in s:
            self.instructions = s["instructions"] or ""
        if s.get("voice"):
            self.voice = s["voice"]
        if "tools" in s:
            self.tools = _rt_tools(s["tools"])
        self._emit({"type": "session.updated", "session": {}})

    async def _on_input_audio_buffer_append(self, ev: dict) -> None:
        try:
            self._audio += base64.b64decode(ev.get("audio") or "")
        except (ValueError, TypeError):
            pass

    async def _on_input_audio_buffer_clear(self, ev: dict) -> None:
        self._audio.clear()
        self._emit({"type": "input_audio_buffer.cleared"})

    async def _on_input_audio_buffer_commit(self, ev: dict) -> None:
        observe(self, "commit_entered", id(self))
        pcm = bytes(self._audio)
        self._audio.clear()
        if not pcm:
            self._error("input_audio_buffer_commit_empty",
                        "the buffer is empty", event_id=ev.get("event_id", ""))
            return
        prev = self.items[-1]["id"] if self.items else None
        item = self._add_item("user", "")
        iid = item["id"]
        observe(self, "commit", iid, id(self))
        # Until its transcript lands the model is handed the AUDIO itself, so
        # the reply does not wait for the transcription round trip.
        item["audio"] = pcm
        self._emit({"type": "input_audio_buffer.committed", "item_id": iid,
                    "previous_item_id": prev})
        task = self._spawn(self._transcribe(iid, pcm))
        self._pending_stt.append(task)

    async def _on_conversation_item_create(self, ev: dict) -> None:
        item = ev.get("item") or {}
        eid = ev.get("event_id", "")
        prev = ev.get("previous_item_id")
        if item.get("type") == "function_call_output":
            made = self._add_item("tool", str(item.get("output") or ""),
                                  iid=item.get("id"), kind="function_call_output",
                                  call_id=item.get("call_id"))
        else:
            text = "".join(c.get("text", "") for c in item.get("content") or []
                           if isinstance(c, dict))
            made = self._add_item(item.get("role") or "user", text,
                                  iid=item.get("id"), prev=prev)
        self._emit({"type": "conversation.item.added", "event_id": eid,
                    "previous_item_id": prev, "item": self._wire_item(made)})

    async def _on_conversation_item_delete(self, ev: dict) -> None:
        iid = ev.get("item_id")
        before = len(self.items)
        self.items = [i for i in self.items if i["id"] != iid]
        if len(self.items) == before and self._dropped.pop(iid, None) is None:
            self._error("item_not_found", f"no item {iid}",
                        event_id=ev.get("event_id", ""))
            return
        self._emit({"type": "conversation.item.deleted", "item_id": iid,
                    "event_id": ev.get("event_id", "")})

    async def _on_response_create(self, ev: dict) -> None:
        rid = self._id("resp")
        # Relay rooms freeze at create, before either lock acquisition or
        # the cancelled reply's join can admit later participant text.
        messages = self._messages() if getattr(self, "snapshot_on_create", False) else None
        observe(self, "create", rid, id(self))
        # A turn-end cancel can be followed immediately by a commit/create.
        # Creates and cancels share this lock. A cancel arriving during the
        # join must target the replacement reply, not cancel the old one twice.
        async with self._create_lock:
            previous, previous_id = self._reply, self._reply_id
            if previous is not None and not previous.done():
                if not previous.cancelling():
                    observe(self, "done", rid, "refused_active_response")
                    self._error("conversation_already_has_active_response",
                                "a reply is already in progress",
                                event_id=ev.get("event_id", ""))
                    return
                try:
                    await asyncio.wait_for(previous, REPLY_CANCEL_TIMEOUT_S)
                except asyncio.CancelledError:
                    if asyncio.current_task().cancelling():
                        raise
                except asyncio.TimeoutError:
                    # wait_for cancels it again; _respond's inner finally
                    # emits done even if that interrupts TTS teardown.
                    log.warning("Cascade reply cancellation exceeded %.1f s",
                                REPLY_CANCEL_TIMEOUT_S)
                except Exception as exc:  # noqa: BLE001 - its reply has ended
                    log.warning("Cascade cancelled reply failed: %s", type(exc).__name__)
            if previous_id is not None:
                # Also closes a reply cancelled before its first task step.
                self._ensure_done(previous_id)
            if self._closed:
                return
            observe(self, "stamp", rid, "create_released")
            task = self._spawn(self._respond(rid, messages=messages))
            self._reply, self._reply_id = task, rid
            task.add_done_callback(lambda _t, rid=rid: self._ensure_done(rid))

    async def _on_response_cancel(self, ev: dict) -> None:
        async with self._create_lock:
            if self._reply is None or self._reply.done():
                self._error("response_cancel_not_active",
                            "no response is active", event_id=ev.get("event_id", ""))
                return
            self._reply.cancel()

    # -- steps -----------------------------------------------------------------
    async def _transcribe(self, iid: str, pcm: bytes) -> None:
        started = time.time()
        observe(self, "stt", iid, "started")
        outcome = "cancelled"
        text = ""
        try:
            text = await self._hear(pcm)
            outcome = "no_speech" if text == "[no speech]" else "completed"
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - a failed hearing is an event
            outcome = "failed"
            # No transcript is coming. _messages keeps the audio only while
            # the turn is current or a reply answered it; stale audio-only
            # user turns made flash-lite speak the participant's next line
            # (S2A 2026-11-14: 10/12 replays, 0/12 without them).
            for it in self.items:
                if it["id"] == iid:
                    it["stt_failed"] = True
            self._emit({"type": "conversation.item.input_audio_transcription.failed",
                        "item_id": iid, "error": {
                            "type": "server_error", "code": "stt_failed",
                            "message": f"{type(exc).__name__}: {exc}"[:200]}})
            return
        finally:
            self.timings["stt_ms"] = int((time.time() - started) * 1000)
            observe(self, "stt", iid, "done", outcome)
        for it in self.items:
            if it["id"] == iid:
                it["text"] = text
                it.pop("audio", None)
        self._emit({"type": "conversation.item.input_audio_transcription.completed",
                    "item_id": iid, "transcript": text})

    async def _hear(self, pcm: bytes) -> str:
        from ..speech_check import _prompt as hear_prompt
        payload = {
            "model": stt_model(), "max_tokens": 400, "temperature": 0,
            "messages": [{"role": "user", "content": [
                {"type": "text", "text": hear_prompt(())},
                {"type": "input_audio", "input_audio": {
                    "data": base64.b64encode(wav_bytes(pcm, self._rate)).decode("ascii"),
                    "format": "wav"}}]}],
        }
        with _Attempt(upstream="gateway", route="chat_completions", purpose="stt",
                      session_id=self.session_id) as attempt:
            r = await self._http.post(
                f"{gateway_base_url().rstrip('/')}/v1/chat/completions",
                headers={"Authorization": f"Bearer {self.api_key}"}, json=payload)
            attempt.fields["status"] = r.status_code
            r.raise_for_status()
            body = r.json()
            choices = body.get("choices") if isinstance(body, dict) else None
            if not isinstance(choices, list) or not choices:
                raise ValueError("STT response has no choices")
            choice = choices[0]
            if not isinstance(choice, dict):
                raise ValueError("STT response choice is not an object")
            finish_reason = choice.get("finish_reason")
            if finish_reason not in ("stop", "length"):
                raise ValueError("STT response did not finish with stop or length")
            message = choice.get("message")
            if not isinstance(message, dict) or "content" not in message:
                raise ValueError("STT response has no message content")
            for part in (body, choice, message):
                fields = part.get("provider_specific_fields")
                if part.get("refusal") not in (None, "") or (
                        isinstance(fields, dict) and fields.get("refusal") not in (None, "")):
                    raise ValueError("STT response contains a refusal")
            content = message["content"]
            if content is not None and not isinstance(content, str):
                raise ValueError("STT response content is not text or null")
            text = (content or "").strip()
            if finish_reason == "length":
                if not text:
                    raise ValueError("STT response reached length without text")
                # Keep the partial long turn, as flash-lite did before validation.
                # One diagnostic, with no participant text, names the token cap.
                log.warning("Cascade STT finish_reason=length max_tokens=%s; keeping truncated transcript",
                            payload["max_tokens"])
                return text
            # Transcribe returns content:null for silence (gateway, 2026-11-12).
            # Null can also mean real speech the model could not transcribe. This
            # layer cannot distinguish it from silence; it is recorded as no speech.
            # Only a valid stop response can mean no speech; malformed responses
            # take _transcribe's stt_failed path. Keep this out of the shared speech
            # check, where an empty hearing is a different outcome.
            return text or "[no speech]"

    def _messages(self) -> list:
        msgs = [{"role": "system", "content": self.instructions or "You are a helpful voice assistant."}]
        for n, it in enumerate(self.items):
            if it.get("stt_failed") and it.get("audio"):
                # A failed hearing's audio is kept only when the turn is
                # still current (nothing after it but the reply in flight) or
                # an assistant item follows it (its reply was spoken).
                # Otherwise it would be a stale audio-only user turn, and
                # dropping it after a spoken reply would leave two
                # assistant turns in a row.
                nxt = self.items[n + 1] if n + 1 < len(self.items) else None
                if nxt is not None and nxt["role"] != "assistant":
                    continue
            if it["type"] == "function_call_output":
                msgs.append({"role": "tool", "tool_call_id": it.get("call_id") or "",
                             "content": it["text"]})
            elif it["type"] == "function_call":
                msgs.append({"role": "assistant", "content": None, "tool_calls": [{
                    "id": it["call_id"], "type": "function",
                    "function": {"name": it["name"], "arguments": it["text"]}}]})
            elif it.get("audio") and not it["text"].strip():
                msgs.append({"role": "user", "content": [{
                    "type": "input_audio", "input_audio": {
                        "data": base64.b64encode(
                            wav_bytes(it["audio"], self._rate)).decode("ascii"),
                        "format": "wav"}}]})
            elif it["text"].strip():
                role = it["role"] if it["role"] in ("user", "assistant", "system") else "user"
                msgs.append({"role": role, "content": it["text"]})
        return msgs

    def _ensure_done(self, rid: str) -> None:
        if rid not in self._finished and not self._closed:
            self._finished.add(rid)
            observe(self, "done", rid, "cancelled")
            self._emit({"type": "response.created", "response": {"id": rid}})
            self._emit({"type": "response.done", "response": {
                "id": rid, "status": "cancelled", "output": []}})

    async def _respond(self, rid: str, *, messages: Optional[list] = None) -> None:
        """One reply: stream Argon's words into one ElevenLabs stream."""
        self.timings = {k: v for k, v in self.timings.items() if k == "stt_ms"}
        started = time.time()
        spoken: list = []
        item = self._add_item("assistant", "")
        iid = item["id"]
        tts = None
        cancelled = False
        failed = False
        try:
            # The turn is not awaited: the model hears the audio itself (see
            # _messages) while the transcript is written in parallel.
            self._pending_stt = [t for t in self._pending_stt if not t.done()]
            self._emit({"type": "response.created", "response": {"id": rid, "status": "in_progress"}})
            self._emit({"type": "response.output_item.added", "response_id": rid,
                        "output_index": 0, "item": {"id": iid, "type": "message",
                                                    "role": "assistant", "content": []}})
            voice_id = voice_id_for(self.voice)
            # The ElevenLabs stream opens while the model thinks; the first
            # sentence waits for it (say() in _stream_llm). Only the stream
            # holds an ElevenLabs slot (_open_reply_tts): the model starts at
            # once even when every slot is taken.
            tts = self._spawn(self._open_reply_tts(voice_id, rid, iid, started))
            # _stream_llm has sent close_socket once the words were all in.
            call = await self._stream_llm(tts, voice_id, spoken, rid, iid, started,
                                          messages=messages)
            ws, reader, _slot = await tts
            await reader
            if call:
                await self._finish_call(rid, call)
        except asyncio.CancelledError:
            cancelled = True
        except Exception as exc:  # noqa: BLE001 - one reply, not the session
            failed = True
            self._error("cascade_failed", f"{type(exc).__name__}: {exc}"[:300])
        finally:
            try:
                if tts is not None:
                    await self._close_reply_tts(tts)
            finally:
                # A second cancel at the join bound must still close this
                # response before the next response.created can be emitted.
                cancelled = cancelled or bool(asyncio.current_task().cancelling())
                text = " ".join(spoken).strip()
                if cancelled and "first_audio_ms" not in self.timings:
                    # No audio reached the bridge: none of these generated
                    # sentences belongs in the next reply's conversation.
                    text = ""
                # After audio starts, raw TTS PCM has no sentence alignment
                # or page-playback acknowledgement. Exact trimming is a
                # separate limitation documented in OPERATIONS (11-12b).
                for it in self.items:
                    if it["id"] == iid:
                        it["text"] = text
                if not text:
                    # Also a reply that was only a function call: its call
                    # item stays, its empty message item goes.
                    self.items = [i for i in self.items if i["id"] != iid]
                    self._dropped[iid] = True
                    while len(self._dropped) > 256:
                        self._dropped.pop(next(iter(self._dropped)))
                self._finished.add(rid)
                observe(self, "done", rid, "cancelled" if cancelled else "failed" if failed else "done")
                if not self._closed:
                    if text:
                        self._emit({"type": "response.output_audio_transcript.done",
                                    "response_id": rid, "item_id": iid, "transcript": text})
                    self._emit({"type": "response.output_audio.done", "response_id": rid, "item_id": iid})
                    self._emit({"type": "response.done", "response": {
                        "id": rid, "status": "cancelled" if cancelled else "completed",
                        "output": [{"id": iid, "type": "message", "role": "assistant"}] if text else [],
                        "metadata": {"timings": dict(self.timings)}}})
        if cancelled:
            raise asyncio.CancelledError

    async def _open_reply_tts(self, voice_id: str, rid: str, iid: str,
                              started: float):
        """Take an ElevenLabs slot, open the reply's stream, register the
        voice and start relaying its audio. Returns (ws, reader, slot); the
        slot is held until _close_reply_tts releases it."""
        slot = _tts_slot()
        asked = time.time()
        observe(self, "stamp", rid, "tts_slot_wait_start")
        queued_at = time.perf_counter()
        try:
            await slot.acquire()
        except asyncio.CancelledError:
            record(upstream="elevenlabs", route="tts_ws", purpose="tts",
                   outcome="cancelled", duration_ms=0.0,
                   queue_wait_ms=(time.perf_counter() - queued_at) * 1000,
                   session_id=self.session_id, detail="slot_wait")
            raise
        queue_wait_ms = (time.perf_counter() - queued_at) * 1000
        observe(self, "stamp", rid, "tts_slot_acquired")
        ws = None
        try:
            self.timings["tts_slot_wait_ms"] = int((time.time() - asked) * 1000)
            connect_at = time.time()
            ws = await self._connect_tts(queue_wait_ms=queue_wait_ms)
            observe(self, "stamp", rid, "tts_connected")
            self.timings["tts_connect_ms"] = int((time.time() - connect_at) * 1000)
            await ws.send(json.dumps({"voices": [voice_id]}))
            reader = self._spawn(self._relay_audio(ws, rid, iid, started))
        except BaseException:
            try:
                if ws is not None:
                    await _close_tts(ws)
            finally:
                slot.release()
            raise
        return ws, reader, slot

    @staticmethod
    async def _close_reply_tts(tts: asyncio.Task) -> None:
        """Stop the reply's stream whatever state its opening is in."""
        if not tts.done():
            tts.cancel()
            try:
                await tts
            except BaseException:  # noqa: BLE001 - it is being torn down
                pass
            return
        if tts.cancelled() or tts.exception() is not None:
            return
        ws, reader, slot = tts.result()
        try:
            if reader.cancelled() or not reader.done():
                # An injected stream (a test, a harness) carries no attempt.
                attempt = getattr(ws, "attempt", None)
                if attempt is not None:
                    attempt.finish(outcome="cancelled")
                reader.cancel()
            await _close_tts(ws)
        finally:
            slot.release()

    async def _connect_tts(self, *, queue_wait_ms=None):
        """Open the reply's stream; a 429 (the plan's concurrency) or 409 (the
        first use of a library voice being added to the account) is waited out
        three times with a growing pause."""
        for attempt in range(4):
            measurement = _Attempt(
                upstream="elevenlabs", route="tts_ws", purpose="tts",
                session_id=self.session_id, attempt=attempt + 1,
                queue_wait_ms=queue_wait_ms if attempt == 0 else None)
            try:
                ws = await open_tts()
            except websockets.exceptions.InvalidStatus as exc:
                measurement.fields["status"] = exc.response.status_code
                measurement.finish(exc=exc)
                if exc.response.status_code in (429, 409) and attempt < 3:
                    await asyncio.sleep(0.4 * (2 ** attempt))
                    continue
                raise RuntimeError(
                    f"elevenlabs {exc.response.status_code}: "
                    f"{exc.response.body.decode('utf-8', 'replace')[:300]}")
            except BaseException as exc:
                measurement.finish(exc=exc)
                raise
            else:
                measurement.fields["status"] = 101
                return _TtsSocket(ws, measurement)

    async def _stream_llm(self, tts: asyncio.Task, voice_id: str, spoken: list,
                          rid: str, iid: str, started: float, *,
                          messages: Optional[list] = None) -> Optional[dict]:
        """Stream the reply; each sentence goes into the stream the moment it
        is whole. `tts` is the task opening the ElevenLabs stream
        (_open_reply_tts); the first sentence waits for it. Returns a
        function call if the model made one."""
        payload = {"model": llm_model(), "messages": (self._messages() if messages is None else messages),
                   "stream": True, "max_tokens": 1200}
        if reasoning_effort():
            payload["reasoning_effort"] = reasoning_effort()
        if self.tools:
            payload["tools"] = self.tools

        async def say(sentence: str) -> None:
            waiting_at = time.perf_counter()
            try:
                ws = (await tts)[0]
                spoken.append(sentence)
                self._emit({"type": "response.output_audio_transcript.delta",
                            "response_id": rid, "item_id": iid, "delta": sentence + " "})
                observe(self, "stamp", rid, "first_text_sent")
                await ws.send(json.dumps({"inputs": [{"text": tts_text(sentence) + " ", "voice_id": voice_id}],
                                          "flush": True}))
            finally:
                # Duration is request start through DONE minus time in say().
                # This undercounts generation that overlaps a TTS wait. Keep
                # body reads and the 60 s read timeout at their original sites.
                attempt.started += time.perf_counter() - waiting_at

        # The stream closes itself after 20 s without a message; Argon can
        # think for longer than that on a slow turn.
        async def keep_alive() -> None:
            ws = (await tts)[0]
            while True:
                await asyncio.sleep(10)
                await ws.send(json.dumps({"keep_alive": True}))
        alive = self._spawn(keep_alive())
        try:
            observe(self, "stamp", rid, "request_started")
            with _Attempt(upstream="gateway", route="chat_completions_stream",
                          purpose="actor", session_id=self.session_id) as attempt:
                async with self._http.stream(
                        "POST", f"{gateway_base_url().rstrip('/')}/v1/chat/completions",
                        headers={"Authorization": f"Bearer {self.api_key}"}, json=payload) as r:
                    attempt.fields["status"] = r.status_code
                    if r.status_code >= 400:
                        body = (await r.aread()).decode("utf-8", "replace")[:300]
                        raise RuntimeError(f"llm {r.status_code}: {body}")
                    lines = _GatewayStream(r.aiter_lines(), attempt)
                    try:
                        call = await self._read_llm_stream(lines, tts, started, say, alive, rid)
                        if lines.ended:
                            # Preserve the pooled-connection drain after TTS
                            # close_socket. Telemetry stops at protocol DONE.
                            await _drain(lines.lines, DRAIN_AFTER_DONE_S)
                        self.timings["llm_total_ms"] = int((time.time() - started) * 1000)
                        return call
                    except BaseException as exc:
                        # A failed reply is not a cancelled one (ElevenLabs
                        # 401, a send error): record what stopped it.
                        lines.attempt.finish(exc=exc)
                        raise
        finally:
            alive.cancel()

    async def _read_llm_stream(self, lines, tts, started, say, alive, rid):
        buffer, first, call = "", True, None
        async for line in lines:
            if line:
                if not line.startswith("data:"):
                    continue
                data = line[5:].strip()
                if data == "[DONE]":
                    break
                try:
                    delta = ((json.loads(data).get("choices") or [{}])[0]
                             .get("delta") or {})
                except ValueError:
                    continue
                for tc in delta.get("tool_calls") or []:
                    fn = tc.get("function") or {}
                    call = call or {"call_id": tc.get("id") or self._id("call"),
                                    "name": "", "arguments": ""}
                    call["name"] += fn.get("name") or ""
                    call["arguments"] += fn.get("arguments") or ""
                piece = delta.get("content") or ""
                if not piece:
                    continue
                observe(self, "stamp", rid, "first_content")
                if first:
                    self.timings["llm_first_token_ms"] = int((time.time() - started) * 1000)
                buffer += piece
                sentences, buffer = split_sentences(buffer, first=first)
                if sentences:
                    observe(self, "stamp", rid, "first_sentence_ready", sentence_trigger="punctuation")
                for sentence in sentences:
                    first = False
                    await say(sentence)
        sentences, buffer = split_sentences(buffer, first=first, final=True)
        if sentences:
            observe(self, "stamp", rid, "first_sentence_ready", sentence_trigger="final_flush")
        for sentence in sentences:
            await say(sentence)
        # Every word is in: ElevenLabs may finish the reply now.
        alive.cancel()
        ws = (await tts)[0]
        await ws.send(json.dumps({"close_socket": True}))
        return call

    async def _relay_audio(self, ws, rid: str, iid: str, started: float) -> None:
        """Hand the stream's audio on as it arrives, until its final message."""
        with ws.attempt:
            await self._read_tts_audio(ws, rid, iid, started)

    async def _read_tts_audio(self, ws, rid: str, iid: str, started: float) -> None:
        carry = b""
        async for raw in ws:
            m = json.loads(raw)
            if m.get("error"):
                raise RuntimeError(f"elevenlabs: {m.get('message') or m['error']}")
            if m.get("audio"):
                observe(self, "stamp", rid, "tts_first_audio")
                chunk = carry + base64.b64decode(m["audio"])
                # PCM16 frames are two bytes; never split a sample.
                carry = chunk[-1:] if len(chunk) % 2 else b""
                chunk = chunk[:len(chunk) - len(carry)]
                if "first_audio_ms" not in self.timings:
                    self.timings["first_audio_ms"] = int((time.time() - started) * 1000)
                observe(self, "stamp", rid, "first_output_emitted")
                self._emit({"type": "response.output_audio.delta",
                            "response_id": rid, "item_id": iid,
                            "delta": base64.b64encode(chunk).decode("ascii")})
            if m.get("is_final"):
                return
        ws.attempt.fields["outcome"] = "closed"

    async def _finish_call(self, rid: str, call: dict) -> None:
        cid = call["call_id"]
        self._add_item("assistant", call["arguments"], iid=self._id("item"),
                       kind="function_call", call_id=cid, name=call["name"])
        self._emit({"type": "response.function_call_arguments.done",
                    "response_id": rid, "call_id": cid,
                    "name": call["name"], "arguments": call["arguments"]})
