"""The simulated participant's voice: committed stimulus lines and room tone.

Every line the sim can say is a file in tools/sim/stim/, named in
tools/sim/stim/lines.txt as `name|voice|text`. The voice is a macOS `say`
voice (Samantha for the 1:1 and S3 lines, Daniel for S4), so the stimulus is
synthetic speech with a known transcript and no participant in it: nothing
recorded from a person is committed here, and this repository is public.

WHY µ-law and not the PCM the server is sent. The server takes 16 kHz mono
signed 16-bit PCM, which for the ~40 lines the four default sequences use is
about 4 MB, in git history for good. G.711 µ-law at the same 16 kHz is half
that, is decoded here in a few lines of standard-library Python on every
platform, and costs the transcriber nothing it can measure on clean synthetic
speech (telephone audio is µ-law at half this rate). load() hands the driver
PCM, so nothing downstream knows.

Room tone is generated, not recorded: seeded, low-passed noise at about
-57 dBFS, well under the server's speech hint level (VAD_HINT_RMS 160 in
server/voice/realtime.py), so a "tone" step is a quiet room rather than
digital silence, deterministically, on every machine.
"""
from __future__ import annotations

import math
import random
import sys
from array import array
from functools import lru_cache
from pathlib import Path
from typing import Dict, Tuple

STIM_DIR = Path(__file__).resolve().parent / "stim"
LINES = STIM_DIR / "lines.txt"

RATE = 16000                 # what the page's worklet sends (static/pcm-worklet.js)
BYTES_PER_S = RATE * 2       # mono, 16-bit
EXT = ".ulaw"

_BIAS = 0x84
_CLIP = 32635


def _decode_one(u: int) -> int:
    u = ~u & 0xFF
    sign, exponent, mantissa = u & 0x80, (u >> 4) & 0x07, u & 0x0F
    sample = (((mantissa << 3) + _BIAS) << exponent) - _BIAS
    return -sample if sign else sample


_DECODE = [_decode_one(u) for u in range(256)]


def ulaw_encode(pcm: bytes) -> bytes:
    """16-bit little-endian PCM to G.711 µ-law (used by make_stim.py)."""
    samples = array("h")
    samples.frombytes(pcm[: len(pcm) // 2 * 2])
    if sys.byteorder == "big":
        samples.byteswap()
    out = bytearray(len(samples))
    for i, s in enumerate(samples):
        sign = 0x80 if s < 0 else 0
        mag = min(-s if s < 0 else s, _CLIP) + _BIAS
        exponent = min(7, max(0, (mag >> 7).bit_length() - 1))
        mantissa = (mag >> (exponent + 3)) & 0x0F
        out[i] = ~(sign | (exponent << 4) | mantissa) & 0xFF
    return bytes(out)


def ulaw_decode(data: bytes) -> bytes:
    """G.711 µ-law to 16-bit little-endian PCM."""
    samples = array("h", (_DECODE[b] for b in data))
    if sys.byteorder == "big":
        samples.byteswap()
    return samples.tobytes()


def lines() -> Dict[str, Tuple[str, str]]:
    """{name: (voice, text)} from lines.txt.

    A row is `name|voice|text`, optionally `|say input` after it when the
    audio was made from something other than the plain text (pauses written as
    `say` [[slnc N]] commands). `text` is always the words spoken, because it
    is what tools/sim/analyze.py matches the server's transcripts against.
    """
    out: Dict[str, Tuple[str, str]] = {}
    for raw in LINES.read_text(encoding="utf-8").splitlines():
        if not raw.strip() or raw.startswith("#"):
            continue
        name, voice, text = raw.split("|", 3)[:3]
        out[name.strip()] = (voice.strip(), text.strip())
    return out


@lru_cache(maxsize=None)
def load(name: str) -> bytes:
    """The PCM the driver streams for stimulus `name` (with or without .ulaw)."""
    stem = name[: -len(EXT)] if name.endswith(EXT) else name
    path = STIM_DIR / f"{stem}{EXT}"
    if not path.is_file():
        raise FileNotFoundError(f"no stimulus {stem!r} in {STIM_DIR}")
    return ulaw_decode(path.read_bytes())


def text(name: str) -> str:
    return lines()[name][1]


@lru_cache(maxsize=1)
def _tone_loop() -> bytes:
    """12 s of room tone, the loop length the recorded tone it replaces had."""
    rng = random.Random(20260928)
    n = 12 * RATE
    raw, y = [], 0.0
    for _ in range(n):
        # One-pole low-pass over white noise: a soft broadband hush rather
        # than hiss, which is what an empty room through a laptop mic is.
        y = 0.92 * y + 0.08 * rng.gauss(0.0, 1.0)
        raw.append(y)
    rms = math.sqrt(sum(v * v for v in raw) / n)
    target = 45.0            # about -57 dBFS
    samples = array("h", (max(-32768, min(32767, int(round(v * target / rms)))) for v in raw))
    if sys.byteorder == "big":
        samples.byteswap()
    return samples.tobytes()


def room_tone(seconds: float) -> bytes:
    n = int(float(seconds) * RATE) * 2
    loop = _tone_loop()
    return (loop * (n // len(loop) + 1))[:n]


def rms(pcm: bytes) -> float:
    samples = array("h")
    samples.frombytes(pcm[: len(pcm) // 2 * 2])
    if sys.byteorder == "big":
        samples.byteswap()
    if not samples:
        return 0.0
    return math.sqrt(sum(s * s for s in samples) / len(samples))
