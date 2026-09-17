"""Routes for Phase 2 human rating: the two credentials, and the blinding.

These tests cover the HTTP layer only — the rating modules themselves
(server.raters, server.ratings, server.rater_packet) are replaced by fakes, so
what is under test is what the routes do with them: which credential opens
which route, what a rater's token can and cannot reach, what leaves in a
response body, and what a malformed body does.

The fakes are injected as `server.*` submodules rather than patched onto real
ones on purpose: the routes import inside the handler, so a fake registered on
the package is what `from . import raters` finds, whether or not the real module
exists yet on this branch.

Nothing here opens a socket. The one S3 call the packet route can make
(video.playback_url) is faked in both directions — a URL, and a raised
exception — because presigning is a local computation and an unreachable bucket
must not take the packet down with it.
"""
from __future__ import annotations

import shutil
import sqlite3
import sys
import types
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from server import app as appmod

REPO_ROOT = Path(__file__).resolve().parent.parent

# The session index the cohort tests read comes from the recorded wave, which
# tests/conftest.py resolves from RF_FIXTURE_DIR / RF_FIXTURE / DATA_DIR or a
# checked-in wave. This module used to name one machine's scratchpad path,
# session UUID and all, with no environment override at all: everywhere else
# the `fakes` fixture quietly fell back to an empty synthesised table and the
# two cohort tests skipped, so three assertions about real data stopped
# existing without a word. `optional_wave` comes from tests/conftest.py and
# never skips: the wave is richer input where a machine has one, not a
# precondition for running these tests at all. Nothing in this module skips for
# want of a wave any more — the fixture synthesises an index with the same
# schema server/storage.py builds, so the cohort routes are exercised
# everywhere and a skip here would mean coverage that genuinely went missing.

RATER_KEY = "test-session-key"
TOKEN_A = "rt_" + "a" * 32
TOKEN_B = "rt_" + "b" * 32


# --- fakes -------------------------------------------------------------------

class FakeRaters:
    """Two raters, two assignments each, one already submitted."""

    def __init__(self):
        self.people = {
            "ra_1": {"rater_id": "ra_1", "name": "Rater A", "kind": "trained",
                     "email": "a@example.edu", "token_hash": "SECRET-HASH",
                     "created_at": "2026-03-11T09:00:00Z"},
            "ra_2": {"rater_id": "ra_2", "name": "Rater B", "kind": "crowd",
                     "email": None, "token_hash": "OTHER-HASH",
                     "created_at": "2026-03-11T09:01:00Z"},
        }
        self.tokens = {TOKEN_A: "ra_1", TOKEN_B: "ra_2"}
        self.assignments = {
            "as_aaaaaaaaaaaa": {
                "assignment_id": "as_aaaaaaaaaaaa", "session_id": "s_1772460300_44c9a2",
                "rater_id": "ra_1", "construct": "conflict_management",
                "status": "pending", "assigned_at": "2026-03-11T09:02:00Z"},
            "as_aaaaaaaaaaab": {
                "assignment_id": "as_aaaaaaaaaaab", "session_id": "s_1772461030_ea4b6f",
                "rater_id": "ra_1", "construct": "teamwork",
                "status": "submitted", "assigned_at": "2026-03-11T09:02:01Z"},
            "as_bbbbbbbbbbbb": {
                "assignment_id": "as_bbbbbbbbbbbb", "session_id": "s_1772461752_60a5de",
                "rater_id": "ra_2", "construct": "inspirational_leadership",
                "status": "pending", "assigned_at": "2026-03-11T09:02:02Z"},
        }
        self.created = []
        self.issued = []
        self.assign_calls = []

    # contract
    def create_rater(self, name, kind="crowd", email=None):
        if kind not in ("crowd", "trained", "expert"):
            raise ValueError(f"unknown rater kind: {kind}")
        rec = {"rater_id": f"ra_{len(self.people) + 1}", "name": name,
               "kind": kind, "email": email}
        self.people[rec["rater_id"]] = rec
        self.created.append(rec)
        return rec

    def get_rater(self, rater_id):
        return self.people.get(rater_id)

    def list_raters(self):
        return list(self.people.values())

    def issue_token(self, rater_id, days=30):
        self.issued.append((rater_id, days))
        return "rt_" + "c" * 32

    def rater_for_token(self, token):
        rid = self.tokens.get(token)
        return self.people.get(rid) if rid else None

    def revoke_token(self, token):
        return self.tokens.pop(token, None) is not None

    def assign(self, session_ids, rater_ids, per_encounter=3, seed=None):
        self.assign_calls.append((list(session_ids), list(rater_ids), per_encounter, seed))
        if per_encounter > len(rater_ids):
            raise ValueError("not enough raters")
        return [
            {"assignment_id": f"as_{i:012x}", "session_id": s,
             "rater_id": rater_ids[i % len(rater_ids)], "construct": "teamwork",
             "status": "pending", "assigned_at": "2026-03-11T09:03:00Z"}
            for i, s in enumerate(session_ids)
        ]

    def assignments_for_rater(self, rater_id, status=None):
        return [a for a in self.assignments.values()
                if a["rater_id"] == rater_id and (status is None or a["status"] == status)]

    def get_assignment(self, assignment_id):
        return self.assignments.get(assignment_id)

    def list_assignments(self, cohort=None, status=None):
        return [a for a in self.assignments.values()
                if (cohort is None or a.get("cohort") == cohort)
                and (status is None or a["status"] == status)]


class FakeRatings:
    """Stands in for server.ratings, including its two error shapes.

    The real InvalidRating subclasses ValueError and the real UnknownAssignment
    subclasses LookupError; the fakes raise the same base classes so the route's
    handling is tested against the contract rather than against an import.
    """

    def __init__(self):
        self.submitted = []
        self.raise_with = None
        self.return_errors = None
        self.version = 1

    def submit(self, assignment_id, rater_id, scores, open_ended, seconds):
        if self.raise_with is not None:
            raise self.raise_with
        if self.return_errors is not None:
            return {"errors": self.return_errors}
        rec = {"assignment_id": assignment_id, "rater_id": rater_id,
               "scores": scores, "open_ended": open_ended, "seconds": seconds,
               "submitted_at": "2026-03-11T10:00:00Z",
               "version": self.version, "amends": None if self.version == 1 else "rt_prev"}
        self.version += 1
        self.submitted.append(rec)
        return rec

    def all_ratings(self, cohort=None):
        return [r for r in self.submitted]


class FakePacket:
    def __init__(self):
        self.built = []
        self.order_seeds = []
        # Shaped like the real one: the media block, including the playback URL
        # and its three states, is built inside rater_packet, not by the route.
        self.packet = {"rating_code": "RC-XXXXXXXXXX",
                       "situation": {"brief": "You are Alex..."},
                       "transcript": [],
                       "media": {"video_url": "https://s3.invalid/v?sig=x",
                                 "video_available": True, "expires_in": 3600,
                                 "note": None}}

    def rating_code(self, session_id):
        return "RC-" + (session_id or "")[-10:].upper()

    def build(self, session_id, *, order_seed=None):
        # order_seed is the assignment id; the route passes it so a rater's item
        # order is their own and is stable across reloads. Recorded here so a
        # test can assert the route actually seeds it rather than dropping it.
        self.built.append(session_id)
        self.order_seeds.append(order_seed)
        if self.packet is None:
            return None
        return dict(self.packet)


class FakeReliability:
    def __init__(self):
        self.calls = []
        self.extra = {}

    def report(self, cohort=None):
        self.calls.append(cohort)
        return {"cohort": cohort, "constructs": {}, "items": {}, **self.extra}


def _module(name, obj):
    """Wrap an instance as a `server.<name>` submodule."""
    mod = types.ModuleType(f"server.{name}")
    for attr in dir(obj):
        if not attr.startswith("_"):
            setattr(mod, attr, getattr(obj, attr))
    return mod


# --- fixtures ----------------------------------------------------------------

@pytest.fixture()
def fakes(monkeypatch, tmp_path, optional_wave):
    """Wire the fakes, the session key, and a throwaway copy of the wave index.

    Module globals are patched rather than environment variables because
    server.app reads SESSION_KEY and ALLOWED_HOSTS once at import, and this
    module may not be the first thing in the suite to import it.
    """
    import server

    raters, ratings, packet, reliability = (
        FakeRaters(), FakeRatings(), FakePacket(), FakeReliability())
    for name, obj in (("raters", raters), ("ratings", ratings),
                      ("rater_packet", packet), ("reliability", reliability)):
        mod = _module(name, obj)
        monkeypatch.setitem(sys.modules, f"server.{name}", mod)
        monkeypatch.setattr(server, name, mod, raising=False)

    # A presigned GET is a local computation, so the fake returns a URL rather
    # than reaching for a bucket. The failing direction is exercised separately.
    from server import video
    monkeypatch.setattr(video, "playback_url",
                        lambda sid, seconds=3600: f"https://s3.invalid/{sid}?sig=x",
                        raising=False)

    monkeypatch.setattr(appmod, "SESSION_KEY", RATER_KEY)
    if appmod.ALLOWED_HOSTS and "testserver" not in appmod.ALLOWED_HOSTS:
        monkeypatch.setattr(appmod, "ALLOWED_HOSTS",
                            list(appmod.ALLOWED_HOSTS) + ["testserver"])

    # The cohort→sessions query runs against a copy, never the wave itself.
    #
    # DB_PATH is repointed BEFORE the schema is built, not after. init_storage()
    # reads the module global at call time, so patching afterwards would create
    # the tables in the real data directory and leave the tmp file empty.
    db = tmp_path / "index.db"
    from server import storage
    monkeypatch.setattr(storage, "DB_PATH", db)

    # The run directory is repointed for the same reason, and it is the less
    # obvious half. The assignment route applies a third filter beyond the two
    # `_cohort_in_index` mirrors below: it joins the cohort against the run
    # files and withholds any encounter no run recorded as completed. RUNS_DIR is resolved from the
    # live DATA_DIR, so left alone that join reads whatever this machine's
    # data/runs happens to hold. On a laptop that has never run a study it is
    # empty and the join stands down; on a researcher's own machine one
    # completed run is enough to make every session in the index below look like
    # an abandoned fragment, and the cohort tests fail for a reason that has
    # nothing to do with the route. An empty tmp directory is the one input that
    # means the same thing on every machine: no evidence either way, assign
    # everything. What the join does with a real fragment is covered where the
    # join lives, not here.
    from server import runs
    monkeypatch.setattr(runs, "RUNS_DIR", tmp_path / "runs")

    fixture_db = (optional_wave / "index.db") if optional_wave else None
    if fixture_db is not None and fixture_db.is_file():
        shutil.copy(fixture_db, db)

    # The schema comes from init_storage(), never from a CREATE TABLE written
    # out here. This fixture used to hand-transcribe a five-column `sessions`
    # stub, and CREATE TABLE IF NOT EXISTS then left that stub alone when the
    # client's startup hook ran init_storage() for real: the statement after it,
    # CREATE INDEX ... ON sessions(participant_id), raised against a table with
    # no such column, so every test in this file that opens a client errored at
    # setup on any machine without a recorded wave — 108 of them, which is the
    # whole researcher and rater API surface and all nine CI matrix cells.
    # Deriving the schema is what stops the stub drifting from server/storage.py
    # the next time a column is added. Run over a copied wave it is also the
    # migration the app itself performs on startup, so an index.db written
    # before run_id/cohort existed gets the same treatment here as in service.
    storage.init_storage()

    if fixture_db is None or not fixture_db.is_file():
        # Synthesised rather than left empty, so that the cohort routes are
        # exercised on a machine with no wave instead of skipping. Three
        # encounters: two in `study` (oldest first is the order the route must
        # return) and one in `internal`, which has to come back as a separate
        # pool. No "unattributed" row, because the empty-cohort test asks for
        # that name and expects a 400.
        conn = sqlite3.connect(db)
        conn.executemany(
            "INSERT INTO sessions (id, participant_id, scenario, model,"
            " started_at, status, n_turns, dir, cohort)"
            " VALUES (?,?,?,?,?,?,?,?,?)",
            [("s_1772460300_44c9a2", "p_1", "S1A", "fake-model", 1772460300.0,
              "closed", 8, "data/sessions/s_1772460300_44c9a2", "study"),
             ("s_1772461030_ea4b6f", "p_2", "S2A", "fake-model", 1772461030.0,
              "closed", 12, "data/sessions/s_1772461030_ea4b6f", "study"),
             ("s_1772461752_60a5de", "p_3", "S1B", "fake-model", 1772461752.0,
              "closed", 5, "data/sessions/s_1772461752_60a5de", "internal")])
        conn.commit()
        conn.close()

    return types.SimpleNamespace(raters=raters, ratings=ratings, packet=packet,
                                 reliability=reliability, db=db)


@pytest.fixture()
def client(fakes):
    with TestClient(appmod.app) as c:
        yield c


# --- the two credentials are disjoint ----------------------------------------

RATER_ROUTES = [
    ("GET", "/api/rater/me"),
    ("GET", "/api/rater/assignments"),
    ("GET", "/api/rater/packet/as_aaaaaaaaaaaa"),
    ("POST", "/api/rater/ratings/as_aaaaaaaaaaaa"),
    ("GET", "/rate"),
]


RESEARCHER_ROUTES = [
    ("GET", "/api/raters"),
    ("GET", "/api/rater-assignments"),
    ("GET", "/api/ratings"),
    ("GET", "/api/reliability"),
    ("POST", "/api/raters"),
    ("POST", "/api/raters/ra_1/token"),
    ("POST", "/api/rater-assignments"),
]


# --- the branch nothing above exercises: no SESSION_KEY at all ---------------
#
# `fakes` pins SESSION_KEY to a value for every test in this file, so every
# assertion above is about the configured branch. check_key is
# `if SESSION_KEY and key != SESSION_KEY`, which means the unconfigured branch
# is not a weaker guard, it is no guard: an empty SESSION_KEY opens the whole
# researcher surface to anyone with the URL, and nothing in a response says so.
# That is deliberate for local development (see the note above check_key), and
# it is the deployment that forgets to set the secret that this pair of tests
# is here to keep in view — a study server with a Gemini key, a bucket and no
# researcher credential looks completely healthy. Refusing that configuration
# is the startup guard's job, not check_key's; these two say exactly what the
# routes do once it has been allowed through, so that if anyone ever decides
# the routes should refuse instead, they change these tests deliberately.


@pytest.fixture()
def keyless_client(fakes, monkeypatch):
    # A local-only allowlist as well as an empty key, because that pair is the
    # configuration this posture is defended for: a development server on a
    # laptop. The startup guard refuses the other pair — no key and a public
    # hostname — and tests/test_phase2_blockers.py holds it to that.
    monkeypatch.setattr(appmod, "SESSION_KEY", "")
    monkeypatch.setattr(appmod, "ALLOWED_HOSTS",
                        ["localhost", "127.0.0.1", "testserver"])
    with TestClient(appmod.app) as c:
        yield c


@pytest.mark.parametrize("method,path", RESEARCHER_ROUTES + [
    ("GET", "/api/encounters"), ("GET", "/api/sessions"), ("GET", "/api/runs"),
    ("GET", "/researcher"), ("GET", "/evidence"),
])
def test_an_unset_session_key_leaves_every_researcher_route_open(
        keyless_client, method, path):
    r = keyless_client.request(method, path)
    assert r.status_code != 401, (
        "check_key is a no-op with no SESSION_KEY configured; if this route "
        "now refuses, the posture changed and the startup guard's reason to "
        "exist changed with it"
    )


# --- a token reaches only its own rater --------------------------------------

# --- the packet ---------------------------------------------------------------

# --- submission ---------------------------------------------------------------

# --- the console page ---------------------------------------------------------

def test_rate_is_404_when_the_console_is_not_installed(client, monkeypatch, tmp_path):
    monkeypatch.setattr(appmod, "STATIC_DIR", tmp_path)
    assert client.get("/rate", params={"token": TOKEN_A}).status_code == 404


# --- researcher: roster and tokens --------------------------------------------

# --- researcher: assignment ----------------------------------------------------

def _cohort_in_index(db, cohort):
    """What the index says a cohort's rateable encounters are, oldest first.

    Read out of the copied index rather than written down as literals. The old
    version asserted `n_sessions == 26` and named two session ids from one
    machine's fixture; that is an assertion about which directory the runner
    was pointed at, not about what the route does, and it turns any other
    recorded wave — a colleague's, the lab's, next term's — into a red suite.
    Deriving the expectation keeps the route under test on every wave.
    """
    conn = sqlite3.connect(db)
    try:
        rows = conn.execute(
            # The same two filters _rateable_sessions applies: an encounter
            # still recording has no record to read, and one where nobody
            # spoke has nothing to score.
            "SELECT id FROM sessions WHERE cohort = ? AND status != 'active'"
            "   AND COALESCE(n_turns, 0) > 0 ORDER BY started_at ASC",
            (cohort,)).fetchall()
    finally:
        conn.close()
    return [r[0] for r in rows]


# --- researcher: export and reliability ---------------------------------------

# --- the fixture itself --------------------------------------------------------
#
# Three assertions about `fakes`, because every test above depends on it and
# nothing above notices when it is wrong. It used to hand-write a five-column
# `sessions` table; CREATE TABLE IF NOT EXISTS left that stub in place when the
# app's startup hook ran init_storage() for real, the CREATE INDEX after it
# named a column the stub did not have, and 108 tests here — the whole
# researcher and rater surface — errored at setup on every machine without a
# recorded wave, all nine CI cells included. Loud, and still easy to read past:
# a wall of identical setup errors reads as one broken environment rather than
# as the API surface going untested, which is how it survived a round of fixes.


def test_the_fixture_index_carries_the_schema_the_app_builds(fakes):
    """The stub is the real schema or it is not a stub, it is a trap.

    Derived, not transcribed: this is the assertion that would have caught the
    five-column table, and it is also the one that catches the next column added
    to server/storage.py without this fixture hearing about it.
    """
    from server import storage

    conn = sqlite3.connect(fakes.db)
    try:
        have = {r[1] for r in conn.execute("PRAGMA table_info(sessions)")}
    finally:
        conn.close()
    for column in ("id", "participant_id", "scenario", "model", "started_at",
                   "status", "n_turns", "dir", "run_id", "cohort"):
        assert column in have, (
            f"the fixture index has no {column!r} column; it was built by hand "
            f"rather than by storage.init_storage() (columns: {sorted(have)})"
        )
    assert storage.DB_PATH == fakes.db


def test_the_startup_hook_finishes_against_the_fixture_index(client, fakes):
    """What the 108 setup errors were, in one assertion.

    The client fixture runs the app's startup hooks, one of which is
    init_storage(). It ran to completion only if the index it could not create
    on a five-column table is there.
    """
    conn = sqlite3.connect(fakes.db)
    try:
        names = {r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'index'")}
    finally:
        conn.close()
    assert "sessions_participant" in names, (
        "init_storage() did not get past CREATE INDEX ... ON "
        "sessions(participant_id) during app startup"
    )


def test_the_fixture_reads_no_state_from_outside_its_tmp_dir(fakes, tmp_path):
    """Both paths the assignment route reads are under tmp_path.

    Not tidiness. A route that reads the developer's live data/ directory gives
    a different answer on every machine, which is how a green suite here and a
    red one on a colleague's laptop both stop meaning anything.
    """
    from server import runs, storage

    assert Path(storage.DB_PATH).is_relative_to(tmp_path)
    assert Path(runs.RUNS_DIR).is_relative_to(tmp_path)
