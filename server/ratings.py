"""The rating store: what a human rater said about one encounter, kept forever.

Phase 2 turns recorded encounters into gold labels. A rater is handed a blinded
packet (``rater_packet``), scores the participant on the 22 ESCI Relationship
Management items (``esci``), and submits. This module is where that submission
lands, and it is the only thing standing between "a rater clicked submit" and
"reliability was computed over these numbers".

Two properties the rest of Phase 2 depends on:

* **A submitted rating is immutable.** An amendment appends a new version next
  to the old one; nothing is ever overwritten. A rating that silently changed
  after ICC was computed is a data-integrity failure with no symptom: the
  reliability figure in the paper would describe numbers that no longer exist
  and nobody would ever find out. Versions are cheap; that is not.
* **A rating is complete or it is refused.** Every item is answered, where
  "not enough information to judge" is a real answer stored as null, never as a
  number. Coercing an N/A to 3 would invent agreement out of ignorance, and a
  partial rating quietly averaged over 14 items instead of 22 is the same lie
  with fewer digits. ``esci.validate`` is the arbiter; this module refuses
  anything it complains about, and re-checks completeness itself because that
  particular failure is the one this store exists to prevent.

On-disk layout, following ``storage``: JSON per record under
``DATA_DIR/ratings/{assignment_id}/v{n}.json``, plus a ``ratings`` table in the
existing ``index.db`` for the queries the reliability pass and the export route
need (per encounter, per cohort, latest-version-per-assignment). A version file,
once written, is never opened for writing again — that is what immutability
means here, rather than a promise in a docstring.

The assignment's ``status`` is flipped to ``submitted`` through ``raters``; this
module never writes another module's records.

CLI-free by design: submissions arrive over HTTP (``POST
/api/rater/ratings/{assignment_id}``) or through ``import_qualtrics``.
"""

from __future__ import annotations

import json
import math
import os
import re
import sqlite3
import time
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional

from . import esci, raters
from .storage import (
    DATA_DIR, _add_missing_columns, _db, init_storage, replace_with_retry,
)

RATINGS_DIR = DATA_DIR / "ratings"

SOURCE_CONSOLE = "console"
SOURCE_QUALTRICS = "qualtrics"

# The ESCI items are not ours. The rating instrument's own header says so, and
# the warning has to travel with the items wherever they are shown or exported,
# including into a CSV somebody opens two years from now with no idea where the
# numbers came from. It is stamped on every stored rating and on every ingest
# report for that reason: a notice that lives only in a README is a notice that
# will be separated from the data on the first export.
INSTRUMENT_NOTICE = getattr(
    esci, "NOTICE",
    getattr(
        esci, "INSTRUMENT_NOTICE",
        "ESCI items (Boyatzis, Goleman & Korn Ferry) are a proprietary "
        "instrument, reproduced here for research reference only. Confirm "
        "licensing/permission before fielding.",
    ),
)

# Advisory quality-control thresholds. Flags are recorded, never enforced:
# throwing away a rater's work automatically, on a heuristic, would silently
# bias the very reliability estimate the flags exist to inform. A human decides
# what to do with a flagged rating.
FAST_SECONDS_PER_ANSWERED_ITEM = 3.0

# Assignment ids are minted by raters.assign as "as_<12 hex>". Validate the
# shape before it reaches the filesystem: the id arrives from a query string,
# and a crafted one ("../../sessions/s_x") would otherwise resolve outside
# RATINGS_DIR on the way to reading or writing a rating.
_ASSIGNMENT_ID_RE = re.compile(r"as_[0-9a-f]{12}")

# Free text is stored verbatim, but a rating console is a public-ish endpoint
# and an unbounded string field is a way to fill a disk. Truncation is recorded
# on the record rather than done quietly.
MAX_OPEN_ENDED_CHARS = 20000

_NA_TOKENS = {"", "na", "n/a", "not enough information to judge", "null", "none", "-99"}


class RatingError(Exception):
    """Base for everything this module refuses."""


class UnknownAssignment(RatingError, LookupError):
    """No such assignment, or not this rater's.

    Deliberately one error for both cases. A rater holding a scoped token must
    not be able to probe for assignment ids that exist but belong to someone
    else; the HTTP layer turns this into 404, never 403, so the two are
    indistinguishable from outside.
    """


class InvalidRating(RatingError, ValueError):
    """The submission is not a complete, in-range rating.

    ``problems`` carries the human-readable list from esci.validate (plus this
    module's own completeness check), suitable for showing a rater.
    """

    def __init__(self, problems: List[str]):
        self.problems = list(problems)
        super().__init__("; ".join(self.problems))


# ---------- index ----------

_index_ready = False


def _ensure_index() -> None:
    """Create the ratings table if it is not there yet. Idempotent.

    Same shape as storage.init_storage: CREATE TABLE IF NOT EXISTS plus an
    _add_missing_columns pass, because IF NOT EXISTS leaves an older table with
    fewer columns alone and every INSERT below would then fail on a deployment
    that already has ratings in it.
    """
    global _index_ready
    if _index_ready:
        return
    init_storage()  # sessions/participants tables + DATA_DIR, both idempotent
    RATINGS_DIR.mkdir(parents=True, exist_ok=True)
    with _db() as conn:
        conn.executescript("""
        CREATE TABLE IF NOT EXISTS ratings (
            assignment_id TEXT NOT NULL,
            version INTEGER NOT NULL,
            rating_id TEXT NOT NULL,
            session_id TEXT,
            rater_id TEXT,
            construct TEXT,
            cohort TEXT,
            submitted_at REAL NOT NULL,
            seconds REAL,
            source TEXT,
            item_bank_version TEXT,
            n_answered INTEGER,
            n_na INTEGER,
            path TEXT,
            PRIMARY KEY (assignment_id, version)
        );
        CREATE INDEX IF NOT EXISTS ratings_session ON ratings(session_id);
        CREATE INDEX IF NOT EXISTS ratings_rater ON ratings(rater_id);
        CREATE INDEX IF NOT EXISTS ratings_cohort ON ratings(cohort);
        """)
        _add_missing_columns(conn, "ratings", {
            "cohort": "TEXT",
            "construct": "TEXT",
            "source": "TEXT",
            "item_bank_version": "TEXT",
            "n_answered": "INTEGER",
            "n_na": "INTEGER",
            "path": "TEXT",
        })
    _index_ready = True


def _write_atomic(p: Path, data: str) -> None:
    """Temp file + storage.replace_with_retry, as storage, runs and raters do.

    A kill mid-write must not leave a truncated rating: it would be a JSON file
    that exists, indexes fine, and fails to parse at analysis time — the worst
    of the three possible outcomes.

    The rename retries because a bare os.replace is not atomic on Windows, which
    is a supported researcher platform: it raises PermissionError outright while
    any handle is open on either side, and this directory is read by the
    researcher console and the reliability routes while ratings are being
    submitted into it. This is the one write in the system that costs a human
    being their completed work — a rater has just spent twenty minutes on 22
    ESCI items — so losing it to a millisecond of overlap is not acceptable.
    replace_with_retry re-raises if the window never closes, so a lost write is
    still loud rather than silent.
    """
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_name(p.name + ".tmp")
    tmp.write_text(data, encoding="utf-8")
    replace_with_retry(tmp, p)


# ---------- item bank version ----------

_item_bank_version_cache: Optional[str] = None


def item_bank_version() -> str:
    """A short digest of the item bank this rating was made against.

    The CSV is edited between waves (an item reworded, a reverse flag
    corrected), and a rating carries only item ids, so without a stamp there is
    nothing on a stored rating that says which text the rater actually read.
    Recomputing it later is impossible; recording it costs a hash. Same
    reasoning as storage.spec_fingerprint, applied to the instrument instead of
    the scenario.

    Hashes id/text/reverse in bank order, so a reworded item or a flipped
    reverse flag changes the stamp while a comment in the CSV does not.
    """
    global _item_bank_version_cache
    if _item_bank_version_cache is None:
        import hashlib

        payload = "\n".join(
            f"{it['id']}|{it.get('text', '')}|{bool(it.get('reverse'))}"
            for it in esci.all_items()
        )
        _item_bank_version_cache = hashlib.sha256(payload.encode("utf-8")).hexdigest()[:12]
    return _item_bank_version_cache


# ---------- normalisation ----------

def _coerce_score(raw: Any) -> Any:
    """One answer as the rater meant it: an int 1..5, or None for N/A.

    JSON from the console arrives typed; a Qualtrics export arrives as strings,
    with N/A spelled a dozen ways. Both funnel through here so the store never
    holds "4" next to 4 for the same item, and so an unrecognised value survives
    unchanged into validation rather than being silently rounded into range.
    """
    if raw is None:
        return None
    if isinstance(raw, bool):
        return raw  # not a score; let validation say so rather than int()-ing it
    if isinstance(raw, int):
        return raw
    if isinstance(raw, float):
        return int(raw) if float(raw).is_integer() else raw
    if isinstance(raw, str):
        s = raw.strip()
        if s.lower() in _NA_TOKENS:
            return None
        try:
            return int(s)
        except ValueError:
            try:
                f = float(s)
            except ValueError:
                return raw
            return int(f) if f.is_integer() else f
    return raw


def _coerce_scores(scores: Any) -> Dict[str, Any]:
    """Values normalised; keys left exactly as the caller wrote them.

    Keys are canonicalised only after esci.validate has seen them (see
    _canonical_keys), because validate is the thing that notices the same item
    named twice — "8" and "ESCI-08" in one payload — and collapsing them first
    would turn a contradiction into a silent overwrite.
    """
    if not isinstance(scores, dict):
        raise InvalidRating(["scores must be an object mapping item id to 1-5 or null"])
    return {str(k): _coerce_score(v) for k, v in scores.items()}


def _canonical_keys(scores: Dict[str, Any]) -> Dict[str, Any]:
    """Store every answer under the item's canonical id.

    esci accepts an item by number, id or slug, so a console or a survey can
    legitimately send "8", 8 or "ESCI-08" for the same item. The store keeps one
    spelling: reliability joins ratings by item id across raters, and three
    spellings of item 8 would read as three items with one rater each.
    """
    out: Dict[str, Any] = {}
    for key, value in scores.items():
        found = esci.item(key)
        out[found["id"] if found else key] = value
    return out


def _clean_open_ended(open_ended: Any) -> Dict[str, Any]:
    """The two free-text prompts, plus anything else the caller sent.

    Unknown keys are kept rather than dropped: if a console adds a third prompt
    before this module hears about it, the rater's words should still be in the
    record. Values are stringified and capped, and a cap that bites is recorded
    so an analyst reading a sentence that stops mid-word knows why.
    """
    if open_ended is None:
        open_ended = {}
    if not isinstance(open_ended, dict):
        raise InvalidRating(["open_ended must be an object with 'better' and 'notable'"])
    out: Dict[str, Any] = {"better": "", "notable": ""}
    truncated: List[str] = []
    for k, v in open_ended.items():
        if str(k).startswith("_"):
            # Bookkeeping this function itself added on an earlier pass (the
            # ingest path re-cleans an already-cleaned dict). Copy it through
            # rather than stringifying a list into a sentence.
            out[str(k)] = v
            continue
        text = "" if v is None else str(v)
        if len(text) > MAX_OPEN_ENDED_CHARS:
            text = text[:MAX_OPEN_ENDED_CHARS]
            truncated.append(str(k))
        out[str(k)] = text
    if truncated:
        out["_truncated"] = sorted(truncated)
    return out


def _coerce_seconds(seconds: Any) -> Optional[float]:
    """How long the rater took, or None if nobody measured it.

    float() is generous in a way this store cannot afford: "nan", "NaN", "inf",
    "Infinity" and "1e400" all parse, and a non-finite value then walks straight
    past `val < 0` and through round() into the record. json.dumps writes it as
    the bare token NaN and json.loads reads it back, so nothing on this side
    notices — but the version file, which this module's docstring calls a
    record kept forever, is no longer JSON. jq, R's jsonlite, pandas and a
    browser all refuse it, and GET /api/ratings 500s for the entire export
    because one row cannot be serialised. So the non-finite cases are settled
    here, at the only door they can come through.

    Every non-finite value becomes None, and none of them costs the rater their
    work. They arrive for different reasons — NaN is pandas' missing-value
    marker, and a Qualtrics export is a pandas CSV, so a blank duration column
    comes across as the literal text "nan"; Infinity is a broken clock, a
    "1e400" from a scripted client, a browser subtracting two timestamps the
    wrong way round — but the duration is diagnostic, not data, and the contract
    stated at server/app.py:1933-1937 is that a browser reporting it wrongly
    must not be able to reject a rating a human spent twenty minutes on.
    Refusing Infinity broke exactly that: InvalidRating is a ValueError, app.py
    turns it into a 400, and 22 answered items were discarded over a number
    nobody would have used anyway. So an unusable value becomes None — "we do
    not know how long this took" — and the record says so out loud rather than
    swallowing it: _quality_flags adds "no_timing", and submit() adds
    "bad_timing" when a number was reported that no clock could have produced
    (see _timing_was_unusable). A negative duration is still refused: it is
    finite, so none of the above applies, and app.py already normalises it to
    None before this is reached — the refusal only bites the import path, where
    a row can be rejected, corrected and re-sent.
    """
    if seconds is None or seconds == "":
        return None
    try:
        val = float(seconds)
    except (TypeError, ValueError):
        raise InvalidRating([f"seconds must be a number, got {seconds!r}"])
    if math.isnan(val) or math.isinf(val):
        # Neither is storable: json.dumps writes both as bare tokens no other
        # JSON reader accepts, and the allow_nan=False backstop in
        # _append_version would refuse to write the entire rating.
        return None
    if val < 0:
        raise InvalidRating(["seconds must not be negative"])
    return round(val, 3)


def _timing_was_unusable(seconds: Any) -> bool:
    """True when a duration was reported and no clock could have produced it.

    Kept apart from _coerce_seconds so that function keeps its Optional[float]
    contract, and because the two answers are different facts. A missing
    duration and a duration of Infinity both store as None, but "nobody measured
    this" and "something measured this and reported nonsense" mean different
    things to whoever later asks why a rating has no timing: the second says the
    console or the import was broken at that moment, which is worth knowing
    before the rest of that wave's timings are trusted to spot straight-lining.

    NaN is deliberately not counted here. It is pandas' marker for an empty
    cell, so it really does mean "not measured", and "no_timing" already says
    that.
    """
    if seconds is None or seconds == "":
        return False
    try:
        return math.isinf(float(seconds))
    except (TypeError, ValueError):
        return False


def _completeness_problems(scores: Dict[str, Any], already: str = "") -> List[str]:
    """The check this store exists for, run whatever esci.validate does.

    esci.validate is the arbiter of a valid rating and is called first. This is
    a second pair of eyes on the one failure mode that is invisible downstream:
    a rating missing items still computes an ICC, just over a different set of
    items than the one it is reported against. Cheap to check here, impossible
    to notice later.

    ``already`` is validate's own output. When it has named the same items, this
    stays quiet: the messages go back to a rater looking at the form, and being
    told twice that item 14 is unanswered helps nobody. The check still runs —
    it just does not speak unless it caught something validate did not.
    """
    expected = [it["id"] for it in esci.all_items()]
    have = set(scores)
    missing = [i for i in expected if i not in have]
    unknown = sorted(k for k in have if k not in set(expected))
    problems: List[str] = []
    if missing and not all(i in already for i in missing):
        problems.append(
            f"incomplete rating: {len(missing)} of {len(expected)} items unanswered "
            f"({', '.join(missing[:6])}{'...' if len(missing) > 6 else ''}). "
            "Use null for 'not enough information to judge'."
        )
    if unknown and not all(str(i) in already for i in unknown):
        problems.append(f"not ESCI items: {', '.join(unknown[:6])}")
    return problems


def _quality_flags(scores: Dict[str, Any], seconds: Optional[float]) -> List[str]:
    """Advisory signals for rater quality control. Never a reason to refuse.

    The instrument's administration notes lean on the reverse-scored items
    (11, 15, 24) as straight-lining checks, so an identical raw answer across
    forward and reverse items is exactly the pattern worth flagging.
    """
    answered = [v for v in scores.values() if isinstance(v, int) and not isinstance(v, bool)]
    flags: List[str] = []
    if not answered:
        flags.append("all_na")
    elif len(answered) >= 2 and len(set(answered)) == 1:
        flags.append("straight_lining")
    if seconds is None:
        flags.append("no_timing")
    elif answered and seconds / len(answered) < FAST_SECONDS_PER_ANSWERED_ITEM:
        flags.append("fast")
    na = len(scores) - len(answered)
    if scores and na / len(scores) >= 0.5 and answered:
        flags.append("high_na")
    return flags


def _cohort_for_session(session_id: Optional[str]) -> Optional[str]:
    """The encounter's cohort, snapshotted onto the rating at submit time.

    Read from the sessions index rather than left to a join: cohort is what
    keeps internal test traffic out of the study dataset, and a rating that has
    to re-derive it later is a rating that silently changes cohort if the
    session row is ever rewritten.
    """
    if not session_id:
        return None
    try:
        with _db() as conn:
            row = conn.execute(
                "SELECT cohort FROM sessions WHERE id = ?", (session_id,)
            ).fetchone()
    except sqlite3.Error:
        return None
    return row["cohort"] if row else None


# ---------- assignment status ----------

def _mark_assignment_submitted(assignment_id: str, submitted_at: float) -> bool:
    """Ask raters to flip the assignment to submitted.

    This module must not write the assignment file itself: two writers on one
    record is how a status and its rating end up disagreeing. The timestamp is
    passed across so the two records say the same thing about when this
    happened, rather than differing by however long the write took.

    A failure here is recorded on the returned rating (and left for a repair
    pass) instead of raised: the rating is the irreplaceable artefact and is
    already on disk by this point, whereas the status is derivable — an
    assignment with a rating was submitted, whatever its file says. Losing the
    rating to keep the flag consistent would be the wrong trade.
    """
    fn = getattr(raters, "mark_submitted", None)
    if fn is not None:
        fn(assignment_id, submitted_at)
        return True
    fn = getattr(raters, "set_assignment_status", None)
    if fn is not None:
        fn(assignment_id, "submitted")
        return True
    return False


# ---------- submit ----------

def submit(
    assignment_id: str,
    rater_id: str,
    scores: Dict[str, Any],
    open_ended: Optional[Dict[str, Any]] = None,
    # Any, not Optional[float]: this is whatever the console or a CSV column
    # said, and _coerce_seconds is the thing that decides what it means. The
    # import path deliberately hands the raw value through (see import_qualtrics).
    seconds: Any = None,
    *,
    source: str = SOURCE_CONSOLE,
    submitted_at: Optional[float] = None,
    note: Optional[str] = None,
) -> Dict[str, Any]:
    """Store one rater's complete rating of one encounter.

    Refuses anything esci.validate objects to, anything missing an item, and
    anything whose assignment does not exist or belongs to another rater
    (UnknownAssignment, so the HTTP layer can 404 rather than confirm that some
    other rater's assignment id is real).

    Called again for the same assignment it appends version n+1 and leaves
    version n exactly as it was. The amendment must come from the same rater:
    an assignment is one rater's judgement, and a second person's numbers under
    the first person's id would make the ICC's rater factor a fiction.
    """
    _ensure_index()

    if not assignment_id or not _ASSIGNMENT_ID_RE.fullmatch(str(assignment_id)):
        raise UnknownAssignment(f"no such assignment: {assignment_id!r}")
    assignment = raters.get_assignment(assignment_id)
    if assignment is None:
        raise UnknownAssignment(f"no such assignment: {assignment_id!r}")
    if not rater_id or assignment.get("rater_id") != rater_id:
        # Same error as "does not exist", on purpose. See UnknownAssignment.
        raise UnknownAssignment(f"no such assignment: {assignment_id!r}")

    submitted = _coerce_scores(scores)
    problems = list(esci.validate(submitted) or [])
    clean = _canonical_keys(submitted)
    for extra in _completeness_problems(clean, " ".join(problems)):
        if extra not in problems:
            problems.append(extra)
    if problems:
        raise InvalidRating(problems)

    text = _clean_open_ended(open_ended)
    took = _coerce_seconds(seconds)
    # A duration that arrived and could not be used is not the same fact as no
    # duration at all, and both store as None. Flag the difference here, where
    # the raw value is still in hand — one layer down it is already gone.
    flags = _quality_flags(clean, took)
    if _timing_was_unusable(seconds) and "bad_timing" not in flags:
        flags.append("bad_timing")

    # Reverse scoring is applied once, here, and stored beside the raw answers
    # rather than instead of them. Reliability wants the scored values; a rater
    # dispute, a re-check of the reverse flags, or a re-import wants to know
    # what the human actually clicked. Recomputing raw from scored is only
    # possible while the reverse flags are unchanged, which is exactly the thing
    # item_bank_version exists to doubt.
    values = {item_id: esci.score_value(item_id, raw) for item_id, raw in clean.items()}
    answered = [v for v in clean.values() if isinstance(v, int) and not isinstance(v, bool)]

    session_id = assignment.get("session_id")
    previous = get_rating(assignment_id)
    now = float(submitted_at) if submitted_at is not None else time.time()

    record: Dict[str, Any] = {
        "assignment_id": assignment_id,
        "rater_id": rater_id,
        "session_id": session_id,
        "construct": assignment.get("construct"),
        "cohort": _cohort_for_session(session_id),
        "scores": clean,
        "values": values,
        "open_ended": text,
        "seconds": took,
        "submitted_at": now,
        "source": source,
        "item_bank_version": item_bank_version(),
        "item_count": len(esci.all_items()),
        "n_answered": len(answered),
        "n_na": len(clean) - len(answered),
        "quality_flags": flags,
        "amends": None if previous is None else previous.get("rating_id"),
        "note": note,
        "instrument_notice": INSTRUMENT_NOTICE,
    }

    stored = _append_version(record)
    stored["assignment_status_synced"] = _mark_assignment_submitted(assignment_id, now)
    stored["ok"] = True
    return stored


def _append_version(record: Dict[str, Any]) -> Dict[str, Any]:
    """Reserve the next version number, then write it. Never overwrite.

    The index row is the lock: (assignment_id, version) is the primary key, so
    two concurrent submits for the same assignment cannot both claim version n
    — the loser gets an IntegrityError and retries at n+1 instead of one of them
    vanishing. The file is written only after the reservation succeeds, and the
    reservation is rolled back if the write fails, so the index never points at
    a rating that is not on disk.
    """
    assignment_id = record["assignment_id"]
    version = _latest_version(assignment_id) + 1
    for _ in range(8):
        rating_id = f"rg_{os.urandom(6).hex()}"
        path = RATINGS_DIR / assignment_id / f"v{version}.json"
        if path.exists():
            # A file with no index row: the index was rebuilt or lost. Skip
            # rather than overwrite; the earlier rating stays authoritative.
            version += 1
            continue
        try:
            with _db() as conn:
                conn.execute(
                    """INSERT INTO ratings
                       (assignment_id, version, rating_id, session_id, rater_id,
                        construct, cohort, submitted_at, seconds, source,
                        item_bank_version, n_answered, n_na, path)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (assignment_id, version, rating_id, record.get("session_id"),
                     record.get("rater_id"), record.get("construct"),
                     record.get("cohort"), record["submitted_at"],
                     record.get("seconds"), record.get("source"),
                     record.get("item_bank_version"), record.get("n_answered"),
                     record.get("n_na"), str(path)),
                )
        except sqlite3.IntegrityError:
            version += 1
            continue
        out = dict(record, version=version, rating_id=rating_id)
        try:
            # allow_nan=False is the backstop under _coerce_seconds. Python's
            # json happily writes NaN/Infinity as bare tokens that no other JSON
            # reader accepts, and a version file is never opened for writing
            # again — so a record that is not JSON is not a bug to fix later,
            # it is a permanently unreadable rating. Refusing to write it is the
            # only outcome that stays recoverable. A ValueError from here is a
            # write that failed, so it rolls the reservation back the same way
            # an OSError does rather than leaving an index row pointing at a
            # file that was never created.
            _write_atomic(path, json.dumps(out, indent=2, ensure_ascii=False,
                                           allow_nan=False))
        except (OSError, ValueError):
            with _db() as conn:
                conn.execute(
                    "DELETE FROM ratings WHERE assignment_id = ? AND version = ?",
                    (assignment_id, version),
                )
            raise
        return out
    raise RatingError(f"could not allocate a version for {assignment_id}")


def _latest_version(assignment_id: str) -> int:
    with _db() as conn:
        row = conn.execute(
            "SELECT MAX(version) AS v FROM ratings WHERE assignment_id = ?",
            (assignment_id,),
        ).fetchone()
    indexed = (row["v"] or 0) if row else 0
    # The files are the record; the index is an index. If a rebuilt or deleted
    # index.db has fewer versions than the directory, trusting it would append
    # over a rating that exists on disk.
    on_disk = 0
    d = RATINGS_DIR / assignment_id
    if d.is_dir():
        for f in d.glob("v*.json"):
            try:
                on_disk = max(on_disk, int(f.stem[1:]))
            except ValueError:
                continue
    return max(indexed, on_disk)


# ---------- reads ----------

def _load_version(assignment_id: str, version: int) -> Optional[Dict[str, Any]]:
    path = RATINGS_DIR / assignment_id / f"v{version}.json"
    try:
        rec = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    rec.setdefault("assignment_id", assignment_id)
    rec.setdefault("version", version)
    return rec


def rating_versions(assignment_id: str) -> List[Dict[str, Any]]:
    """Every version ever submitted for this assignment, oldest first.

    The audit view: what was rated, when, and what each amendment changed.
    Reliability uses only the latest (get_rating), but a reviewer asking "was
    this number the same when the ICC was run" needs the whole chain.
    """
    if not assignment_id or not _ASSIGNMENT_ID_RE.fullmatch(str(assignment_id)):
        return []
    _ensure_index()
    latest = _latest_version(assignment_id)
    out = []
    for v in range(1, latest + 1):
        rec = _load_version(assignment_id, v)
        if rec is not None:
            rec["is_current"] = v == latest
            out.append(rec)
    return out


def get_rating(assignment_id: str) -> Optional[Dict[str, Any]]:
    """The current rating for an assignment: its latest version, or None."""
    if not assignment_id or not _ASSIGNMENT_ID_RE.fullmatch(str(assignment_id)):
        return None
    _ensure_index()
    version = _latest_version(assignment_id)
    if version <= 0:
        return None
    rec = _load_version(assignment_id, version)
    if rec is not None:
        rec["is_current"] = True
    return rec


def _current_rows(where: str = "", params: Iterable[Any] = ()) -> List[sqlite3.Row]:
    """Index rows for the current version of each matching assignment."""
    _ensure_index()
    sql = """SELECT r.* FROM ratings r
             JOIN (SELECT assignment_id, MAX(version) AS v
                   FROM ratings GROUP BY assignment_id) m
               ON m.assignment_id = r.assignment_id AND m.v = r.version"""
    if where:
        sql += f" WHERE {where}"
    sql += " ORDER BY r.submitted_at"
    with _db() as conn:
        return list(conn.execute(sql, tuple(params)))


def ratings_for_encounter(session_id: str) -> List[Dict[str, Any]]:
    """Every rater's current rating of one encounter, oldest submission first.

    This is the unit reliability is computed over: k raters on one encounter.
    """
    if not session_id:
        return []
    out = []
    for row in _current_rows("r.session_id = ?", (session_id,)):
        rec = _load_version(row["assignment_id"], row["version"])
        if rec is not None:
            rec["is_current"] = True
            out.append(rec)
    return out


def all_ratings(cohort: Optional[str] = None) -> List[Dict[str, Any]]:
    """Every submitted rating, current versions only, for export and reporting.

    ``cohort`` filters on the *encounter's* cohort as recorded when the rating
    was submitted, so a reliability run over the study data cannot be
    contaminated by ratings of internal test encounters — the same exclusion the
    rest of the platform makes, made in the same place in the pipeline.
    """
    if cohort is None:
        rows = _current_rows()
    else:
        rows = _current_rows("r.cohort = ?", (cohort,))
    out = []
    for row in rows:
        rec = _load_version(row["assignment_id"], row["version"])
        if rec is None:
            continue
        rec["is_current"] = True
        if cohort is not None and rec.get("cohort") != cohort:
            # The file disagrees with the index (hand-edited, or a stale row).
            # The file wins.
            continue
        out.append(rec)
    return out


# ---------- Qualtrics ingest ----------

# Reserved mapping targets: a column can be pointed at one of these instead of
# at an item, so a caller can name their actual Qualtrics columns without this
# module guessing.
_META_TARGETS = {"assignment_id", "rater_id", "rating_code", "seconds", "better", "notable"}

_DEFAULT_META_COLUMNS: Dict[str, str] = {
    "assignment_id": "assignment_id",
    "assignmentid": "assignment_id",
    "assignment": "assignment_id",
    "rater_id": "rater_id",
    "raterid": "rater_id",
    "rater": "rater_id",
    "rating_code": "rating_code",
    "ratingcode": "rating_code",
    "code": "rating_code",
    "seconds": "seconds",
    "duration": "seconds",
    "duration (in seconds)": "seconds",
    # server.qualtrics._flatten's own name for the same field, so a row that
    # came through that module needs no mapping of its own.
    "duration_s": "seconds",
    "better": "better",
    "notable": "notable",
}


def default_mapping() -> Dict[str, str]:
    """Column name -> item id (or reserved metadata target), lowercased keys.

    Defaults to the item *numbers*, because that is what a Qualtrics survey
    built from esci_construct4_items.csv will call its questions: item 8 becomes
    Q8 / 8 / item_8 / ESCI-08 depending on who built the survey, and all four
    spellings mean the same item. A caller with a survey that names things
    differently passes its own mapping and none of this applies.
    """
    out = dict(_DEFAULT_META_COLUMNS)
    for it in esci.all_items():
        item_id = it["id"]
        num = it["number"]
        for name in (str(num), f"q{num}", f"item_{num}", f"item{num}",
                     f"esci_{num}", item_id.lower(), item_id.lower().replace("-", "_")):
            out.setdefault(name, item_id)
    return out


def _flatten_row(row: Dict[str, Any]) -> Dict[str, Any]:
    """One flat dict of column -> value.

    server.qualtrics._flatten nests the answers under "values"; a raw CSV export
    read with csv.DictReader does not. Accept both rather than making the caller
    reshape, and let top-level keys win so an explicitly-set assignment_id is
    not shadowed by an embedded-data field of the same name.
    """
    flat: Dict[str, Any] = {}
    # labels first, values second: Qualtrics carries both the coded answer (4)
    # and its label ("Often") under the same question key, and the coded answer
    # is the one to store. Letting labels win would turn every score into a word
    # and the whole export would arrive as unscorable text.
    for nested_key in ("labels", "values"):
        nested = row.get(nested_key)
        if isinstance(nested, dict):
            flat.update(nested)
    flat.update({k: v for k, v in row.items() if k not in ("values", "labels")})
    return flat


def _resolve_assignment(meta: Dict[str, Any]) -> Dict[str, Any]:
    """Find the real assignment this row is a rating of, or raise.

    A row that cannot be attributed is rejected, never guessed at. An
    unattributable rating is worse than a missing one: it silently adds a rater
    to some encounter's reliability estimate, and there is no later evidence
    that it was ever attached to the wrong thing.

    Two routes. An ``assignment_id`` column is the direct one. Otherwise the
    survey shows the rater the opaque ``rating_code`` from rater_packet (it must
    not show the session id, which would deblind the packet), so the code is
    resolved against that rater's own assignments — which also means a code can
    only ever attribute to a rater who was actually assigned it.
    """
    assignment_id = meta.get("assignment_id")
    rater_id = meta.get("rater_id")
    if assignment_id:
        assignment_id = str(assignment_id).strip()
        if not _ASSIGNMENT_ID_RE.fullmatch(assignment_id):
            raise UnknownAssignment(f"not an assignment id: {assignment_id!r}")
        assignment = raters.get_assignment(assignment_id)
        if assignment is None:
            raise UnknownAssignment(f"no such assignment: {assignment_id!r}")
        if rater_id and assignment.get("rater_id") != str(rater_id).strip():
            raise UnknownAssignment(
                f"assignment {assignment_id} does not belong to rater {rater_id}"
            )
        return assignment

    code = meta.get("rating_code")
    if code and rater_id:
        try:
            from .rater_packet import rating_code
        except ImportError:  # pragma: no cover - rater_packet lands separately
            raise UnknownAssignment(
                "row identifies its assignment by rating_code, but rater_packet "
                "is unavailable to resolve it"
            )
        code = str(code).strip()
        for assignment in raters.assignments_for_rater(str(rater_id).strip()):
            sid = assignment.get("session_id")
            if not sid:
                continue
            try:
                minted = rating_code(sid)
            except ValueError:
                # An assignment pointing at a malformed session id cannot be
                # coded, so it cannot be the row's target. Skip it rather than
                # letting one bad assignment abort the whole import.
                continue
            if minted == code:
                return assignment
        raise UnknownAssignment(
            f"rating code {code!r} matches none of rater {rater_id}'s assignments"
        )

    raise UnknownAssignment(
        "row carries no assignment_id, and no rating_code + rater_id to resolve one"
    )


def _same_rating(previous: Optional[Dict[str, Any]], scores: Dict[str, Any],
                 text: Dict[str, Any], seconds: Optional[float]) -> bool:
    if previous is None:
        return False
    return (
        previous.get("scores") == scores
        and {k: v for k, v in (previous.get("open_ended") or {}).items() if not k.startswith("_")}
        == {k: v for k, v in text.items() if not k.startswith("_")}
        and previous.get("seconds") == seconds
    )


def import_qualtrics(rows: List[Dict[str, Any]],
                     mapping: Optional[Dict[str, str]] = None) -> Dict[str, Any]:
    """Ingest rater scores from a Qualtrics export.

    The working path for Phase 2 is this platform's own rating console, but the
    study may still field the instrument in Qualtrics, and a rating that exists
    only in a survey tool is not a rating this platform can compute reliability
    over. This is the way back in.

    ``rows`` are dicts (csv.DictReader rows, or server.qualtrics._flatten
    output). ``mapping`` is column name -> item id, or -> one of
    assignment_id / rater_id / rating_code / seconds / better / notable;
    it defaults to the item numbers (see default_mapping) and is matched
    case-insensitively.

    Every accepted row goes through ``submit``, so an import is held to exactly
    the same completeness and validity rules as a console submission — an
    import path that could write rows the console would refuse is a hole in the
    dataset, not a convenience.

    Re-importing the same export is a no-op rather than a wall of amendments:
    an identical row for an assignment that already has that rating is counted
    as a duplicate and skipped. A row that genuinely differs *does* append a
    version, because that is a corrected rating and the previous one still
    happened.

    Returns what it took and what it refused, with a reason per rejected row.
    """
    _ensure_index()
    table = {str(k).strip().lower(): v for k, v in (mapping or default_mapping()).items()}
    known_items = {it["id"] for it in esci.all_items()}

    report: Dict[str, Any] = {
        "rows": len(rows or []),
        "accepted": 0,
        "amended": 0,
        "skipped_duplicate": 0,
        "rejected": 0,
        "ratings": [],
        "errors": [],
        "unmapped_columns": [],
        "instrument_notice": INSTRUMENT_NOTICE,
    }
    unmapped: set = set()

    for i, row in enumerate(rows or []):
        if not isinstance(row, dict):
            report["rejected"] += 1
            report["errors"].append({"row": i, "reason": "row is not an object"})
            continue
        flat = _flatten_row(row)
        scores: Dict[str, Any] = {}
        meta: Dict[str, Any] = {}
        for column, value in flat.items():
            target = table.get(str(column).strip().lower())
            if target is None:
                unmapped.add(str(column))
                continue
            if target in known_items:
                scores[target] = value
            elif target in _META_TARGETS:
                meta[target] = value
            else:
                unmapped.add(str(column))

        try:
            assignment = _resolve_assignment(meta)
        except UnknownAssignment as exc:
            report["rejected"] += 1
            report["errors"].append({
                "row": i,
                "assignment_id": meta.get("assignment_id"),
                "rater_id": meta.get("rater_id"),
                "reason": str(exc),
            })
            continue

        rater_id = assignment.get("rater_id")
        text = {"better": meta.get("better") or "", "notable": meta.get("notable") or ""}
        try:
            clean = _coerce_scores(scores)
            cleaned_text = _clean_open_ended(text)
            took = _coerce_seconds(meta.get("seconds"))
        except InvalidRating as exc:
            report["rejected"] += 1
            report["errors"].append({
                "row": i,
                "assignment_id": assignment.get("assignment_id"),
                "reason": "; ".join(exc.problems),
            })
            continue

        previous = get_rating(assignment["assignment_id"])
        if _same_rating(previous, clean, cleaned_text, took):
            report["skipped_duplicate"] += 1
            continue

        try:
            stored = submit(
                # The RAW duration, not `took`. submit re-coerces it to exactly
                # the same value, and passing the raw is what lets it tell an
                # empty duration column apart from an "inf" one: both coerce to
                # None, and only the raw still says which arrived. `took` is
                # kept for the duplicate check above, which compares against a
                # stored (already coerced) value.
                assignment["assignment_id"], rater_id, clean, cleaned_text,
                meta.get("seconds"),
                source=SOURCE_QUALTRICS,
            )
        except RatingError as exc:
            report["rejected"] += 1
            problems = getattr(exc, "problems", None)
            report["errors"].append({
                "row": i,
                "assignment_id": assignment.get("assignment_id"),
                "reason": "; ".join(problems) if problems else str(exc),
            })
            continue

        report["accepted"] += 1
        if stored["version"] > 1:
            report["amended"] += 1
        report["ratings"].append({
            "assignment_id": stored["assignment_id"],
            "rating_id": stored["rating_id"],
            "version": stored["version"],
            "rater_id": stored["rater_id"],
            "session_id": stored["session_id"],
            "n_na": stored["n_na"],
        })

    report["unmapped_columns"] = sorted(unmapped)
    return report
