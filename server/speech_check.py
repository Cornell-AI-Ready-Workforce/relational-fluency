"""The participant speech check (pipeline 2026-09-30a; gpt route).

On a real microphone the gpt route's transcriber (gpt-4o-transcribe, language
hint "en") writes words over noise and over the characters' own voices coming
back through the participant's speakers. S4A s_1790781273_b8b7cc (2026-09-30,
build 55de607) recorded ten such lines as the participant's: "լավ.",
"Democrat", "Tuurlijk.", "The", "Good afternoon.", "Hi.", "Post.", "Hi.",
"Sexuality", "Ehh". The room routed on most of them and the characters
answered people who had said nothing. S2A s_1790632427_b653e2 recorded
"Right now." while Morgan was saying "...more than they should right now".
None of the existing gates can see these: each has a word in it, the voice
counter heard sound under it (2.8 s under "Democrat"), and it is too short
for the echo guard.

So a line that looks like one of them is heard again, from the audio its own
commit held, by a text model that accepts audio (SPEECH_CHECK_MODEL), which
answers with what the person at the microphone said, or [no speech]. A line
it hears nobody say is suppressed as no_speech, as the other gates suppress
theirs; any other answer keeps the live transcript, except that a line in
another script is replaced by the English it heard. A check that fails or
runs out of time keeps the line as it came, which is what happened before.

WHICH LINES (check_reason). Short ones (SPEECH_CHECK_MAX_WORDS, 3), any line
with a letter outside the Latin script, and a line of up to
SPEECH_CHECK_PLAYBACK_MAX_WORDS (7) that began while a character was playing
or within SPEECH_CHECK_PLAYBACK_TAIL_S (1.0 s) of its end. Every phantom
above is one of those; a long line is never held.

MEASURED (2026-09-30): the 83 participant lines of six production encounters
whose audio could be cut from user_audio.wav near its commit's bounds, heard
by nto.gemini-3.1-flash-lite with PROMPT. The ten phantoms of b8b7cc, the
"Right now." and a "Hello," of b653e2 (both recorded over Morgan's playback)
and a 400 ms "嗯。" came back [no speech]; so did three 6-13 s lines of S2A
09bcbb, an encounter recorded before spoken_at existed and cut at a guess,
which are far too long to be checked here. Every short real line was heard
("Hi Riley.", "Hey Sam.", "Yeah.", "Um", "Good.", "Very upsetting.", "I'm
organ." heard as "Hi Morgan."). Median 1.33 s, p90 1.72 s, over all 266
lines. A prompt that asks for [inaudible] instead (fix/gemini-native's
language guard) wrote "I'm going to go ahead and get started." or a variant
over six of the b8b7cc phantoms: the model fills a clip with nobody in it
unless it is told what nobody sounds like.

Knobs, read per call:
  SPEECH_CHECK                    default on; "0" turns the check off (the
                                  route's family row must also carry
                                  participant_speech_check: the gpt rows)
  SPEECH_CHECK_MODEL              default nto.gemini-3.1-flash-lite
  SPEECH_CHECK_TIMEOUT_S          default 2.5
  SPEECH_CHECK_MAX_WORDS          default 3
  SPEECH_CHECK_PLAYBACK_MAX_WORDS default 7
  SPEECH_CHECK_PLAYBACK_TAIL_S    default 1.0
"""

from __future__ import annotations

import asyncio
import base64
import io
import re
import time
import unicodedata
import wave
from typing import Iterable, Optional

from .llm import gateway_api_key, gateway_base_url, redact_key, setting
from .retranscribe import _completion_text

DEFAULT_TIMEOUT_S = 2.5
# Shorter than this is not a turn the model can judge (a commit is at least
# PARTICIPANT_COMMIT_MIN_VOICED_MS of voice plus the pre-roll); the line is
# kept unchecked, outcome "no_audio".
MIN_AUDIO_MS = 200

PROMPT = (
    "This clip is from one person's microphone during a live voice "
    "conversation in English. Transcribe, verbatim and in English, only words "
    "that the person at the microphone clearly speaks. If there are no clearly "
    "spoken words from them (silence, breathing, typing, noise, music, or "
    "faint voices in the background), output exactly [no speech]. Output only "
    "the transcript or [no speech], with no labels or commentary."
)

_NO_SPEECH = re.compile(r"[\[(]?\s*(?:no speech|inaudible|silence)\s*[\])]?\.?", re.I)
_WORD = re.compile(r"[^\W\d_]+(?:['’][^\W\d_]+)*")


def _flag(name: str, default: str) -> bool:
    return setting(name, default).strip().lower() not in ("0", "false", "no", "off", "")


def _number(name: str, default: float) -> float:
    try:
        value = float(setting(name, str(default)))
    except ValueError:
        return default
    return value if value >= 0 else default


def enabled() -> bool:
    return _flag("SPEECH_CHECK", "1")


def check_model() -> str:
    # The literal is what tests/test_final_preflight.py pins against
    # llm._MODEL_ROLES.
    return setting("SPEECH_CHECK_MODEL", "nto.gemini-3.1-flash-lite")


def timeout_s() -> float:
    return _number("SPEECH_CHECK_TIMEOUT_S", DEFAULT_TIMEOUT_S) or DEFAULT_TIMEOUT_S


def max_words() -> int:
    return int(_number("SPEECH_CHECK_MAX_WORDS", 3))


def playback_max_words() -> int:
    return int(_number("SPEECH_CHECK_PLAYBACK_MAX_WORDS", 7))


def playback_tail_s() -> float:
    return _number("SPEECH_CHECK_PLAYBACK_TAIL_S", 1.0)


def provenance() -> dict:
    return {"enabled": enabled(), "model": check_model(),
            "timeout_s": timeout_s(), "max_words": max_words(),
            "playback_max_words": playback_max_words(),
            "playback_tail_s": playback_tail_s()}


# ── which lines ─────────────────────────────────────────────────────────────

# Letters with no LATIN in their name that Latin text uses: the micro sign
# ("5 µs") folds to Greek mu under NFKC.
_LATIN_SIGNS = frozenset("µ")


def _latin(c: str) -> bool:
    try:
        name = unicodedata.name(c)
    except ValueError:
        return False
    if name.startswith("LATIN") or " LATIN " in name or c in _LATIN_SIGNS:
        return True
    # "Nº 5", "3º", "1ª": ordinal indicators fold to Latin letters.
    folded = unicodedata.normalize("NFKC", c)
    return bool(folded) and folded != c and all(
        f.isascii() or _latin(f) for f in folded if f.isalpha())


def non_latin(text: str) -> bool:
    """Any letter outside the Latin script. An English transcript has none;
    "լավ." (Armenian) and "嗯。" came over noise on the gpt route."""
    return any(unicodedata.category(c) in ("Lu", "Ll", "Lt", "Lo") and not _latin(c)
               for c in text or "")


def word_count(text: str) -> int:
    return len(_WORD.findall(text or ""))


def check_reason(text: str, *, during_playback: bool = False) -> Optional[str]:
    """Why this line is heard again, or None when it is written as it came.

    "non_latin", "short" (at most max_words() words) or "during_playback" (at
    most playback_max_words(), begun while a character was playing or within
    playback_tail_s() of its end; the caller decides that)."""
    if not text or not text.strip():
        return None
    if non_latin(text):
        return "non_latin"
    words = word_count(text)
    if words <= max_words():
        return "short"
    if during_playback and words <= playback_max_words():
        return "during_playback"
    return None


# ── the check ───────────────────────────────────────────────────────────────

def wav_bytes(pcm16: bytes, rate: int = 16000) -> bytes:
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(pcm16)
    return buf.getvalue()


def _prompt(names: Iterable[str]) -> str:
    names = [n for n in names if n]
    if not names:
        return PROMPT
    return (PROMPT + " The other people in the conversation are "
            + ", ".join(names) + "; if the speaker says one of those names, "
            "spell it that way.")


def _clean(text: str) -> str:
    text = (text or "").strip()
    if len(text) >= 2 and text[0] == text[-1] and text[0] in "\"'“”":
        text = text[1:-1].strip()
    return re.sub(r"\s{2,}", " ", text)


# One client per event loop: a client per call paid a fresh TCP and TLS
# handshake inside the check's own budget. A client belongs to the loop it was
# made on, so a new loop (a test's asyncio.run, a restarted worker) gets its
# own.
_CLIENT: Optional[tuple] = None


def _client():
    global _CLIENT
    import httpx
    loop = asyncio.get_running_loop()
    if _CLIENT is not None:
        held_loop, client = _CLIENT
        if held_loop is loop and not client.is_closed:
            return client
    client = httpx.AsyncClient(timeout=DEFAULT_TIMEOUT_S)
    _CLIENT = (loop, client)
    return client


async def _post(payload: dict, timeout: float) -> Optional[str]:
    r = await _client().post(
        f"{gateway_base_url().rstrip('/')}/v1/chat/completions",
        headers={"Authorization": f"Bearer {gateway_api_key()}",
                 "Content-Type": "application/json"},
        json=payload, timeout=timeout,
    )
    r.raise_for_status()
    return _completion_text(r.json())


async def hear(pcm16: bytes, *, rate: int = 16000, names: Iterable[str] = (),
               timeout: Optional[float] = None,
               model: Optional[str] = None) -> dict:
    """Hear one commit's audio again, within `timeout` seconds.

    Returns {"outcome", "text", "model", "ms", "audio_ms"}: "heard" (`text`
    is what the model heard), "no_speech", or why there is no answer:
    "no_audio", "timeout", "error" (with "error", redacted) or "empty". Never
    raises: a check that fails keeps the line, it does not cost the
    participant it."""
    model = model or check_model()
    timeout = timeout or timeout_s()
    audio_ms = int(len(pcm16 or b"") / 2 / rate * 1000) if rate else 0
    out = {"model": model, "text": None, "ms": 0, "audio_ms": audio_ms}
    if not pcm16 or audio_ms < MIN_AUDIO_MS:
        out["outcome"] = "no_audio"
        return out
    payload = {
        "model": model,
        "max_tokens": 300,
        "temperature": 0,
        "messages": [{
            "role": "user",
            "content": [
                {"type": "text", "text": _prompt(names or ())},
                {"type": "input_audio", "input_audio": {
                    "data": base64.b64encode(wav_bytes(pcm16, rate)).decode("ascii"),
                    "format": "wav"}},
            ],
        }],
    }
    started = time.time()
    try:
        text = await asyncio.wait_for(_post(payload, timeout), timeout)
    except asyncio.TimeoutError:
        out.update(outcome="timeout", ms=int((time.time() - started) * 1000))
        return out
    except Exception as exc:  # noqa: BLE001 - any failure keeps the line
        # Redacted like every other gateway error this codebase stores: the
        # string goes into events.jsonl, which is archived and shipped.
        out.update(outcome="error",
                   error=redact_key(f"{type(exc).__name__}: {exc}")[:200],
                   ms=int((time.time() - started) * 1000))
        return out
    out["ms"] = int((time.time() - started) * 1000)
    text = _clean(text or "")
    if not text:
        out["outcome"] = "empty"
        return out
    if _NO_SPEECH.fullmatch(text):
        out["outcome"] = "no_speech"
        return out
    out.update(outcome="heard", text=text)
    return out
