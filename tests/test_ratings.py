"""Tests for server.ratings, the Phase 2 rating store and Qualtrics ingest.

Run from the repo root:

    python -m pytest tests

Every test runs against its own empty DATA_DIR: server.storage resolves the
environment variable at import time, so the autouse fixture points it at a
tmp_path and reloads storage and then ratings (which binds DATA_DIR by value,
the runs.py idiom). Doing that per test keeps the suite from tripping over
another test module's data directory, or over a pytest process pointing
DATA_DIR at the fixture wave.

The item bank is the real server.esci, over the study's real 22-item CSV: a
store that validates against a made-up bank would prove nothing about the one
the raters will actually see.

server.raters is replaced with a small in-memory double. The store's job is to
refuse, version and index what a rater submits; whether an assignment was drawn
with the right seed is raters' business, and its allocation needs a wave of
recorded encounters that these tests have no reason to build.
"""

from __future__ import annotations

import hashlib
import importlib
import json
import os
import sys
import tempfile
import types
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

# The initial import binds storage.DATA_DIR wherever the environment points, so
# point it somewhere harmless first: importing this module must never be able to
# write into the repo's own data directory. The real per-test isolation is the
# autouse fixture below; the original value is put back so other test modules
# see the environment they expected.
_ORIGINAL_DATA_DIR = os.environ.get("DATA_DIR")
os.environ["DATA_DIR"] = tempfile.mkdtemp(prefix="rf_ratings_import_")


class FakeRaters:
    """Just enough of server.raters for the store to work against."""

    def __init__(self):
        self.assignments = {}
        self.submitted = []
        self._n = 0

    def mint(self, session_id, rater_id, construct="teamwork"):
        self._n += 1
        aid = f"as_{self._n:012x}"
        self.assignments[aid] = {
            "assignment_id": aid,
            "session_id": session_id,
            "rater_id": rater_id,
            "construct": construct,
            "status": "pending",
            "assigned_at": 1772460000.0 + self._n,
        }
        return aid

    # --- the surface server.ratings uses ---
    def get_assignment(self, assignment_id):
        a = self.assignments.get(assignment_id)
        return dict(a) if a else None

    def assignments_for_rater(self, rater_id, status=None):
        return [dict(a) for a in self.assignments.values()
                if a["rater_id"] == rater_id
                and (status is None or a["status"] == status)]

    def mark_submitted(self, assignment_id, submitted_at=None):
        self.assignments[assignment_id]["status"] = "submitted"
        self.assignments[assignment_id]["submitted_at"] = submitted_at
        self.submitted.append(assignment_id)
        return dict(self.assignments[assignment_id])


from server import ratings, storage  # noqa: E402

if _ORIGINAL_DATA_DIR is None:
    del os.environ["DATA_DIR"]
else:
    os.environ["DATA_DIR"] = _ORIGINAL_DATA_DIR

ESCI = ratings.esci
ITEM_IDS = [it["id"] for it in ESCI.all_items()]
REVERSE_IDS = [it["id"] for it in ESCI.all_items() if it["reverse"]]


@pytest.fixture(autouse=True)
def data_dir(tmp_path, monkeypatch):
    """An empty DATA_DIR per test, with storage and ratings rebound to it."""
    monkeypatch.setenv("DATA_DIR", str(tmp_path / "data"))
    importlib.reload(storage)
    importlib.reload(ratings)
    assert ratings.RATINGS_DIR == storage.DATA_DIR / "ratings"
    return tmp_path / "data"


@pytest.fixture
def fake_raters(data_dir, monkeypatch):
    fr = FakeRaters()
    monkeypatch.setattr(ratings, "raters", fr)
    return fr


def full_scores(value=4, **overrides):
    """A complete rating: every item answered, then whatever the test overrides."""
    scores = {item_id: value for item_id in ITEM_IDS}
    scores.update(overrides)
    return scores


def sid(label):
    """A realistically-shaped session id for a test label.

    s_{epoch}_{6 hex}, because rater_packet.rating_code refuses anything else
    and a test that invents "s_q6" would be exercising a shape the platform
    never mints.
    """
    return "s_1772460300_" + hashlib.sha1(label.encode()).hexdigest()[:6]


def seed_session(label, cohort="study"):
    """A row in the sessions index, which is where ratings reads cohort from."""
    session_id = sid(label)
    ratings._ensure_index()
    with storage._db() as conn:
        conn.execute(
            """INSERT OR REPLACE INTO sessions
               (id, participant_id, scenario, model, started_at, status, dir, run_id, cohort)
               VALUES (?, 'p_1', 'S4A', 'test', 1772460000.0, 'closed', ?, 'r1', ?)""",
            (session_id, f"data/sessions/{session_id}", cohort),
        )
    return session_id


# --------------------------------------------------------------------------
# The item bank the store is validating against
# --------------------------------------------------------------------------

def test_item_bank_is_the_studys_22_items():
    assert len(ITEM_IDS) == 22
    assert REVERSE_IDS == ["ESCI-15", "ESCI-24", "ESCI-11"] or set(REVERSE_IDS) == {
        "ESCI-11", "ESCI-15", "ESCI-24"
    }


def test_item_bank_version_is_stable_and_short():
    assert ratings.item_bank_version() == ratings.item_bank_version()
    assert len(ratings.item_bank_version()) == 12


# --------------------------------------------------------------------------
# submit
# --------------------------------------------------------------------------

def test_submit_stores_a_complete_rating(fake_raters):
    seed_session("s_submit_1")
    aid = fake_raters.mint(sid("s_submit_1"), "rater_a", construct="teamwork")

    out = ratings.submit(aid, "rater_a", full_scores(4),
                         {"better": "named the interruption", "notable": "calm"}, 480)

    assert out["ok"] is True
    assert out["version"] == 1
    assert out["session_id"] == sid("s_submit_1")
    assert out["construct"] == "teamwork"
    assert out["cohort"] == "study"
    assert out["seconds"] == 480.0
    assert out["source"] == ratings.SOURCE_CONSOLE
    assert out["item_bank_version"] == ratings.item_bank_version()
    assert out["n_answered"] == 22 and out["n_na"] == 0
    assert out["rating_id"].startswith("rg_")

    path = ratings.RATINGS_DIR / aid / "v1.json"
    assert path.exists()
    on_disk = json.loads(path.read_text(encoding="utf-8"))
    assert on_disk["scores"] == full_scores(4)


def test_submit_flips_the_assignment_through_raters(fake_raters):
    seed_session("s_flip")
    aid = fake_raters.mint(sid("s_flip"), "rater_a")
    out = ratings.submit(aid, "rater_a", full_scores(3), {}, 300)
    assert out["assignment_status_synced"] is True
    assert fake_raters.assignments[aid]["status"] == "submitted"
    assert fake_raters.submitted == [aid]


def test_reverse_scoring_is_applied_to_values_and_raw_is_kept(fake_raters):
    seed_session("s_rev")
    aid = fake_raters.mint(sid("s_rev"), "rater_a")
    out = ratings.submit(aid, "rater_a", full_scores(5), {}, 400)
    for item_id in ITEM_IDS:
        assert out["scores"][item_id] == 5, "the rater's own answer is kept verbatim"
        assert out["values"][item_id] == (1 if item_id in REVERSE_IDS else 5)


def test_na_is_stored_as_null_never_coerced_to_a_number(fake_raters):
    seed_session("s_na")
    aid = fake_raters.mint(sid("s_na"), "rater_a")
    scores = full_scores(4, **{"ESCI-03": None, "ESCI-49": None})

    out = ratings.submit(aid, "rater_a", scores, {}, 400)

    assert out["scores"]["ESCI-03"] is None
    assert out["values"]["ESCI-03"] is None
    assert out["n_answered"] == 20 and out["n_na"] == 2
    raw = (ratings.RATINGS_DIR / aid / "v1.json").read_text(encoding="utf-8")
    assert '"ESCI-03": null' in raw


def test_the_completeness_backstop_does_not_repeat_what_validate_said(fake_raters):
    """One message per problem: a rater is reading these on the form."""
    seed_session("s_dup_msg")
    aid = fake_raters.mint(sid("s_dup_msg"), "rater_a")
    scores = full_scores(4)
    del scores["ESCI-14"]
    with pytest.raises(ratings.InvalidRating) as exc:
        ratings.submit(aid, "rater_a", scores, {}, 200)
    assert sum(1 for p in exc.value.problems if "ESCI-14" in p) == 1


def test_a_partial_rating_is_refused_and_nothing_is_written(fake_raters):
    seed_session("s_partial")
    aid = fake_raters.mint(sid("s_partial"), "rater_a")
    scores = full_scores(4)
    del scores["ESCI-14"]

    with pytest.raises(ratings.InvalidRating) as exc:
        ratings.submit(aid, "rater_a", scores, {}, 200)

    assert any("ESCI-14" in p for p in exc.value.problems)
    assert ratings.get_rating(aid) is None
    assert not (ratings.RATINGS_DIR / aid).exists()
    assert fake_raters.assignments[aid]["status"] == "pending"


def test_out_of_range_and_unknown_items_are_refused(fake_raters):
    seed_session("s_range")
    aid = fake_raters.mint(sid("s_range"), "rater_a")

    with pytest.raises(ratings.InvalidRating):
        ratings.submit(aid, "rater_a", full_scores(4, **{"ESCI-08": 7}), {}, 200)
    with pytest.raises(ratings.InvalidRating):
        ratings.submit(aid, "rater_a", full_scores(4, **{"ESCI-08": 0}), {}, 200)
    with pytest.raises(ratings.InvalidRating) as exc:
        ratings.submit(aid, "rater_a", full_scores(4, **{"ESCI-99": 3}), {}, 200)
    assert any("ESCI-99" in p for p in exc.value.problems)
    assert ratings.get_rating(aid) is None


def test_unscorable_text_is_refused_rather_than_guessed(fake_raters):
    seed_session("s_text")
    aid = fake_raters.mint(sid("s_text"), "rater_a")
    with pytest.raises(ratings.InvalidRating):
        ratings.submit(aid, "rater_a", full_scores(4, **{"ESCI-08": "often"}), {}, 200)


def test_scores_must_be_an_object(fake_raters):
    seed_session("s_obj")
    aid = fake_raters.mint(sid("s_obj"), "rater_a")
    with pytest.raises(ratings.InvalidRating):
        ratings.submit(aid, "rater_a", [4] * 22, {}, 100)


def test_unknown_assignment_and_another_raters_assignment_are_indistinguishable(fake_raters):
    seed_session("s_scope")
    aid = fake_raters.mint(sid("s_scope"), "rater_a")

    with pytest.raises(ratings.UnknownAssignment) as missing:
        ratings.submit("as_ffffffffffff", "rater_b", full_scores(), {}, 100)
    with pytest.raises(ratings.UnknownAssignment) as not_yours:
        ratings.submit(aid, "rater_b", full_scores(), {}, 100)

    # Same exception, same shape of message: a rater must not be able to tell a
    # real assignment they do not own from one that does not exist.
    assert "no such assignment" in str(missing.value)
    assert "no such assignment" in str(not_yours.value)
    assert "rater_a" not in str(not_yours.value)
    assert ratings.get_rating(aid) is None


def test_a_traversal_shaped_assignment_id_never_reaches_the_filesystem(fake_raters):
    for bad in ("../../etc/passwd", "as_../../x", "as_ZZZZZZZZZZZZ", "", None):
        with pytest.raises(ratings.UnknownAssignment):
            ratings.submit(bad, "rater_a", full_scores(), {}, 100)
        assert ratings.get_rating(bad) is None
        assert ratings.rating_versions(bad) == []


def test_seconds_is_recorded_and_a_negative_duration_is_refused(fake_raters):
    seed_session("s_secs")
    aid = fake_raters.mint(sid("s_secs"), "rater_a")
    with pytest.raises(ratings.InvalidRating):
        ratings.submit(aid, "rater_a", full_scores(), {}, -1)
    with pytest.raises(ratings.InvalidRating):
        ratings.submit(aid, "rater_a", full_scores(), {}, "eleven")

    out = ratings.submit(aid, "rater_a", full_scores(), {}, "612.5")
    assert out["seconds"] == 612.5

    aid2 = fake_raters.mint(sid("s_secs"), "rater_b")
    out2 = ratings.submit(aid2, "rater_b", full_scores(), {}, None)
    assert out2["seconds"] is None
    assert "no_timing" in out2["quality_flags"]


def test_quality_flags_are_advisory_only(fake_raters):
    seed_session("s_flags")
    straight = fake_raters.mint(sid("s_flags"), "rater_a")
    out = ratings.submit(straight, "rater_a", full_scores(3), {}, 900)
    assert "straight_lining" in out["quality_flags"]

    fast = fake_raters.mint(sid("s_flags"), "rater_b")
    varied = full_scores(4, **{"ESCI-08": 2, "ESCI-15": 5, "ESCI-11": 1})
    out = ratings.submit(fast, "rater_b", varied, {}, 10)
    assert "fast" in out["quality_flags"] and "straight_lining" not in out["quality_flags"]

    blank = fake_raters.mint(sid("s_flags"), "rater_c")
    out = ratings.submit(blank, "rater_c", {i: None for i in ITEM_IDS}, {}, 900)
    # An all-N/A rating is a legitimate answer ("nothing here to judge") and is
    # stored, flagged, not refused.
    assert out["quality_flags"] == ["all_na"]
    assert out["n_answered"] == 0 and out["n_na"] == 22


def test_open_ended_is_kept_capped_and_the_cap_is_recorded(fake_raters):
    seed_session("s_text2")
    aid = fake_raters.mint(sid("s_text2"), "rater_a")
    long_text = "x" * (ratings.MAX_OPEN_ENDED_CHARS + 500)
    out = ratings.submit(aid, "rater_a", full_scores(), {
        "better": long_text, "notable": "held the floor", "third_prompt": "kept",
    }, 400)
    assert len(out["open_ended"]["better"]) == ratings.MAX_OPEN_ENDED_CHARS
    assert out["open_ended"]["_truncated"] == ["better"]
    assert out["open_ended"]["notable"] == "held the floor"
    assert out["open_ended"]["third_prompt"] == "kept", "an unexpected prompt is not dropped"


def test_missing_open_ended_defaults_to_empty_prompts(fake_raters):
    seed_session("s_text3")
    aid = fake_raters.mint(sid("s_text3"), "rater_a")
    out = ratings.submit(aid, "rater_a", full_scores(), None, 400)
    assert out["open_ended"] == {"better": "", "notable": ""}


def test_every_stored_rating_carries_the_proprietary_instrument_notice(fake_raters):
    seed_session("s_notice")
    aid = fake_raters.mint(sid("s_notice"), "rater_a")
    out = ratings.submit(aid, "rater_a", full_scores(), {}, 400)
    notice = out["instrument_notice"].lower()
    assert "proprietary" in notice and "licens" in notice
    on_disk = json.loads((ratings.RATINGS_DIR / aid / "v1.json").read_text(encoding="utf-8"))
    assert on_disk["instrument_notice"] == out["instrument_notice"]
    assert all(r["instrument_notice"] for r in ratings.all_ratings())


def test_a_status_flip_that_cannot_happen_is_reported_not_raised(monkeypatch):
    """The rating is the irreplaceable artefact; the flag is derivable."""
    fr = FakeRaters()
    seed_session("s_nosync")
    aid = fr.mint(sid("s_nosync"), "rater_a")
    crippled = types.SimpleNamespace(
        get_assignment=fr.get_assignment,
        assignments_for_rater=fr.assignments_for_rater,
    )  # no mark_submitted / set_assignment_status
    monkeypatch.setattr(ratings, "raters", crippled)

    out = ratings.submit(aid, "rater_a", full_scores(), {}, 300)
    assert out["ok"] is True
    assert out["assignment_status_synced"] is False
    assert ratings.get_rating(aid)["version"] == 1


def test_the_other_status_spelling_is_accepted(monkeypatch):
    fr = FakeRaters()
    seed_session("s_sync2")
    aid = fr.mint(sid("s_sync2"), "rater_a")
    seen = []
    alt = types.SimpleNamespace(
        get_assignment=fr.get_assignment,
        assignments_for_rater=fr.assignments_for_rater,
        set_assignment_status=lambda a, s: seen.append((a, s)),
    )
    monkeypatch.setattr(ratings, "raters", alt)
    out = ratings.submit(aid, "rater_a", full_scores(), {}, 300)
    assert out["assignment_status_synced"] is True
    assert seen == [(aid, "submitted")]


# --------------------------------------------------------------------------
# Immutability and versions
# --------------------------------------------------------------------------

def test_an_amendment_appends_a_version_and_leaves_the_original_byte_identical(fake_raters):
    seed_session("s_amend")
    aid = fake_raters.mint(sid("s_amend"), "rater_a")
    ratings.submit(aid, "rater_a", full_scores(2), {"better": "first pass"}, 300)
    v1_before = (ratings.RATINGS_DIR / aid / "v1.json").read_bytes()

    amended = ratings.submit(aid, "rater_a", full_scores(5),
                             {"better": "rewatched the video"}, 700)

    assert amended["version"] == 2
    assert (ratings.RATINGS_DIR / aid / "v1.json").read_bytes() == v1_before
    assert amended["amends"] == json.loads(v1_before)["rating_id"]
    assert ratings.get_rating(aid)["version"] == 2
    assert ratings.get_rating(aid)["scores"]["ESCI-08"] == 5

    chain = ratings.rating_versions(aid)
    assert [r["version"] for r in chain] == [1, 2]
    assert [r["is_current"] for r in chain] == [False, True]
    assert chain[0]["scores"]["ESCI-08"] == 2


def test_a_second_rater_cannot_amend_another_raters_rating(fake_raters):
    seed_session("s_amend2")
    aid = fake_raters.mint(sid("s_amend2"), "rater_a")
    ratings.submit(aid, "rater_a", full_scores(2), {}, 300)
    with pytest.raises(ratings.UnknownAssignment):
        ratings.submit(aid, "rater_b", full_scores(5), {}, 300)
    assert len(ratings.rating_versions(aid)) == 1


def test_a_lost_index_never_overwrites_a_rating_on_disk(fake_raters):
    """The files are the record; index.db is an index and can be rebuilt."""
    seed_session("s_rebuild")
    aid = fake_raters.mint(sid("s_rebuild"), "rater_a")
    ratings.submit(aid, "rater_a", full_scores(1), {}, 300)
    v1_before = (ratings.RATINGS_DIR / aid / "v1.json").read_bytes()

    with storage._db() as conn:
        conn.execute("DELETE FROM ratings WHERE assignment_id = ?", (aid,))

    out = ratings.submit(aid, "rater_a", full_scores(4), {}, 300)
    assert out["version"] == 2
    assert (ratings.RATINGS_DIR / aid / "v1.json").read_bytes() == v1_before


# --------------------------------------------------------------------------
# Reads
# --------------------------------------------------------------------------

def test_ratings_for_encounter_returns_one_current_rating_per_rater(fake_raters):
    seed_session("s_enc")
    seed_session("s_other")
    a1 = fake_raters.mint(sid("s_enc"), "rater_a")
    a2 = fake_raters.mint(sid("s_enc"), "rater_b")
    a3 = fake_raters.mint(sid("s_enc"), "rater_c")
    other = fake_raters.mint(sid("s_other"), "rater_a")
    for aid, rid, v in ((a1, "rater_a", 3), (a2, "rater_b", 4), (a3, "rater_c", 2)):
        ratings.submit(aid, rid, full_scores(v), {}, 300)
    ratings.submit(other, "rater_a", full_scores(5), {}, 300)
    ratings.submit(a2, "rater_b", full_scores(5), {}, 900)  # amendment

    got = ratings.ratings_for_encounter(sid("s_enc"))
    assert sorted(r["rater_id"] for r in got) == ["rater_a", "rater_b", "rater_c"]
    assert len(got) == 3, "an amendment must not add a fourth rater to the ICC"
    by_rater = {r["rater_id"]: r for r in got}
    assert by_rater["rater_b"]["version"] == 2
    assert by_rater["rater_b"]["scores"]["ESCI-08"] == 5
    assert ratings.ratings_for_encounter("s_nothing_here") == []
    assert ratings.ratings_for_encounter("") == []


def test_all_ratings_excludes_internal_cohort_encounters(fake_raters):
    seed_session("s_study_c", cohort="study")
    seed_session("s_internal_c", cohort="internal")
    seed_session("s_orphan_c", cohort=None)
    study = fake_raters.mint(sid("s_study_c"), "rater_a")
    internal = fake_raters.mint(sid("s_internal_c"), "rater_a")
    orphan = fake_raters.mint(sid("s_orphan_c"), "rater_a")
    for aid in (study, internal, orphan):
        ratings.submit(aid, "rater_a", full_scores(4), {}, 300)

    study_only = {r["assignment_id"] for r in ratings.all_ratings(cohort="study")}
    assert study in study_only
    assert internal not in study_only and orphan not in study_only

    internal_only = [r["assignment_id"] for r in ratings.all_ratings(cohort="internal")]
    assert internal_only == [internal]

    everything = {r["assignment_id"] for r in ratings.all_ratings()}
    assert {study, internal, orphan} <= everything


def test_get_rating_is_none_before_anything_is_submitted(fake_raters):
    aid = fake_raters.mint(sid("s_none"), "rater_a")
    assert ratings.get_rating(aid) is None
    assert ratings.rating_versions(aid) == []


# --------------------------------------------------------------------------
# Qualtrics ingest
# --------------------------------------------------------------------------

def qualtrics_row(assignment_id, rater_id, value="4", **extra):
    row = {str(it["number"]): value for it in ESCI.all_items()}
    row.update({
        "assignment_id": assignment_id,
        "rater_id": rater_id,
        "Duration (in seconds)": "455",
        "better": "should have named the interruption",
        "notable": "let the quiet one finish",
    })
    row.update(extra)
    return row


def test_import_qualtrics_takes_a_well_formed_export(fake_raters):
    seed_session("s_q1")
    aid = fake_raters.mint(sid("s_q1"), "rater_a")
    report = ratings.import_qualtrics([qualtrics_row(aid, "rater_a")])

    assert report["rows"] == 1 and report["accepted"] == 1 and report["rejected"] == 0
    assert report["errors"] == []
    assert "proprietary" in report["instrument_notice"].lower()

    stored = ratings.get_rating(aid)
    assert stored["source"] == ratings.SOURCE_QUALTRICS
    assert stored["seconds"] == 455.0
    assert stored["scores"]["ESCI-08"] == 4, "string answers are coerced, once"
    assert stored["values"]["ESCI-15"] == 2, "reverse scoring survives the import path"
    assert stored["open_ended"]["notable"] == "let the quiet one finish"
    assert fake_raters.assignments[aid]["status"] == "submitted"


def test_import_qualtrics_reads_the_na_spellings_a_survey_produces(fake_raters):
    seed_session("s_q_na")
    aid = fake_raters.mint(sid("s_q_na"), "rater_a")
    row = qualtrics_row(aid, "rater_a")
    row["3"] = ""
    row["49"] = "N/A"
    row["20"] = "-99"
    report = ratings.import_qualtrics([row])

    assert report["accepted"] == 1
    stored = ratings.get_rating(aid)
    assert stored["scores"]["ESCI-03"] is None
    assert stored["scores"]["ESCI-49"] is None
    assert stored["scores"]["ESCI-20"] is None
    assert stored["n_na"] == 3


def test_import_qualtrics_refuses_a_row_it_cannot_attribute(fake_raters):
    seed_session("s_q2")
    mine = fake_raters.mint(sid("s_q2"), "rater_a")
    unattributed = qualtrics_row(mine, "rater_a")
    del unattributed["assignment_id"]
    del unattributed["rater_id"]

    report = ratings.import_qualtrics([
        unattributed,
        qualtrics_row("as_ffffffffffff", "rater_a"),   # no such assignment
        qualtrics_row("not-an-id", "rater_a"),         # not even the right shape
        qualtrics_row(mine, "rater_b"),                # real row, wrong rater
        "a bare string, not a row",
    ])

    assert report["accepted"] == 0
    assert report["rejected"] == 5
    assert [e["row"] for e in report["errors"]] == [0, 1, 2, 3, 4]
    assert all(e["reason"] for e in report["errors"])
    assert "no assignment_id" in report["errors"][0]["reason"]
    assert "no such assignment" in report["errors"][1]["reason"]
    assert "does not belong" in report["errors"][3]["reason"]
    assert ratings.get_rating(mine) is None
    assert ratings.all_ratings() is not None


def test_import_qualtrics_holds_rows_to_the_same_completeness_rule(fake_raters):
    seed_session("s_q3")
    aid = fake_raters.mint(sid("s_q3"), "rater_a")
    row = qualtrics_row(aid, "rater_a")
    del row["14"]
    del row["26"]

    report = ratings.import_qualtrics([row])

    assert report["accepted"] == 0 and report["rejected"] == 1
    reason = report["errors"][0]["reason"]
    assert "ESCI-14" in reason and "ESCI-26" in reason
    assert report["errors"][0]["assignment_id"] == aid
    assert ratings.get_rating(aid) is None


def test_reimporting_the_same_export_is_a_no_op(fake_raters):
    seed_session("s_q4")
    aid = fake_raters.mint(sid("s_q4"), "rater_a")
    rows = [qualtrics_row(aid, "rater_a")]

    first = ratings.import_qualtrics(rows)
    second = ratings.import_qualtrics(rows)

    assert first["accepted"] == 1 and first["skipped_duplicate"] == 0
    assert second["accepted"] == 0 and second["skipped_duplicate"] == 1
    assert len(ratings.rating_versions(aid)) == 1, "a re-import must not manufacture amendments"

    corrected = qualtrics_row(aid, "rater_a", value="2")
    third = ratings.import_qualtrics([corrected])
    assert third["accepted"] == 1 and third["amended"] == 1
    assert [r["version"] for r in ratings.rating_versions(aid)] == [1, 2]


def test_import_qualtrics_accepts_a_caller_supplied_mapping_and_nested_values(fake_raters):
    seed_session("s_q5")
    aid = fake_raters.mint(sid("s_q5"), "rater_a")
    mapping = {f"QID{it['number']}_1": it["id"] for it in ESCI.all_items()}
    mapping.update({
        "RaterAssignment": "assignment_id",
        "RaterID": "rater_id",
        "duration": "seconds",
        "Q_better": "better",
        "Q_notable": "notable",
    })
    row = {
        "responseId": "R_abc",
        "values": {f"QID{it['number']}_1": 5 for it in ESCI.all_items()},
        "labels": {"junk": "ignored"},
        "RaterAssignment": aid,
        "RaterID": "rater_a",
        "duration": 501,
        "Q_better": "b",
        "Q_notable": "n",
    }

    report = ratings.import_qualtrics([row], mapping=mapping)

    assert report["accepted"] == 1, report["errors"]
    assert "responseId" in report["unmapped_columns"]
    stored = ratings.get_rating(aid)
    assert stored["scores"]["ESCI-61"] == 5
    assert stored["seconds"] == 501.0
    assert stored["open_ended"] == {"better": "b", "notable": "n"}


def test_import_qualtrics_resolves_a_row_by_its_blinded_rating_code(fake_raters):
    """The survey shows the rater the opaque code, never the session id."""
    from server import rater_packet

    session_id = seed_session("s_q6")
    aid = fake_raters.mint(sid("s_q6"), "rater_a")

    row = qualtrics_row(aid, "rater_a")
    del row["assignment_id"]
    row["rating_code"] = rater_packet.rating_code(session_id)

    report = ratings.import_qualtrics([row])
    assert report["accepted"] == 1, report["errors"]
    assert ratings.get_rating(aid)["assignment_id"] == aid

    # A code that resolves to none of this rater's assignments is refused, so a
    # rater cannot post a rating onto an encounter they were never given.
    other = fake_raters.mint(sid("s_q6"), "rater_b")
    stray = qualtrics_row(other, "rater_b")
    del stray["assignment_id"]
    stray["rating_code"] = "RC-0000000000"
    bad = ratings.import_qualtrics([stray])
    assert bad["rejected"] == 1 and "matches none" in bad["errors"][0]["reason"]
    assert ratings.get_rating(other) is None


def test_import_qualtrics_on_an_empty_export(fake_raters):
    report = ratings.import_qualtrics([])
    assert report["rows"] == 0 and report["accepted"] == 0 and report["rejected"] == 0


def test_an_item_named_by_its_number_is_stored_under_its_canonical_id(fake_raters):
    """One spelling in the store, whatever the console or survey sent."""
    seed_session("s_canon")
    aid = fake_raters.mint(sid("s_canon"), "rater_a")
    by_number = {str(it["number"]): 4 for it in ESCI.all_items()}

    out = ratings.submit(aid, "rater_a", by_number, {}, 300)

    assert set(out["scores"]) == set(ITEM_IDS)
    assert set(out["values"]) == set(ITEM_IDS)
    assert out["n_answered"] == 22 and out["n_na"] == 0


def test_the_same_item_rated_twice_under_two_spellings_is_refused(fake_raters):
    """A contradiction, not an overwrite: the rater has to say which they meant."""
    seed_session("s_dupe")
    aid = fake_raters.mint(sid("s_dupe"), "rater_a")
    scores = full_scores(4)
    scores["8"] = 1  # ESCI-08 is already in there, rated 4

    with pytest.raises(ratings.InvalidRating) as exc:
        ratings.submit(aid, "rater_a", scores, {}, 300)

    assert any("more than once" in p for p in exc.value.problems)
    assert ratings.get_rating(aid) is None


def test_the_rating_and_the_assignment_agree_on_when_it_was_submitted(fake_raters):
    seed_session("s_when")
    aid = fake_raters.mint(sid("s_when"), "rater_a")
    out = ratings.submit(aid, "rater_a", full_scores(), {}, 300)
    assert fake_raters.assignments[aid]["submitted_at"] == out["submitted_at"]


def test_a_qualtrics_label_never_shadows_the_coded_answer(fake_raters):
    """An export carries both 4 and "Often" under the same key; store the 4."""
    session_id = seed_session("s_labels")
    aid = fake_raters.mint(session_id, "rater_a")
    row = {
        "responseId": "R_labels",
        "labels": {str(it["number"]): "Often" for it in ESCI.all_items()},
        "values": {str(it["number"]): 4 for it in ESCI.all_items()},
        "duration_s": 470,
        "assignment_id": aid,
        "rater_id": "rater_a",
    }

    report = ratings.import_qualtrics([row])

    assert report["accepted"] == 1, report["errors"]
    stored = ratings.get_rating(aid)
    assert stored["scores"]["ESCI-08"] == 4
    assert stored["seconds"] == 470.0, "server.qualtrics._flatten's duration_s is read"


def test_default_mapping_covers_the_spellings_a_survey_produces():
    mapping = ratings.default_mapping()
    for spelling in ("8", "q8", "item_8", "esci-08", "esci_08"):
        assert mapping[spelling] == "ESCI-08"
    assert mapping["duration (in seconds)"] == "seconds"
    assert mapping["assignment_id"] == "assignment_id"
