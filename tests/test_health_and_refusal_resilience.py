"""What the new diagnostics may cost when they themselves go wrong.

Two surfaces gained a diagnosis in the last round, and a diagnosis is code that
runs on the hot path of the thing it is diagnosing.

  * /health gained a `config` block computed per request. _health_status's own
    docstring spends a paragraph arguing that this route must keep answering
    200 when the deployment is misconfigured, because the target group matches
    "200" with unhealthy_threshold 3 over a 30 s interval, the service sets
    deployment_minimum_healthy_percent = 100, and nothing sets a grace period or
    a deployment circuit breaker — so a non-200 here replaces every task about
    ninety seconds after it boots, forever, with no rollback. An uncaught
    exception is a 500, and a 500 is a non-200. The block added two lines below
    that argument could have reintroduced the outage the argument exists to
    prevent, through a different door: a wave that records nothing becoming a
    site that serves nothing.

  * POST /api/consent's refusal gained _why_consent_was_refused, which globs and
    JSON-parses every file in RUNS_DIR — two lines after the same route's own
    comment explaining why the scan beside it was moved to a worker thread,
    because this coroutine shares its event loop with every live encounter's
    audio. On the deployment it was written for, the one with
    UPSTREAM_CONSENT_VERSION unset, it fires for every participant's consent
    POST and again for every press of Try again. Measured at 8.6 ms for one run
    file and 162 ms for four hundred, all of it on the loop.
"""

from __future__ import annotations

import threading

import pytest
from fastapi.testclient import TestClient

from server import app as appmod
from server import storage

VERSION_ENV = storage.UPSTREAM_CONSENT_VERSION_ENV


@pytest.fixture()
def runs_mod(tmp_path, monkeypatch):
    from server import runs

    monkeypatch.setattr(runs, "DATA_DIR", tmp_path)
    monkeypatch.setattr(runs, "RUNS_DIR", tmp_path / "runs")
    return runs


@pytest.fixture()
def store(tmp_path, monkeypatch):
    monkeypatch.setattr(storage, "DATA_DIR", tmp_path)
    monkeypatch.setattr(storage, "SESSIONS_DIR", tmp_path / "sessions")
    monkeypatch.setattr(storage, "PARTICIPANTS_DIR", tmp_path / "participants")
    monkeypatch.setattr(storage, "DB_PATH", tmp_path / "index.db")
    return storage


@pytest.fixture()
def client(monkeypatch, runs_mod, store):
    monkeypatch.setattr(appmod, "SESSION_KEY", "", raising=False)
    if appmod.ALLOWED_HOSTS and "testserver" not in appmod.ALLOWED_HOSTS:
        monkeypatch.setattr(appmod, "ALLOWED_HOSTS",
                            list(appmod.ALLOWED_HOSTS) + ["testserver"])
    with TestClient(appmod.app) as c:
        yield c


# --- /health may not take the service off the load balancer -------------------

@pytest.mark.parametrize("boom", [OSError("rglob failed"), RecursionError(),
                                  MemoryError(), RuntimeError("anything")])
def test_a_broken_config_check_does_not_become_a_crash_loop(client, monkeypatch,
                                                            boom):
    """The failure this prevents is a permanent one: every task replaced every
    ninety seconds because the health route raised."""
    def explode():
        raise boom

    monkeypatch.setattr(appmod, "missing_required_env", explode)
    r = client.get("/health")
    assert r.status_code == 200, r.text
    assert r.json()["active_sessions"] == 0


def test_a_config_check_that_never_ran_is_not_reported_as_ok(client, monkeypatch):
    """The fallback may not be the empty list. Empty means "nothing is missing",
    which is the one sentence this block exists to stop /health saying when it is
    not true — and a check that could not run has proved nothing at all."""
    monkeypatch.setattr(appmod, "_LAST_REQUIRED_ENV", None, raising=False)
    monkeypatch.setattr(appmod, "missing_required_env",
                        lambda: (_ for _ in ()).throw(OSError("no")))
    body = client.get("/health").json()
    assert body["status"] == "degraded"
    assert body["ready"] is False
    assert body["config"]["ok"] is False
    # Named, not invented: every entry is a real required variable.
    assert set(body["config"]["missing_required_env"]) <= set(storage.REQUIRED_ENV)
    assert body["config"]["missing_required_env"]


def test_a_broken_recheck_reports_the_last_answer_it_had(client, monkeypatch):
    """A variable that was missing a moment ago is still missing now. Reporting
    the last computed answer keeps the alarm that is already sounding."""
    monkeypatch.setenv(VERSION_ENV, "cornell-irb-2026-09-v3")
    first = client.get("/health").json()

    monkeypatch.setattr(appmod, "missing_required_env",
                        lambda: (_ for _ in ()).throw(OSError("no")))
    second = client.get("/health").json()
    assert second["config"]["missing_required_env"] == \
        first["config"]["missing_required_env"]
    assert second["status"] == first["status"]


def test_the_config_block_still_tells_the_truth_when_nothing_is_broken(
        client, monkeypatch):
    """The control: the guard must not be a way for the check to stop running."""
    monkeypatch.delenv(VERSION_ENV, raising=False)
    body = client.get("/health").json()
    assert body["status"] == "degraded" and body["ready"] is False
    monkeypatch.setenv(VERSION_ENV, "cornell-irb-2026-09-v3")
    body = client.get("/health").json()
    assert body["status"] == "ok" and body["ready"] is True


# --- a refusal may not stutter the encounters that still work ------------------

def test_the_consent_refusal_diagnosis_runs_off_the_event_loop(client, store,
                                                               monkeypatch):
    """It scans and parses every run file. The loop it would do that on is
    carrying live audio.

    Proved by comparing threads rather than by timing: record_consent is called
    straight from the coroutine, so whatever thread IT sees is the event loop's.
    The diagnosis must not see the same one.
    """
    seen = {}

    def watching_record_consent(pid, version):
        seen["loop"] = threading.get_ident()
        return None

    real = appmod._why_consent_was_refused

    def watching_why(pid):
        seen["diagnosis"] = threading.get_ident()
        return real(pid)

    monkeypatch.setattr(appmod, "record_consent", watching_record_consent)
    monkeypatch.setattr(appmod, "_why_consent_was_refused", watching_why)

    pid = store.create_participant(code="RFLOOP01", consent_given=False,
                                   consent_version="")
    r = client.post("/api/consent", json={"participant_id": pid,
                                          "code": "RFLOOP01",
                                          "consent_given": True,
                                          "consent_source": "qualtrics"})
    assert r.status_code in (404, 409, 503), r.text
    assert seen["diagnosis"] != seen["loop"]


def test_the_refusal_still_says_which_rule_refused_it(client, monkeypatch):
    """The control on the move: running somewhere else must not change the
    answer the operator reads.

    Entered through /start so the record belongs to a run, which is what makes
    the refusal reachable at all — a record belonging to no run takes
    record_consent's fallback-version path and is consented normally.
    """
    monkeypatch.delenv(VERSION_ENV, raising=False)
    entry = client.get("/start", params={"pid": "RFLOOP02"},
                       follow_redirects=False)
    pid = entry.headers["location"].split("participant_id=")[1].split("&")[0]
    r = client.post("/api/consent", json={"participant_id": pid,
                                          "code": "RFLOOP02",
                                          "consent_given": True,
                                          "consent_source": "qualtrics"})
    assert r.status_code in (409, 503), r.text
    body = r.json()
    assert body["reason"] in (appmod.CONSENT_REFUSAL_VERSION_UNSET,
                              appmod.CONSENT_REFUSAL_NO_QID)
    assert len(body["detail"]) > 40


# --- and the page the participant reads --------------------------------------

def _v2() -> str:
    return (appmod.STATIC_DIR / "v2.html").read_text(encoding="utf-8")


def test_the_participant_page_knows_whose_fault_a_missing_survey_id_is():
    """The refusal names it; the one screen a stopped participant reads
    carefully was not told.

    static/v2.html branched only on consent_version_unset to decide whether to
    say "this is a fault in our setup, not anything you have done". A missing
    ?qid= is precisely as much the deployment's fault — an operator's Qualtrics
    redirect — and it fell through to "we could not confirm that you completed
    the consent step in the Cornell survey", which blames the participant for
    something they did do, and then sends them back to the survey tab to open
    the link again, which cannot work: the link is what is broken.
    """
    page = _v2()
    assert appmod.CONSENT_REFUSAL_NO_QID in page
    assert appmod.CONSENT_REFUSAL_VERSION_UNSET in page
    # Both of the deployment's own faults set the flag that swaps the advice.
    flag = page.split("const ourFault", 1)[1].split(";", 1)[0]
    assert appmod.CONSENT_REFUSAL_NO_QID in flag
    assert appmod.CONSENT_REFUSAL_VERSION_UNSET in flag


def test_the_page_still_blames_nobody_it_should_not():
    """The constants and the page must not drift apart again: every reason the
    page branches on has to be one the server can actually send."""
    page = _v2()
    for name in (appmod.CONSENT_REFUSAL_NO_QID,
                 appmod.CONSENT_REFUSAL_VERSION_UNSET):
        assert f"'{name}'" in page
