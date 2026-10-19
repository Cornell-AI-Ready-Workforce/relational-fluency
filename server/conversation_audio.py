"""The whole conversation as one WAV: what the participant heard and said.

The session directory keeps one WAV per speaker (user_audio.wav,
assistant_audio[_<agent>].wav), and every one of them is GAPLESS: the mic
channel drops samples while muted, and an agent channel accumulates only while
that agent speaks, at whatever rate the gateway delivered it. None of them is
on the conversation's clock, so they cannot be laid side by side or summed to
hear the encounter as it happened.

So SessionStore keeps a Timeline as audio is appended: for every chunk, where
it sits in its own channel's WAV and where it falls on the session clock.

  mic        placed by arrival. A continuous stream stays continuous (its
             sample count is exact); a gap in arrival longer than
             MIC_GAP_S — a mute, a stalled socket — starts a new run at the
             time the chunk was captured (arrival minus its own length).
  agent      placed where the page plays it: max(arrival, the end of that
             agent's previous chunk), the same play cursor the runner keeps
             (_advance_play_cursor) and the page's own `playbackTime`.
  play_end   an interrupted reply stops on the page at the ack's `at`; the
             agent's placements are cut there, so the mix does not keep
             talking over a participant who cut in.

The timeline is written to audio_timeline.json at close and rendered to
conversation_audio.wav off the request path (render). 16 kHz mono s16le like
the channels, overlaps summed and clipped. render() needs only the session
directory, so an encounter can be re-rendered later from what is on disk.
"""
from __future__ import annotations

import json
import logging
import struct
import sys
import time
from array import array
from pathlib import Path
from typing import Dict, List, Optional

log = logging.getLogger(__name__)

SAMPLE_RATE = 16000
BYTES_PER_SAMPLE = 2
TIMELINE_NAME = "audio_timeline.json"
CONVERSATION_NAME = "conversation_audio.wav"

# Mic chunks arrive every 20-100 ms. A pause in arrival longer than this is
# audio that was never sent (a mute, a stalled socket), not jitter.
MIC_GAP_S = 0.75


class Timeline:
    """Where each appended chunk falls on the session clock. Thread-safe enough
    for its callers: each channel is appended from one task, and the lists are
    only read at close."""

    def __init__(self, started_at: float):
        self.started_at = started_at
        # file -> [[timeline_sample, src_byte_offset, n_bytes], ...]
        self.runs: Dict[str, List[List[int]]] = {}
        self._src_bytes: Dict[str, int] = {}
        self._cursor: Dict[str, int] = {}   # next timeline sample per file

    def _now_samples(self, now: Optional[float]) -> int:
        return int(round(((time.time() if now is None else now) - self.started_at) * SAMPLE_RATE))

    def _place(self, file: str, at: int, n_bytes: int) -> None:
        src = self._src_bytes.get(file, 0)
        runs = self.runs.setdefault(file, [])
        last = runs[-1] if runs else None
        if last and last[0] + last[2] // BYTES_PER_SAMPLE == at and last[1] + last[2] == src:
            last[2] += n_bytes   # contiguous on both clocks: extend the run
        else:
            runs.append([at, src, n_bytes])
        self._src_bytes[file] = src + n_bytes
        self._cursor[file] = at + n_bytes // BYTES_PER_SAMPLE

    def mic(self, file: str, n_bytes: int, now: Optional[float] = None) -> None:
        if n_bytes <= 0:
            return
        n = n_bytes // BYTES_PER_SAMPLE
        captured = max(0, self._now_samples(now) - n)
        cursor = self._cursor.get(file)
        if cursor is None or captured - cursor > MIC_GAP_S * SAMPLE_RATE:
            cursor = captured
        self._place(file, cursor, n_bytes)

    def agent(self, file: str, n_bytes: int, now: Optional[float] = None) -> None:
        if n_bytes <= 0:
            return
        at = max(self._now_samples(now), self._cursor.get(file, 0))
        self._place(file, at, n_bytes)

    def cut(self, file: str, at_s: float) -> None:
        """The page stopped this agent's playback at `at_s` (session seconds)."""
        stop = int(round(at_s * SAMPLE_RATE))
        kept = []
        for start, src, nb in self.runs.get(file, []):
            if start >= stop:
                continue
            end = start + nb // BYTES_PER_SAMPLE
            if end > stop:
                nb = (stop - start) * BYTES_PER_SAMPLE
            kept.append([start, src, nb])
        if file in self.runs:
            self.runs[file] = kept
        if self._cursor.get(file, 0) > stop:
            self._cursor[file] = stop

    def to_json(self) -> dict:
        return {"sample_rate": SAMPLE_RATE, "format": "pcm_s16le",
                "started_at": self.started_at, "runs": self.runs}


def write_timeline(session_dir: Path, timeline: Timeline) -> None:
    (Path(session_dir) / TIMELINE_NAME).write_text(
        json.dumps(timeline.to_json()), encoding="utf-8")


def _read_pcm(path: Path, src: int, n_bytes: int) -> array:
    out = array("h")
    with open(path, "rb") as fh:
        fh.seek(44 + src)
        data = fh.read(n_bytes)
    out.frombytes(data[: len(data) - len(data) % 2])
    if sys.byteorder != "little":
        out.byteswap()
    return out


def _wav_header(data_bytes: int) -> bytes:
    return struct.pack(
        "<4sI4s4sIHHIIHH4sI", b"RIFF", 36 + data_bytes, b"WAVE", b"fmt ", 16, 1, 1,
        SAMPLE_RATE, SAMPLE_RATE * BYTES_PER_SAMPLE, BYTES_PER_SAMPLE, 16,
        b"data", data_bytes)


def render(session_dir: Path) -> Optional[Path]:
    """Mix audio_timeline.json into conversation_audio.wav. None if there is
    nothing to mix. Never raises: it runs in a thread nobody is watching."""
    session_dir = Path(session_dir)
    try:
        spec = json.loads((session_dir / TIMELINE_NAME).read_text(encoding="utf-8"))
        runs = {f: r for f, r in (spec.get("runs") or {}).items()
                if r and (session_dir / f).is_file() and "/" not in f and "\\" not in f}
        if not runs:
            return None
        total = max(start + nb // BYTES_PER_SAMPLE
                    for rs in runs.values() for start, _src, nb in rs)
        mix = array("h", bytes(total * BYTES_PER_SAMPLE))
        # The first channel is copied in (no per-sample loop for the longest
        # one, the mic); every later one is summed and clipped.
        order = sorted(runs, key=lambda f: (f != "user_audio.wav", f))
        filled = array("b", bytes(total)) if len(order) > 1 else None
        for i, f in enumerate(order):
            for start, src, nb in runs[f]:
                pcm = _read_pcm(session_dir / f, src, nb)
                if i == 0:
                    mix[start:start + len(pcm)] = pcm
                    if filled is not None:
                        filled[start:start + len(pcm)] = array("b", b"\x01" * len(pcm))
                    continue
                for k, s in enumerate(pcm):
                    j = start + k
                    if filled[j]:
                        v = mix[j] + s
                        mix[j] = 32767 if v > 32767 else (-32768 if v < -32768 else v)
                    else:
                        mix[j] = s
                        filled[j] = 1
        out = session_dir / CONVERSATION_NAME
        tmp = out.with_name(out.name + ".tmp")
        with open(tmp, "wb") as fh:
            fh.write(_wav_header(len(mix) * BYTES_PER_SAMPLE))
            if sys.byteorder != "little":
                mix.byteswap()
            fh.write(mix.tobytes())
        tmp.replace(out)
        return out
    except FileNotFoundError:
        return None
    except Exception:  # noqa: BLE001
        log.exception("could not render the conversation audio for %s", session_dir.name)
        return None


def _render_then_archive(session_dir: Path, *, archive_after: bool) -> None:
    if render(session_dir) is not None:
        try:
            from .encounter_record import write as write_record
            write_record(session_dir)
        except Exception:  # noqa: BLE001
            log.exception("could not rebuild the record for %s", Path(session_dir).name)
    if archive_after:
        from .archive import archive_session
        archive_session(session_dir)


def finish_later(session_dir: Path):
    """After a session closes: render the mix, then archive (if archiving is
    on), in one daemon thread so the close returns at once. Returns the thread."""
    import threading
    from . import archive

    session_dir = Path(session_dir)
    if archive.enabled():
        return archive.archive_session_later(
            session_dir, runner=lambda d: _render_then_archive(d, archive_after=True))
    t = threading.Thread(target=_render_then_archive, args=(session_dir,),
                         kwargs={"archive_after": False},
                         name=f"mix-{session_dir.name}", daemon=True)
    t.start()
    return t
