"""Persistent storage for the dataset.

Per-session layout under data/sessions/{session_id}/:
  manifest.json           — scenario, model, participant, consent, durations
  events.jsonl            — turn-level events (replaces logs/{id}.jsonl)
  user_audio.wav          — 16 kHz mono mic stream
  assistant_audio.wav     — 16 kHz mono TTS stream

A SQLite index at data/index.db lets you query across sessions:
  sessions(id, participant_id, scenario, model, started_at, ended_at, duration_s,
           status, n_turns, dir)
  participants(id, code, consent_given, consent_text_version, created_at)

The WavAppender writes incrementally so a crash mid-session still leaves a
valid WAV — we patch the header on every flush.
"""
from __future__ import annotations

import json
import os
import sqlite3
import struct
import threading
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Dict, List, Optional


ROOT = Path(__file__).parent.parent
# DATA_DIR can be overridden via env var so the deploy host can mount a
# persistent volume somewhere other than the source tree (e.g. Fly's /data).
DATA_DIR = Path(os.environ.get("DATA_DIR", str(ROOT / "data"))).resolve()


def _record_dir(path: Path) -> str:
    """Path stored in the index.

    Relative to the repo root when the data lives inside it (development), and
    absolute otherwise. In the container DATA_DIR is /data, outside /app, and an
    unguarded relative_to() raised ValueError — which crashed every session at
    creation and closed the socket the moment a participant tried to speak.
    """
    try:
        return str(path.relative_to(ROOT))
    except ValueError:
        return str(path)
SESSIONS_DIR = DATA_DIR / "sessions"
PARTICIPANTS_DIR = DATA_DIR / "participants"
DB_PATH = DATA_DIR / "index.db"

SAMPLE_RATE = 16000
CHANNELS = 1
SAMPLE_WIDTH = 2  # bytes; 16-bit PCM


def init_storage() -> None:
    """Create directories and DB tables if not present. Idempotent."""
    SESSIONS_DIR.mkdir(parents=True, exist_ok=True)
    PARTICIPANTS_DIR.mkdir(parents=True, exist_ok=True)
    with _db() as conn:
        conn.executescript("""
        CREATE TABLE IF NOT EXISTS participants (
            id TEXT PRIMARY KEY,
            code TEXT NOT NULL,
            consent_given INTEGER NOT NULL,
            consent_text_version TEXT,
            created_at REAL NOT NULL
        );
        CREATE TABLE IF NOT EXISTS sessions (
            id TEXT PRIMARY KEY,
            participant_id TEXT,
            scenario TEXT NOT NULL,
            model TEXT NOT NULL,
            started_at REAL NOT NULL,
            ended_at REAL,
            duration_s REAL,
            status TEXT NOT NULL,
            n_turns INTEGER DEFAULT 0,
            dir TEXT NOT NULL,
            FOREIGN KEY (participant_id) REFERENCES participants(id)
        );
        CREATE INDEX IF NOT EXISTS sessions_started_at ON sessions(started_at);
        CREATE INDEX IF NOT EXISTS sessions_participant ON sessions(participant_id);
        """)


@contextmanager
def _db():
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH, timeout=10)
    conn.execute("PRAGMA journal_mode=WAL;")
    conn.row_factory = sqlite3.Row
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


# ---------- WAV appender ----------

class WavAppender:
    """Append-safe WAV writer for 16-bit PCM mono.

    We write a placeholder header at open, then patch the size fields after
    each batch of frames. If the process crashes, the file still plays back
    up to the last patched header.
    """

    def __init__(self, path: Path, sample_rate: int = SAMPLE_RATE):
        self.path = Path(path)
        self.sample_rate = sample_rate
        self._lock = threading.Lock()
        self._fh = None
        self._data_bytes = 0
        self._open()

    def _open(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._fh = open(self.path, "wb")
        self._write_header(data_bytes=0)
        self._fh.flush()

    def _write_header(self, data_bytes: int) -> None:
        riff_size = 36 + data_bytes
        byte_rate = self.sample_rate * CHANNELS * SAMPLE_WIDTH
        block_align = CHANNELS * SAMPLE_WIDTH
        header = struct.pack(
            "<4sI4s4sIHHIIHH4sI",
            b"RIFF", riff_size, b"WAVE",
            b"fmt ", 16, 1, CHANNELS,
            self.sample_rate, byte_rate, block_align, SAMPLE_WIDTH * 8,
            b"data", data_bytes,
        )
        self._fh.seek(0)
        self._fh.write(header)

    def append(self, pcm_bytes: bytes) -> None:
        if not pcm_bytes:
            return
        with self._lock:
            if self._fh is None:
                return
            self._fh.seek(44 + self._data_bytes)
            self._fh.write(pcm_bytes)
            self._data_bytes += len(pcm_bytes)
            # Patch header so partial files remain valid.
            self._write_header(self._data_bytes)
            self._fh.flush()

    def close(self) -> None:
        with self._lock:
            if self._fh is None:
                return
            self._write_header(self._data_bytes)
            self._fh.flush()
            self._fh.close()
            self._fh = None

    @property
    def duration_s(self) -> float:
        return self._data_bytes / (self.sample_rate * SAMPLE_WIDTH * CHANNELS)


# ---------- Session storage ----------

class SessionStore:
    """Owns the on-disk directory + manifest + audio writers for one session.

    For single-agent sessions, assistant audio goes to assistant_audio.wav
    (legacy filename). For multi-agent group sessions, each agent gets its own
    assistant_audio_{agent_id}.wav so per-agent analysis is straightforward.
    """

    def __init__(
        self,
        session_id: str,
        *,
        scenario: str,
        model: str,
        participant_id: Optional[str],
        capture_audio: bool,
        agent_ids: Optional[List[str]] = None,
    ):
        self.id = session_id
        self.dir = SESSIONS_DIR / session_id
        self.dir.mkdir(parents=True, exist_ok=True)
        self.scenario = scenario
        self.model = model
        self.participant_id = participant_id
        self.agent_ids = list(agent_ids or [])
        self.started_at = time.time()
        self.events_path = self.dir / "events.jsonl"
        self.events_fh = self.events_path.open("a", buffering=1)
        self.user_audio: Optional[WavAppender] = None
        self.assistant_audio: Dict[str, WavAppender] = {}
        if capture_audio:
            self.user_audio = WavAppender(self.dir / "user_audio.wav")
            if not self.agent_ids or len(self.agent_ids) <= 1:
                # Single-agent — preserve legacy filename
                key = self.agent_ids[0] if self.agent_ids else "__default__"
                self.assistant_audio[key] = WavAppender(self.dir / "assistant_audio.wav")
            else:
                for aid in self.agent_ids:
                    self.assistant_audio[aid] = WavAppender(
                        self.dir / f"assistant_audio_{aid}.wav"
                    )

        with _db() as conn:
            conn.execute(
                """INSERT INTO sessions
                   (id, participant_id, scenario, model, started_at, status, dir)
                   VALUES (?, ?, ?, ?, ?, 'active', ?)""",
                (self.id, self.participant_id, self.scenario, self.model,
                 self.started_at, _record_dir(self.dir)),
            )
        self._write_manifest(status="active")

    def event(self, type_: str, **fields: Any) -> None:
        rec: Dict[str, Any] = {
            "t": round(time.time() - self.started_at, 3),
            "wall": time.time(),
            "type": type_,
        }
        rec.update(fields)
        self.events_fh.write(json.dumps(rec, ensure_ascii=False) + "\n")

    def append_user_audio(self, pcm: bytes) -> None:
        if self.user_audio:
            self.user_audio.append(pcm)

    def append_assistant_audio(self, pcm: bytes, agent_id: Optional[str] = None) -> None:
        if not self.assistant_audio:
            return
        if agent_id and agent_id in self.assistant_audio:
            self.assistant_audio[agent_id].append(pcm)
        else:
            # Single-file fallback for legacy callers that don't pass agent_id.
            next(iter(self.assistant_audio.values())).append(pcm)

    def _write_manifest(self, *, status: str, ended_at: Optional[float] = None,
                        n_turns: int = 0) -> None:
        per_agent_audio = {
            aid: round(w.duration_s, 3) for aid, w in self.assistant_audio.items()
        }
        manifest = {
            "session_id": self.id,
            "scenario": self.scenario,
            "model": self.model,
            "participant_id": self.participant_id,
            "agent_ids": self.agent_ids,
            "started_at": self.started_at,
            "ended_at": ended_at,
            "status": status,
            "n_turns": n_turns,
            "audio": {
                "sample_rate": SAMPLE_RATE,
                "channels": CHANNELS,
                "format": "pcm_s16le",
                "user_audio_duration_s": (
                    round(self.user_audio.duration_s, 3) if self.user_audio else None
                ),
                "assistant_audio_duration_s_by_agent": per_agent_audio,
            },
        }
        (self.dir / "manifest.json").write_text(json.dumps(manifest, indent=2))

    def close(self, *, n_turns: int = 0) -> None:
        ended_at = time.time()
        duration = ended_at - self.started_at
        if self.user_audio:
            self.user_audio.close()
        for w in self.assistant_audio.values():
            w.close()
        try:
            self.events_fh.close()
        except Exception:
            pass
        self._write_manifest(status="closed", ended_at=ended_at, n_turns=n_turns)
        # Build the analysis-facing aligned record alongside the raw event log.
        try:
            from .encounter_record import write as write_record
            write_record(self.dir)
        except Exception:  # noqa: BLE001 — never fail a session close on this
            pass
        with _db() as conn:
            conn.execute(
                """UPDATE sessions
                   SET ended_at = ?, duration_s = ?, status = 'closed', n_turns = ?
                   WHERE id = ?""",
                (ended_at, round(duration, 3), n_turns, self.id),
            )


# ---------- Participants / consent ----------

def create_participant(code: str, consent_given: bool, consent_version: str) -> str:
    pid = f"p_{int(time.time())}_{os.urandom(3).hex()}"
    rec = {
        "id": pid,
        "code": code,
        "consent_given": bool(consent_given),
        "consent_text_version": consent_version,
        "created_at": time.time(),
    }
    (PARTICIPANTS_DIR / f"{pid}.json").write_text(json.dumps(rec, indent=2))
    with _db() as conn:
        conn.execute(
            """INSERT INTO participants
               (id, code, consent_given, consent_text_version, created_at)
               VALUES (?, ?, ?, ?, ?)""",
            (rec["id"], rec["code"], int(rec["consent_given"]),
             rec["consent_text_version"], rec["created_at"]),
        )
    return pid


def get_participant(pid: str) -> Optional[Dict[str, Any]]:
    path = PARTICIPANTS_DIR / f"{pid}.json"
    if not path.exists():
        return None
    return json.loads(path.read_text())
