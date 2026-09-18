"""Persistent storage for the dataset.

Per-session layout under data/sessions/{session_id}/:
  manifest.json          , scenario, model, participant, run/cohort, durations
  events.jsonl           , turn-level events (replaces logs/{id}.jsonl)
  user_audio.wav         , 16 kHz mono mic stream
  assistant_audio.wav    , 16 kHz mono TTS stream

A SQLite index at data/index.db lets you query across sessions:
  sessions(id, participant_id, scenario, model, started_at, ended_at, duration_s,
           status, n_turns, dir, run_id, cohort)
  participants(id, code, consent_given, consent_text_version, created_at,
               consent_source, consent_reference, consent_reference_kind)

Consent is taken outside this platform entirely (2026-09-17), so a participant
record carries identity, the run it was minted for, and a withdrawal if one
was recorded. The consent_* columns are kept in the schema for existing
indexes and are written as 0 / NULL.

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

import ast
import hashlib
import json
import logging
import os
import re
import sqlite3
import struct
import sys
import threading
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Dict, List, Optional


log = logging.getLogger(__name__)

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


# ---------- identifiers that become filenames ----------
#
# The three platforms disagree about what a path component means, and every
# disagreement below was reproduced on the Windows 11 dev box rather than
# assumed:
#
#   * case:      "S_1772460300_44C9A2" opens the directory "s_1772460300_44c9a2"
#                on Windows and on a default (case-insensitive APFS) macOS
#                volume; on Linux it is a different name and 404s.
#   * trailing:  "s_1772460300_44c9a2." and "s_1772460300_44c9a2 " are silently
#                stripped to the bare name by the Windows API — mkdir("sub.")
#                creates "sub" — so two ids that are distinct on Linux address
#                one directory on Windows.
#   * devices:   a component named NUL (and, depending on the Windows build,
#                CON/PRN/AUX/COM1-9/LPT1-9, with or without an extension) is a
#                character device: writing to it succeeds and the bytes are
#                discarded. Verified here: Path("nul").write_text("hello")
#                returns cleanly and reads back "".
#   * separator: "\" is a separator on Windows only, so a check that looks for
#                "/" alone leaves a traversal open on one platform.
#
# Nothing about that is theoretical for this study: rater_packet.rating_code
# HMACs the session id STRING, so on a Windows or macOS host the same encounter
# addressed under two spellings mints two different RC- codes and the blinded
# handle stops being one-per-encounter; video.video_key builds an S3 key from
# the same string, and S3 keys are case-sensitive everywhere, so a wrong-cased
# id would upload a participant's webcam recording to a key nothing else looks
# at. The defence is to validate the id against the shape it was MINTED in
# rather than to ask the filesystem what matches — the minted shape is
# lowercase by construction, which settles the case question on all three
# platforms at once.

# Session ids are minted by session.new_session_id as f"s_{epoch}_{token_hex(3)}".
# raters.py has enforced exactly this for a while; it lives here now so app.py,
# rater_packet.py and video.py can share the one definition instead of the
# permissive [A-Za-z0-9_-]{1,64} they each carried, which accepted uppercase.
SESSION_ID_RE = re.compile(r"s_[0-9]{1,20}_[0-9a-f]{6}")

# Reserved device names, checked without the extension because older Windows
# builds treat "nul.yaml" as the device too.
_WINDOWS_DEVICE_NAMES = frozenset(
    ["CON", "PRN", "AUX", "NUL"]
    + [f"COM{i}" for i in range(1, 10)]
    + [f"LPT{i}" for i in range(1, 10)]
)


def valid_session_id(session_id: Optional[str]) -> bool:
    """True when this is exactly the shape session.new_session_id mints."""
    return bool(session_id) and SESSION_ID_RE.fullmatch(session_id) is not None


def is_safe_path_component(name: Optional[str]) -> bool:
    """True when `name` names the same single path component on all three OSes.

    For identifiers whose charset cannot be narrowed to a minted shape — a
    scenario id is a human-written filename fragment like S1A or
    missed_deadlines — this is the portable floor: no separator of either
    flavour, no traversal, no Windows device name, and nothing whose trailing
    characters Windows would quietly rewrite.
    """
    if not name or name in (".", ".."):
        return False
    if "/" in name or "\\" in name or ":" in name or "\x00" in name:
        return False
    if name != name.rstrip(". "):
        return False
    return name.split(".", 1)[0].upper() not in _WINDOWS_DEVICE_NAMES


def replace_with_retry(tmp: Path, dest: Path, attempts: int = 8) -> None:
    """os.replace, retried past the Windows "file in use" window.

    POSIX rename() over an open file always succeeds, so on macOS and Linux this
    is os.replace with an unreachable loop. Windows fails the rename with
    PermissionError if ANY handle is open on EITHER side — a concurrent reader
    on the destination (WinError 5: app.py's _load_manifest, rater_packet and
    raters all read manifest.json from FastAPI's sync threadpool while the loop
    closes a session), or a scanner still holding the brand-new temp file
    (WinError 32: Defender opens .tmp files the moment they appear). Both were
    reproduced on the dev box, and both cleared on the first retry.

    Raises the last PermissionError if the window never closes, so a caller that
    must know still finds out.
    """
    for i in range(attempts):
        try:
            os.replace(tmp, dest)
            return
        except PermissionError:
            if i == attempts - 1:
                raise
            time.sleep(0.05 * (i + 1))


def init_storage() -> None:
    """Create directories and DB tables if not present. Idempotent.

    Called from every public entry point that WRITES (SessionStore.__init__,
    create_participant, record_withdrawal) and from the server's
    startup hook — never from this module's body, and never from server.app's.
    Same rule and same shape as raters.init_rater_storage, for three reasons:

      * an import must not mint storage. `import server.app` used to create
        DATA_DIR, sessions/, participants/ and index.db as a side effect, so the
        offline tools (verify_record, retranscribe), every pytest
        process and the CI matrix's `python -c "import server.app"` each left a
        schema-only data directory behind them, and an import on a read-only
        filesystem raised before anything had asked for anything.
      * a DATA_DIR that appears AFTER import — a mounted volume, a test
        repointing it — is still initialised, because the first write does it.
      * a process that only reads does not have to remember to call this.

    The cost is one mkdir pair and one CREATE TABLE IF NOT EXISTS pass per
    encounter and per participant, which is not on any hot path.
    """
    SESSIONS_DIR.mkdir(parents=True, exist_ok=True)
    PARTICIPANTS_DIR.mkdir(parents=True, exist_ok=True)
    with _db() as conn:
        conn.executescript("""
        CREATE TABLE IF NOT EXISTS participants (
            id TEXT PRIMARY KEY,
            code TEXT NOT NULL,
            consent_given INTEGER NOT NULL,
            consent_text_version TEXT,
            created_at REAL NOT NULL,
            consent_source TEXT,
            consent_reference TEXT,
            consent_reference_kind TEXT
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
        # Same reason, for the provenance columns: every deployment that has
        # already collected anything has a participants table without them, and
        # an INSERT naming a column that is not there fails at the moment a
        # participant is being admitted to the study.
        _add_missing_columns(conn, "participants", {
            "consent_source": "TEXT",
            "consent_reference": "TEXT",
            "consent_reference_kind": "TEXT",
        })


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
        # Validate at the point the directory is minted, not only where the id
        # is later used to find it. Every reader downstream (app._session_dir,
        # rater_packet, video, raters) now insists on this shape, so a store
        # created under any other one would write an encounter that no packet
        # builder, console route or rater assignment could ever address — and it
        # would do so silently, halfway through a paid participant's session.
        if not valid_session_id(session_id):
            raise ValueError(f"bad session_id: {session_id!r}")
        # The first write is what mints storage (see init_storage): an import
        # does not, so the directories and the sessions table may not exist yet.
        # It has to happen before the INSERT below, and before the mkdir, or a
        # participant's first encounter dies on "no such table: sessions".
        init_storage()
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
        replace_with_retry(tmp, self.dir / "manifest.json")

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
        try:
            self._write_manifest(status="closed", ended_at=ended_at, n_turns=n_turns)
        except OSError:  # noqa: BLE001, the close must finish regardless
            # Everything below this line is independent of the manifest, and
            # each of it matters more. Unguarded, a manifest write that lost the
            # rename race (Windows; see replace_with_retry) skipped BOTH the
            # record.json build and the DB status flip, so the encounter stayed
            # 'active' in the index and carried no analysis record at all: it
            # vanished from /api/encounters and from the rater packet builder
            # while its audio and events.jsonl sat on disk, intact and unread.
            # A stale manifest is a bad outcome; an invisible encounter is worse.
            log.exception("manifest write failed at close for %s", self.id)
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


# ---------- The encounter clock ----------
#
# Study 1 asks for at least seven minutes per encounter and wraps it at twelve
# (docs/study1-plan.md, E4). Three numbers, read here so the runner, the advance
# route and the participant page (through runs.view) agree on them:
#
#   ENCOUNTER_MIN_SECONDS   the floor — nothing completes an encounter earlier,
#                           except a withdrawal, which is never gated
#   ENCOUNTER_WRAP_SECONDS  the actor is told to close the scene
#   ENCOUNTER_MAX_SECONDS   the hard stop — the encounter completes regardless
#
# Measured from the moment the voice socket opens, which is when the page's
# timer starts, so the number the participant watches is the number the server
# enforces. Internal-cohort runs are exempt from the floor.
ENCOUNTER_MIN_SECONDS_DEFAULT = 420.0
ENCOUNTER_WRAP_SECONDS_DEFAULT = 720.0
ENCOUNTER_MAX_SECONDS_DEFAULT = 780.0


def encounter_timing() -> Dict[str, float]:
    """{"min_seconds", "wrap_seconds", "max_seconds"} from the environment.

    An unusable value falls back to the default and is named once, for the
    same reason MAX_VIDEO_UPLOAD_BYTES does: an import must not break on a typo,
    and a floor that silently became 0 would collect a wave of short encounters.
    """
    out: Dict[str, float] = {}
    for key, env, default in (("min_seconds", "ENCOUNTER_MIN_SECONDS", ENCOUNTER_MIN_SECONDS_DEFAULT),
                              ("wrap_seconds", "ENCOUNTER_WRAP_SECONDS", ENCOUNTER_WRAP_SECONDS_DEFAULT),
                              ("max_seconds", "ENCOUNTER_MAX_SECONDS", ENCOUNTER_MAX_SECONDS_DEFAULT)):
        raw = os.getenv(env, "").strip()
        try:
            val = float(raw) if raw else default
            if val < 0:
                raise ValueError(raw)
        except ValueError:
            print(f"  WARNING: {env}={raw!r} is not a number of seconds; using {default:g}")
            val = default
        out[key] = val
    if out["max_seconds"] < out["min_seconds"]:
        out["max_seconds"] = out["min_seconds"]
    if not (out["min_seconds"] <= out["wrap_seconds"] <= out["max_seconds"]):
        out["wrap_seconds"] = out["max_seconds"]
    return out


# ---------- Participants ----------
#
# A participant record is identity plus, if it comes to that, a withdrawal.
# Consent is taken in Qualtrics before the participant reaches /start
# (2026-09-17); this module records nothing about it, and the voice socket's
# gate (server/app.py, _participant_may_capture) asks only that the record
# exists and is not withdrawn. The consent_* columns stay in the SQLite table
# for existing indexes and are written as 0 / NULL.


class _RequiredEnvDeclarations(Dict[str, str]):
    """This module's own REQUIRED_ENV, and on lookup every other module's.

    The whole payload of the general rule is the one line saying what an
    operator puts in the variable — it is the only thing on the boot warning
    that tells somebody what to do next. server/app.py renders that line with
    `REQUIRED_ENV.get(name, ...)` against THIS mapping, so a variable made
    mandatory anywhere else under server/ printed the fallback "required by this
    server" and the operator learned only that something they had never heard of
    was missing. Iteration and len stay this module's own declarations — a
    caller asking what storage requires must not be handed the whole server's —
    but a lookup answers for whichever module declared the name, because the
    question a lookup asks is always "what do I tell the operator about this
    one".
    """

    def __missing__(self, name: str) -> str:
        why = _declared_required_env().get(name)
        if why is None:
            raise KeyError(name)
        return why

    def get(self, name, default=None):  # type: ignore[override]
        return _declared_required_env().get(name, default)


# Nothing this module reads is mandatory any more: consent, and the version
# string that named its wording, are taken outside the platform (2026-09-17).
# The mapping stays so other modules' declarations still route through it.
REQUIRED_ENV: _RequiredEnvDeclarations = _RequiredEnvDeclarations({})

#: A value that is still somebody's note to themselves rather than an answer.
#: THE UNIVERSAL HALF of the rule: this much is asked of every variable any
#: module makes required, because "[FILL IN: ...]", "changeme" and "xxx" are
#: nobody's port, bucket, header or version. Copied out of .env.example or a
#: tfvars template unedited, a placeholder is worse than the blank it replaced —
#: a blank is refused loudly below, while "[FILL IN: ...]" would be carried
#: into the wave as though it were a value.
_ENV_PLACEHOLDER_PATTERN = r"fill[ _-]?in|TBD|TODO|XXX|placeholder|change[ _-]?me"
_ENV_PLACEHOLDER_RE = re.compile(_ENV_PLACEHOLDER_PATTERN, re.I)

def is_unfilled_placeholder(value: Optional[str]) -> bool:
    """Whether this value is a template marker, for ANY required variable.

    The general rule's whole test, and deliberately a narrow one. A placeholder
    test written for a version string is not a placeholder test for a header, a
    port or a bucket name: promoted unscoped, it reported three correctly set
    variables — "0", "none" and a JSON header block — as missing, which is the
    same total silent void the rule exists to prevent, fired at a deployment
    that was configured right. What survives here is only what cannot be
    anybody's value for anything: "FILL IN", "changeme", "TODO", "xxx".
    """
    s = str(value or "").strip()
    if not s:
        return False
    return bool(_ENV_PLACEHOLDER_RE.search(s))


#: What a module gets told about a variable it declared without an explanation.
#: Only reachable when the declaration is built at runtime rather than written
#: as a literal, which the source scan below cannot read.
_UNDOCUMENTED_REQUIRED = "required by this server"

#: Where the source scan looks. A module attribute so a test can point it at a
#: fixture tree instead of at the real one.
_SERVER_DIR = Path(__file__).resolve().parent

#: Parsed source is stable for the life of a process and the scan is on the
#: /health path, which an ALB polls; keyed by directory so a test pointing
#: _SERVER_DIR elsewhere gets its own answer rather than this one.
_SCANNED_REQUIRED_ENV: Dict[str, Dict[str, str]] = {}


def _scan_required_env(server_dir: Path) -> Dict[str, str]:
    """Every REQUIRED_ENV declared under server/, read WITHOUT importing.

    The half of the general rule that was missing. Reading only sys.modules made
    the runtime check see whatever server.app happens to import at module level
    — and server/runs.py, server/qualtrics.py and server/identity.py are all
    imported inside functions, to break the cycle through this module. So a
    REQUIRED_ENV declared in one of those was found by a test that walks the
    tree and invisible to the boot preflight and /health (which did not), and a
    wave could run with the variable unset and no surface saying so.

    Importing them from here is still not an option — storage is imported BY
    most of them, so it is a cycle, and an env check is no reason to drag the
    realtime voice stack into an offline tool — so the declaration is read the
    way the deployment tests read it, from the source text.
    Keys and values are usually module-level constants rather than literals,
    so plain module-level string assignments in
    the same file are resolved too. Anything this cannot resolve statically is
    left to the sys.modules pass, which sees the real objects.
    """
    found: Dict[str, str] = {}
    for path in sorted(server_dir.rglob("*.py")):
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"))
        except (OSError, SyntaxError, ValueError):
            continue  # not this function's business to report
        constants: Dict[str, str] = {}
        declarations: List[ast.AST] = []
        for node in tree.body:
            targets = (node.targets if isinstance(node, ast.Assign)
                       else [node.target] if isinstance(node, ast.AnnAssign)
                       else [])
            value = getattr(node, "value", None)
            for target in targets:
                if not isinstance(target, ast.Name):
                    continue
                if isinstance(value, ast.Constant) and isinstance(value.value, str):
                    constants[target.id] = value.value
                elif target.id == "REQUIRED_ENV" and value is not None:
                    declarations.append(value)

        def _text(node: Optional[ast.AST]) -> Optional[str]:
            if isinstance(node, ast.Constant) and isinstance(node.value, str):
                return node.value
            if isinstance(node, ast.Name):
                return constants.get(node.id)
            return None

        for declared in declarations:
            # A dict literal, or one wrapped in a call — REQUIRED_ENV in this
            # module is _RequiredEnvDeclarations({...}).
            if isinstance(declared, ast.Call) and declared.args:
                declared = declared.args[0]
            if not isinstance(declared, ast.Dict):
                continue
            for key, why in zip(declared.keys, declared.values):
                name = _text(key)
                if name:
                    found[name] = _text(why) or _UNDOCUMENTED_REQUIRED
    return found


def _declared_required_env() -> Dict[str, str]:
    """Every REQUIRED_ENV declared by a module of this server, merged.

    This module's own dict is not the whole rule. The rule — stated in
    REQUIRED_ENV above and enforced by the source scan — is that ANY module
    under server/ may declare one, so that the next variable
    somebody makes mandatory is caught the day it is made mandatory.

    Two passes, because neither sees everything. The source scan reaches modules
    this process never imported, which is where a lazily imported module's
    declaration lives; the
    sys.modules pass reaches declarations built at runtime and holds the real
    objects, so it wins where both have an answer.
    """
    key = str(_SERVER_DIR)
    if key not in _SCANNED_REQUIRED_ENV:
        _SCANNED_REQUIRED_ENV[key] = _scan_required_env(_SERVER_DIR)
    merged: Dict[str, str] = dict(_SCANNED_REQUIRED_ENV[key])
    for name, module in list(sys.modules.items()):
        if not (name == "server" or name.startswith("server.")):
            continue
        declared = getattr(module, "REQUIRED_ENV", None)
        if isinstance(declared, dict):
            for var, why in dict.items(declared):
                if isinstance(var, str):
                    merged[var] = str(why)
    return merged


class _UnusableVar(str):
    """The name of a required variable, carrying why it cannot be used.

    Its string data is the bare NAME and nothing else: /health publishes this
    list as JSON to anyone, REQUIRED_ENV is keyed by these, and every caller
    that compares or serialises one must keep seeing the name it always saw.
    Only FORMATTING it — which is what the boot warning does — adds the reason,
    so an operator who has set the variable is no longer told it is missing.

    The reason never contains the value. A required variable can hold a token or
    a bucket name, and a boot warning is the last place to print one; naming the
    variable and what is wrong with it is what an operator needs anyway.
    """

    def __new__(cls, name: str, reason: str) -> "_UnusableVar":
        self = super().__new__(cls, name)
        self.name = str(name)
        self.reason = str(reason)
        return self

    def __format__(self, spec: str) -> str:
        if spec:
            return format(self.name, spec)
        return f"{self.name} ({self.reason})"


#: The two states, told apart. Round three's surfaces said "missing" for both,
#: so an operator who had set the variable went looking for a setting that was
#: already there instead of at the value they had typed.
_ENV_NOT_SET = "not set on this task"
_ENV_REJECTED = ("set, but rejected as a placeholder rather than a value; "
                 "the value is not printed here")


def _unusable_required_env_reason(name: str, raw: Optional[str]) -> Optional[str]:
    """Why this process cannot use what it was given for `name`, or None.

    Every variable is held to the universal placeholder test and nothing more:
    a port, a bucket or a JSON header block is not a version string.
    """
    s = str(raw or "").strip()
    if not s:
        return _ENV_NOT_SET
    rejected = is_unfilled_placeholder(s)
    return _ENV_REJECTED if rejected else None


def missing_required_env() -> List[str]:
    """The names in REQUIRED_ENV this process has no usable value for.

    Empty is the only state in which a study arrival can be recorded. Anything
    else is a deployment that will answer /health with 200, accept participants,
    open runs for them and record nothing — so this is what a boot preflight and
    /health should publish, rather than leaving the first evidence of the
    misconfiguration to be an empty dataset at the end of the wave.

    It is both of those now. server.app's run_preflights names what is missing
    beside the gateway and bucket warnings, and /health publishes it under
    `config`. Until that wiring existed this function was dead outside the test
    suite: it described the surfaces that should publish it and nothing did, so
    the one check written to catch the silent void was itself silent.

    Each name carries whether it is unset or set-and-rejected (see _UnusableVar)
    without ceasing to be the name, so the warning an operator reads tells them
    which of the two it is and /health's JSON is unchanged.

    Read per call, so a value that appears after boot is seen without a reload.
    """
    unusable: List[str] = []
    for name in _declared_required_env():
        reason = _unusable_required_env_reason(name, os.environ.get(name))
        if reason:
            unusable.append(_UnusableVar(name, reason))
    return unusable


# ---------- Withdrawal ----------
#
# A WITHDRAWAL IS A STATEMENT ABOUT THE PERSON, SO IT LIVES ON THEIR RECORD.
#
# It used to live only on run documents. runs.withdraw stamped every run under
# the participant key and the capture socket went looking for those stamps, so
# the two routes anybody thought to check were closed — and every OTHER reader
# asks the record, because a record id is what a participant carries. Eight
# reproduced consequences came out of that one omission: the webcam PUT and the
# presigned URL both opened, the camera-absence report wrote into a withdrawn
# person's trail on a host with no S3 credentials at all, the scorer and the
# debriefer spent gateway budget, a bare record mint produced a SECOND record
# carrying no withdrawal and the voice socket opened on it, and an unreadable
# run file made a withdrawal vanish because the scan skipped what it could not
# parse.
#
# They are not eight bugs. They are one bug with eight exits, and the fix is to
# put the fact where every reader already looks. The run-level stamps stay: an
# analyst needs to know WHICH run they stopped in and at which encounter. The
# record answers "did this person stop"; the run answers "where".

def participant_withdrawal(pid: str) -> Optional[Dict[str, Any]]:
    """The withdrawal on this participant record, or None.

    One file read, and the file is the same one get_participant already opens on
    every socket open. An unparseable record answers None here and None from
    get_participant, so the caller sees a record that does not exist rather than
    a live one — which is the closed direction.
    """
    rec = get_participant(pid)
    if not rec:
        return None
    w = rec.get("withdrawn")
    return w if isinstance(w, dict) and w else None


def record_withdrawal(pid: str, stamp: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """Write a withdrawal onto a participant record. Idempotent.

    A second call keeps the first moment, because that is when they actually
    stopped; a report whose timestamp moves every time the button is pressed is
    not a report. Returns the updated record, or None when there is no such
    record to stamp.

    The withdrawal is a statement about the recording that was about to
    happen, and the capture gate (server/app.py, _participant_may_capture) is
    what reads it. Nothing else on the record is rewritten.
    """
    rec = get_participant(pid)
    if rec is None:
        return None
    if rec.get("withdrawn"):
        return rec
    rec["withdrawn"] = dict(stamp)
    init_storage()
    dest = PARTICIPANTS_DIR / f"{pid}.json"
    tmp = PARTICIPANTS_DIR / f"{pid}.json.tmp"
    tmp.write_text(json.dumps(rec, indent=2), encoding="utf-8")
    # Retried for the same reason every other write to this file is: on Windows
    # a reader holding the destination fails the rename outright, and the reader
    # here is the capture gate, on every socket open.
    replace_with_retry(tmp, dest)
    return rec


def _withdrawal_for_code(code: Optional[str]) -> Optional[Dict[str, Any]]:
    """Has the person behind this participant KEY already stopped?

    Asked when a record is minted, which is the moment the hole opened
    through: a bare mint under a stopped participant's key produced a
    brand-new record with no withdrawal on it, and the voice
    socket opened on that record. A record minted for somebody who has stopped
    has to be born carrying that fact, or the stop lasts exactly as long as it
    takes to press Continue again.

    Imported inside the function: server.runs imports this module, so a
    module-scope import would be a cycle. Any failure is None — the caller is
    minting a record either way, and the gate asks again at capture time.
    """
    if not code:
        return None
    try:
        from . import runs

        return runs.participant_withdrawal(code)
    except Exception as e:  # noqa: BLE001, the mint must still happen
        log.warning("could not check whether participant key %r has withdrawn "
                    "(%s: %s); the capture gate asks again at socket open",
                    code, type(e).__name__, e)
        return None


def create_participant(code: str, *, run_id: Optional[str] = None,
                       cohort: Optional[str] = None) -> str:
    """Mint a participant record.

    `run_id`/`cohort` bind the record to the run it was minted for, at the one
    moment that binding is known for certain. They are what stops an encounter's
    cohort being re-derived later: without them the only answer to "which run is
    this record's" was the NEWEST run pointing at it, so a second run adopting
    the record moved every encounter it had already recorded into that run's
    cohort. See server/app.py's _run_context.

    Consent is taken outside this platform (2026-09-17), so the record carries
    identity and withdrawal only. The consent_* columns stay in the SQLite table
    for existing indexes and are written as 0 / NULL.
    """
    init_storage()
    pid = f"p_{int(time.time())}_{os.urandom(3).hex()}"
    rec: Dict[str, Any] = {"id": pid, "code": code, "created_at": time.time()}
    if run_id:
        rec["run_id"] = str(run_id)
    if cohort:
        rec["cohort"] = str(cohort)
    prior = _withdrawal_for_code(code)
    if prior:
        rec["withdrawn"] = dict(prior)
        log.warning(
            "minted participant record %s for a participant key (%r) that has "
            "already withdrawn; the record carries the withdrawal, so the "
            "capture socket refuses it.", pid, code)
    dest = PARTICIPANTS_DIR / f"{pid}.json"
    tmp = PARTICIPANTS_DIR / f"{pid}.json.tmp"
    tmp.write_text(json.dumps(rec, indent=2), encoding="utf-8")
    replace_with_retry(tmp, dest)
    with _db() as conn:
        conn.execute(
            """INSERT INTO participants
               (id, code, consent_given, consent_text_version, created_at)
               VALUES (?, ?, 0, NULL, ?)""",
            (rec["id"], rec["code"], rec["created_at"]),
        )
    return pid


_PID_RE = re.compile(r"p_[0-9]+_[0-9a-f]{6}")


def get_participant(pid: str) -> Optional[Dict[str, Any]]:
    # pid arrives from a websocket query parameter, which may contain '/'.
    # Validate its exact minted shape before touching the filesystem so a
    # traversal string (e.g. "../runs/<run_id>") cannot resolve to another
    # record and slip past the voice-path capture gate.
    if not pid or not _PID_RE.fullmatch(pid):
        return None
    path = PARTICIPANTS_DIR / f"{pid}.json"
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except ValueError:
        return None
