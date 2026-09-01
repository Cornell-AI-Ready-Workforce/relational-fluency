"""Gemini Live speech-to-speech over the Cornell LiteLLM gateway.

Replaces the v1 cascade (Deepgram STT -> text LLM -> ElevenLabs TTS) with a
single realtime session: participant audio goes up, agent audio and both
transcripts come back.

Gateway notes, verified 2026-08-19 (see docs/migration-plan.md):

* Transport is a WebSocket at /v1/realtime?model=...; the WebRTC path is not
  wired up. The upgrade needs HTTP/1.1.
* `session.update` must stay FLAT and minimal, instructions, voice, tools.
  Sending `modalities`, the nested GA `audio: {...}` block, or
  `input_audio_transcription` leaves the session alive but permanently mute,
  with no error event. This is the single easiest way to break it.
* Server VAD is accepted but inert: without an explicit
  `input_audio_buffer.commit` + `response.create` the model never replies. Turn
  detection therefore lives here, in `SilenceDetector`.
* Input transcription arrives without being asked for.
"""

from __future__ import annotations

import base64
import json
import math
import os
import struct
import time
from dataclasses import dataclass, field
from typing import AsyncIterator, Optional

import websockets

# Config comes from server.llm so the .env file wins over ambient environment:
# a stray exported variable must not be able to redirect study traffic.
from ..llm import gateway_api_key, gateway_base_url, setting

try:  # audioop was removed in Python 3.13 (PEP 594); fall back to pure Python.
    import audioop  # type: ignore
except ModuleNotFoundError:  # pragma: no cover - exercised only on 3.13+
    audioop = None


def _rms(pcm: bytes) -> int:
    """Root-mean-square of signed 16-bit mono PCM."""
    if audioop is not None:
        return audioop.rms(pcm, 2)
    n = len(pcm) // 2
    if n == 0:
        return 0
    samples = struct.unpack("<%dh" % n, pcm[: n * 2])
    return int(math.sqrt(sum(s * s for s in samples) / n))


def _ratecv(pcm: bytes, inrate: int, outrate: int, state):
    """Resample signed 16-bit mono PCM, carrying interpolation state between
    chunks. Mirrors the audioop.ratecv contract we rely on; the pure-Python
    path is a streaming linear interpolator with its own opaque state tuple."""
    if audioop is not None:
        return audioop.ratecv(pcm, 2, 1, inrate, outrate, state)
    n = len(pcm) // 2
    if n == 0:
        return b"", state
    samples = struct.unpack("<%dh" % n, pcm[: n * 2])
    if state is None:
        prev, pos = samples[0], 0.0
    else:
        prev, pos = state
    combined = (prev,) + samples  # index 0 == previous chunk's last sample
    step = inrate / outrate
    out = []
    while pos < n:
        i = int(pos)
        frac = pos - i
        a = combined[i]
        b = combined[i + 1]
        val = int(a + (b - a) * frac)
        if val > 32767:
            val = 32767
        elif val < -32768:
            val = -32768
        out.append(val)
        pos += step
    new_state = (samples[-1], pos - n)
    return struct.pack("<%dh" % len(out), *out), new_state

GATEWAY = gateway_base_url()
MODEL = setting("REALTIME_MODEL", "nto.gemini-live-2.5-flash")
VOICE = setting("REALTIME_VOICE", "Puck")

# The browser captures and plays 16 kHz; the gateway emits 24 kHz PCM16.
CLIENT_RATE = 16000
GATEWAY_OUTPUT_RATE = int(setting("REALTIME_OUTPUT_RATE", "24000"))


def _ws_url(model: str) -> str:
    base = GATEWAY.replace("https://", "wss://").replace("http://", "ws://").rstrip("/")
    return f"{base}/v1/realtime?model={model}"


@dataclass
class SilenceDetector:
    """End-of-turn detection, because the gateway does not do it for us.

    Speech is detected on RMS energy; a turn ends after `silence_ms` of quiet
    following speech. `min_speech_ms` keeps a cough or a door slam from opening
    a turn that immediately closes.
    """

    threshold: int = field(default_factory=lambda: int(setting("VAD_RMS_THRESHOLD", "500")))
    silence_ms: int = field(default_factory=lambda: int(setting("VAD_SILENCE_MS", "900")))
    min_speech_ms: int = 250
    rate: int = CLIENT_RATE

    speaking: bool = False
    _speech_ms: float = 0.0
    _silence_ms_run: float = 0.0

    def feed(self, pcm: bytes) -> Optional[str]:
        """Returns 'speech_started', 'turn_ended', or None."""
        if not pcm:
            return None
        chunk_ms = len(pcm) / 2 / self.rate * 1000.0
        rms = _rms(pcm)

        if rms >= self.threshold:
            self._silence_ms_run = 0.0
            self._speech_ms += chunk_ms
            if not self.speaking and self._speech_ms >= self.min_speech_ms:
                self.speaking = True
                return "speech_started"
            return None

        if self.speaking:
            self._silence_ms_run += chunk_ms
            if self._silence_ms_run >= self.silence_ms:
                self.speaking = False
                self._speech_ms = 0.0
                self._silence_ms_run = 0.0
                return "turn_ended"
            return None

        # Not speaking and below threshold: decay any partial speech that never
        # opened a turn, so isolated noise bursts (cough, keyboard, door) can't
        # accumulate across long silences and eventually cross min_speech_ms.
        self._silence_ms_run += chunk_ms
        if self._silence_ms_run >= self.silence_ms:
            self._speech_ms = 0.0
        return None

    def reset(self) -> None:
        self.speaking = False
        self._speech_ms = 0.0
        self._silence_ms_run = 0.0


class RealtimeVoiceSession:
    """One live conversation with the agent.

    Emits dicts: {"type": "user_transcript"|"agent_transcript_delta"|
    "agent_transcript"|"agent_audio"|"response_done"|"error", ...}
    Audio is PCM16 resampled to the client's rate.
    """

    def __init__(
        self,
        instructions: str,
        *,
        model: str = MODEL,
        voice: str = VOICE,
        tools: Optional[list] = None,
        api_key: Optional[str] = None,
    ) -> None:
        self.instructions = instructions
        self.model = model
        self.voice = voice
        self.tools = tools or []
        self.api_key = api_key or gateway_api_key()
        self.ws: Optional[websockets.WebSocketClientProtocol] = None
        # Audio appended since the last commit. The bridge kills a session
        # that commits an empty buffer, so give_floor checks this first; it
        # is zeroed when a bridge auto-fired response consumes the buffer.
        self.pending_input = 0
        self._resample_state = None
        self._agent_buffer = ""
        self._response_active = False
        self._done_ids: set = set()
        self.debug_log: list | None = [] if os.getenv("RT_DEBUG") else None

    async def connect(self, *, open_conversation: bool = True) -> None:
        if not self.api_key:
            raise RuntimeError("No gateway API key (set LITELLM_API_KEY)")
        # websockets renamed extra_headers -> additional_headers when the new
        # asyncio client became the top-level default in 14.0; requirements
        # allow >=12, so pick the kwarg the installed version actually accepts.
        header_kwarg = "additional_headers"
        try:
            if int(websockets.__version__.split(".")[0]) < 14:
                header_kwarg = "extra_headers"
        except (ValueError, AttributeError):
            pass
        self.ws = await websockets.connect(
            _ws_url(self.model),
            max_size=None,
            ping_interval=20,
            **{header_kwarg: {"Authorization": f"Bearer {self.api_key}"}},
        )
        session: dict = {"instructions": self.instructions}
        if self.voice:
            session["voice"] = self.voice
        if self.tools:
            session["tools"] = self.tools
        # Deliberately nothing else, see module docstring.
        await self._send({"type": "session.update", "session": session})

    async def _send(self, payload: dict) -> None:
        if self.ws:
            await self.ws.send(json.dumps(payload))

    async def update_instructions(self, instructions: str) -> None:
        """Re-issue the actor's brief. This is how the director steers: the
        stage direction is appended to the persona before the next reply, the
        same contract the v1 director-actor loop used."""
        self.instructions = instructions
        session: dict = {"instructions": instructions}
        if self.voice:
            session["voice"] = self.voice
        if self.tools:
            session["tools"] = self.tools
        await self._send({"type": "session.update", "session": session})

    async def send_audio(self, pcm16: bytes) -> None:
        """Append participant audio (PCM16 at CLIENT_RATE)."""
        if not pcm16:
            return
        self.pending_input += len(pcm16)
        await self._send({
            "type": "input_audio_buffer.append",
            "audio": base64.b64encode(pcm16).decode("ascii"),
        })

    async def commit_input(self) -> None:
        """Close the participant's turn without asking for a reply. Group rooms
        need this separately: one commit, then a reply per speaker."""
        self.pending_input = 0
        await self._send({"type": "input_audio_buffer.commit"})

    @property
    def responding(self) -> bool:
        """True while a reply is in flight. A group sequencer must wait for this
        to clear before handing the floor to the next character, the gateway
        rejects a second response.create with
        conversation_already_has_active_response."""
        return self._response_active

    def clear_response_state(self) -> None:
        """Force the in-flight flag down after a timeout, so one stalled reply
        cannot mute every character that follows it."""
        self._response_active = False

    async def request_response(self) -> None:
        """Ask the current character to speak."""
        if self._response_active:
            return
        self._response_active = True
        await self._send({"type": "response.create"})

    async def commit_turn(self) -> None:
        """Close the participant's turn and ask for a reply. Required, the
        gateway will not do this on its own."""
        if self._response_active:
            return
        if self.pending_input < 3200:
            # Committing an empty buffer kills the session on this bridge;
            # pad with 300 ms of silence if an auto-fire consumed the audio.
            await self.send_audio(b"\x00" * 9600)
        await self.commit_input()
        await self.request_response()

    async def cancel_response(self) -> None:
        """Barge-in: stop the agent mid-utterance. Sent unconditionally so that
        bridge auto-fired responses — which never flip _response_active — can
        also be cancelled; response.cancel is harmless when nothing is active."""
        await self._send({"type": "response.cancel"})
        self._response_active = False

    def _to_client_rate(self, pcm: bytes) -> bytes:
        if GATEWAY_OUTPUT_RATE == CLIENT_RATE:
            return pcm
        converted, self._resample_state = _ratecv(
            pcm, GATEWAY_OUTPUT_RATE, CLIENT_RATE, self._resample_state
        )
        return converted

    async def events(self) -> AsyncIterator[dict]:
        if not self.ws:
            raise RuntimeError("connect() first")
        try:
            async for raw in self.ws:
                try:
                    ev = json.loads(raw)
                except (ValueError, TypeError):
                    continue
                etype = ev.get("type", "")
                if self.debug_log is not None:
                    self.debug_log.append((time.time(), etype, str(ev)[:160]))

                # The bridge auto-fires responses without going through
                # request_response(); mark the session active on the first
                # streamed delta so responding/cancel_response track them too.
                if etype.startswith("response.") and etype.endswith(".delta"):
                    self._response_active = True

                if etype in ("response.output_audio.delta", "response.audio.delta"):
                    pcm = base64.b64decode(ev.get("delta") or "")
                    if pcm:
                        yield {"type": "agent_audio", "pcm": self._to_client_rate(pcm)}

                elif etype in (
                    "response.output_audio_transcript.delta",
                    "response.audio_transcript.delta",
                ):
                    delta = ev.get("delta") or ""
                    self._agent_buffer += delta
                    yield {"type": "agent_transcript_delta", "text": delta}

                elif etype in (
                    "response.output_audio_transcript.done",
                    "response.audio_transcript.done",
                ):
                    text = (ev.get("transcript") or self._agent_buffer).strip()
                    self._agent_buffer = ""
                    if text:
                        yield {"type": "agent_transcript", "text": text}

                elif etype == "conversation.item.input_audio_transcription.completed":
                    text = (ev.get("transcript") or "").strip()
                    if text:
                        yield {"type": "user_transcript", "text": text}

                elif etype == "response.done":
                    self._response_active = False
                    # The gateway can repeat response.done for one reply; emit
                    # it once per response id.
                    rid = (ev.get("response") or {}).get("id") or ev.get("response_id")
                    if rid and rid in self._done_ids:
                        continue
                    if rid:
                        self._done_ids.add(rid)
                    yield {"type": "response_done"}

                elif etype == "response.function_call_arguments.done":
                    yield {
                        "type": "tool_call",
                        "name": ev.get("name"),
                        "call_id": ev.get("call_id"),
                        "arguments": ev.get("arguments"),
                    }

                elif etype == "error":
                    self._response_active = False
                    yield {"type": "error", "message": str(ev.get("error"))}
        except websockets.ConnectionClosedOK:
            self._response_active = False
            return
        except websockets.ConnectionClosedError as exc:
            self._response_active = False
            yield {"type": "error", "message": f"realtime connection lost: {exc}"}

    async def close(self) -> None:
        if self.ws:
            try:
                await self.ws.close()
            except Exception:
                pass
            self.ws = None
