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

Because every one of those failure modes is quiet, `events()` holds one rule of
its own: it never ends, and never stops answering, without saying so. A reply
that produces nothing at all is timed out and reported; a reply the gateway
abandons mid-sentence (a stall, an `error` frame, or the socket going away) is
closed out with an `interrupted` `response_done` and then reported; and ANY
other exception out of the loop — a malformed frame, a bug in here — is caught
and reported the same way rather than escaping as a traceback only the server
log would see. The single exception is a socket we closed ourselves, which is
an ordinary end of the pump. A character that has gone silent must leave a mark
an analyst can find, because an encounter that quietly stopped and one that
concluded look identical in the record otherwise.

That contract is now honoured downstream too. The `interrupted` flag on a
synthetic `response_done` is read by both of the runner's finalize spawn sites
(`_pump`/`_pump_member` in server/realtime_voice_session.py) and reaches the
record on the turn itself, as `assistant_turn.interrupted` and on the steering
pair, so a rater comparing a stage direction to the line it produced can tell a
truncated delivery from a bad one. It also clamps that turn's transcript grace
to 1 s, which is all a dead session's transcript is worth waiting for. The
terminal `error` message that follows every truncation still names the
truncation in words: it is the frame the participant's page acts on, and it is
what an analyst reading events.jsonl alone will find first.

`error` events carry `transient: True` when they describe a fault the session
survived intact — one discarded audio chunk, not a lost turn. The runner still
records every one of them, and shows the participant at most one per reply: the
frame reaches the page as "Something went wrong", and audio deltas arrive every
few tens of milliseconds, so a gateway sending a run of malformed frames would
otherwise bury the participant in identical banners mid-conversation.
"""

from __future__ import annotations

import asyncio
import base64
import binascii
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

# How long a reply we asked for may produce nothing at all before events()
# declares it lost. 45 s matches the timeout the group sequencer already waits
# out (realtime_voice_session._speak_as), so a stalled reply is called dead at
# the same moment whichever loop is watching it.
RESPONSE_STALL_S = float(setting("REALTIME_RESPONSE_STALL_S", "45"))
# How often events() comes up for air to run that check. Nothing on the wire is
# normal between turns, so this is a poll interval, not a socket timeout.
RECV_POLL_S = 5.0


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
        # A response the bridge started on its own (after speech + silence),
        # as opposed to one we asked for. Tracked separately from
        # _response_active so group-room suppression behaviour is unchanged.
        self.autofire_active = False
        self._last_output_at = 0.0
        self._resample_state = None
        self._agent_buffer = ""
        self._response_active = False
        # When the in-flight reply was asked for, and whether anything has come
        # back for it yet. Only ever read through _response_stalled() and only
        # ever cleared through _end_response(), so a reply that dies without a
        # response.done cannot leave a latch behind — the failure that muted an
        # encounter for good.
        self._response_started_at = 0.0
        self._response_saw_output = False
        # True once close() has been called on the CURRENT socket. A socket WE
        # closed (a character switch closes the outgoing session deliberately)
        # is an ordinary end of the pump, not a fault, and must not be reported
        # as one; a socket the gateway closed always is. Scoped to one socket,
        # not to this object: connect() puts it back down, because a latch with
        # no clearing path is what muted an encounter for good once already
        # (see _end_response), and this one would swallow every genuine gateway
        # close on a reconnected session.
        self._closing = False
        self._done_ids: set = set()
        self.debug_log: list | None = [] if os.getenv("RT_DEBUG") else None

    async def connect(self, *, open_conversation: bool = True) -> None:
        # Both of these are per-socket, and both SUPPRESS an event when set, so
        # carrying either into a new socket loses a turn silently: _closing
        # would make events() end a reconnected session without the terminal
        # error it promises, and a response id left in _done_ids would make the
        # new socket's response.done for that id a no-op, so the runner would
        # never finalise that turn. Cleared here rather than in close(), so a
        # close still reads as intentional right up to the next connect.
        self._closing = False
        self._done_ids.clear()
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
        conversation_already_has_active_response.

        Reports the raw flag, including one that has gone stale, because a
        caller waiting on a reply should still see it and time out on its own
        terms; it is commit_turn/request_response that refuse to be blocked by
        a flag older than any reply (see _response_stalled)."""
        return self._response_active

    def _end_response(self) -> None:
        """Drop every trace of an in-flight reply.

        One clearing path for all four flags, called from each way a response
        can end — done, error, cancel, timeout, or the socket going away. They
        went out of sync once (a cancelled reply left autofire_active latched
        and deadlocked the encounter, see cancel_response), and each new piece
        of state here is another chance to do it again, so there is exactly one
        place that puts them all down."""
        self._response_active = False
        self.autofire_active = False
        self._response_started_at = 0.0
        self._response_saw_output = False

    def _response_stalled(self) -> bool:
        """True when a reply is nominally in flight but has produced nothing
        for longer than any reply plausibly takes.

        Measured from the last sign of life, not from the request, so a long
        reply that is still streaming audio is never mistaken for a dead one.
        A stall is what a lost response.done looks like from here, and it is
        what the module docstring's "alive but permanently mute" session looks
        like too."""
        if not self._response_active:
            return False
        last = max(self._response_started_at, self._last_output_at)
        return last > 0.0 and (time.time() - last) > RESPONSE_STALL_S

    def clear_response_state(self) -> None:
        """Force the in-flight flag down after a timeout, so one stalled reply
        cannot mute every character that follows it. The auto-fire latch goes
        with it: a reply that timed out will not emit the response.done that
        would otherwise clear it, and leaving it up makes commit_turn() skip
        the very commit this call exists to unblock."""
        self._end_response()

    async def request_response(self) -> None:
        """Ask the current character to speak."""
        if self._response_active and not self._response_stalled():
            return
        # A stale flag is overridden rather than obeyed. events() normally
        # times the stall out first and says so; this is the backstop for a
        # session whose events() is not being drained at that moment, so that
        # the worst a lost response.done can cost is one turn rather than
        # every turn that follows it.
        self._response_active = True
        self._response_started_at = time.time()
        self._response_saw_output = False
        await self._send({"type": "response.create"})

    async def commit_turn(self) -> None:
        """Close the participant's turn and ask for a reply. Required, the
        gateway will not do this on its own."""
        if self._response_active and not self._response_stalled():
            return
        if self.autofire_active and time.time() - self._last_output_at < 15:
            # The bridge is already answering this turn; a commit + create
            # here produces a second, paraphrased reply on top of it.
            return
        if self.pending_input < 3200:
            # Committing an empty buffer kills the session on this bridge;
            # pad with 300 ms of silence if an auto-fire consumed the audio.
            await self.send_audio(b"\x00" * 9600)
        await self.commit_input()
        await self.request_response()

    async def cancel_response(self) -> None:
        """Barge-in: stop the agent mid-utterance. Sent unconditionally, because
        response.cancel is harmless when nothing is active and a bridge
        auto-fired reply has to be cancellable too.

        The reply's whole state is dropped here (_end_response). A cancelled
        response may never produce the
        response.done that would otherwise clear autofire_active, and a latched
        autofire_active deadlocks the encounter: the runner's turn loop reads
        the flag, believes the bridge is already answering, and skips
        commit_turn() — so no new response is ever created, so no response.done
        ever arrives, and the agent goes permanently silent after one barge-in.
        """
        await self._send({"type": "response.cancel"})
        self._end_response()

    def _to_client_rate(self, pcm: bytes) -> bytes:
        if GATEWAY_OUTPUT_RATE == CLIENT_RATE:
            return pcm
        converted, self._resample_state = _ratecv(
            pcm, GATEWAY_OUTPUT_RATE, CLIENT_RATE, self._resample_state
        )
        return converted

    async def _frames(self) -> AsyncIterator[Optional[str]]:
        """Raw gateway frames, with a None every RECV_POLL_S of quiet.

        Two reasons this is recv() in a loop rather than `async for raw in
        self.ws`. The None is one: it gives events() a heartbeat on which to
        notice a reply that has stopped producing, which a blocking read never
        would. The other is that the websockets iterator swallows a clean close
        and simply stops — the exact silent ending this module must not have —
        while recv() raises ConnectionClosedOK and can be reported.

        The socket is captured once: close() drops self.ws to None right after
        closing it, and reading the attribute per frame would turn a teardown
        race into an AttributeError out of the pump."""
        ws = self.ws
        while True:
            try:
                yield await asyncio.wait_for(ws.recv(), RECV_POLL_S)
            except asyncio.TimeoutError:
                yield None

    async def events(self) -> AsyncIterator[dict]:
        if not self.ws:
            raise RuntimeError("connect() first")
        # Set from whichever exception ends the loop, and yielded on the way
        # out. See the tail of this method: no exit from here is silent.
        closing = "the gateway closed the realtime session"
        try:
            async for raw in self._frames():
                if raw is None:
                    # Quiet wire. That is ordinary between turns, and a fault
                    # only when a reply we asked for has produced nothing at
                    # all: a lost response.done leaves _response_active latched
                    # and every later commit_turn() is a silent no-op, so the
                    # participant goes on talking to a character that will
                    # never answer again — the encounter is muted for good and
                    # nothing anywhere says why.
                    if not self._response_stalled():
                        continue
                    waited = round(
                        time.time()
                        - max(self._response_started_at, self._last_output_at)
                    )
                    partial = self._response_saw_output
                    self._end_response()
                    if partial:
                        # Part of the reply was spoken to the participant and
                        # is already in the assistant WAV. Close that turn so
                        # the words survive in the transcript rather than
                        # vanishing from it, or being glued onto the front of
                        # whatever this character says next.
                        yield {"type": "response_done", "interrupted": True}
                    yield {
                        "type": "error",
                        # Two different failures, and the message says which.
                        # "No reply" was false whenever part of one had already
                        # arrived. The runner now also carries the `interrupted`
                        # flag above onto the turn itself, so the record says
                        # the same thing twice, in the two channels an analyst
                        # reads separately (see the module docstring).
                        "message": (
                            f"the gateway stopped mid-reply and sent nothing "
                            f"for {waited}s; the turn was cut off "
                            f"mid-sentence"
                            if partial else
                            f"no reply from the gateway after {waited}s; "
                            "abandoning the turn"
                        ),
                    }
                    continue
                try:
                    ev = json.loads(raw)
                except (ValueError, TypeError):
                    continue
                etype = ev.get("type", "")
                if self.debug_log is not None:
                    self.debug_log.append((time.time(), etype, str(ev)[:160]))

                # The bridge auto-fires responses without going through
                # request_response(). Detect that first — an auto-fired reply
                # is one whose deltas arrive while _response_active is still
                # False — then mark the session active anyway, so responding()
                # and cancel_response() track auto-fired replies too. Order
                # matters: setting _response_active before the autofire test
                # would make every reply look like one we asked for.
                if etype.startswith("response.") and etype.endswith(".delta"):
                    self._last_output_at = time.time()
                    if not self._response_active:
                        self.autofire_active = True
                        # An auto-fired reply starts its own stall clock: it was
                        # never requested, so nothing else has started one, and
                        # a reply with no clock can never be timed out.
                        self._response_started_at = self._last_output_at
                    self._response_active = True
                    # This reply has been heard. Only such a reply is worth
                    # closing out when the socket or the gateway abandons it
                    # mid-turn; one that produced nothing has no turn to write.
                    self._response_saw_output = True

                if etype in ("response.output_audio.delta", "response.audio.delta"):
                    try:
                        pcm = base64.b64decode(ev.get("delta") or "")
                    except (binascii.Error, ValueError) as exc:
                        # One malformed audio frame must cost one chunk, not
                        # the encounter. Unguarded, this raise leaves events()
                        # entirely, trips run()'s finally in the runner and
                        # drops the participant mid-conversation — loud on
                        # screen, but with nothing in the record saying why.
                        # Reported per chunk rather than counted: a gateway
                        # sending corrupt audio at all is not a normal
                        # condition, and a row per occurrence is the record an
                        # analyst needs to see how much audio was lost.
                        #
                        # `transient` says the session survived this: one chunk
                        # of audio is gone, the turn is not. The per-chunk store
                        # row is the point and stays; what must NOT be per-chunk
                        # is the participant-visible frame the runner builds
                        # from it, because deltas arrive every few tens of
                        # milliseconds and a corrupt run would fill the page
                        # with dozens of identical error banners mid-encounter.
                        # See _pump in server/realtime_voice_session.py, which
                        # shows at most one such notice per reply.
                        yield {
                            "type": "error",
                            "transient": True,
                            "message": (
                                "discarded a corrupt audio frame from the "
                                f"gateway: {exc}"
                            ),
                        }
                        continue
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
                    self._end_response()
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
                    # An errored response never reaches response.done either, so
                    # clear the auto-fire latch here too (see cancel_response).
                    # Read whether anything was spoken BEFORE _end_response
                    # zeroes that flag, or the rescue below can never fire.
                    partial = self._response_saw_output
                    self._end_response()
                    if partial:
                        # The third way the gateway abandons a reply mid-
                        # sentence, and the one the stall and close paths did
                        # not cover. Without a response_done the runner never
                        # finalises this turn, so the words already spoken stay
                        # in its buffer and are glued onto the front of
                        # whatever this character says next: one assistant_turn
                        # holding two separate replies under one stage
                        # direction. Verified before this line existed: deltas
                        # "Turn one words." / error / "Turn two words." /
                        # response.done produced a single assistant_turn
                        # reading "Turn one words.Turn two words.".
                        yield {"type": "response_done", "interrupted": True}
                    yield {"type": "error", "message": str(ev.get("error"))}
        except websockets.ConnectionClosedOK as exc:
            # A clean 1000/1001/1005 is not a conclusion. It is what a bridge
            # recycling a connection, a session hitting its duration limit, or
            # a polite quota cut-off looks like, and it used to end this
            # iterator with no event of any kind: the character simply stopped
            # answering, no voice_error, no frame on the participant's screen,
            # nothing in the record to distinguish it from an encounter that
            # ran out of things to say. Report it like any other loss.
            closing = f"realtime session closed by gateway: {exc}"
        except websockets.ConnectionClosedError as exc:
            closing = f"realtime connection lost: {exc}"
        except Exception as exc:  # noqa: BLE001 — deliberate: see the docstring
            # The module docstring promises events() never ends without saying
            # so, and before this handler that promise held only for the two
            # close exceptions: anything else propagated past the trailing
            # yields (they sit after the try, so an exception skips them),
            # leaving _pump and _model_to_client unguarded, tripping run()'s
            # finally, and dropping the participant with no voice_error in the
            # record to explain it. Reported like any other loss instead; repr
            # keeps the exception type, so a bug in here is still diagnosable.
            # CancelledError and GeneratorExit are BaseException and are
            # deliberately NOT caught: a consumer that walked away must not be
            # answered, and a yield during GeneratorExit is an error.
            closing = f"realtime session failed: {exc!r}"
        finally:
            # Whatever ended the loop — a close, an unexpected raise, or the
            # consumer abandoning this generator — no in-flight latch may
            # outlive it. Nothing is yielded from here: a yield during
            # GeneratorExit is an error, and a dead session's flags matter more
            # than its last words.
            partial = self._response_saw_output
            self._end_response()

        if self._closing:
            # We closed this socket ourselves (a character switch closes the
            # outgoing session on purpose, and so does the end of the
            # encounter). That is an ordinary end of the pump, and reporting it
            # would put a spurious error in the record and on the
            # participant's screen at every interaction boundary. It also must
            # not close out the abandoned turn: by now the runner has already
            # moved on to the next character, and the finalise would file the
            # old character's words under the new one.
            return
        if partial:
            # The socket died mid-reply. Those words reached the participant
            # and their audio is in the assistant WAV, so dropping the text
            # here would leave the two halves of the record disagreeing and
            # this turn's stage direction paired with nothing — the same
            # failure the barge-in path finalises to avoid.
            yield {"type": "response_done", "interrupted": True}
            # Said in the message as well as in the flag. The runner does read
            # the flag now (see the module docstring), so this is no longer the
            # only trace of the truncation — but it is the one that travels with
            # the reason the socket died, which the flag on the turn cannot
            # carry.
            closing += " — a reply was cut off mid-sentence"
        yield {"type": "error", "message": closing}

    async def close(self) -> None:
        # Set before the await so events(), which can only wake after it, sees
        # an intentional teardown rather than reporting a gateway failure.
        self._closing = True
        if self.ws:
            try:
                await self.ws.close()
            except Exception:
                pass
            self.ws = None
