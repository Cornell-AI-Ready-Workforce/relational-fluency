"""Persistent storage for the dataset.

Per-session layout under data/sessions/{session_id}/:
  manifest.json          , scenario, model, participant, run/cohort, durations
  events.jsonl           , turn-level events (replaces logs/{id}.jsonl)
  user_audio.wav         , 16 kHz mono mic stream
  assistant_audio.wav    , 16 kHz mono TTS stream

A SQLite index at data/index.db lets you query across sessions:
  sessions(id, participant_id, scenario, model, started_at, ended_at, duration_s,
           status, n_turns, dir, run_id, cohort)
  participants(id, code, consent_given, consent_text_version, created_at)

run_id/cohort/participant_key are carried on the manifest and the sessions row
so an encounter is self-describing. Before that, the only link from a recorded
encounter back to its run (and therefore to its cohort) was an entry the
browser POSTed to /api/run/{id}/advance, so an encounter whose client never
reported back was orphaned and no offline tool could tell internal test traffic
from study data. A null cohort means the encounter did not come through a run
at all (ad-hoc landing-page or researcher-launch traffic); it is not study data.

The WavAppender writes incrementally so a crash mid-session still leaves a
valid WAV, we patch the header on every flush.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
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
    unguarded relative_to() raised ValueError, which crashed every session at
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
            run_id TEXT,
            cohort TEXT,
            FOREIGN KEY (participant_id) REFERENCES participants(id)
        );
        CREATE INDEX IF NOT EXISTS sessions_started_at ON sessions(started_at);
        CREATE INDEX IF NOT EXISTS sessions_participant ON sessions(participant_id);
        """)
        # CREATE TABLE IF NOT EXISTS leaves an already-existing table alone, so
        # a database written before run_id/cohort existed would keep the old
        # columns and every INSERT below would fail, killing sessions on a
        # deployment that already has data. Add the columns in place instead.
        _add_missing_columns(conn, "sessions", {"run_id": "TEXT", "cohort": "TEXT"})


def _add_missing_columns(conn, table: str, columns: Dict[str, str]) -> None:
    """ALTER TABLE ... ADD COLUMN for any of `columns` not already present."""
    have = {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}
    for name, decl in columns.items():
        if name not in have:
            try:
                conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {decl}")
            except sqlite3.OperationalError:
                # Racing worker already added it, or the file is read-only.
                # Readers below degrade to "column absent" rather than failing.
                pass


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


def spec_fingerprint(scenario_id: Optional[str]) -> Optional[Dict[str, Any]]:
    """The planted triggers of a v3 scenario spec, as it stands right now.

    Coverage is reported as fired/planted and the denominator is read from the
    spec file at verification time, not from the encounter. The spec files do
    get edited between waves (S2A gained two planted beats to reach parity with
    S2B), and nothing on a recorded encounter said which version it had actually
    been run against, so re-verifying an archived encounter scored it against a
    plan it never saw: an encounter that fired every beat it was given reads as
    4/6, and two encounters in the same study stop being comparable with nothing
    in the data to show why. Stamping the trigger ids at session start is the
    only moment that information exists; after the edit it is unrecoverable.

    Returns None for anything outside the v3 instrument (the legacy demo
    scenarios plant no triggers, so there is no denominator to pin) and for a
    spec that will not load, since an encounter must never fail to start over
    bookkeeping.
    """
    if not scenario_id:
        return None
    try:
        from .scenarios_v3 import load_spec

        spec = load_spec(scenario_id)
        ids = [
            t["id"]
            for i in spec.get("interactions", [])
            for t in i.get("triggers", [])
        ]
    except Exception:  # noqa: BLE001, not a v3 scenario, or an unreadable spec
        return None
    if not ids:
        return None
    # Hash the ordered ids rather than the file bytes: a comment or a wording
    # tweak in the YAML must not read as a changed instrument, whereas planting,
    # dropping or reordering a beat must.
    digest = hashlib.sha256("\n".join(ids).encode("utf-8")).hexdigest()
    return {"trigger_ids": ids, "sha256": digest}


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

    run_id/cohort/participant_key/encounter_index describe the study context and
    are optional: sessions started outside a run (landing page, researcher
    launch) simply record them as null, and callers that predate them keep
    working unchanged.
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
        run_id: Optional[str] = None,
        cohort: Optional[str] = None,
        participant_key: Optional[str] = None,
        encounter_index: Optional[int] = None,
    ):
        self.id = session_id
        self.dir = SESSIONS_DIR / session_id
        self.dir.mkdir(parents=True, exist_ok=True)
        self.scenario = scenario
        self.model = model
        self.participant_id = participant_id
        self.agent_ids = list(agent_ids or [])
        # Study context, written into the manifest and the index so an encounter
        # can be attributed (or excluded) without a client-maintained join.
        self.run_id = run_id
        self.cohort = cohort
        self.participant_key = participant_key
        self.encounter_index = encounter_index
        # Which version of the scenario's planted-trigger plan this encounter
        # was run against (see spec_fingerprint). Captured once, here, because
        # this is the last moment it is knowable.
        self.spec_fingerprint = spec_fingerprint(self.scenario)
        self.started_at = time.time()
        self.events_path = self.dir / "events.jsonl"
        # event() writes json.dumps(..., ensure_ascii=False), so the log must be
        # UTF-8. Without an explicit encoding the locale default (cp1252 on a
        # Windows host) raises UnicodeEncodeError on the first non-ANSI character
        # a participant or the model produces, killing the live encounter.
        self.events_fh = self.events_path.open("a", buffering=1, encoding="utf-8")
        self.user_audio: Optional[WavAppender] = None
        self.assistant_audio: Dict[str, WavAppender] = {}
        if capture_audio:
            self.user_audio = WavAppender(self.dir / "user_audio.wav")
            if not self.agent_ids or len(self.agent_ids) <= 1:
                # Single-agent, preserve legacy filename
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
                   (id, participant_id, scenario, model, started_at, status, dir,
                    run_id, cohort)
                   VALUES (?, ?, ?, ?, ?, 'active', ?, ?, ?)""",
                (self.id, self.participant_id, self.scenario, self.model,
                 self.started_at, _record_dir(self.dir), self.run_id, self.cohort),
            )
        self._write_manifest(status="active")

    def event(self, type_: str, **fields: Any) -> None:
        # Guard against writes after close(): a fire-and-forget steering task or
        # a still-connected researcher socket can call event() after the store
        # was closed; writing to the closed handle would raise ValueError and
        # kill that task/socket. Drop the late event silently instead.
        if self.events_fh is None or self.events_fh.closed:
            return
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
            # The study context, so this encounter can be joined to its run and
            # excluded by cohort on its own, without the run file and without
            # the browser's advance POST having succeeded. Null on sessions that
            # did not come through a run, which are never study data.
            "run_id": self.run_id,
            "cohort": self.cohort,
            "participant_key": self.participant_key,
            "encounter_index": self.encounter_index,
            # The planted-trigger plan in force when this encounter started, so
            # a later verification pass can tell "this encounter missed beats"
            # from "the spec grew beats after this encounter was recorded".
            "spec_fingerprint": self.spec_fingerprint,
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
        # Write atomically: a kill mid-write must not leave a truncated
        # manifest, which /api/encounters would skip, vanishing the encounter
        # from the dashboard even though its events/WAVs are intact.
        tmp = self.dir / "manifest.json.tmp"
        tmp.write_text(json.dumps(manifest, indent=2), encoding="utf-8")
        os.replace(tmp, self.dir / "manifest.json")

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
        self.events_fh = None
        self._write_manifest(status="closed", ended_at=ended_at, n_turns=n_turns)
        # Build the analysis-facing aligned record alongside the raw event log.
        try:
            from .encounter_record import write as write_record
            write_record(self.dir)
        except Exception:  # noqa: BLE001, never fail a session close on this
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
    # Atomic write: a kill mid-write must not leave a truncated consent file,
    # which get_participant would fail to parse and lock the participant out.
    dest = PARTICIPANTS_DIR / f"{pid}.json"
    tmp = PARTICIPANTS_DIR / f"{pid}.json.tmp"
    tmp.write_text(json.dumps(rec, indent=2), encoding="utf-8")
    os.replace(tmp, dest)
    with _db() as conn:
        conn.execute(
            """INSERT INTO participants
               (id, code, consent_given, consent_text_version, created_at)
               VALUES (?, ?, ?, ?, ?)""",
            (rec["id"], rec["code"], int(rec["consent_given"]),
             rec["consent_text_version"], rec["created_at"]),
        )
    return pid


_PID_RE = re.compile(r"p_[0-9]+_[0-9a-f]{6}")


def get_participant(pid: str) -> Optional[Dict[str, Any]]:
    # pid arrives from a websocket query parameter, which may contain '/'.
    # Validate its exact minted shape before touching the filesystem so a
    # traversal string (e.g. "../runs/<run_id>") cannot resolve to another
    # record and slip past the voice-path consent gate.
    if not pid or not _PID_RE.fullmatch(pid):
        return None
    path = PARTICIPANTS_DIR / f"{pid}.json"
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except ValueError:
        return None


def record_decline(pid: str, consent_version: str,
                   run_id: Optional[str] = None) -> Optional[Dict[str, Any]]:
    """Record that a participant read the consent form and refused.

    A refusal is data. Without it the only trace of someone deciding not to take
    part is an abandoned tab, which is indistinguishable from a browser crash,
    and the study cannot report how many people declined after reading the form
    — a figure an IRB asks for.

    The record stays consent_given=False, so nothing downstream can mistake it
    for consent: the voice websocket already refuses any record whose flag is
    not set.
    """
    rec = get_participant(pid)
    if rec is None:
        return None
    rec["consent_given"] = False
    rec["declined"] = True
    rec["declined_at"] = time.time()
    rec["consent_text_version"] = consent_version
    if run_id:
        rec["run_id"] = run_id
    dest = PARTICIPANTS_DIR / f"{pid}.json"
    tmp = PARTICIPANTS_DIR / f"{pid}.json.tmp"
    tmp.write_text(json.dumps(rec, indent=2), encoding="utf-8")
    os.replace(tmp, dest)
    with _db() as conn:
        conn.execute(
            "UPDATE participants SET consent_given = 0, consent_text_version = ? WHERE id = ?",
            (consent_version, pid),
        )
    return rec


def record_consent(pid: str, consent_version: str) -> Optional[Dict[str, Any]]:
    """Flip an existing pending participant record to consented.

    /start mints the participant record early so one person keeps one identity
    across all four encounters, but it must not assert consent on their behalf.
    The record is therefore created with consent_given=False and only this
    function, called from POST /api/consent after the participant ticks the box,
    may set it true. Returns the updated record, or None if there is no such
    record.
    """
    rec = get_participant(pid)
    if rec is None:
        return None
    rec["consent_given"] = True
    rec["consent_text_version"] = consent_version
    rec["consent_recorded_at"] = time.time()
    dest = PARTICIPANTS_DIR / f"{pid}.json"
    tmp = PARTICIPANTS_DIR / f"{pid}.json.tmp"
    tmp.write_text(json.dumps(rec, indent=2), encoding="utf-8")
    os.replace(tmp, dest)
    with _db() as conn:
        conn.execute(
            "UPDATE participants SET consent_given = 1, consent_text_version = ? WHERE id = ?",
            (consent_version, pid),
        )
    return rec
