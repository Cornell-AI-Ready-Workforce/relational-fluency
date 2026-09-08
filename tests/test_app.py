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
FIXTURE_DB = Path(
    "C:/Users/benj9/AppData/Local/Temp/claude/"
    "C--Users-benj9-Downloads-relational-fluency-main--1-/"
    "4b640cd3-9836-4d35-8114-6f2468c17345/scratchpad/fixture/index.db"
)

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
def fakes(monkeypatch, tmp_path):
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

    # The cohort→sessions query runs against a copy, never the fixture itself.
    if FIXTURE_DB.exists():
        db = tmp_path / "index.db"
        shutil.copy(FIXTURE_DB, db)
    else:  # pragma: no cover - the suite still runs without the fixture
        db = tmp_path / "index.db"
        conn = sqlite3.connect(db)
        conn.execute("CREATE TABLE sessions (id TEXT, status TEXT, n_turns INT,"
                     " started_at TEXT, cohort TEXT)")
        conn.commit()
        conn.close()
    from server import storage
    monkeypatch.setattr(storage, "DB_PATH", db)

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


@pytest.mark.parametrize("method,path", RATER_ROUTES)
def test_rater_routes_refuse_a_missing_token(client, method, path):
    assert client.request(method, path).status_code == 401


@pytest.mark.parametrize("method,path", RATER_ROUTES)
def test_rater_routes_refuse_an_unknown_token(client, method, path):
    r = client.request(method, path, params={"token": "rt_" + "0" * 32})
    assert r.status_code == 401


@pytest.mark.parametrize("method,path", RATER_ROUTES)
def test_the_session_key_does_not_open_a_rater_route(client, method, path):
    """SESSION_KEY is the researcher credential. Independent ratings need one
    identifiable author each, so it must not be usable to act as a rater."""
    r = client.request(method, path, params={"token": RATER_KEY})
    assert r.status_code == 401
    r = client.request(method, path, params={"key": RATER_KEY})
    assert r.status_code == 401


RESEARCHER_ROUTES = [
    ("GET", "/api/raters"),
    ("GET", "/api/rater-assignments"),
    ("GET", "/api/ratings"),
    ("GET", "/api/reliability"),
    ("POST", "/api/raters"),
    ("POST", "/api/raters/ra_1/token"),
    ("POST", "/api/rater-assignments"),
]


@pytest.mark.parametrize("method,path", RESEARCHER_ROUTES)
def test_a_rater_token_does_not_open_a_researcher_route(client, method, path):
    assert client.request(method, path, params={"key": TOKEN_A}).status_code == 401
    assert client.request(method, path, params={"token": TOKEN_A}).status_code == 401
    assert client.request(method, path).status_code == 401


@pytest.mark.parametrize("path", ["/api/encounters", "/api/sessions", "/api/runs",
                                  "/researcher", "/evidence"])
def test_a_rater_token_does_not_open_the_existing_researcher_routes(client, path):
    """The token is new; the routes it must not reach are mostly old ones."""
    assert client.get(path, params={"key": TOKEN_A}).status_code == 401


def test_check_key_itself_rejects_a_rater_token(monkeypatch):
    from fastapi import HTTPException

    monkeypatch.setattr(appmod, "SESSION_KEY", RATER_KEY)

    with pytest.raises(HTTPException):
        appmod.check_key(TOKEN_A)


# --- a token reaches only its own rater --------------------------------------

def test_me_reports_only_this_raters_pending_work(client):
    r = client.get("/api/rater/me", params={"token": TOKEN_A})
    assert r.status_code == 200
    body = r.json()
    assert body["rater_id"] == "ra_1"
    assert body["name"] == "Rater A"
    assert body["kind"] == "trained"
    assert body["assignments_pending"] == 1  # the other one is submitted
    assert "ESCI" in body["notice"]


def test_assignment_list_is_scoped_and_blinded(client):
    r = client.get("/api/rater/assignments", params={"token": TOKEN_A})
    assert r.status_code == 200
    rows = r.json()
    assert {row["assignment_id"] for row in rows} == {"as_aaaaaaaaaaaa", "as_aaaaaaaaaaab"}
    for row in rows:
        # Exactly the four contract fields. A session id would deanonymise the
        # encounter; the construct would tell a blinded rater which competency
        # the scenario was built to elicit.
        assert set(row) == {"assignment_id", "rating_code", "status", "assigned_at"}
        assert row["rating_code"].startswith("RC-")


def test_assignment_list_can_be_filtered_by_status(client):
    rows = client.get("/api/rater/assignments",
                      params={"token": TOKEN_A, "status": "pending"}).json()
    assert [row["assignment_id"] for row in rows] == ["as_aaaaaaaaaaaa"]


def test_another_raters_assignment_is_404_not_403(client):
    """A 403 would confirm the assignment exists, and a rater who can tell
    'not yours' from 'not there' can count the wave."""
    mine = client.get("/api/rater/packet/as_aaaaaaaaaaaa", params={"token": TOKEN_A})
    theirs = client.get("/api/rater/packet/as_bbbbbbbbbbbb", params={"token": TOKEN_A})
    nowhere = client.get("/api/rater/packet/as_zzzzzzzzzzzz", params={"token": TOKEN_A})
    assert mine.status_code == 200
    assert theirs.status_code == 404
    assert nowhere.status_code == 404
    # Indistinguishable in the body as well as the status line.
    assert theirs.json() == nowhere.json()


def test_a_rater_cannot_submit_against_another_raters_assignment(client, fakes):
    r = client.post("/api/rater/ratings/as_bbbbbbbbbbbb", params={"token": TOKEN_A},
                    json={"scores": {"ESCI-08": 4}, "open_ended": {}, "seconds": 60})
    assert r.status_code == 404
    assert fakes.ratings.submitted == []


def test_the_packet_route_never_builds_another_raters_encounter(client, fakes):
    client.get("/api/rater/packet/as_bbbbbbbbbbbb", params={"token": TOKEN_A})
    assert fakes.packet.built == []


# --- the packet ---------------------------------------------------------------

def test_the_packet_is_served_as_the_packet_builder_made_it(client, fakes):
    body = client.get("/api/rater/packet/as_aaaaaaaaaaaa",
                      params={"token": TOKEN_A}).json()
    assert fakes.packet.built == ["s_1772460300_44c9a2"]
    # The media block, including the playback URL and the three states it keeps
    # apart, belongs to rater_packet. The route adds the two assignment fields
    # the packet has no way to know, and nothing else.
    assert body["media"] == fakes.packet.packet["media"]
    assert body["assignment_id"] == "as_aaaaaaaaaaaa"
    assert body["status"] == "pending"


def test_the_route_does_not_mint_a_second_playback_url(client, monkeypatch):
    """Signing the same object twice per read would flatten the packet's
    'has a video but could not sign it' state into 'has no video'."""
    from server import video

    def boom(*a, **k):  # pragma: no cover - the point is that it is never called
        raise AssertionError("app.py signed a playback URL of its own")

    monkeypatch.setattr(video, "playback_url", boom, raising=False)
    r = client.get("/api/rater/packet/as_aaaaaaaaaaaa", params={"token": TOKEN_A})
    assert r.status_code == 200


def test_a_packet_without_a_notice_gets_one(client, fakes):
    body = client.get("/api/rater/packet/as_aaaaaaaaaaaa",
                      params={"token": TOKEN_A}).json()
    assert "proprietary" in body["notice"]


def test_the_packet_builders_own_notice_is_not_duplicated(client, fakes):
    """Two licensing notices reading slightly differently is how a reader learns
    to skip both."""
    fakes.packet.packet = dict(fakes.packet.packet,
                               instrument_notice="ESCI items, licensed under X")
    body = client.get("/api/rater/packet/as_aaaaaaaaaaaa",
                      params={"token": TOKEN_A}).json()
    assert body["instrument_notice"] == "ESCI items, licensed under X"
    assert "notice" not in body


def test_a_packet_for_a_vanished_encounter_is_404(client, fakes):
    """rater_packet.build answers {} for an encounter it cannot reach, and that
    has to be the same 404 as an assignment that is not yours."""
    fakes.packet.packet = {}
    theirs = client.get("/api/rater/packet/as_bbbbbbbbbbbb", params={"token": TOKEN_A})
    r = client.get("/api/rater/packet/as_aaaaaaaaaaaa", params={"token": TOKEN_A})
    assert r.status_code == 404
    assert r.json() == theirs.json()


# --- submission ---------------------------------------------------------------

def test_a_submission_is_recorded_under_the_token_holder(client, fakes):
    r = client.post("/api/rater/ratings/as_aaaaaaaaaaaa", params={"token": TOKEN_A},
                    json={"scores": {"ESCI-08": 4, "ESCI-15": None},
                          "open_ended": {"better": "b", "notable": "n"},
                          "seconds": 812.5})
    assert r.status_code == 200
    assert r.json() == {"ok": True, "submitted_at": "2026-03-11T10:00:00Z",
                        "version": 1, "amends": None}
    rec = fakes.ratings.submitted[0]
    assert rec["rater_id"] == "ra_1"          # from the token, never from the body
    assert rec["assignment_id"] == "as_aaaaaaaaaaaa"
    assert rec["scores"] == {"ESCI-08": 4, "ESCI-15": None}
    assert rec["seconds"] == 812.5


def test_a_null_score_survives_as_null(client, fakes):
    """'Not enough information to judge' is stored as null, never as a number:
    it is excluded pairwise at analysis rather than averaged in as a 3."""
    client.post("/api/rater/ratings/as_aaaaaaaaaaaa", params={"token": TOKEN_A},
                json={"scores": {"ESCI-03": None}, "open_ended": {}, "seconds": 1})
    assert fakes.ratings.submitted[0]["scores"]["ESCI-03"] is None


@pytest.mark.parametrize("body", [
    {},                                            # no scores at all
    {"scores": [], "open_ended": {}},              # a list, not an object
    {"scores": "ESCI-08=4"},                       # a string
    {"scores": {"ESCI-08": 4}, "open_ended": ["x"]},  # open_ended is not an object
])
def test_a_malformed_body_is_400(client, fakes, body):
    r = client.post("/api/rater/ratings/as_aaaaaaaaaaaa",
                    params={"token": TOKEN_A}, json=body)
    assert r.status_code == 400
    assert fakes.ratings.submitted == []


def test_a_body_that_is_not_json_is_400(client, fakes):
    r = client.post("/api/rater/ratings/as_aaaaaaaaaaaa", params={"token": TOKEN_A},
                    content=b"not json", headers={"content-type": "application/json"})
    assert r.status_code == 400
    assert fakes.ratings.submitted == []


@pytest.mark.parametrize("seconds", ["oops", -5, None, {"a": 1}])
def test_a_bad_clock_does_not_lose_the_ratings(client, fakes, seconds):
    """seconds is diagnostic, not data. A browser reporting it wrongly must not
    be able to reject twenty minutes of a rater's work — and the value it could
    not read becomes None, not 0: nobody measured 0, and 0 reads as
    straight-lining to the quality flags downstream."""
    r = client.post("/api/rater/ratings/as_aaaaaaaaaaaa", params={"token": TOKEN_A},
                    json={"scores": {"ESCI-08": 4}, "open_ended": {},
                          "seconds": seconds})
    assert r.status_code == 200
    assert fakes.ratings.submitted[0]["seconds"] is None


def test_a_rejected_score_reaches_the_rater_as_text(client, fakes):
    fakes.ratings.raise_with = ValueError("ESCI-08: 7 is outside 1..5")
    r = client.post("/api/rater/ratings/as_aaaaaaaaaaaa", params={"token": TOKEN_A},
                    json={"scores": {"ESCI-08": 7}, "open_ended": {}, "seconds": 5})
    assert r.status_code == 400
    assert "outside 1..5" in r.json()["detail"]


def test_errors_returned_rather_than_raised_are_also_400(client, fakes):
    fakes.ratings.return_errors = ["ESCI-08 is missing", "ESCI-14 is missing"]
    r = client.post("/api/rater/ratings/as_aaaaaaaaaaaa", params={"token": TOKEN_A},
                    json={"scores": {}, "open_ended": {}, "seconds": 5})
    assert r.status_code == 400
    assert "ESCI-14 is missing" in r.json()["detail"]


def test_an_unknown_assignment_from_the_ratings_module_is_404(client, fakes):
    """ratings.submit re-checks ownership and raises LookupError for both
    'missing' and 'not yours'. Reachable here only in a race, and it must not
    surface as a 500 that says which."""
    fakes.ratings.raise_with = LookupError("no such assignment: 'as_...'")
    r = client.post("/api/rater/ratings/as_aaaaaaaaaaaa", params={"token": TOKEN_A},
                    json={"scores": {"ESCI-08": 4}, "open_ended": {}, "seconds": 5})
    assert r.status_code == 404
    assert r.json()["detail"] == "no such assignment"


def test_resubmitting_is_an_amendment_and_the_version_comes_back(client, fakes):
    """A rater who spots a mis-click can correct it: ratings.submit appends a
    version and leaves the original byte-identical. Refusing the second POST
    here would leave the wrong numbers in the ICC."""
    body = {"scores": {"ESCI-08": 4}, "open_ended": {}, "seconds": 5}
    first = client.post("/api/rater/ratings/as_aaaaaaaaaaaa",
                        params={"token": TOKEN_A}, json=body)
    second = client.post("/api/rater/ratings/as_aaaaaaaaaaaa",
                         params={"token": TOKEN_A},
                         json={**body, "scores": {"ESCI-08": 2}})
    assert first.json()["version"] == 1 and first.json()["amends"] is None
    assert second.json()["version"] == 2 and second.json()["amends"] == "rt_prev"
    assert len(fakes.ratings.submitted) == 2


def test_an_assignment_already_marked_submitted_still_accepts_an_amendment(client,
                                                                           fakes):
    r = client.post("/api/rater/ratings/as_aaaaaaaaaaab", params={"token": TOKEN_A},
                    json={"scores": {"ESCI-08": 4}, "open_ended": {}, "seconds": 5})
    assert r.status_code == 200
    assert fakes.ratings.submitted[0]["assignment_id"] == "as_aaaaaaaaaaab"


# --- the console page ---------------------------------------------------------

def test_rate_serves_the_console_to_a_valid_token(client, monkeypatch, tmp_path):
    monkeypatch.setattr(appmod, "STATIC_DIR", tmp_path)
    (tmp_path / "rater.html").write_text("<h1>rating console</h1>", encoding="utf-8")
    r = client.get("/rate", params={"token": TOKEN_A})
    assert r.status_code == 200
    assert "rating console" in r.text
    # Participant- and rater-facing HTML must never be cached: a stale build is
    # invisible to the person using it.
    assert "no-store" in r.headers.get("cache-control", "")


def test_rate_is_404_when_the_console_is_not_installed(client, monkeypatch, tmp_path):
    monkeypatch.setattr(appmod, "STATIC_DIR", tmp_path)
    assert client.get("/rate", params={"token": TOKEN_A}).status_code == 404


# --- researcher: roster and tokens --------------------------------------------

def test_creating_a_rater(client, fakes):
    r = client.post("/api/raters", params={"key": RATER_KEY},
                    json={"name": "Rater C", "kind": "expert", "email": "c@x.edu"})
    assert r.status_code == 200
    assert r.json()["name"] == "Rater C"
    assert fakes.raters.created[0]["kind"] == "expert"


@pytest.mark.parametrize("body", [{}, {"name": "   "}, {"kind": "expert"}])
def test_a_rater_needs_a_name(client, body):
    r = client.post("/api/raters", params={"key": RATER_KEY}, json=body)
    assert r.status_code == 400


def test_an_unknown_rater_kind_is_400_not_500(client):
    r = client.post("/api/raters", params={"key": RATER_KEY},
                    json={"name": "X", "kind": "volunteer"})
    assert r.status_code == 400


def test_the_roster_does_not_carry_credential_material(client):
    """A stored hash cannot be turned back into a token, but this listing is the
    kind of thing that gets pasted into a shared spreadsheet."""
    rows = client.get("/api/raters", params={"key": RATER_KEY}).json()
    assert len(rows) == 2
    for row in rows:
        assert "token_hash" not in row
        assert not any("HASH" in str(v) for v in row.values())
    assert {row["rater_id"] for row in rows} == {"ra_1", "ra_2"}


def test_issuing_a_token(client, fakes):
    r = client.post("/api/raters/ra_1/token", params={"key": RATER_KEY}, json={"days": 14})
    assert r.status_code == 200
    assert r.json()["token"].startswith("rt_")
    assert fakes.raters.issued == [("ra_1", 14)]


def test_issuing_a_token_defaults_to_thirty_days(client, fakes):
    client.post("/api/raters/ra_1/token", params={"key": RATER_KEY})
    assert fakes.raters.issued == [("ra_1", 30)]


def test_a_token_cannot_be_issued_for_an_unknown_rater(client, fakes):
    r = client.post("/api/raters/ra_nope/token", params={"key": RATER_KEY}, json={})
    assert r.status_code == 404
    assert fakes.raters.issued == []


@pytest.mark.parametrize("days", [0, -1, 366, 100000, "forever", None])
def test_an_out_of_range_expiry_is_refused(client, fakes, days):
    """An unbounded expiry is a standing credential to participant video held by
    somebody outside the study team."""
    r = client.post("/api/raters/ra_1/token", params={"key": RATER_KEY},
                    json={"days": days})
    assert r.status_code == 400
    assert fakes.raters.issued == []


# --- researcher: assignment ----------------------------------------------------

def test_assigning_named_sessions(client, fakes):
    r = client.post("/api/rater-assignments", params={"key": RATER_KEY},
                    json={"session_ids": ["s_1", "s_2"], "rater_ids": ["ra_1", "ra_2"],
                          "per_encounter": 2, "seed": 7})
    assert r.status_code == 200
    body = r.json()
    assert body["created"] == 2
    assert body["n_sessions"] == 2 and body["n_raters"] == 2
    assert fakes.raters.assign_calls == [(["s_1", "s_2"], ["ra_1", "ra_2"], 2, 7)]


def test_assigning_a_whole_cohort_off_the_session_index(client, fakes):
    """The end-of-wave move: name the cohort, not four hundred session ids."""
    if not FIXTURE_DB.exists():  # pragma: no cover
        pytest.skip("fixture index.db not present")
    r = client.post("/api/rater-assignments", params={"key": RATER_KEY},
                    json={"cohort": "study", "rater_ids": ["ra_1", "ra_2", "ra_3"],
                          "per_encounter": 3, "seed": 1})
    assert r.status_code == 200
    assert r.json()["n_sessions"] == 26          # 26 study encounters in the wave
    sids = fakes.raters.assign_calls[0][0]
    assert sids[0] == "s_1772460300_44c9a2"      # oldest first
    assert "s_1773142745_384dad" not in sids     # the internal test encounter


def test_the_internal_cohort_is_a_separate_pool(client, fakes):
    if not FIXTURE_DB.exists():  # pragma: no cover
        pytest.skip("fixture index.db not present")
    r = client.post("/api/rater-assignments", params={"key": RATER_KEY},
                    json={"cohort": "internal", "rater_ids": ["ra_1"],
                          "per_encounter": 1})
    assert r.json()["n_sessions"] == 1


def test_an_empty_cohort_is_refused_rather_than_silently_assigning_nothing(client):
    r = client.post("/api/rater-assignments", params={"key": RATER_KEY},
                    json={"cohort": "unattributed", "rater_ids": ["ra_1"]})
    assert r.status_code == 400
    assert "no encounters" in r.json()["detail"]


def test_a_session_index_without_the_cohort_column_yields_nothing(client, monkeypatch,
                                                                  tmp_path):
    db = tmp_path / "old.db"
    conn = sqlite3.connect(db)
    conn.execute("CREATE TABLE sessions (id TEXT, status TEXT, n_turns INT,"
                 " started_at TEXT)")
    conn.execute("INSERT INTO sessions VALUES ('s_1','closed',8,'2026-01-01')")
    conn.commit()
    conn.close()
    from server import storage
    monkeypatch.setattr(storage, "DB_PATH", db)
    r = client.post("/api/rater-assignments", params={"key": RATER_KEY},
                    json={"cohort": "study", "rater_ids": ["ra_1"]})
    assert r.status_code == 400   # nothing to assign, not a 500


def test_active_and_silent_encounters_are_not_assigned(client, monkeypatch, tmp_path):
    """An encounter still recording has no record to read, and one in which the
    participant never spoke has nothing to score."""
    db = tmp_path / "mixed.db"
    conn = sqlite3.connect(db)
    conn.execute("CREATE TABLE sessions (id TEXT, status TEXT, n_turns INT,"
                 " started_at TEXT, cohort TEXT)")
    conn.executemany("INSERT INTO sessions VALUES (?,?,?,?,?)", [
        ("s_good", "closed", 8, "2026-01-01", "study"),
        ("s_live", "active", 3, "2026-01-02", "study"),
        ("s_mute", "closed", 0, "2026-01-03", "study"),
        ("s_null", "closed", None, "2026-01-04", "study"),
    ])
    conn.commit()
    conn.close()
    from server import storage
    monkeypatch.setattr(storage, "DB_PATH", db)
    assert appmod._rateable_sessions("study") == ["s_good"]


@pytest.mark.parametrize("body,fragment", [
    ({"cohort": "study"}, "rater_ids is required"),
    ({"rater_ids": []}, "rater_ids is required"),
    ({"rater_ids": "ra_1"}, "list of rater ids"),
    ({"rater_ids": [1, 2]}, "list of rater ids"),
    ({"rater_ids": ["ra_1"]}, "session_ids or cohort"),
    ({"rater_ids": ["ra_1"], "session_ids": "s_1"}, "list of session ids"),
    ({"rater_ids": ["ra_1"], "session_ids": ["s_1"], "per_encounter": 0}, "at least 1"),
    ({"rater_ids": ["ra_1"], "session_ids": ["s_1"], "per_encounter": "many"},
     "whole number"),
    ({"rater_ids": ["ra_1"], "session_ids": ["s_1"], "per_encounter": 1,
      "seed": "beans"}, "whole number"),
])
def test_assignment_input_is_checked_before_the_draw(client, fakes, body, fragment):
    r = client.post("/api/rater-assignments", params={"key": RATER_KEY}, json=body)
    assert r.status_code == 400
    assert fragment in r.json()["detail"]
    assert fakes.raters.assign_calls == []


def test_a_bug_in_the_draw_is_a_500_not_a_400(client, fakes):
    """A ValueError from the draw is the researcher's request being impossible.
    A TypeError is our own bug, and dressing it up as a 400 sends them hunting
    for a mistake in their request that is not there — which is exactly what
    happened when the draw answered '_repair_connectivity() takes 3 positional
    arguments but 4 were given'."""
    def broken(*a, **k):
        raise TypeError("_repair_connectivity() takes 3 positional arguments but 4 were given")

    fakes.raters.assign = broken
    import sys as _sys
    _sys.modules["server.raters"].assign = broken
    with TestClient(appmod.app, raise_server_exceptions=False) as raw:
        r = raw.post("/api/rater-assignments", params={"key": RATER_KEY},
                     json={"session_ids": ["s_1"], "rater_ids": ["ra_1"],
                           "per_encounter": 1})
    assert r.status_code == 500


def test_more_raters_per_encounter_than_raters_is_arithmetic_not_a_traceback(client):
    r = client.post("/api/rater-assignments", params={"key": RATER_KEY},
                    json={"session_ids": ["s_1"], "rater_ids": ["ra_1", "ra_2"],
                          "per_encounter": 3})
    assert r.status_code == 400
    assert "at least that many raters" in r.json()["detail"]


def test_listing_the_plan_joins_the_rating_code_back_to_the_encounter(client):
    rows = client.get("/api/rater-assignments", params={"key": RATER_KEY}).json()
    assert len(rows) == 3
    by_id = {row["assignment_id"]: row for row in rows}
    mine = by_id["as_aaaaaaaaaaaa"]
    # The researcher is not blinded: the session id and the rating code are both
    # here, because that join is the only way to act on "RC-... is broken".
    assert mine["session_id"] == "s_1772460300_44c9a2"
    assert mine["rating_code"] == "RC-" + "1772460300_44c9a2"[-10:].upper()
    assert mine["rater_name"] == "Rater A"
    assert [row["assignment_id"] for row in rows] == sorted(by_id)


def test_the_plan_can_be_filtered_by_rater_and_status(client):
    rows = client.get("/api/rater-assignments",
                      params={"key": RATER_KEY, "rater_id": "ra_1",
                              "status": "submitted"}).json()
    assert [row["assignment_id"] for row in rows] == ["as_aaaaaaaaaaab"]


def test_the_plan_can_be_filtered_by_cohort(client, fakes):
    for a in fakes.raters.assignments.values():
        a["cohort"] = "study" if a["rater_id"] == "ra_1" else "internal"
    rows = client.get("/api/rater-assignments",
                      params={"key": RATER_KEY, "cohort": "internal"}).json()
    assert [row["assignment_id"] for row in rows] == ["as_bbbbbbbbbbbb"]
    rows = client.get("/api/rater-assignments",
                      params={"key": RATER_KEY, "rater_id": "ra_2",
                              "cohort": "study"}).json()
    assert rows == []


def test_filtering_the_plan_by_an_unknown_rater_is_404(client):
    r = client.get("/api/rater-assignments",
                   params={"key": RATER_KEY, "rater_id": "ra_nope"})
    assert r.status_code == 404


# --- researcher: export and reliability ---------------------------------------

def test_the_ratings_export_carries_the_licensing_notice(client, fakes):
    fakes.ratings.submitted = [{"assignment_id": "as_1", "scores": {"ESCI-08": 4}}]
    body = client.get("/api/ratings", params={"key": RATER_KEY, "cohort": "study"}).json()
    assert body["n"] == 1
    assert body["cohort"] == "study"
    assert body["ratings"][0]["assignment_id"] == "as_1"
    assert "licensing" in body["notice"]


def test_the_reliability_report_is_passed_the_cohort(client, fakes):
    body = client.get("/api/reliability",
                      params={"key": RATER_KEY, "cohort": "study"}).json()
    assert fakes.reliability.calls == ["study"]
    assert body["cohort"] == "study"
    assert "notice" in body


def test_reliability_without_a_cohort_covers_everything(client, fakes):
    client.get("/api/reliability", params={"key": RATER_KEY})
    assert fakes.reliability.calls == [None]


def test_an_undefined_coefficient_is_null_not_a_500(client, fakes):
    """JSON has no NaN, and Starlette serialises with allow_nan=False, so one
    NaN anywhere in the report is an empty 500 rather than a missing field.
    Krippendorff's alpha over an item with no observed disagreement really does
    come back NaN — the reference wave produced it on ESCI-15."""
    nan, inf = float("nan"), float("inf")
    fakes.reliability.extra = {
        "items": {"ESCI-15": {"alpha": nan, "icc": {"icc21": inf, "n": 26},
                              "kappas": [0.4, nan]}},
        "n_ratings": 78,
    }
    r = client.get("/api/reliability", params={"key": RATER_KEY})
    assert r.status_code == 200
    item = r.json()["items"]["ESCI-15"]
    assert item["alpha"] is None
    assert item["icc"] == {"icc21": None, "n": 26}
    assert item["kappas"] == [0.4, None]
    assert r.json()["n_ratings"] == 78     # finite numbers are untouched


def test_the_reliability_reports_own_notice_is_not_duplicated(client, fakes):
    fakes.reliability.extra = {"item_source_notice": "ESCI items, licensed under X"}
    body = client.get("/api/reliability", params={"key": RATER_KEY}).json()
    assert body["item_source_notice"] == "ESCI items, licensed under X"
    assert "notice" not in body
