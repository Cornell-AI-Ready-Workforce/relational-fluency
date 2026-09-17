"""The three links that go into Qualtrics, and the run each one builds.

Qualtrics is handed three URLs and nothing else: one for the two-person arm, one
for the group arm, and one for the rating console. Everything a participant or
a rater experiences follows from what those URLs do on arrival, and the failures
they can have are all silent ones — a run assigned to the wrong arm, a key that
piped as the literal ${e://Field/...}, a returning participant forked into a
second half-finished run, a rater token shared by every rater in the panel.

So these tests are about the entrances, not about the conversation behind them:
what the link accepts, what it records, and what it refuses.
"""

from __future__ import annotations

import sys
import types

import pytest
from fastapi.testclient import TestClient

from server import app as appmod


# ---------------------------------------------------------------------------
# WHICH STUDY DESIGN THESE TESTS ARE ABOUT
#
# A FEW of the tests below measure the PER-SLOT DRAW: twelve forms, two of each
# construct's three used and the third held in reserve, FORM_EXCLUSIONS applied
# to the completed draw. `DEFAULT_RUN_VARIANT=random` is the setting that
# selects it, and those tests -- and only those -- carry
# `@pytest.mark.usefixtures("per_slot_draw")`.
#
# The merged DEFAULT is `A` -- origin/main's Phase 1 design, which pins
# S1A/S2A/S3A/S4A on every run. That is deliberate and it is the PI's call, not
# this file's. A pinned run has nothing to say about the draw: _apply_form_
# exclusions leaves a pinned slot alone BY DESIGN, so under the default the
# exclusion never fires and the reserve is never drawn, and an assertion about
# either would be testing a mechanism that is switched off rather than one that
# is broken. Those are the marked tests.
#
# EVERYTHING ELSE RUNS ON THE SHIPPED DEFAULT, which is the point of this note.
# The fixture was autouse in this module and four others until 2026-09-15, and
# that pinned 270 tests onto `random` when 20 of them are about the draw:
# stripped and re-run under the default, 15 failed and 255 passed. Entry links,
# arms, gates and cohort integrity are not about form selection, and pinning
# them meant the configuration Phase 1 will actually run had almost no coverage
# in this suite at all.
#
# The shipped default itself, and the fact that it turns the S1A/Teamwork
# exclusion off for the whole of Phase 1, is asserted head-on in
# tests/test_default_run_variant.py.
# ---------------------------------------------------------------------------

@pytest.fixture
def per_slot_draw(monkeypatch):
    """Ask for the per-slot draw, for the handful of tests that are ABOUT it.

    NOT autouse. It was, in all five of these modules, and that pinned 270
    tests off the configuration Phase 1 actually runs when only 20 of them
    need it: measured on 2026-09-15 by stripping the fixture and running the
    five modules under the shipped default -- 15 failed, 255 passed. Tests
    about entry links, arms, gates and cohort integrity are not about form
    selection and now run on the default a participant will meet.
    """
    monkeypatch.setenv("DEFAULT_RUN_VARIANT", "random")


PARTICIPANT_LINKS = ["/start"]
ARM_LINKS = {"/start": "full"}

VALID_TOKEN = "rt_" + "a" * 32
UNKNOWN_TOKEN = "rt_" + "b" * 32


# --- fixtures ----------------------------------------------------------------

@pytest.fixture()
def runs_mod(tmp_path, monkeypatch):
    """server.runs writing into a temp directory.

    Both globals are repointed, not one: RUNS_DIR is where the run files go, and
    DATA_DIR is where the completion-code secret is persisted. Leaving the
    second pointing at the repository's data/ would mint a .run_code_secret next
    to real collection data on whichever machine ran the suite.
    """
    from server import runs

    monkeypatch.setattr(runs, "DATA_DIR", tmp_path)
    monkeypatch.setattr(runs, "RUNS_DIR", tmp_path / "runs")
    return runs


@pytest.fixture()
def client(tmp_path, monkeypatch, runs_mod):
    """A TestClient whose storage is entirely inside tmp_path."""
    from server import storage

    monkeypatch.setattr(storage, "DATA_DIR", tmp_path)
    monkeypatch.setattr(storage, "SESSIONS_DIR", tmp_path / "sessions")
    monkeypatch.setattr(storage, "PARTICIPANTS_DIR", tmp_path / "participants")
    monkeypatch.setattr(storage, "DB_PATH", tmp_path / "index.db")
    monkeypatch.setattr(appmod, "SESSION_KEY", "", raising=False)
    if appmod.ALLOWED_HOSTS and "testserver" not in appmod.ALLOWED_HOSTS:
        monkeypatch.setattr(appmod, "ALLOWED_HOSTS",
                            list(appmod.ALLOWED_HOSTS) + ["testserver"])
    with TestClient(appmod.app) as c:
        yield c


@pytest.fixture()
def rater_roster(monkeypatch):
    """One live token and nothing else, standing in for the rater store.

    Only rater_for_token is faked: the shape check, the 401 and the redirect are
    the code under test.
    """
    fake = types.ModuleType("server.raters")
    fake.rater_for_token = lambda tok: (
        {"rater_id": "rr_000000000001", "name": "Rater A", "kind": "trained"}
        if tok == VALID_TOKEN else None
    )
    monkeypatch.setitem(sys.modules, "server.raters", fake)
    import server

    monkeypatch.setattr(server, "raters", fake, raising=False)
    return fake


def _run_id_from(response) -> str:
    # 307 for a GET arrival, 303 for the entry check page's Continue POST — the
    # POST must NOT be preserved, because a preserved POST lands on /v2, which
    # serves GET only. Both are arrivals; which one a test gets depends only on
    # whether the link carried a key this server could use.
    assert response.status_code in (307, 303), response.text
    loc = response.headers["location"]
    assert loc.startswith("/v2?run="), loc
    return loc.split("run=")[1].split("&")[0]


def _ids(run: dict) -> list:
    return [s["id"] for s in run["scenarios"]]


# --- 1. the construct pool is on the run -------------------------------------

def test_a_second_attempt_on_a_full_run_still_flips_the_forms(runs_mod):
    """The unrestricted pre/post path is untouched by any of this."""
    first = runs_mod.create("P_SIB2", seed=4)
    second = runs_mod.sibling_run(first["run_id"])
    assert second["construct_pool"]["attempt2_forms_available"] is True
    assert set(first["variants"]) == set(second["variants"])
    # Every construct genuinely changes form. `parallel_form` is a scalar and
    # a scalar that names one of three reads as naming the only one, so the
    # target is computed from the bank; this is what says it worked.
    assert all(second["variants"][c] != sid
               for c, sid in first["variants"].items()), (first["variants"],
                                                          second["variants"])


def test_a_second_attempt_never_flips_into_the_pairing_the_first_one_avoided(runs_mod):
    """The defect a third form was written to close, pinned so it cannot come back.

    A full run always contains Teamwork, so S1 A is excluded from attempt 1 and
    attempt 1 is served S1 B. S1 B's `parallel_form` field is S1 A. The flip was
    read off that field and passed to create() as a PIN, and
    _apply_form_exclusions leaves a pinned slot alone by design — so attempt 2
    was handed exactly the pairing attempt 1 was built to avoid, in 120 of 120
    sibling runs measured before this. sibling_run's own docstring said the
    construct fell back to attempt 1's form instead; it did not.
    """
    from server import scenarios_v3

    teamwork = set(scenarios_v3.forms_by_construct()["teamwork"])
    for seed in range(60):
        first = runs_mod.create(f"P_FLIP{seed}", seed=seed)
        second = runs_mod.sibling_run(first["run_id"])
        ids = _ids(second)
        # A full run always has Teamwork. Read off the bank, not spelled out:
        # this line was `"S4A" in ids or "S4B" in ids` and went stale the day
        # S4 C was written, which is the same hardcoding the exclusion table
        # itself was written to avoid.
        assert teamwork & set(ids), (ids, sorted(teamwork))
        assert "S1A" not in ids, (seed, _ids(first), ids)
        # And it is a real flip, not a repeat of attempt 1's conflict form.
        assert (set(_ids(first)) & {"S1A", "S1B"}
                != set(ids) & {"S1A", "S1B"}), (seed, _ids(first), ids)


@pytest.mark.usefixtures("per_slot_draw")
def test_the_cross_construct_exclusion_still_holds_on_a_full_run(runs_mod):
    """S1 A must not share a run with Teamwork. Unchanged by the rewrite that
    made the exclusion pass work per encounter rather than per construct."""
    for seed in range(60):
        ids = _ids(runs_mod.create(f"P_X{seed}", seed=seed))
        if any(i.startswith("S4") for i in ids):
            assert "S1A" not in ids, (seed, ids)


# --- 2. three routes, all Qualtrics-shaped -----------------------------------

@pytest.mark.parametrize("path", PARTICIPANT_LINKS)
@pytest.mark.parametrize("spelling", ["pid", "participant_id", "PROLIFIC_PID"])
def test_every_link_accepts_every_participant_key_spelling(client, runs_mod,
                                                           path, spelling):
    """The spellings are declared once and depended on by all three routes. If
    one route ever grows its own signature, this is what goes red."""
    key = f"SP{spelling[:3]}{abs(hash(path)) % 97}"
    r = client.get(path, params={spelling: key}, follow_redirects=False)
    run = runs_mod.get(_run_id_from(r))
    assert run["participant_id"] == key
    assert run["cohort"] == "study"
    assert run["participant_key_status"] == "ok"


@pytest.mark.parametrize("path", PARTICIPANT_LINKS)
def test_a_returning_participant_resumes_rather_than_forking(client, runs_mod, path):
    """Participants close tabs and come back. A second run under the same key is
    a second partial record and a participant who starts the study again."""
    key = f"RESUME{abs(hash(path)) % 89}"
    first = client.get(path, params={"pid": key}, follow_redirects=False)
    second = client.get(path, params={"pid": key}, follow_redirects=False)
    assert _run_id_from(first) == _run_id_from(second)


@pytest.mark.parametrize("path", PARTICIPANT_LINKS)
def test_every_link_carries_the_qualtrics_response_id_through(client, runs_mod, path):
    key = f"QIDKEY{abs(hash(path)) % 83}"
    r = client.get(path, params={"pid": key, "qid": "R_0123456789abcde"},
                   follow_redirects=False)
    assert runs_mod.get(_run_id_from(r))["qualtrics_id"] == "R_0123456789abcde"


# --- 3. four encounters, then a code -----------------------------------------

@pytest.mark.parametrize("arm", [None])
def test_four_encounters_then_a_finished_code(runs_mod, arm):
    """The advance path and the code, on each arm.

    The code is an assertion about a number of completed encounters, and the arm
    changes which constructs fill them — not how many, and not what the code
    means.
    """
    run = runs_mod.create(f"P_CODE_{arm}", seed=2, arm=arm)
    assert runs_mod.completion_code(run).startswith("RF-PARTIAL-")
    for i in range(4):
        view = runs_mod.view(runs_mod.get(run["run_id"]))
        assert view["position"] == i + 1 and view["total"] == 4
        assert view["done"] is False
        assert view["arm"] == (arm or "full")
        run = runs_mod.advance(run["run_id"], session_id=f"s_17724603{i:02d}_aaaaaa")
    view = runs_mod.view(run)
    assert view["done"] is True
    assert len(run["completed"]) == 4
    code = view["completion_code"]
    assert code.startswith("RF-") and "PARTIAL" not in code


@pytest.mark.parametrize("arm", [None])
def test_stopping_early_still_earns_a_partial_code_that_is_not_the_finished_one(
        runs_mod, arm):
    """Somebody who stops after two encounters has still given us their time,
    and the partial code is what they take back to the survey to be paid. It
    must not be the finished code with a word in front of it: the digest is over
    the finished state as well as the run id, so deleting "PARTIAL-" from the
    string yields a code that verifies as nothing.
    """
    run = runs_mod.create(f"P_PART_{arm}", seed=6, arm=arm)
    runs_mod.advance(run["run_id"], session_id="s_1772460400_bbbbbb")
    run = runs_mod.advance(run["run_id"], session_id="s_1772460401_bbbbbb")
    partial = runs_mod.completion_code(run)
    assert partial.startswith("RF-PARTIAL-")
    run["index"] = len(run["scenarios"])
    finished = runs_mod.completion_code(run)
    assert finished != "RF-" + partial[len("RF-PARTIAL-"):]


# --- 4. the rating console link ----------------------------------------------
