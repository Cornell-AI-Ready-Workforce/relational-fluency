"""The rater registry: who rates, what they may reach, and which encounters.

Phase 2 turns recorded encounters into gold labels. Two or three independent
raters watch each encounter and score the participant on the 22 ESCI
Relationship Management items, and reliability (ICC, weighted kappa,
Krippendorff's alpha) is computed per construct before anything is modelled.
This module owns the three things that has to sit on: the people, their
credentials, and the allocation of encounters to them.

Three decisions are baked in here, and each of them is a study-design
constraint rather than an implementation preference:

*Raters never hold the session key.* SESSION_KEY opens the whole dataset —
every recorded encounter, every download, the researcher views. A rater is a
crowd worker or a trained coder hired for a wave, and handing them that key to
score forty videos would put the entire study's raw data behind a credential
that travels by email. A rater holds a scoped token instead, which reaches
their own assignments and nothing else.

*Only a hash of the token is stored.* A leaked data directory is a plausible
accident — a copied volume, a shared archive of a wave, a laptop backup. If
the tokens were in it, whoever holds the copy can act as any rater, and worse,
can walk the assignment list to learn which encounters exist. The hash is
one-way, so the copy is inert.

*Assignment is a design, not a shuffle.* See ``assign``.

Nothing in this module puts ESCI item text in front of anybody: the items live
in ``server/esci.py`` and reach a rater through the packet and the console,
which carry the instrument's licensing notice with them. The ESCI is a
proprietary instrument (Boyatzis, Goleman & Korn Ferry), reproduced in this
repository for research reference only, and licensing must be confirmed before
fielding. An assignment's ``construct`` here is a label on the encounter, not
a subset of items: a rater scores all 22 items on every encounter, because the
multitrait-multimethod structure the study is built on needs the off-target
cells too.

Storage follows server/storage.py: one JSON document per record under
DATA_DIR, written atomically, plus rows in the shared SQLite index for the
three queries that cannot be answered by reading one file (token -> rater,
rater -> assignments, encounter -> assignments).
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import os
import random
import re
import secrets
import sqlite3
import time
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Set, Tuple

# _db is the storage module's own connection policy (WAL, a 10 s busy timeout,
# Row factory). Reaching for it rather than opening index.db a second way here
# keeps the two from drifting apart on locking behaviour, which is the kind of
# difference that only shows up as an intermittent "database is locked" under a
# researcher poll and a rater submitting at the same moment.
from .storage import DATA_DIR, SESSIONS_DIR, _add_missing_columns, _db

log = logging.getLogger(__name__)

RATERS_DIR = DATA_DIR / "raters"
ASSIGNMENTS_DIR = DATA_DIR / "rater_assignments"

# crowd: recruited per wave, trained on two practice encounters and calibrated
# against gold ratings before entering the pool. trained: a coder who has done
# a previous wave. expert: the study team. The kind travels with every rating
# because reliability is reported by rater type — a crowd/expert ICC gap is a
# finding about the instrument, not noise to be pooled away.
RATER_KINDS = ("crowd", "trained", "expert")

TOKEN_BYTES = 16  # 128 bits, hex-encoded -> "rt_" + 32 hex characters
DEFAULT_TOKEN_DAYS = 30

_RATER_ID_RE = re.compile(r"rr_[0-9a-f]{12}")
_ASSIGNMENT_ID_RE = re.compile(r"as_[0-9a-f]{12}")
_TOKEN_RE = re.compile(r"rt_[0-9a-f]{32}")
# Minted by session._new_session_id as f"s_{int(time.time())}_{token_hex(3)}".
_SESSION_ID_RE = re.compile(r"s_[0-9]{1,20}_[0-9a-f]{6}")

_STATUS_PENDING = "pending"
_STATUS_SUBMITTED = "submitted"


# ---------- storage plumbing ----------

def init_rater_storage() -> None:
    """Create the rater directories and index tables. Idempotent.

    Called from every public entry point rather than once at import, so a
    process that only reads (a CLI, a test) does not have to remember to
    initialise, and so a data directory that appears after import (a mounted
    volume, a test pointing DATA_DIR somewhere new) still works.
    """
    RATERS_DIR.mkdir(parents=True, exist_ok=True)
    ASSIGNMENTS_DIR.mkdir(parents=True, exist_ok=True)
    with _db() as conn:
        conn.executescript("""
        CREATE TABLE IF NOT EXISTS raters (
            id TEXT PRIMARY KEY,
            name TEXT NOT NULL,
            kind TEXT NOT NULL,
            email TEXT,
            created_at REAL NOT NULL,
            active INTEGER NOT NULL DEFAULT 1
        );
        CREATE TABLE IF NOT EXISTS rater_tokens (
            token_hash TEXT PRIMARY KEY,
            rater_id TEXT NOT NULL,
            issued_at REAL NOT NULL,
            expires_at REAL,
            revoked_at REAL,
            FOREIGN KEY (rater_id) REFERENCES raters(id)
        );
        CREATE INDEX IF NOT EXISTS rater_tokens_rater ON rater_tokens(rater_id);
        CREATE TABLE IF NOT EXISTS rater_assignments (
            assignment_id TEXT PRIMARY KEY,
            session_id TEXT NOT NULL,
            rater_id TEXT NOT NULL,
            construct TEXT,
            cohort TEXT,
            status TEXT NOT NULL,
            assigned_at REAL NOT NULL,
            submitted_at REAL,
            FOREIGN KEY (rater_id) REFERENCES raters(id)
        );
        CREATE INDEX IF NOT EXISTS rater_assignments_rater
            ON rater_assignments(rater_id, status);
        CREATE INDEX IF NOT EXISTS rater_assignments_session
            ON rater_assignments(session_id);
        -- One rater rates one encounter once. Enforced in allocate_plan too,
        -- with a message that says which pair collided; this index is the
        -- backstop for a second writer racing the first, where the in-memory
        -- check cannot see the other process's rows.
        CREATE UNIQUE INDEX IF NOT EXISTS rater_assignments_unique
            ON rater_assignments(session_id, rater_id);
        """)
        # Same reason as storage.init_storage: CREATE TABLE IF NOT EXISTS
        # leaves an older table alone, so a data directory written before a
        # column existed would keep the old shape and every INSERT would fail.
        _add_missing_columns(conn, "rater_assignments",
                             {"cohort": "TEXT", "submitted_at": "REAL"})


def _write_atomic(path: Path, payload: Dict[str, Any]) -> None:
    """Temp file + os.replace, the same as runs._write_atomic.

    A kill mid-write must not leave a truncated rater or assignment document. A
    truncated assignment is worse than a missing one: the index still lists it,
    so the rater's console shows a packet it can never open, and the encounter
    silently ends up one rater short of its complement.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    os.replace(tmp, path)


def _read_json(path: Path) -> Optional[Dict[str, Any]]:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def _now() -> float:
    return time.time()


# ---------- raters ----------

def _public_rater(rec: Dict[str, Any]) -> Dict[str, Any]:
    """The rater as the researcher routes and the console should see them.

    The stored document carries the token digests. They are not secrets — a
    digest cannot be turned back into a token — but they are not useful to any
    caller either, and a hash that travels in a JSON response is a hash that
    ends up pasted into a ticket, so it is projected away. What a caller
    actually wants to know is whether the rater currently has a way in.
    """
    now = _now()
    live = [
        t for t in rec.get("tokens", [])
        if not t.get("revoked_at")
        and (t.get("expires_at") is None or t["expires_at"] > now)
    ]
    return {
        "rater_id": rec["rater_id"],
        "name": rec.get("name"),
        "kind": rec.get("kind"),
        "email": rec.get("email"),
        "created_at": rec.get("created_at"),
        "active": bool(rec.get("active", True)),
        "tokens_active": len(live),
        # The soonest a live token dies, so an operator can see a wave about to
        # lock its raters out before the raters discover it.
        "token_expires_at": min((t["expires_at"] for t in live
                                 if t.get("expires_at") is not None),
                                default=None),
    }


def _rater_path(rater_id: str) -> Path:
    return RATERS_DIR / f"{rater_id}.json"


def _load_rater(rater_id: str) -> Optional[Dict[str, Any]]:
    """The stored document, tokens included. Internal: see _public_rater."""
    # rater_id reaches this module from a URL path segment. Check its exact
    # minted shape before it touches the filesystem, the same way runs.get
    # does, so a traversal string cannot resolve to some other JSON on disk.
    if not rater_id or not _RATER_ID_RE.fullmatch(str(rater_id)):
        return None
    return _read_json(_rater_path(rater_id))


def create_rater(name: str, kind: str = "crowd",
                 email: Optional[str] = None) -> Dict[str, Any]:
    """Register a rater. Returns the public record; no token is issued here.

    Issuing a credential is a separate, explicit step (``issue_token``) because
    a rater is usually registered when the wave is planned and credentialed
    when it starts, and a token that existed for the weeks in between is a
    token that expired unused or leaked in a spreadsheet.
    """
    init_rater_storage()
    name = (name or "").strip()
    if not name:
        # A rater with no name cannot be told apart from another in the
        # reliability report, which is per-rater by construction.
        raise ValueError("rater name is required")
    if kind not in RATER_KINDS:
        raise ValueError(
            f"kind must be one of {', '.join(RATER_KINDS)}, got {kind!r}"
        )
    email = (email or "").strip() or None
    rec = {
        "rater_id": f"rr_{secrets.token_hex(6)}",
        "name": name,
        "kind": kind,
        "email": email,
        "created_at": _now(),
        "active": True,
        "tokens": [],
    }
    _write_atomic(_rater_path(rec["rater_id"]), rec)
    with _db() as conn:
        conn.execute(
            "INSERT INTO raters (id, name, kind, email, created_at, active)"
            " VALUES (?, ?, ?, ?, ?, 1)",
            (rec["rater_id"], name, kind, email, rec["created_at"]),
        )
    return _public_rater(rec)


def get_rater(rater_id: str) -> Optional[Dict[str, Any]]:
    init_rater_storage()
    rec = _load_rater(rater_id)
    return _public_rater(rec) if rec else None


def list_raters() -> List[Dict[str, Any]]:
    """Every registered rater, oldest first.

    Reads the JSON documents rather than the index: the documents are the
    source of truth, and a wave is tens of raters, not thousands.
    """
    init_rater_storage()
    out = []
    for path in sorted(RATERS_DIR.glob("rr_*.json")):
        rec = _read_json(path)
        if rec and rec.get("rater_id"):
            out.append(_public_rater(rec))
    out.sort(key=lambda r: (r.get("created_at") or 0, r["rater_id"]))
    return out


# ---------- tokens ----------

def _token_digest(token: str) -> str:
    """SHA-256 of the token, hex.

    Unsalted and unstretched, deliberately. The threat a password hash defends
    against is a guessable secret: iterate a dictionary, compare. This secret
    is 128 bits from os.urandom, so there is no dictionary and no useful
    search. What a salt would cost is the thing that makes the lookup possible
    at all — with a per-token salt the only way to answer "whose token is
    this?" is to rehash against every row, on every request. One-wayness is
    what is needed here, and plain SHA-256 gives it.
    """
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def issue_token(rater_id: str, days: int = DEFAULT_TOKEN_DAYS) -> str:
    """Mint a scoped token for this rater. Returned once and never recoverable.

    The caller must show it to the operator immediately; nothing on disk can
    reproduce it. If it is lost, issue another and revoke the old one.

    Tokens expire because a rating wave ends. A credential that outlives the
    wave is a live path into study data held by somebody who is no longer
    working on the study, and nobody remembers to clean those up by hand.
    """
    init_rater_storage()
    rec = _load_rater(rater_id)
    if rec is None:
        raise ValueError(f"no such rater: {rater_id!r}")
    try:
        days = int(days)
    except (TypeError, ValueError):
        raise ValueError("days must be a whole number of days") from None
    if days <= 0:
        # A token that is already expired is not a safer token, it is a support
        # ticket. Refuse rather than mint one that cannot be used.
        raise ValueError("days must be positive")

    token = f"rt_{secrets.token_hex(TOKEN_BYTES)}"
    issued_at = _now()
    entry = {
        "token_hash": _token_digest(token),
        "issued_at": issued_at,
        "expires_at": issued_at + days * 86400.0,
        "revoked_at": None,
    }
    rec.setdefault("tokens", []).append(entry)
    _write_atomic(_rater_path(rater_id), rec)
    with _db() as conn:
        conn.execute(
            "INSERT OR REPLACE INTO rater_tokens"
            " (token_hash, rater_id, issued_at, expires_at, revoked_at)"
            " VALUES (?, ?, ?, ?, NULL)",
            (entry["token_hash"], rater_id, issued_at, entry["expires_at"]),
        )
    return token


def rater_for_token(token: Optional[str]) -> Optional[Dict[str, Any]]:
    """The rater this token belongs to, or None.

    None covers every failure the caller must treat identically: not a token,
    unknown, revoked, expired, or a rater record that has since gone. The
    caller's job is to answer 404 for all of them — a rater must not be able to
    tell "wrong token" from "right token, wrong assignment", because that
    difference is how you enumerate the study.

    The index lookup is on the digest, which is not the secret; the digest the
    caller computed is then checked against the stored one with
    compare_digest, so the decision that actually admits a request does not
    turn on a short-circuiting string comparison.
    """
    if not token or not _TOKEN_RE.fullmatch(str(token)):
        return None
    init_rater_storage()
    digest = _token_digest(token)
    with _db() as conn:
        row = conn.execute(
            "SELECT rater_id, token_hash, expires_at, revoked_at"
            " FROM rater_tokens WHERE token_hash = ?",
            (digest,),
        ).fetchone()
    if row is None:
        return None
    if not hmac.compare_digest(str(row["token_hash"]), digest):
        return None
    if row["revoked_at"] is not None:
        return None
    if row["expires_at"] is not None and _now() >= row["expires_at"]:
        return None
    rec = _load_rater(row["rater_id"])
    if rec is None or not rec.get("active", True):
        return None
    return _public_rater(rec)


def revoke_token(token: Optional[str]) -> bool:
    """Kill a token now. True when a live token was revoked.

    False for an unknown, already-revoked or expired token, so a caller can
    report honestly rather than claiming to have revoked something that was
    never there.
    """
    if not token or not _TOKEN_RE.fullmatch(str(token)):
        return False
    init_rater_storage()
    digest = _token_digest(token)
    now = _now()
    with _db() as conn:
        row = conn.execute(
            "SELECT rater_id, revoked_at FROM rater_tokens WHERE token_hash = ?",
            (digest,),
        ).fetchone()
        if row is None or row["revoked_at"] is not None:
            return False
        conn.execute(
            "UPDATE rater_tokens SET revoked_at = ? WHERE token_hash = ?",
            (now, digest),
        )
        rater_id = row["rater_id"]
    # The document is the source of truth, so it has to carry the revocation
    # too; an index rebuilt from the documents must not resurrect the token.
    rec = _load_rater(rater_id)
    if rec:
        for t in rec.get("tokens", []):
            if hmac.compare_digest(str(t.get("token_hash", "")), digest):
                t["revoked_at"] = now
        _write_atomic(_rater_path(rater_id), rec)
    return True


# ---------- encounters ----------

def _session_meta(session_id: str) -> Optional[Dict[str, Any]]:
    """Scenario, construct and cohort for one recorded encounter.

    Read from the manifest, which storage.SessionStore writes at session start
    and rewrites at close, so it exists for an encounter that crashed halfway
    as well as one that finished. Returns None when there is no such
    encounter — assign() treats that as an error rather than a skip, because a
    mistyped session id would otherwise produce an assignment whose packet
    404s and a rater with nothing to do.
    """
    if not session_id or not _SESSION_ID_RE.fullmatch(str(session_id)):
        return None
    manifest = SESSIONS_DIR / session_id / "manifest.json"
    if not manifest.exists():
        return None
    m = _read_json(manifest)
    if m is None:
        return None
    scenario = m.get("scenario")
    construct = None
    try:
        from .scenarios_v3 import load_spec

        construct = load_spec(scenario)["construct"]
    except Exception:  # noqa: BLE001 — legacy demo scenarios have no construct
        construct = None
    return {
        "session_id": session_id,
        "scenario": scenario,
        "construct": construct,
        "cohort": m.get("cohort"),
    }


# ---------- assignments ----------

def _assignment_path(assignment_id: str) -> Path:
    return ASSIGNMENTS_DIR / f"{assignment_id}.json"


def get_assignment(assignment_id: str) -> Optional[Dict[str, Any]]:
    init_rater_storage()
    # Same traversal guard as get_rater: this id arrives as a URL path segment
    # on the rater-facing routes.
    if not assignment_id or not _ASSIGNMENT_ID_RE.fullmatch(str(assignment_id)):
        return None
    return _read_json(_assignment_path(assignment_id))


def assignments_for_rater(rater_id: str,
                          status: Optional[str] = None) -> List[Dict[str, Any]]:
    """This rater's assignments, oldest first. `status` filters pending/submitted.

    The index answers "which assignments does this rater have"; the documents
    answer "what is in them". Going through the index means a rater's console
    does not scan every assignment in the study to find its own three.
    """
    init_rater_storage()
    if not rater_id or not _RATER_ID_RE.fullmatch(str(rater_id)):
        return []
    sql = "SELECT assignment_id FROM rater_assignments WHERE rater_id = ?"
    params: List[Any] = [rater_id]
    if status is not None:
        sql += " AND status = ?"
        params.append(status)
    sql += " ORDER BY assigned_at ASC, assignment_id ASC"
    with _db() as conn:
        ids = [r["assignment_id"] for r in conn.execute(sql, params)]
    out = []
    for aid in ids:
        rec = get_assignment(aid)
        if rec:
            out.append(rec)
    return out


def assignments_for_encounter(session_id: str) -> List[Dict[str, Any]]:
    """Every assignment on one encounter, in allocation order.

    Not part of the module contract the other agents code against, but
    reliability and the researcher views both need "who rated this one", and
    the alternative is each of them re-deriving it from the index.
    """
    init_rater_storage()
    if not session_id or not _SESSION_ID_RE.fullmatch(str(session_id)):
        return []
    with _db() as conn:
        ids = [
            r["assignment_id"]
            for r in conn.execute(
                "SELECT assignment_id FROM rater_assignments WHERE session_id = ?"
                " ORDER BY assigned_at ASC, assignment_id ASC",
                (session_id,),
            )
        ]
    return [rec for rec in (get_assignment(a) for a in ids) if rec]


def list_assignments(cohort: Optional[str] = None,
                     status: Optional[str] = None) -> List[Dict[str, Any]]:
    """Every assignment in the study, for the researcher view."""
    init_rater_storage()
    sql = "SELECT assignment_id FROM rater_assignments WHERE 1=1"
    params: List[Any] = []
    if cohort is not None:
        sql += " AND cohort = ?"
        params.append(cohort)
    if status is not None:
        sql += " AND status = ?"
        params.append(status)
    sql += " ORDER BY assigned_at ASC, assignment_id ASC"
    with _db() as conn:
        ids = [r["assignment_id"] for r in conn.execute(sql, params)]
    return [rec for rec in (get_assignment(a) for a in ids) if rec]


def mark_submitted(assignment_id: str,
                   submitted_at: Optional[float] = None) -> Optional[Dict[str, Any]]:
    """Flip an assignment to submitted. Called by ratings.submit.

    The assignment's status lives here, not in the rating, because "how much of
    this wave is done" is a question about the allocation and the console asks
    it on every poll. Re-submission is allowed (a rater who reopens a packet
    and corrects a score is doing the study a favour) so submitted_at moves,
    but first_submitted_at does not: the elapsed-time analysis wants the first
    pass, not the correction.
    """
    init_rater_storage()
    rec = get_assignment(assignment_id)
    if rec is None:
        return None
    when = _now() if submitted_at is None else float(submitted_at)
    rec["status"] = _STATUS_SUBMITTED
    rec["submitted_at"] = when
    rec.setdefault("first_submitted_at", when)
    _write_atomic(_assignment_path(assignment_id), rec)
    with _db() as conn:
        conn.execute(
            "UPDATE rater_assignments SET status = ?, submitted_at = ?"
            " WHERE assignment_id = ?",
            (_STATUS_SUBMITTED, when, assignment_id),
        )
    return rec


# ---------- allocation ----------

def _components(session_raters: Dict[str, List[str]]) -> List[Set[str]]:
    """Connected components of the rater-overlap graph.

    Two raters are adjacent when they share an encounter, so each encounter
    contributes a clique. Union-find rather than a real graph: the only
    question ever asked is "same component?".
    """
    parent: Dict[str, str] = {}

    def find(x: str) -> str:
        parent.setdefault(x, x)
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    def union(a: str, b: str) -> None:
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[ra] = rb

    for raters in session_raters.values():
        for r in raters:
            find(r)
        for r in raters[1:]:
            union(raters[0], r)

    groups: Dict[str, Set[str]] = {}
    for r in parent:
        groups.setdefault(find(r), set()).add(r)
    # Sorted so the repair below walks components in the same order on every
    # run with the same seed.
    return sorted(groups.values(), key=lambda s: sorted(s)[0])


def allocate_plan(session_ids: Sequence[str], rater_ids: Sequence[str],
                  per_encounter: int = 3, seed: Optional[int] = None,
                  existing: Optional[Dict[str, List[str]]] = None,
                  ) -> Dict[str, List[str]]:
    """Which raters each encounter gets. Pure: touches no disk, returns a plan.

    Split out from ``assign`` so the allocation can be reasoned about and
    tested without a data directory, and so a researcher can preview a wave's
    balance before it is written.

    See ``assign`` for what the allocation guarantees and why.
    """
    sessions = list(dict.fromkeys(session_ids))
    raters = list(dict.fromkeys(rater_ids))
    existing = {sid: list(existing.get(sid, [])) for sid in sessions} if existing \
        else {sid: [] for sid in sessions}

    if per_encounter < 1:
        raise ValueError("per_encounter must be at least 1")
    if not sessions:
        raise ValueError("no encounters to assign")
    if not raters:
        raise ValueError("no raters to assign to")
    if len(raters) < per_encounter:
        # Not a shortfall to work around: the complement is the design. Three
        # raters per encounter drawn from two people is two raters per
        # encounter, and the reliability figure computed from it would be
        # reported as if it came from three.
        raise ValueError(
            f"{len(raters)} rater(s) available but per_encounter={per_encounter}; "
            "every encounter needs that many distinct raters"
        )
    # The other end of the same constraint, and the less obvious one. A wave is
    # N encounters, each an edge joining `per_encounter` raters. A hypergraph of
    # N edges of size k spans at most N*(k-1)+1 vertices while staying
    # connected, so a pool larger than that CANNOT be given a connected overlap
    # graph, however it is dealt: somebody ends up in a block that shares no
    # rater with the rest and whose scores are on their own scale. Three
    # encounters and twenty raters is not a large wave, it is an unmeasurable
    # one, and the operator needs to hear that now rather than discover it when
    # the ICC comes back from a matrix with no common column. The fix is theirs
    # to choose (fewer raters, more encounters, a larger complement), so this
    # refuses instead of quietly benching people.
    max_pool = len(sessions) * (per_encounter - 1) + 1
    if per_encounter >= 2 and len(raters) > max_pool:
        raise ValueError(
            f"{len(raters)} raters for {len(sessions)} encounter(s) at "
            f"per_encounter={per_encounter}: at most {max_pool} raters can "
            "appear in a design whose overlap graph is connected, and "
            "reliability cannot be computed across raters who share no "
            "encounter. Use a smaller pool, more encounters, or a larger "
            "per_encounter."
        )

    # Existing assignments are fixed. They already exist on disk, a rater may
    # already have watched the video, and rewriting them would silently change
    # what a half-finished wave means. They count towards the complement and
    # towards each rater's load, so topping a wave up from two raters to three
    # lands on the raters who have done least.
    #
    # `load` is keyed by every rater standing anywhere in this wave, not just by
    # the caller's pool. A top-up names the raters to add, and `existing` comes
    # from what is already on disk, so the two sets need not overlap at all: ask
    # for a second pass over the same encounters with a fresh pair of raters and
    # the wave contains people `raters` never mentioned. Those people are in the
    # plan, so they are in the overlap graph _repair_connectivity walks, and a
    # `load` that ranged only over the caller's pool made the repair index a
    # rater it did not have — a KeyError, surfacing as a 500 from
    # POST /api/rater-assignments with nothing written and nothing explained.
    # Counting them here also makes the number honest: a rater who already has
    # four encounters is not load 0 just because this call did not name them.
    load: Dict[str, int] = {r: 0 for r in raters}
    for sid, already in existing.items():
        dupes = [r for r in already if already.count(r) > 1]
        if dupes:
            raise ValueError(
                f"encounter {sid} already has a duplicate rater: {sorted(set(dupes))}"
            )
        for r in already:
            load[r] = load.get(r, 0) + 1

    # A seeded shuffle decides who wins ties. Without it the first rater in the
    # caller's list takes every tie and picks up the extra assignments in every
    # wave; with it, the tie order is stable for a given seed, so the wave is
    # reproducible but not systematically unfair to anyone.
    rng = random.Random(seed)
    tie_order = raters[:]
    rng.shuffle(tie_order)
    rank = {r: i for i, r in enumerate(tie_order)}

    plan: Dict[str, List[str]] = {sid: list(existing[sid]) for sid in sessions}
    new: Dict[str, List[str]] = {sid: [] for sid in sessions}
    # Raters already standing somewhere in this wave. An encounter that shares
    # one of them is joined to the rest of the wave; an encounter that shares
    # none of them starts a second island.
    used: Set[str] = {r for members in plan.values() for r in members}

    for sid in sessions:
        need = per_encounter - len(plan[sid])
        if need <= 0:
            used.update(plan[sid])
            continue
        eligible = [r for r in raters if r not in plan[sid]]
        if len(eligible) < need:
            raise ValueError(
                f"encounter {sid} needs {need} more rater(s) but only "
                f"{len(eligible)} of the {len(raters)} given are not already on it"
            )
        # Least loaded first, ties by the shuffled rank. This is the balancing
        # rule: because every encounter takes the currently-lightest raters,
        # load stays within one across the wave.
        eligible.sort(key=lambda r: (load[r], rank[r]))
        picked: List[str] = []
        if per_encounter >= 2 and used and not (set(plan[sid]) & used):
            # ...and this is the connectivity rule, which pulls the other way.
            # Pure "least loaded first" always reaches for someone who has not
            # rated yet, because they have load 0 — so encounter after
            # encounter is filled entirely with fresh raters and the wave comes
            # out as a pile of disjoint islands that no pairwise agreement can
            # span. So one slot is reserved for a rater who already appears
            # elsewhere in this wave. The reserved slot goes to the lightest
            # such rater, which costs at most one unit of imbalance and buys
            # the invariant that matters: every encounter after the first
            # touches the component built so far, so the overlap graph is
            # connected by construction rather than by inspection afterwards.
            anchors = [r for r in eligible if r in used]
            if anchors:
                picked.append(anchors[0])
        for r in eligible:
            if len(picked) >= need:
                break
            if r not in picked:
                picked.append(r)
        for r in picked:
            load[r] += 1
        plan[sid].extend(picked)
        new[sid].extend(picked)
        used.update(plan[sid])

    if per_encounter >= 2:
        # Construction connects a wave allocated from scratch. It cannot
        # connect two blocks that were already on disk facing away from each
        # other, so the graph is still checked, and repaired if it has to be.
        _repair_connectivity(sessions, plan, new, load)

    return plan


def _repair_connectivity(sessions: Sequence[str], plan: Dict[str, List[str]],
                         new: Dict[str, List[str]],
                         load: Dict[str, int]) -> None:
    """Join the components of the overlap graph, one reallocation per split.

    This is the fallback, not the main mechanism: allocate_plan connects a wave
    by construction. What it cannot connect is a wave that arrived split — two
    blocks of assignments already on disk that share no rater, from separate
    earlier calls. Merging those needs a placement to actually move.

    The move: take a movable placement (an encounter e_B and a rater r_b on it
    that this call allocated) in one component, and hand that slot to a rater
    r_a from another component. e_B keeps its other members, so its own
    component does not fall apart, and it now holds r_a, whose other encounters
    are in A — so A and B are one component. Nothing else changes, so the
    component count drops by exactly one per move and the loop terminates.

    An exchange would have been tidier — swap r_a and r_b so neither one's
    workload moves — but it does not reliably merge anything: unless r_a keeps
    a foothold in the component it leaves, the exchange re-partitions the same
    split into a different split and the loop spins (two encounters, four
    raters, two each: swap one and you still have two islands). Correctness
    first, so this moves a slot rather than trading one, and pays for it by
    taking the lightest rater from A and the heaviest movable one from B, which
    keeps the disturbance to one unit of load per merge.

    The move is *verified*, not assumed. Dropping r_b can itself break B in
    two, if r_b was the only rater holding two halves of B together, and then
    the merge and the split cancel out and the loop makes no progress. So each
    candidate is tried and the component count re-counted, cheapest candidate
    first (a rater who appears on no other encounter cannot break anything),
    and only a move that strictly reduces the number of components is kept.
    """
    guard = len(plan) + len(load) + 8  # can only ever need one move per split
    while guard > 0:
        guard -= 1
        comps = _components(plan)
        if len(comps) <= 1:
            return
        comp_of: Dict[str, int] = {}
        for i, comp in enumerate(comps):
            for r in comp:
                comp_of[r] = i
        degree: Dict[str, int] = {}
        for members in plan.values():
            for r in members:
                degree[r] = degree.get(r, 0) + 1

        move: Optional[Tuple[str, str, str]] = None
        for sid in sessions:
            movable = sorted(new.get(sid, ()), key=lambda r: (degree[r], -load[r], r))
            if not movable:
                continue
            here = comp_of[plan[sid][0]]
            # The lightest rater from any other component who is not already on
            # this encounter. Any of them merges the two components; the choice
            # only decides whose workload grows, so it goes to whoever has
            # least. Sorted so a given seed always makes the same move.
            outsiders = sorted(
                (r for r in comp_of if comp_of[r] != here and r not in plan[sid]),
                key=lambda r: (load[r], r),
            )
            if not outsiders:
                continue
            r_in = outsiders[0]
            for r_out in movable:
                trial = dict(plan)
                trial[sid] = [r_in if r == r_out else r for r in plan[sid]]
                if len(_components(trial)) < len(comps):
                    move = (sid, r_out, r_in)
                    break
            if move:
                break
        if move is None:
            raise ValueError(
                "cannot connect the rater-overlap graph: the encounters in "
                f"{len(comps)} separate group(s) cannot be reallocated without "
                "rewriting assignments that already exist. Reliability cannot "
                "be computed across groups that share no rater; re-run the "
                "allocation over the whole wave, or add an encounter that both "
                "groups rate."
            )
        sid, r_out, r_in = move
        plan[sid] = [r_in if r == r_out else r for r in plan[sid]]
        new[sid] = [r_in if r == r_out else r for r in new[sid]]
        load[r_out] -= 1
        load[r_in] = load.get(r_in, 0) + 1
    raise ValueError("could not connect the rater-overlap graph")


def _check_raters_exist(rater_ids: Iterable[str]) -> None:
    """Refuse a wave that would be assigned to somebody who cannot rate it.

    Two conditions, both fatal, both about the same silence: an assignment
    written for a rater who does not exist, or whose record is deactivated, is
    an assignment nobody can ever open — and it looks exactly like a pending one
    on every listing. The encounter then sits unrated for the length of the wave
    with the plan insisting it is covered.

    The mechanism for the inactive half is ``rater_for_token`` (see above), not
    ``issue_token``, which this docstring used to name: issuing is happy to mint
    a token for a deactivated rater, and it is authentication that then refuses
    it. So the credential exists, the assignment exists, and the rater still
    cannot open it. Nothing about that is visible from the plan.

    WHAT WRITES ``active: False``: no function in this module and no route in
    server/app.py — ``create_rater`` hard-codes True and nothing flips it. The
    branch is still live rather than dead code, because the rater's JSON
    document under DATA_DIR/raters is the source of truth (``_load_rater``
    re-reads it every call, ``list_raters`` reads the documents and not the
    index), so an operator editing a record by hand — the only way to bench a
    rater today — reaches it. Do not read it as a promise that a deactivation
    *feature* exists; adding one is a set_rater_active helper plus a researcher
    route, and this check is where it would already be enforced.

    Called twice by ``assign``: once over the ids the caller named, so a typo is
    refused before anything is read, and once over the finished plan, because
    the plan is not a subset of that list. Existing on-disk assignments feed
    ``allocate_plan``'s connectivity repair, which may hand a slot to a rater
    this call never mentioned — and that rater's record is one this check had
    never looked at.
    """
    unknown = []
    inactive = []
    for r in dict.fromkeys(rater_ids):
        rec = _load_rater(r)
        if rec is None:
            unknown.append(r)
        elif not rec.get("active", True):
            inactive.append(r)
    if unknown:
        raise ValueError(f"unknown rater(s): {', '.join(map(str, unknown))}")
    if inactive:
        raise ValueError(
            f"deactivated rater(s): {', '.join(map(str, inactive))}; they cannot "
            "hold a token, so the encounters would show as assigned and never "
            "be rated"
        )


def assign(session_ids: Sequence[str], rater_ids: Sequence[str],
           per_encounter: int = 3, seed: Optional[int] = None,
           ) -> List[Dict[str, Any]]:
    """Allocate encounters to raters and write the assignments.

    Returns the assignments created by this call (an encounter that already had
    its full complement contributes none), oldest-first in session order.

    WHY THIS IS NOT A SHUFFLE
    -------------------------
    The obvious implementation — for each encounter, pick `per_encounter`
    raters at random — satisfies the visible requirement and quietly destroys
    the study. Reliability is a statement about pairs of raters who saw the
    same thing. Deal 27 encounters to 9 raters at random and you can easily get
    two clusters that share nobody: each cluster is internally consistent,
    there is no pairwise comparison that spans them, and the ICC over the whole
    wave is a number computed from a matrix that never had a common column.
    Nothing in the output looks wrong. So the allocation has four properties,
    and each one is load-bearing:

    *Full complement.* Every encounter ends with exactly `per_encounter`
    distinct raters. ICC(2,k) is reported for a fixed k; an encounter rated
    twice in a wave designed for three is not a smaller sample, it is a
    different estimator.

    *Even load.* Encounters are filled in order and each takes the currently
    least-loaded raters, so in a wave allocated in one call no rater's load can
    exceed another's by more than one. (Topping an existing wave up balances
    the new placements against the load already on disk, which is the best that
    can be done without rewriting somebody's half-finished work; the spread can
    stay wider if the earlier call left it wider.) Uneven load is not just
    unfair to the raters: a rater who scores three
    times as many encounters as anyone else dominates every mean, and their
    personal severity becomes the study's.

    *A connected overlap graph.* Draw an edge between two raters who share an
    encounter. That graph must be connected, or the wave splits into blocks
    whose scores cannot be put on a common scale. Balancing alone actively
    works against this — the lightest raters are the ones who have not rated
    yet, so a purely greedy fill deals each encounter a fresh set and produces
    islands — so every encounter after the first reserves one slot for a rater
    who already appears in the wave. That makes connectivity an invariant of
    the construction rather than something to hope for and check. It is checked
    anyway, and repaired, for the one case construction cannot cover: a wave
    that was already split across earlier calls. See _repair_connectivity.

    That repair outranks the caller's pool, and says so. Joining two blocks that
    are already on disk means placing a rater who is in one of them, whether or
    not `rater_ids` named them — so a top-up can create work for people the
    request never mentioned and, in a small wave, leave the named raters with
    none at all. Every assignment therefore carries `unrequested`, true when the
    allocation chose that rater rather than the caller, and a call whose plan
    substituted anyone (or benched a named rater) logs a warning naming both
    sets. The substituted raters are checked for existence and for being active
    before anything is written, which the pre-plan check cannot do because it
    has not seen them.

    There is an upper bound on the pool this can be done for: N encounters of
    k raters span at most N*(k-1)+1 people while staying connected, and a
    larger pool is refused rather than dealt into islands.

    *Reproducibility.* Given the same encounters, raters and `seed`, the
    allocation is identical. A wave that has to be rebuilt — a data directory
    restored, an allocation reviewed a year later in a paper's appendix — must
    come back the same, and a seeded run is the only way to say what was
    actually done rather than what the code would do today.

    With `per_encounter=1` there is nothing to connect: no encounter is rated
    twice, so no pair of raters shares one and no agreement of any kind can be
    computed. That is refused as a design but permitted as a deliberate choice
    only in the sense that the connectivity requirement is skipped — the caller
    gets what they asked for, and this docstring is the warning.

    Raises ValueError, loudly, when the design cannot be honoured: fewer raters
    than the complement, an unknown rater (named or substituted), an unknown
    encounter, a duplicate pairing, or an overlap graph that cannot be
    connected. A rater whose stored record carries ``active: False`` is refused
    on the same path — but nothing in this codebase writes that flag, so read it
    as a guard on the on-disk record rather than as a deactivation feature this
    call protects you from. See _check_raters_exist.
    """
    init_rater_storage()
    sessions = list(dict.fromkeys(session_ids))
    raters = list(dict.fromkeys(rater_ids))
    if not sessions:
        raise ValueError("no encounters to assign")
    if not raters:
        raise ValueError("no raters to assign to")

    # Checked here so a typo is refused before any encounter metadata is read,
    # and checked again over the finished plan below — the plan can contain
    # people this list never mentioned. See _check_raters_exist.
    _check_raters_exist(raters)

    meta: Dict[str, Dict[str, Any]] = {}
    missing = []
    for sid in sessions:
        m = _session_meta(sid)
        if m is None:
            missing.append(sid)
        else:
            meta[sid] = m
    if missing:
        # A mistyped id must not become an assignment: the rater would be shown
        # a packet that 404s, and the encounter it was meant for would go
        # unrated with nothing on record to say so.
        raise ValueError(f"unknown encounter(s): {', '.join(map(str, missing))}")

    existing = {
        sid: [a["rater_id"] for a in assignments_for_encounter(sid)]
        for sid in sessions
    }
    plan = allocate_plan(sessions, raters, per_encounter, seed, existing=existing)

    requested = set(raters)
    # Who this call will actually create work for. Not the same as `rater_ids`:
    # `existing` comes from disk, so the connectivity repair is free to hand a
    # slot to somebody already in the wave — that is the design, and it is what
    # keeps the overlap graph connected — but it means the roster check above
    # ran over the wrong set. A substituted rater has never been checked for
    # existence or for being active by this call, and an assignment written for
    # a rater who cannot hold a token is an encounter that reads as covered on
    # every listing and is never rated. Check them before anything is written.
    # Raters who merely already appear in `existing` are NOT checked: they get
    # no new work here, and a top-up should not be blocked by a stale record it
    # is not adding to.
    substituted = [
        r for r in dict.fromkeys(
            r for sid in sessions for r in plan[sid] if r not in set(existing[sid])
        ) if r not in requested
    ]
    if substituted:
        _check_raters_exist(substituted)

    # The other half of the same surprise: the substitution can leave a rater
    # the researcher explicitly asked for with no work at all (ask for E and F
    # over three encounters that already hold A/B and C/D, and the connected
    # plan is A and C). Silence here would read as "your request was carried
    # out". It is not, so say so — in the log, and on the returned assignments
    # via `unrequested`, which is the only channel this function has back to
    # the researcher.
    idle = [r for r in raters if not any(r in members for members in plan.values())]
    if idle or substituted:
        log.warning(
            "rater assignment: plan substituted %s to keep the overlap graph "
            "connected; requested rater(s) with no encounter in the final plan: %s",
            substituted or "nobody", idle or "none",
        )

    now = _now()
    created: List[Dict[str, Any]] = []
    for sid in sessions:
        already = set(existing[sid])
        for rater_id in plan[sid]:
            if rater_id in already:
                continue
            already.add(rater_id)
            rec = {
                "assignment_id": f"as_{secrets.token_hex(6)}",
                "session_id": sid,
                "rater_id": rater_id,
                # The encounter's target construct, carried so the reliability
                # report can group by it without reopening 27 manifests. It
                # does NOT narrow what the rater scores: every encounter is
                # rated on all 22 items, which is what makes the off-target
                # cells of the multitrait-multimethod matrix estimable.
                "construct": meta[sid]["construct"],
                "scenario": meta[sid]["scenario"],
                # So internal test traffic can be excluded from a reliability
                # report the same way it is excluded from /api/encounters.
                "cohort": meta[sid]["cohort"],
                "status": _STATUS_PENDING,
                "assigned_at": now,
                "submitted_at": None,
                "seed": seed,
                # True when the allocation put this encounter on a rater the
                # request did not name, to keep the overlap graph connected.
                # Written rather than derived: `rater_ids` is not kept anywhere
                # else, so a year later there is no way to reconstruct whether a
                # researcher chose this pairing or the repair did — and that is
                # the difference between "the load was uneven" and "the design
                # was overridden". Always present, on every assignment, so a
                # reader can count them without a KeyError on the ordinary rows.
                "unrequested": rater_id not in requested,
            }
            _write_atomic(_assignment_path(rec["assignment_id"]), rec)
            try:
                with _db() as conn:
                    conn.execute(
                        "INSERT INTO rater_assignments (assignment_id, session_id,"
                        " rater_id, construct, cohort, status, assigned_at,"
                        " submitted_at) VALUES (?, ?, ?, ?, ?, ?, ?, NULL)",
                        (rec["assignment_id"], sid, rater_id, rec["construct"],
                         rec["cohort"], _STATUS_PENDING, now),
                    )
            except sqlite3.IntegrityError as exc:
                # The unique index fired, so another writer put this exact pair
                # in between our read of the existing assignments and this
                # insert. Roll the orphaned document back and say so rather
                # than leaving a document with no index row, which would be
                # invisible to every listing while still occupying the id.
                try:
                    _assignment_path(rec["assignment_id"]).unlink()
                except OSError:
                    pass
                raise ValueError(
                    f"rater {rater_id} is already assigned to encounter {sid}"
                ) from exc
            created.append(rec)
    return created


def coverage(session_ids: Optional[Iterable[str]] = None,
             per_encounter: int = 3) -> Dict[str, Any]:
    """How the wave stands: complement, load spread, and overlap connectivity.

    The researcher view needs an answer to "is this wave rateable yet" that is
    not "read 81 assignment files", and the same three properties assign()
    guarantees at allocation time are the ones worth re-checking afterwards,
    because assignments can also arrive by hand or from a restored backup.
    """
    init_rater_storage()
    if session_ids is None:
        rows = list_assignments()
        sessions = list(dict.fromkeys(a["session_id"] for a in rows))
    else:
        sessions = list(dict.fromkeys(session_ids))
        rows = [a for sid in sessions for a in assignments_for_encounter(sid)]

    by_session: Dict[str, List[str]] = {sid: [] for sid in sessions}
    load: Dict[str, int] = {}
    submitted = 0
    for a in rows:
        by_session.setdefault(a["session_id"], []).append(a["rater_id"])
        load[a["rater_id"]] = load.get(a["rater_id"], 0) + 1
        if a.get("status") == _STATUS_SUBMITTED:
            submitted += 1
    comps = _components(by_session) if by_session else []
    short = sorted(s for s, rs in by_session.items() if len(rs) < per_encounter)
    return {
        "encounters": len(by_session),
        "assignments": len(rows),
        "submitted": submitted,
        "raters": len(load),
        "per_encounter": per_encounter,
        "under_complement": short,
        "load_min": min(load.values()) if load else 0,
        "load_max": max(load.values()) if load else 0,
        "load_by_rater": dict(sorted(load.items())),
        "overlap_components": len(comps),
        "overlap_connected": len(comps) <= 1,
    }
