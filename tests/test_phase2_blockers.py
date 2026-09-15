"""Regression tests for the Phase 2 go-live blockers, and for the second pass.

Each test here exists because the defect it pins was silent: a rater packet that
made an affirmative false statement about an encounter, a rating file that was
not JSON, an allocation that 500ed with nothing written, and an auth suite that
only ever exercised the branch where the credential is configured. None of them
announced itself, and three of the four would have been discovered only after
the wave was collected.

The last section is the second pass, over what the first one left open or broke:
a blinding guard that ran after its own truncation, a media block whose shape
depended on which failure had happened, a duration that could refuse a rating, a
rater substitution nothing reported, and a silence probe handed to the character
the beat was about.

Run from the repo root:

    python -m pytest tests

Nothing here opens a socket, and since the media block started asking storage
rather than reading a browser's receipt, that is enforced instead of assumed:
video.exists() is a HEAD against the study bucket whenever an encounter has no
local recording, which is every media test below. The sessions_root fixture puts
a stub client in front of it. Left to itself, each of these tests signs a real
request to the real Cornell bucket when credentials happen to be in the
environment — and when they are not, waits out two connects to 169.254.169.254,
the EC2 metadata service, which is unroutable anywhere but EC2. The AWS failures
that matter here are reproduced with botocore's own exception types, and the
upload failure that matters is reproduced by writing the event trail the browser
and the confirm endpoint actually leave behind.
"""

from __future__ import annotations

import importlib
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest
from botocore.exceptions import ClientError, NoCredentialsError

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

# See tests/test_ratings.py: server.storage binds DATA_DIR at import, so the
# first import must land somewhere harmless rather than in the repo's own data
# directory. Per-test isolation is the data_dir fixture below.
_ORIGINAL_DATA_DIR = os.environ.get("DATA_DIR")
os.environ["DATA_DIR"] = tempfile.mkdtemp(prefix="rf_blockers_import_")

from server import rater_packet as rp  # noqa: E402
from server import raters as raters_mod  # noqa: E402
from server import ratings, storage, video  # noqa: E402

if _ORIGINAL_DATA_DIR is None:
    del os.environ["DATA_DIR"]
else:
    os.environ["DATA_DIR"] = _ORIGINAL_DATA_DIR


SESSION_ID = "s_1772460300_44c9a2"

# The minted assignment shape (server/raters.py: as_[0-9a-f]{12}). The packet's
# playback URL is addressed to an ASSIGNMENT now rather than to an S3 object, so
# a media test that wants the playable state has to have one.
ASSIGNMENT_ID = "as_5f3c11a90b2d"


# ---------------------------------------------------------------------------
# B3 — "No webcam recording was captured" was said about lost uploads too
# ---------------------------------------------------------------------------

def _session_with_events(root: Path, *events) -> Path:
    """A session directory carrying just the event trail _media reads."""
    sdir = root / SESSION_ID
    sdir.mkdir(parents=True, exist_ok=True)
    with (sdir / "events.jsonl").open("w", encoding="utf-8") as fh:
        fh.write(json.dumps({"t": 0.0, "type": "session_start"}) + "\n")
        for ev in events:
            fh.write(json.dumps(ev) + "\n")
    return sdir


class _StubS3:
    """The study bucket, answered in this process. No socket, ever.

    `objects` maps object key to byte count; anything not in it is absent.
    head_object answers the two ways S3 really answers — a ContentLength, or a
    404 ClientError — because video.head_video branches on botocore's own error
    shape and reads the service's code out of `response`. A stub that raised
    some bespoke exception would exercise a path no deployment ever takes and
    would go on passing after head_video stopped classifying anything correctly.

    `fail_with` puts a BotoCoreError in the same place instead, for the states
    that are about AWS refusing to answer at all rather than about the object.
    """

    def __init__(self, objects=None, fail_with=None):
        self.objects = dict(objects or {})
        self.fail_with = fail_with
        self.heads: list = []

    def head_object(self, Bucket=None, Key=None, **kwargs):  # noqa: N803 — boto3's own kwarg names
        self.heads.append(Key)
        if self.fail_with is not None:
            raise self.fail_with
        size = self.objects.get(Key)
        if size is None:
            raise ClientError(
                {"Error": {"Code": "404", "Message": "Not Found"}}, "HeadObject")
        return {"ContentLength": size, "ContentType": "video/webm"}


@pytest.fixture
def sessions_root(tmp_path, monkeypatch):
    root = tmp_path / "sessions"
    root.mkdir()
    monkeypatch.setattr(rp, "SESSIONS_DIR", root)
    monkeypatch.setattr(video, "SESSIONS_DIR", root)
    # An empty bucket, in this process. Every _media test below is an encounter
    # with no local recording, and _media asks video.exists(), which falls
    # through to a HEAD against the real study bucket — so without this line
    # each of these tests puts a signed request on the wire (or, credential-less,
    # blocks on the EC2 metadata service twice before botocore gives up). Tests
    # that want bytes to be there replace this with a stub that holds the key.
    monkeypatch.setattr(video, "_client", lambda: _StubS3())
    return root


def test_an_encounter_with_no_camera_still_reads_as_no_camera(sessions_root):
    """The honest negative, which must not change: no event, no recording."""
    _session_with_events(sessions_root)
    media = rp._media(SESSION_ID)
    assert media["video_status"] == "absent"
    assert media["video_available"] is False
    assert media["video_url"] is None
    assert "No webcam recording was captured" in media["note"]


def test_a_lost_upload_is_not_reported_as_an_encounter_with_no_camera(sessions_root):
    """The defect: a recording that was made and never reached storage.

    The rater was told "No webcam recording was captured for this encounter"
    and instructed to rate from the transcript with more N/As — an affirmative
    false statement, and one that lands in the ratings table indistinguishable
    from a participant who never turned a camera on. The two are different
    judgements and the difference has to be visible while the rating is being
    made.
    """
    _session_with_events(sessions_root, {
        "t": None, "wall": 1772460800.0, "type": "video_uploaded",
        "key": f"encounters/{SESSION_ID}/webcam.webm", "bytes": 0,
        "status": "failed", "error": "put 403",
    })
    media = rp._media(SESSION_ID)
    assert media["video_status"] == "failed"
    assert media["video_url"] is None
    assert "No webcam recording was captured" not in media["note"]
    assert "WAS recorded" in media["note"]
    assert "put 403" in media["note"]
    assert media["upload_error"] == "put 403"
    # video_available is what the rating console reads to block the submit, and
    # a lost recording is a fault to report rather than a transcript-only
    # rating: the transcript rating cannot be undone, the block can.
    assert media["video_available"] is True


def test_a_zero_byte_confirmation_from_before_the_status_field_still_reads_as_lost(
        sessions_root):
    """An older confirm endpoint wrote no status; `bytes: 0` said the same thing.

    The event exists, so a recording was made and reported; the receipt is None,
    so there is no object behind it. That is the lost state whether or not the
    writer knew to name it.
    """
    _session_with_events(sessions_root, {
        "t": None, "wall": 1772460800.0, "type": "video_uploaded",
        "key": f"encounters/{SESSION_ID}/webcam.webm", "bytes": 0,
    })
    assert video.upload_receipt(SESSION_ID) is None
    assert rp._media(SESSION_ID)["video_status"] == "failed"


def test_an_upload_error_naming_the_session_is_not_passed_through(sessions_root):
    """The packet is blinded, and this string came from a browser.

    A session id carries the encounter's start time to the second, which is the
    cross-linking the rating code exists to prevent; an error message quoting a
    failed URL is the obvious way for one to arrive.
    """
    _session_with_events(sessions_root, {
        "t": None, "type": "video_uploaded", "bytes": 0, "status": "failed",
        "error": f"PUT https://bucket.s3.amazonaws.com/encounters/{SESSION_ID}/"
                 "webcam.webm failed",
    })
    media = rp._media(SESSION_ID)
    assert media["video_status"] == "failed"
    assert media["upload_error"] is None
    assert SESSION_ID not in json.dumps(media)


def test_a_successful_upload_is_still_playable(sessions_root, monkeypatch):
    """The state that must not have been disturbed by teaching _media the others.

    Same guarantee, restated against the design that replaced presigning: an
    encounter whose recording is really there must come back as something a
    rater can play. What "playable" MEANS changed underneath it. It used to be
    "a presigned S3 GET was minted", which is why this test faked the signing —
    and the URL that came out of that was a bearer credential for the study
    bucket, carrying encounters/{session_id}/webcam.webm through the rater's
    address bar and into their browser history. It is now "a URL on this
    application that this application has a route for", which is checked here
    rather than faked, because there is nothing left to fake: no signature, no
    expiry, no credential.

    The upload event is still written because a real successful upload leaves
    one, but nothing branches on it any more — the bytes are what decide.
    """
    _session_with_events(sessions_root, {
        "t": None, "wall": 1772460800.0, "type": "video_uploaded",
        "key": f"encounters/{SESSION_ID}/webcam.webm", "bytes": 8_400_000,
        "status": "ok",
    })
    # Bytes in the bucket, not a receipt saying there are. _media asks storage
    # now (video.exists), and an upload that succeeded is exactly an object that
    # is there — which is the whole of the fix: a confirmation event is a claim
    # ABOUT storage, and an encounter whose confirm leg failed used to be
    # permanently unrateable with its recording sitting in the bucket.
    monkeypatch.setattr(video, "_client",
                        lambda: _StubS3({video.video_key(SESSION_ID): 8_400_000}))

    media = rp._media(SESSION_ID, ASSIGNMENT_ID)

    assert media["video_status"] == "ok"
    assert media["video_available"] is True
    assert media["note"] is None
    assert media["video_url"] == f"/api/rater/video/{ASSIGNMENT_ID}"

    # Playable, and not merely well-formed: the route the packet names has to be
    # a route the application serves. The packet builder holds the template and
    # app.py holds the handler, and nothing but this joins them — a rename on
    # either side leaves every packet in the wave pointing at a 404 with the
    # console reporting a present recording as one that would not load.
    app_py = (REPO_ROOT / "server" / "app.py").read_text(encoding="utf-8")
    assert f'@app.get("{rp.VIDEO_ROUTE}")' in app_py, (
        f"no handler for {rp.VIDEO_ROUTE}; the packet's playback URL 404s")

    # App-relative, so the console fetches it from whatever origin served the
    # console — the only origin the rater's token is good against. An absolute
    # URL would be fixed at build time and wrong the moment the same packet was
    # opened against staging rather than a laptop.
    assert media["video_url"].startswith("/")
    assert "://" not in media["video_url"]

    # Nothing that runs out mid-sitting. The rater has the link for as long as
    # their token is good, so there is no deadline to count down and nothing to
    # re-mint — which is what the two-strikes re-mint retry in the console was
    # for, and why it could be deleted.
    assert media["expires_in"] is None

    # And the leak the route closed, asserted by value: a presigned GET's object
    # key IS encounters/{session_id}/webcam.webm, so the one session id in the
    # packet used to sit in a field the rater could read out of their own
    # network tab. A session id carries the encounter's start time to the
    # second, which is enough to tell that two packets belong to one participant.
    blob = json.dumps(media)
    assert SESSION_ID not in blob
    assert SESSION_ID.split("_")[1] not in blob
    assert "X-Amz" not in blob and "Signature" not in blob


@pytest.mark.parametrize("assignment_id", [
    None, "", "   ", "as_5f3c11a90b2", "AS_5F3C11A90B2D",
    "as_5f3c11a90b2d/../../etc", "as_5f3c11a90b2d?token=x",
])
def test_a_video_this_packet_cannot_address_is_still_its_own_state(
        sessions_root, monkeypatch, assignment_id):
    """A packet that cannot name the recording is not a packet with no recording.

    The state survived the move off presigned URLs; only its cause did not. It
    used to be "the link could not be signed" — no credentials, a wrong region —
    and it is now "there are bytes and this packet has no assignment id to
    address them with": a packet built outside a rating assignment, by an
    operator inspecting an encounter, or built with an id that is not the minted
    shape. A rater arriving through /api/rater/packet/{assignment_id} cannot
    reach it, which is exactly why it is easy to lose, and losing it would mean
    emitting a URL that 404s or reporting the encounter as having no camera.

    What it protects has not moved an inch, and it is the B3 defect this whole
    section exists for: "there IS a recording and this console cannot reach it"
    must never be shown as "there is no recording", because the second sentence
    tells the rater to go ahead and rate from the transcript with more N/As —
    an affirmative false statement, and a rating that cannot be taken back,
    landing in the ratings table indistinguishable from one made about a
    participant who never turned a camera on.

    Parametrised over the ways an id fails the shape check because that check is
    also the reason the id can be interpolated into a <video> src at all: an id
    carrying "/" or "?" would move the element's request to another route or
    displace the token the console appends.
    """
    _session_with_events(sessions_root, {
        "t": None, "type": "video_uploaded", "bytes": 8_400_000, "status": "ok",
    })
    monkeypatch.setattr(video, "_client",
                        lambda: _StubS3({video.video_key(SESSION_ID): 8_400_000}))

    media = rp._media(SESSION_ID, assignment_id)

    assert media["video_status"] == "unsigned"
    # True, and it is the whole point: this is what the console reads to disable
    # the submit. The rater is stopped and told to report it, rather than handed
    # a transcript and left to conclude the camera was off.
    assert media["video_available"] is True
    # No half-formed URL. A "/api/rater/video/" with nothing after it, or one
    # with the caller's punctuation still in it, is a request aimed somewhere it
    # was not meant to go — and the console would render a player over it and
    # report a present recording as a broken one.
    assert media["video_url"] is None
    assert media["expires_in"] is None
    assert "could not be issued" in media["note"]
    # Not the absent branch's advice, which is the confusion this state exists
    # to prevent.
    assert "No webcam recording was captured" not in media["note"]
    if (assignment_id or "").strip():
        # An id was given and refused: something minted, stored or typed an
        # assignment id that is not the minted shape, a console would show a
        # present recording it cannot play, and nothing else in the system will
        # notice. That is a fault, so the rating is stopped rather than made
        # around it. (The no-id reading is an operator inspecting an encounter
        # rather than a rater rating one, so it is deliberately not held to the
        # same sentence — see server/rater_packet._media.)
        assert "Do not rate it" in media["note"]


# ---------------------------------------------------------------------------
# B43 — a top-up whose rater pool omits raters already on disk
# ---------------------------------------------------------------------------

def test_a_top_up_with_a_disjoint_pool_does_not_blow_up_the_allocation():
    """Wave 1 to A and B; wave 2 over more encounters, naming only C and D.

    `existing` comes from what is on disk and is not filtered by the caller's
    pool, so A and B are in the plan — and therefore in the overlap graph the
    connectivity repair walks — while a `load` keyed only by the caller's pool
    did not have them. The repair indexed one and raised KeyError, which reached
    the researcher as a bare HTTP 500 from POST /api/rater-assignments with no
    assignments written and no explanation. The workaround (name the old raters
    too) is not something a researcher can be expected to guess.
    """
    sessions = [f"s_{i:03d}" for i in range(10)]
    existing = {sid: (["A", "B"] if i < 6 else []) for i, sid in enumerate(sessions)}

    plan = raters_mod.allocate_plan(sessions, ["C", "D"], 2, 7, existing=existing)

    assert len(raters_mod._components(plan)) == 1, (
        "reliability cannot be computed across groups that share no rater")
    for sid in sessions:
        assert len(plan[sid]) == 2
        assert len(set(plan[sid])) == 2
    # The encounters that already had their complement keep it untouched.
    for sid in sessions[:6]:
        assert set(plan[sid]) >= {"A"} or set(plan[sid]) >= {"B"}


def test_existing_raters_outside_the_pool_count_towards_their_load():
    """The other half of the same mistake: a load that was not a load.

    A rater carrying four encounters from an earlier call ranked as load 0
    whenever the current call did not name them, so the connectivity repair
    handed its extra work to the busiest person in the wave.
    """
    sessions = ["s_a", "s_b", "s_c", "s_d"]
    existing = {"s_a": ["A", "B"], "s_b": ["A", "B"], "s_c": [], "s_d": []}
    plan = raters_mod.allocate_plan(sessions, ["C", "D", "E"], 2, 3,
                                    existing=existing)
    counts = {}
    for members in plan.values():
        for r in members:
            counts[r] = counts.get(r, 0) + 1
    assert len(raters_mod._components(plan)) == 1
    # A and B already have two each. Nobody who started at zero should have been
    # pushed past them to keep the graph connected.
    assert max(counts.values()) <= 3


# ---------------------------------------------------------------------------
# B45 — NaN and Infinity in the duration
# ---------------------------------------------------------------------------

@pytest.fixture
def data_dir(tmp_path, monkeypatch):
    """An empty DATA_DIR per test, with storage and ratings rebound to it."""
    monkeypatch.setenv("DATA_DIR", str(tmp_path / "data"))
    importlib.reload(storage)
    importlib.reload(ratings)
    return tmp_path / "data"


class _FakeRaters:
    """Just enough of server.raters for the store to accept a submission."""

    def __init__(self):
        self.assignments = {
            "as_aaaaaaaaaaaa": {
                "assignment_id": "as_aaaaaaaaaaaa",
                "session_id": SESSION_ID,
                "rater_id": "ra_1",
                "construct": "teamwork",
                "status": "pending",
                "assigned_at": 1772460000.0,
            }
        }

    def get_assignment(self, assignment_id):
        a = self.assignments.get(assignment_id)
        return dict(a) if a else None

    def mark_submitted(self, assignment_id, submitted_at=None):
        self.assignments[assignment_id]["status"] = "submitted"
        return dict(self.assignments[assignment_id])


@pytest.fixture
def fake_raters(data_dir, monkeypatch):
    fr = _FakeRaters()
    monkeypatch.setattr(ratings, "raters", fr)
    return fr


def _full_scores(value=4):
    return {it["id"]: value for it in ratings.esci.all_items()}


@pytest.mark.parametrize("raw", ["nan", "NaN", float("nan")])
def test_a_nan_duration_is_recorded_as_no_timing_rather_than_as_a_timing(raw):
    """pandas writes "nan" into a missing duration column, and a Qualtrics
    export is a pandas CSV. NaN means "not measured", which is what None already
    means here — and _quality_flags then says so out loud."""
    assert ratings._coerce_seconds(raw) is None
    assert "no_timing" in ratings._quality_flags({"i1": 4}, ratings._coerce_seconds(raw))
    # And NOT "reported and unusable": an empty cell is a missing measurement,
    # not a broken clock, and conflating the two would flag every Qualtrics
    # import that left the duration column out.
    assert ratings._timing_was_unusable(raw) is False


@pytest.mark.parametrize("raw", ["inf", "-inf", "Infinity", "1e400", float("inf")])
def test_an_infinite_duration_becomes_unknown_rather_than_refusing_the_rating(raw):
    """R29. Refusing it was the regression.

    _coerce_seconds raised InvalidRating on Infinity, InvalidRating is a
    ValueError, and server/app.py turns a ValueError from submit into a 400 —
    so `"seconds": Infinity` in the body threw away 22 answered items over a
    number nobody would have used. That contradicts the contract app.py states
    at 1933-1937 in as many words: the duration is diagnostic, not data, and a
    browser reporting it wrongly must not be able to reject a rating a human
    spent twenty minutes on. It becomes None, and the record says it was
    reported and unusable rather than never measured.
    """
    assert ratings._coerce_seconds(raw) is None
    assert ratings._timing_was_unusable(raw) is True


def test_a_negative_duration_is_still_refused():
    """The one finite case that stays a refusal, so this is not a blanket amnesty.

    app.py normalises a negative to None before submit is reached, so the
    refusal only bites the import path — where a row can be rejected, corrected
    and re-sent, and where a negative duration means the column is mismapped.
    """
    with pytest.raises(ratings.InvalidRating):
        ratings._coerce_seconds(-5)
    assert ratings._timing_was_unusable(-5) is False


def test_an_infinite_duration_keeps_the_rating_and_flags_the_clock(fake_raters):
    """End to end through submit: the 22 answers survive, and the loss is named.

    Two things have to be true at once. The rating is stored — anything else
    is the regression — and the stored record does not quietly claim nobody
    timed it, because "the console reported nonsense" is a fact about that
    submission worth having before the wave's timings are used to spot
    straight-lining.
    """
    stored = ratings.submit("as_aaaaaaaaaaaa", "ra_1", _full_scores(),
                            {"better": "", "notable": ""}, float("inf"))
    assert stored["seconds"] is None
    assert stored["n_answered"] == len(ratings.esci.all_items())
    assert "no_timing" in stored["quality_flags"]
    assert "bad_timing" in stored["quality_flags"]

    # ...and it is still strict JSON on disk, which is what the refusal was
    # protecting in the first place.
    text = (ratings.RATINGS_DIR / "as_aaaaaaaaaaaa" / "v1.json").read_text(encoding="utf-8")
    assert "Infinity" not in text and "NaN" not in text
    json.dumps(json.loads(text), allow_nan=False)


def test_a_measured_duration_is_not_flagged_as_a_broken_clock(fake_raters):
    """The false-positive check: an ordinary rating must not gain bad_timing."""
    stored = ratings.submit("as_aaaaaaaaaaaa", "ra_1", _full_scores(),
                            {"better": "", "notable": ""}, 480.0)
    assert stored["seconds"] == 480.0
    assert "bad_timing" not in stored["quality_flags"]
    assert "no_timing" not in stored["quality_flags"]


def test_a_stored_rating_is_always_strict_json(fake_raters):
    """The defect, at the only door it could come through.

    A non-finite duration used to survive float(), `val < 0` and round(), and
    land in the version file as the bare token NaN. Python's json wrote it and
    read it back, so nothing on this side noticed — but the permanent study
    artefact was no longer JSON (jq, jsonlite, pandas and every browser refuse
    it) and GET /api/ratings 500ed for the whole export because one record could
    not be serialised. One bad submission hid all 78 good ones.
    """
    stored = ratings.submit("as_aaaaaaaaaaaa", "ra_1", _full_scores(),
                            {"better": "", "notable": ""}, float("nan"))
    assert stored["seconds"] is None
    assert "no_timing" in stored["quality_flags"]

    path = ratings.RATINGS_DIR / "as_aaaaaaaaaaaa" / "v1.json"
    text = path.read_text(encoding="utf-8")
    assert "NaN" not in text and "Infinity" not in text

    def _no_constants(token):  # what every other JSON reader does
        raise AssertionError(f"non-JSON token {token}")

    on_disk = json.loads(text, parse_constant=_no_constants)
    assert on_disk["seconds"] is None
    # And the export path: json.dumps with allow_nan=False is what Starlette's
    # JSONResponse does, so a record that survives this cannot 500 /api/ratings.
    json.dumps(on_disk, allow_nan=False)


def test_a_non_serialisable_record_is_refused_rather_than_half_written(fake_raters,
                                                                       monkeypatch):
    """The backstop under _coerce_seconds, and the reservation it rolls back.

    If some future field ever carries a NaN past the coercion, the write must
    fail rather than produce a rating file no other reader can open — and it
    must not leave an index row pointing at a file that was never created.
    """
    real_flags = ratings._quality_flags
    monkeypatch.setattr(ratings, "_quality_flags",
                        lambda scores, seconds: real_flags(scores, seconds) + [float("nan")])
    with pytest.raises(ValueError):
        ratings.submit("as_aaaaaaaaaaaa", "ra_1", _full_scores(),
                       {"better": "", "notable": ""}, 480.0)
    assert not (ratings.RATINGS_DIR / "as_aaaaaaaaaaaa" / "v1.json").exists()
    assert ratings.get_rating("as_aaaaaaaaaaaa") is None


# ---------------------------------------------------------------------------
# B52 — the researcher credential, when nobody set one
# ---------------------------------------------------------------------------

_BOOT = """
import sys
from fastapi.testclient import TestClient
import server.app as app
with TestClient(app.app):
    pass
print("STARTED")
"""


def _boot(tmp_path, **env_overrides):
    """Import and start server.app in a clean process, and report what happened.

    A subprocess rather than importlib.reload because the guard is a startup
    guard: whether it is written as a module-level check or as a lifespan
    handler, what has to be true is that this process does not come up serving
    study data. Both shapes are caught by running the import and the startup
    together and looking at the exit code.
    """
    env = dict(os.environ)
    env.pop("SESSION_KEY", None)
    env["DATA_DIR"] = str(tmp_path / "data")

    # "A clean process" was not clean: the child inherited the whole ambient
    # environment. On any machine holding real credentials — a developer's, a CI
    # runner's — the boot these tests measured was therefore NOT a fresh clone's
    # boot, and running pytest transmitted a live gateway key to
    # api.ai.it.cornell.edu three times per run, because llm.preflight signs its
    # probe with `Authorization: Bearer <key>`. It also asked the EC2 metadata
    # service for AWS credentials twelve times, which is unroutable off EC2 and
    # so costs two botocore timeouts each.
    #
    # conftest's socket guard cannot catch any of this: the guard is in-process
    # and this is a subprocess. That is the whole reason it survived the round
    # that added the guard.
    #
    # So scrub what the app looks for, and point the gateway at a closed
    # loopback port so its probe fails instantly and locally rather than
    # crossing the network to fail slowly. Both changes also make these tests
    # measure the thing their names claim — a machine with nothing configured.
    # Scrubbing happens before env_overrides, so a test that wants a credential
    # in the child still passes one explicitly.
    #
    # One residual case, named rather than hidden: server/llm.py reads a .env
    # file in PREFERENCE to the environment, so a developer whose .env sets
    # LLM_BASE_URL outright still redirects the child. A .env copied from
    # .env.example does not set it, which is the case this covers.
    for name in list(env):
        if name.startswith("AWS_") or any(
                token in name for token in
                ("API_KEY", "AUTH_TOKEN", "SECRET", "CREDENTIAL", "PASSWORD")):
            env.pop(name, None)
    env["LLM_BASE_URL"] = "http://127.0.0.1:1"
    env["AWS_EC2_METADATA_DISABLED"] = "true"

    for k, v in env_overrides.items():
        if v is None:
            env.pop(k, None)
        else:
            env[k] = v
    return subprocess.run([sys.executable, "-c", _BOOT], cwd=str(REPO_ROOT),
                          env=env, capture_output=True, text=True, timeout=300)


def test_a_public_deployment_with_no_session_key_refuses_to_start(tmp_path):
    """The credentials-arrival hazard, and the only place it can be caught.

    check_key is `if SESSION_KEY and key != SESSION_KEY`, so an empty
    SESSION_KEY makes every researcher route a no-op guard: /api/ratings,
    /api/raters, /api/reliability, /researcher, /evidence, /api/runs and
    /api/sessions all answer 200 to anyone with the URL. That is deliberate for
    local development (see the note above check_key and _require_owner_or_key),
    and it is indefensible for a deployment a stranger can open a socket to.
    Nothing in a response or in /health distinguishes the two, so the refusal
    has to happen at startup, where an operator sees it.

    The guard keys off the BIND ADDRESS first and the host allowlist second
    (server/app.py:_refuse_unprotected_public_start), so this passes HOST as
    well: ALLOWED_HOSTS defaults to the production hostnames, and testing the
    allowlist alone refused to start on a fresh clone following the README.
    """
    proc = _boot(tmp_path, HOST="0.0.0.0",
                 ALLOWED_HOSTS="rf.example.org,api.rf.example.org")
    assert proc.returncode != 0, (
        "a deployment bound to 0.0.0.0 with a public hostname and no "
        "SESSION_KEY started anyway; it is serving participant transcripts and "
        "the ratings export to anyone with the URL"
    )
    assert "SESSION_KEY" in (proc.stderr + proc.stdout), (
        "the refusal must name the missing credential; an operator reading a "
        "crash log is the only person who can fix it"
    )


def test_a_public_bind_that_answers_to_any_host_also_refuses(tmp_path):
    """Emptying ALLOWED_HOSTS is the widest setting there is, not the narrowest.

    It is also the first thing anyone reaches for when the ALB trips the host
    check, and on a public bind it turns the guard off in both directions at
    once.
    """
    proc = _boot(tmp_path, HOST="0.0.0.0", ALLOWED_HOSTS="")
    assert proc.returncode != 0, (
        "an empty ALLOWED_HOSTS on a public bind means the process answers to "
        "any Host header with no researcher credential at all"
    )


def test_local_development_still_starts_without_a_session_key(tmp_path):
    """The other half of the guard: it must not break the way people work.

    A loopback bind is a development server whatever hostnames it would answer
    to — nobody else can open a socket to it — and demanding a credential there
    would only teach everybody to set SESSION_KEY=x, which is how the production
    value ends up being 'x'.
    """
    proc = _boot(tmp_path, HOST="127.0.0.1", ALLOWED_HOSTS="localhost,127.0.0.1")
    assert proc.returncode == 0, proc.stderr[-2000:]
    assert "STARTED" in proc.stdout


def test_a_fresh_clone_starts_with_nothing_configured(tmp_path):
    """The regression the bind-address rule exists for.

    ALLOWED_HOSTS defaults to the two production hostnames, so a guard that read
    the allowlist alone refused to start on a clone that had set nothing — the
    first thing a new contributor met was a server that would not run, which is
    worse than the exposure it prevents because the fix people find is
    SESSION_KEY=x. HOST is unset here on purpose: this is the README quick
    start, exactly as written.
    """
    proc = _boot(tmp_path, HOST=None, ALLOWED_HOSTS=None)
    assert proc.returncode == 0, proc.stderr[-2000:]
    assert "STARTED" in proc.stdout


def test_a_public_deployment_with_a_session_key_starts(tmp_path):
    """And the configured case, so the guard cannot be a blanket refusal."""
    proc = _boot(tmp_path, HOST="0.0.0.0", ALLOWED_HOSTS="rf.example.org",
                 SESSION_KEY="a-real-researcher-credential")
    assert proc.returncode == 0, proc.stderr[-2000:]
    assert "STARTED" in proc.stdout


# ---------------------------------------------------------------------------
# Round two — what the first pass left open
# ---------------------------------------------------------------------------

# --- R31: the blinding guard ran after the truncation -----------------------

def test_a_session_id_that_straddles_the_truncation_point_is_still_caught(
        sessions_root):
    """_upload_error capped the string BEFORE looking for the session id.

    A 130-character message with the id at offset 110 came back cut through the
    middle of the id, so `session_id in text` was False and the surviving
    prefix — `s_<unix timestamp>` — went into a field the rater reads. That
    prefix is the whole leak: a session id carries the encounter's start time to
    the second, and a rater who collects several can tell which packets were
    recorded twelve minutes apart and therefore belong to one participant. Half
    an id is the same cross-linking as a whole one.
    """
    head = "PUT failed after retry: " + ("x" * 86)
    message = f"{head} {SESSION_ID} webcam.webm"
    assert 100 < message.index(SESSION_ID) < 120, "the id must straddle the cap"
    assert len(message) > 120

    _session_with_events(sessions_root, {
        "t": None, "type": "video_uploaded", "bytes": 0, "status": "failed",
        "error": message,
    })
    media = rp._media(SESSION_ID)
    assert media["video_status"] == "failed"
    assert media["upload_error"] is None
    # By value, not just by field: the note interpolates upload_error, and a
    # renamed or re-worded field is still a leak.
    blob = json.dumps(media)
    assert SESSION_ID not in blob
    assert SESSION_ID.split("_")[1] not in blob, (
        "the timestamp half of the session id survived the truncation"
    )


def test_a_long_but_clean_error_is_still_trimmed_for_the_rater(sessions_root):
    """The cap has not gone away; it just runs after the check, not before.

    The rater is the person who will report this, so the code has to be short
    enough to quote — but nothing is dropped that the blinding check has already
    cleared.
    """
    message = "PUT " + ("y" * 300)
    _session_with_events(sessions_root, {
        "t": None, "type": "video_uploaded", "bytes": 0, "status": "failed",
        "error": message,
    })
    media = rp._media(SESSION_ID)
    assert media["upload_error"] == message[:120]
    assert len(media["upload_error"]) == 120


# --- B3: the packet's own half, finished -------------------------------------

def test_every_media_state_answers_the_same_questions(sessions_root, monkeypatch):
    """upload_error existed only on the 'failed' branch.

    A Python consumer reading media["upload_error"] to decide whether an
    encounter had an upload fault raised KeyError on the other three states —
    and an analysis that crashes on the ordinary rows is an analysis that gets
    run on a filtered set instead. Every state answers every question; the
    answer is just None.

    Each of the four is now produced the way the code actually produces it.
    This test used to build 'unsigned' and 'ok' by patching video.playback_url,
    and when _media stopped calling that it went on passing while silently
    testing 'failed' three times over — the shape guarantee still held, but on
    two states instead of four, and nothing said so. A state built by patching a
    function the code under test no longer calls is not a state.
    """
    keys = {"video_url", "video_available", "video_status", "upload_error",
            "expires_in", "note"}
    empty_bucket = _StubS3()
    full_bucket = _StubS3({video.video_key(SESSION_ID): 8_400_000})

    # absent — nothing in storage and no attempt on the trail.
    _session_with_events(sessions_root)
    monkeypatch.setattr(video, "_client", lambda: empty_bucket)
    absent = rp._media(SESSION_ID, ASSIGNMENT_ID)

    # failed — a recording was made and reported, and storage has nothing.
    _session_with_events(sessions_root, {
        "t": None, "type": "video_uploaded", "bytes": 0, "status": "failed",
        "error": "put 403",
    })
    failed = rp._media(SESSION_ID, ASSIGNMENT_ID)

    # unsigned — bytes are there and this packet has no assignment to address
    # them with. Not a signing failure any more; see the test above.
    _session_with_events(sessions_root, {
        "t": None, "type": "video_uploaded", "bytes": 8_400_000, "status": "ok",
    })
    monkeypatch.setattr(video, "_client", lambda: full_bucket)
    unsigned = rp._media(SESSION_ID, None)

    # ok — bytes, and an assignment to serve them under.
    ok = rp._media(SESSION_ID, ASSIGNMENT_ID)

    # The states really are four, which is the half of this that used to be
    # taken on trust: three of these were 'failed' and nothing noticed.
    assert [absent["video_status"], failed["video_status"],
            unsigned["video_status"], ok["video_status"]] == [
        "absent", "failed", "unsigned", "ok"]

    for state in (absent, failed, unsigned, ok):
        assert set(state) == keys, f"{state['video_status']} has a different shape"
        state["upload_error"]  # the indexing that used to raise
    assert failed["upload_error"] == "put 403"
    assert absent["upload_error"] is None
    assert unsigned["upload_error"] is None
    assert ok["upload_error"] is None
    # expires_in is the same shape rule, and it is now the same answer on every
    # branch: the app serves the bytes for as long as the rater's token is good,
    # so no state has a deadline. The key stays because a consumer reading
    # media["expires_in"] must not have to know which state it is holding.
    for state in (absent, failed, unsigned, ok):
        assert state["expires_in"] is None


def test_an_aws_that_will_not_answer_does_not_become_no_camera(sessions_root,
                                                               monkeypatch):
    """The deployment fault, which outlived the signing it used to arrive as.

    "The link could not be signed" was one way for a deployment to be unable to
    show a rater a recording it holds; it went away with the signing. The
    condition did not. A task with no credentials, a missing VPC endpoint or a
    denied HeadObject cannot tell whether the object is there — video.head_video
    keeps that apart from "S3 says there is no object", and video.exists()
    collapses both to False on purpose, because one unanswerable HEAD must
    downgrade one packet's video rather than raise through every packet in the
    rater's queue and take the console down.

    So the guarantee has to be picked up here instead, and it is the same one
    the old test was defending: a machine that cannot reach storage must not
    tell a rater the encounter had no camera. The event trail is the only
    evidence left that a recording was ever made, and on this branch it is
    believed — the packet says a recording exists, blocks the rating, and asks
    for it to be reported.
    """
    _session_with_events(sessions_root, {
        "t": None, "wall": 1772460800.0, "type": "video_uploaded",
        "key": f"encounters/{SESSION_ID}/webcam.webm", "bytes": 8_400_000,
        "status": "ok",
    })
    # Not a stubbed video.exists(): the real one, over a client that fails the
    # way a credential-less deployment fails. NoCredentialsError is a
    # BotoCoreError and carries no service code, which is the case head_video's
    # classification is easiest to get wrong.
    monkeypatch.setattr(video, "_client",
                        lambda: _StubS3(fail_with=NoCredentialsError()))

    media = rp._media(SESSION_ID, ASSIGNMENT_ID)

    assert media["video_status"] == "failed"
    assert media["video_available"] is True, (
        "a rater is about to be handed a transcript for an encounter that has a "
        "recording this deployment simply cannot reach")
    assert media["video_url"] is None
    assert "No webcam recording was captured" not in media["note"]
    assert "WAS recorded" in media["note"]


# --- R33: the connectivity repair now runs, and can substitute raters --------

@pytest.fixture
def raters_env(tmp_path, monkeypatch):
    """server.raters bound to an empty DATA_DIR (the tests/test_raters.py idiom).

    storage reads DATA_DIR once at import, so a fresh directory means reloading
    storage and then raters, which binds its own paths by value.
    """
    monkeypatch.setenv("DATA_DIR", str(tmp_path / "raters_data"))
    importlib.reload(storage)
    importlib.reload(raters_mod)
    raters_mod.init_rater_storage()
    return raters_mod


def _encounter(mod, session_id, scenario="S1B", cohort="study"):
    d = mod.SESSIONS_DIR / session_id
    d.mkdir(parents=True, exist_ok=True)
    (d / "manifest.json").write_text(json.dumps({
        "session_id": session_id, "scenario": scenario, "cohort": cohort,
        "status": "closed",
    }), encoding="utf-8")
    return session_id


def _split_wave(mod):
    """Two encounters already rated by two disjoint pairs, plus an empty third.

    This is the shape the repair exists for and cannot be produced in one call:
    A/B on one encounter and C/D on another, from separate earlier calls.
    """
    sessions = [_encounter(mod, f"s_17724604{i:02d}_0000{i:02x}") for i in range(3)]
    pool = [mod.create_rater(f"Rater {n}")["rater_id"] for n in "ABCDEF"]
    a, b, c, d, e, f = pool
    mod.assign([sessions[0]], [a, b], per_encounter=2, seed=1)
    mod.assign([sessions[1]], [c, d], per_encounter=2, seed=1)
    return sessions, {"A": a, "B": b, "C": c, "D": d, "E": e, "F": f}


def test_a_rater_the_repair_substituted_is_checked_before_anything_is_written(
        raters_env):
    """The hole `load` being made total opened up.

    Before that fix this path raised KeyError, so a substitution never reached
    disk. It executes now — and assign() was still validating existence only
    over the caller's pool, so the substituted rater's record was one nothing
    had looked at. An assignment written for a rater whose record is gone (or
    deactivated: issue_token refuses those) is an encounter that shows as
    covered on every listing and is never rated by anybody.
    """
    mod = raters_env
    sessions, ids = _split_wave(mod)
    before = len(mod.assignments_for_encounter(sessions[2]))

    # The repair reaches for somebody already in the wave; which of the four it
    # picks depends on the minted rater ids, so take all four records away —
    # the way a half-restored data directory would — and let it pick.
    on_disk = [ids[k] for k in ("A", "B", "C", "D")]
    for rid in on_disk:
        (mod.RATERS_DIR / f"{rid}.json").unlink()

    with pytest.raises(ValueError) as exc:
        mod.assign(sessions, [ids["E"], ids["F"]], per_encounter=2, seed=7)
    assert any(rid in str(exc.value) for rid in on_disk), (
        f"the refusal must name the rater it refused: {exc.value}"
    )
    assert len(mod.assignments_for_encounter(sessions[2])) == before, (
        "the refusal must come before any assignment is written"
    )


def test_an_assignment_the_researcher_did_not_ask_for_says_so(raters_env):
    """Visibility, which is the whole of this fix.

    Asking for E and F over a wave that already holds A/B and C/D produces a
    connected plan built from A and C — the design working as intended — but the
    researcher's request was not carried out, and nothing said so. `unrequested`
    is the only channel assign() has back to them: it is on every assignment, so
    a reader can count the substitutions without a KeyError on the ordinary rows.
    """
    mod = raters_env
    sessions, ids = _split_wave(mod)
    named = {ids["E"], ids["F"]}

    created = mod.assign(sessions, [ids["E"], ids["F"]], per_encounter=2, seed=7)

    assert created, "the top-up wrote nothing at all"
    for rec in created:
        assert "unrequested" in rec, "every assignment must answer this"
        assert rec["unrequested"] == (rec["rater_id"] not in named)
    # The plan really is connected, which is why the substitution happens.
    plan = {sid: [a["rater_id"] for a in mod.assignments_for_encounter(sid)]
            for sid in sessions}
    assert len(mod._components(plan)) == 1
    # And on disk, not just in the return value — a year later the returned list
    # is gone and the assignment file is the record.
    stored = mod.get_assignment(created[0]["assignment_id"])
    assert stored["unrequested"] == created[0]["unrequested"]


def test_a_wave_that_needed_no_substitution_marks_nothing_unrequested(raters_env):
    """The false-positive check: an ordinary allocation is all requested."""
    mod = raters_env
    sessions = [_encounter(mod, f"s_17724605{i:02d}_0000{i:02x}") for i in range(4)]
    pool = [mod.create_rater(f"Rater {n}")["rater_id"] for n in "XYZ"]
    created = mod.assign(sessions, pool, per_encounter=2, seed=3)
    assert created
    assert not any(rec["unrequested"] for rec in created)


# --- R36: the silence probe on a beat that names nobody ----------------------

class _AgentPicker:
    """_trigger_agent, borrowed off the real class with the cast wired in.

    The method reads nothing but self._resolve_agents(), so binding it to a stub
    exercises the real selection rather than a copy of it — the point of the
    test is what realtime_voice_session actually does with these YAML files.
    """

    def __init__(self, cast):
        self._cast = cast

    def _resolve_agents(self):
        return self._cast


def _probe_speaker(scenario, interaction_id, trigger_id, last_speaker):
    """Who _probe_room would hand this beat to, given who just spoke.

    Mirrors _probe_room in server/realtime_voice_session.py: the trigger's own agent if it
    names one, otherwise whoever did not just speak.
    """
    from server.realtime_voice_session import RealtimeVoiceSessionRunner

    interaction = next(i for i in scenario.interactions if i["id"] == interaction_id)
    cast = [a for a in scenario.cast if a.id in (interaction.get("agents") or [])]
    trigger = next(t for t in interaction["triggers"] if t["id"] == trigger_id)

    picker = _AgentPicker(cast)
    speaker = RealtimeVoiceSessionRunner._trigger_agent(picker, trigger)
    order = [a.id for a in cast]
    if speaker not in order:
        speaker = next((a for a in order if a != last_speaker), order[0])
    return speaker


@pytest.mark.parametrize("scenario_id,trigger_id", [
    ("S4A", "t3_decisions_close_with_priya_silent"),
    ("S4B", "t3_runthrough_without_priya"),
])
def test_the_room_close_is_delivered_by_dan_not_by_priya(scenario_id, trigger_id):
    """R36. The beat is ABOUT Priya's silence, so Priya must not deliver it.

    The cue names no character, so _trigger_agent returned None and _probe_room
    fell through to "whoever did not just speak" — which after t2 (Dan's beat)
    is Priya. The participant is scored on encourages_participation and
    solicits_input, i.e. on noticing that Priya has said nothing; a Priya who
    opens the close has answered the question for them. Dan owns the beat in
    both variants — it is a numbered step of his own arc.
    """
    from server import scenarios

    sc = scenarios.load_scenario(scenario_id)
    assert _probe_speaker(sc, "i1", trigger_id, last_speaker="dan") == "dan"
    # And it does not drift with the conversation: the pin holds whoever spoke.
    assert _probe_speaker(sc, "i1", trigger_id, last_speaker="priya") == "dan"


@pytest.mark.parametrize("scenario_id", ["S4A", "S4B"])
def test_every_group_probe_in_the_teamwork_pair_is_bound_to_a_character(scenario_id):
    """The general form, so the next beat added here cannot reintroduce it.

    A probe with no named speaker is not neutral — it is handed to whoever did
    not just speak, which in a three-hander is decided by the previous beat.
    t1/t2 bind by naming Dan as the cue's first word (_trigger_agent infers
    that); t3 binds with an explicit `agent`. Any of the three losing its
    binding puts a scored beat in an arbitrary mouth.
    """
    from server import scenarios
    from server.realtime_voice_session import RealtimeVoiceSessionRunner

    sc = scenarios.load_scenario(scenario_id)
    for interaction in sc.interactions:
        cast = [a for a in sc.cast if a.id in (interaction.get("agents") or [])]
        if len(cast) < 2:
            continue
        picker = _AgentPicker(cast)
        for trigger in interaction.get("triggers") or []:
            if not (trigger.get("on_silence") or "").strip():
                continue
            who = RealtimeVoiceSessionRunner._trigger_agent(picker, trigger)
            assert who in [a.id for a in cast], (
                f"{scenario_id} {interaction['id']} {trigger['id']} names no "
                "character, so the silence probe goes to whoever did not just "
                "speak"
            )


# --- R37: CLAUDE_MODEL is not provenance-only --------------------------------

def test_the_text_engine_really_does_follow_claude_model():
    """The claim the ecs.tf comment now makes, checked against the code.

    Pinning CLAUDE_MODEL to var.director_model was done for provenance, and it
    also moves the deployment's text engine and the researcher's pre-start model
    picker, because CLAUDE_MODEL is what DEFAULT_MODEL is read from. A comment
    warning about a coupling that had quietly gone away would be worse than no
    comment, so the coupling is asserted here rather than described.
    """
    for module in ("server/engine.py", "server/claude_engine.py"):
        text = (REPO_ROOT / module).read_text(encoding="utf-8")
        assert 'setting("CLAUDE_MODEL"' in text, (
            f"{module} no longer takes DEFAULT_MODEL from CLAUDE_MODEL; the "
            "warning in infra/terraform/ecs.tf is now wrong"
        )


def test_the_claude_model_coupling_is_documented_where_it_is_set():
    """Nothing joins ecs.tf to what the app does with the value except a comment.

    Terraform is not installed here and this is not a plan; it is the same
    reasoning as tests/test_task_definition_env.py. An operator repointing
    CLAUDE_MODEL is repointing the text engine and the model picker as well, and
    the only place they will find that out is this line.
    """
    tf = (REPO_ROOT / "infra" / "terraform" / "ecs.tf").read_text(encoding="utf-8")
    block = tf[:tf.index('{ name = "CLAUDE_MODEL"')]
    for needle in ("DEFAULT_MODEL", "engine.py", "picker", "var.text_model"):
        assert needle in block, (
            f"the comment above CLAUDE_MODEL no longer mentions {needle!r}: the "
            "text-engine consequence of this line is undocumented again"
        )
