"""Did the participant actually HEAR the line the record says was spoken?

A turn can be complete in every channel this study writes down and still have
reached the participant as half a sentence. The transcript is the model's own
text and arrives whole; the assistant WAV is written from the same frames the
runner relayed; neither of them can tell you that the audio stopped four words
in, because from the server's side nothing is missing. The one comparison that
can is between a turn's own text and its own delivered audio, and until now
nothing made it, on any encounter, ever.

So the runner counts the audio bytes it hands the participant per turn and
records them on the turn (`audio_ms`), and this module is the arithmetic that
says whether that is enough audio for those words. It is deliberately crude and
deliberately one-sided:

  * `WPM` is a spoken-rate estimate, not a measurement. Measured on live
    nto.gemini-live-2.5-flash replies through the Cornell gateway, healthy turns
    land between 140 and 200 wpm.
  * `SHORTFALL_RATIO` is set where a turn has to be less than HALF the audio its
    own words need before anything is said. A turn at 60% is not flagged. This
    is a detector for "the audio stopped mid-sentence", not a speech-rate meter,
    and a false positive here would teach people to ignore it.
  * `MIN_WORDS` and `MIN_SHORTFALL_MS` keep short lines out of it entirely. "Mm."
    and "Right, okay" carry no useful ratio, and a stage-direction beat can be
    one word long.

The 11% of gateway replies that were measured ending in a bare `response.done`
with the audio stream abandoned mid-cadence are what this catches: their
transcripts are whole and 0.4-0.9 s of audio was delivered for 7-13 words.

Kept in server/voice/ rather than in the runner because two callers need it:
the runner, live, per turn; and a post-hoc reader of events.jsonl deciding
whether an encounter is usable as data. For the second one, `scan_events` takes
the parsed events of an encounter and answers in one call:

    from server.voice.turn_audio import scan_events
    short = scan_events(events)      # -> list of dicts, one per bad turn

tools/encounter_health.py is the natural home for that call; an encounter whose
turns were half-delivered is not usable as data, whatever else is right about it.
"""

from __future__ import annotations

import re
from typing import Iterable, List, Optional

# Bytes per second of the PCM16 stream the runner relays to the participant.
# 16 kHz mono, two bytes a sample: the client rate, not the gateway's 24 kHz,
# because what is counted is what was SENT to the browser.
CLIENT_BYTES_PER_S = 16000 * 2

WPM = 170.0
SHORTFALL_RATIO = 0.5
MIN_WORDS = 4
MIN_SHORTFALL_MS = 1000

_WORD = re.compile(r"[^\s]+")
# Stage directions the actors sometimes speak in parentheses are not words the
# voice has to get through, and neither is the em-dash a truncated line ends on.
_PAREN = re.compile(r"\([^)]*\)")


def word_count(text: str) -> int:
    return len(_WORD.findall(_PAREN.sub(" ", text or "")))


def audio_ms(nbytes: int) -> int:
    """Milliseconds of participant-rate PCM16 in `nbytes`."""
    if nbytes <= 0:
        return 0
    return int(round(nbytes / CLIENT_BYTES_PER_S * 1000))


def expected_ms(text: str) -> int:
    """How long these words take to say, at `WPM`."""
    return int(round(word_count(text) / WPM * 60_000))


def shortfall(text: str, delivered_ms: int) -> Optional[dict]:
    """None when the audio is a plausible delivery of `text`; otherwise the
    numbers that say it is not.

    A turn with NO audio at all is not reported here. That is a different and
    already-reported failure (transcript with no voice, which the stall watchdog
    and `transcript_missing` both cover) and folding it in would bury the case
    this exists for: audio that started normally and stopped early.
    """
    if delivered_ms <= 0:
        return None
    words = word_count(text)
    if words < MIN_WORDS:
        return None
    want = expected_ms(text)
    if delivered_ms >= want * SHORTFALL_RATIO:
        return None
    if want - delivered_ms < MIN_SHORTFALL_MS:
        return None
    return {
        "words": words,
        "audio_ms": delivered_ms,
        "expected_ms": want,
        "delivered_fraction": round(delivered_ms / want, 3),
    }


def scan_events(events: Iterable[dict]) -> List[dict]:
    """Every assistant_turn in an encounter whose audio was too short for its
    own words.

    Turns already flagged `interrupted` are skipped: the participant talking
    over a character is the behaviour two of the scenarios exist to score, and a
    cut-off there is the point rather than a fault. Turns recorded before
    `audio_ms` existed carry no field and are skipped rather than being read as
    zero — a check that condemns every archived encounter on a field it predates
    is a check nobody will keep.
    """
    out: List[dict] = []
    for ev in events:
        if ev.get("type") != "assistant_turn" or ev.get("interrupted"):
            continue
        ms = ev.get("audio_ms")
        if not isinstance(ms, int):
            continue
        bad = shortfall(ev.get("text") or "", ms)
        if bad:
            bad["agent_id"] = ev.get("agent_id")
            bad["text"] = ev.get("text")
            bad["segment"] = ev.get("segment")
            out.append(bad)
    return out
