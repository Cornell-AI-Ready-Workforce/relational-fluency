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
import logging
import os
import re
import sqlite3
import struct
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
        # Validate at the point the directory is minted, not only where the id
        # is later used to find it. Every reader downstream (app._session_dir,
        # rater_packet, video, raters) now insists on this shape, so a store
        # created under any other one would write an encounter that no packet
        # builder, console route or rater assignment could ever address — and it
        # would do so silently, halfway through a paid participant's session.
        if not valid_session_id(session_id):
            raise ValueError(f"bad session_id: {session_id!r}")
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
    # Retry the rename: a consent record is read by get_participant on every
    # socket open, and on Windows a reader holding the destination fails the
    # rename outright — losing the one field an IRB reads.
    replace_with_retry(tmp, dest)
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

    Returns None when there is no such record, and also when the record already
    carries consent — see the guard below. A caller that needs to tell the two
    apart should ask get_participant() first.
    """
    rec = get_participant(pid)
    if rec is None:
        return None
    if rec.get("consent_given") or rec.get("consent_recorded_at"):
        # Consent is not retroactively withdrawable by a later POST. A decline
        # can only follow an un-consented record: the mirror image of the
        # `declined` guard in record_consent below, and for the same reason.
        # Without it a stale second tab (both tabs get &consent=1, so the one
        # you left open still shows Decline after you consented in the other), a
        # back-button resubmit or a replayed request writes consent_given=False
        # over a record that carries consent_recorded_at — and record_consent
        # then refuses to flip it back, so the participant is locked out of the
        # study for good. Worse, the one field an IRB reads would be denying a
        # consent under which audio and webcam were already recorded. Someone
        # who consents and then wants out withdraws the run, which the record
        # already supports; it does not rewrite what they agreed to.
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
    # Retry the rename: a consent record is read by get_participant on every
    # socket open, and on Windows a reader holding the destination fails the
    # rename outright — losing the one field an IRB reads.
    replace_with_retry(tmp, dest)
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
    if rec.get("declined"):
        # A refusal is terminal. Without this a stray or replayed POST could
        # flip a record whose owner had explicitly declined back to consented,
        # and the record would then carry both declined=True and
        # consent_given=True — the worst possible state for the one field an
        # IRB would ask about. Someone who declines and changes their mind
        # starts a new run rather than overwriting the refusal.
        return None
    rec["consent_given"] = True
    rec["consent_text_version"] = consent_version
    rec["consent_recorded_at"] = time.time()
    dest = PARTICIPANTS_DIR / f"{pid}.json"
    tmp = PARTICIPANTS_DIR / f"{pid}.json.tmp"
    tmp.write_text(json.dumps(rec, indent=2), encoding="utf-8")
    # Retry the rename: a consent record is read by get_participant on every
    # socket open, and on Windows a reader holding the destination fails the
    # rename outright — losing the one field an IRB reads.
    replace_with_retry(tmp, dest)
    with _db() as conn:
        conn.execute(
            "UPDATE participants SET consent_given = 1, consent_text_version = ? WHERE id = ?",
            (consent_version, pid),
        )
    return rec
