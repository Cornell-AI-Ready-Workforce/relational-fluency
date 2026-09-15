"""What this deployment says about itself when it cannot record a study.

Three surfaces, one failure. With UPSTREAM_CONSENT_VERSION unset — which is the
state the live task definition is in — every study consent is refused, every
voice socket closes 4403, runs keep accumulating, and the two places anybody
looks both said everything was fine:

  * /health answered 200 with "status": "ok" while its own `config` block said
    ok: false. An uptime check reads the top-level word, so the box looked
    healthy while the wave recorded nothing. That is what made the silent void
    silent.
  * POST /api/consent answered `404 no such participant record` for a record
    sitting on disk — the same sentence for two unrelated operator mistakes
    (the variable unset, or an entry link with no ?qid=), with the truth only in
    a log line nobody reads. The operator went looking for a missing record.

And a fourth thing, quieter than all of them: the consent route echoed
config/consent.yaml's version in its reply while writing the environment's, so
the one field an audit asks about was answered wrongly with confidence.

The status CODE is pinned at 200 throughout this file on purpose.
infra/terraform/alb.tf health-checks /health with matcher "200" and
unhealthy_threshold 3, and ecs.tf sets deployment_minimum_healthy_percent = 100:
a 503 here would take every task out of service 90 seconds after boot and wedge
the deploy that introduced it. The word tells the truth; the code keeps the
lights on.
"""

from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient

from server import app as appmod
from server import storage

VERSION_ENV = storage.UPSTREAM_CONSENT_VERSION_ENV
APPROVED = "cornell-irb-2026-09-v3"

BROWSER = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
           "(KHTML, like Gecko) Chrome/140.0.0.0 Safari/537.36")


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


@pytest.fixture()
def fielded(monkeypatch, client):
    """A deployment that has been told which approved wording the survey shows."""
    monkeypatch.setenv(VERSION_ENV, APPROVED)
    return client


def _arrive(client, key, **params):
    """One participant through the front door. Returns (run_id, record_id)."""
    r = client.get("/start", params=dict({"pid": key}, **params),
                   headers={"User-Agent": BROWSER}, follow_redirects=False)
    assert r.status_code == 307, r.text
    loc = r.headers["location"]
    assert "participant_id=" in loc, loc
    return (loc.split("run=")[1].split("&")[0],
            loc.split("participant_id=")[1].split("&")[0])


# --- /health: the word at the top --------------------------------------------

def test_health_does_not_say_ok_while_it_cannot_record_a_consent(
        client, monkeypatch):
    """The defect, exactly as a monitoring check meets it.

    "status": "ok" over "config": {"ok": false} is not a nuance — it is the one
    line an uptime check reads, saying the opposite of the one line that
    mattered.
    """
    monkeypatch.delenv(VERSION_ENV, raising=False)

    r = client.get("/health")
    body = r.json()

    assert body["config"]["ok"] is False
    assert VERSION_ENV in body["config"]["missing_required_env"]
    assert body["status"] != "ok", (
        "/health called itself ok while no study consent could be recorded")
    assert body["status"] == "degraded"
    assert body["ready"] is False


def test_health_says_ok_once_the_deployment_can_actually_record(fielded):
    """The control. A word that is always "degraded" is as useless as one that
    is always "ok", and an operator who fixes the variable has to see it clear."""
    body = fielded.get("/health").json()

    assert body["config"]["ok"] is True
    assert body["config"]["missing_required_env"] == []
    assert body["status"] == "ok"
    assert body["ready"] is True


@pytest.mark.parametrize("value", ["", "   ", "xxx", "[FILL IN: the version]",
                                   "changeme", "TBD"])
def test_a_placeholder_is_not_a_version_and_health_says_so(client, monkeypatch,
                                                           value):
    """`tofu plan` accepts "xxx" and this side treats it as unset, so a
    deployment can be misconfigured while every variable is "set"."""
    monkeypatch.setenv(VERSION_ENV, value)
    body = client.get("/health").json()
    assert body["status"] == "degraded" and body["ready"] is False


def test_the_word_and_the_config_block_can_never_disagree(client, monkeypatch):
    """The property, rather than the two examples above: whatever else /health
    grows, the top-level word is the config block's own answer."""
    for value, expected in ((APPROVED, "ok"), ("", "degraded"),
                            ("protocol-0042-none-of-the-above", "ok")):
        monkeypatch.setenv(VERSION_ENV, value)
        body = client.get("/health").json()
        assert body["status"] == expected, (value, body["status"])
        assert body["status"] == ("ok" if body["config"]["ok"] else "degraded")
        assert body["ready"] is body["config"]["ok"]


def test_health_still_answers_200_when_it_is_degraded(client, monkeypatch):
    """THE HALF THAT COULD HAVE CAUSED AN OUTAGE.

    infra/terraform/alb.tf: path "/health", matcher "200", interval 30,
    unhealthy_threshold 3. ecs.tf: deployment_minimum_healthy_percent = 100. A
    503 on a missing variable would therefore drain every task 90 seconds after
    it booted and wedge the deploy that introduced it — a wave that records
    nothing replaced by a site that serves nothing. Repointing the target group
    at a liveness path is an infrastructure change a person has to make, and
    this build must be safe whether or not they have made it yet.
    """
    monkeypatch.delenv(VERSION_ENV, raising=False)
    r = client.get("/health")
    assert r.status_code == 200
    assert r.json()["status"] == "degraded"


def test_a_broken_gateway_or_bucket_does_not_move_the_word(client, monkeypatch):
    """A transient upstream blip must not read as a deployment that cannot
    record: those are different failures with different fixes, and the blocks
    that carry them are already published separately."""
    monkeypatch.setenv(VERSION_ENV, APPROVED)
    monkeypatch.setattr(appmod, "_PREFLIGHT",
                        {"ok": False, "checked": True, "gateway": "g"})
    monkeypatch.setattr(appmod, "_STORAGE_PREFLIGHT",
                        {"ok": False, "checked": True, "bucket": "b",
                         "region": "r", "readable": False, "writable": False,
                         "error_code": "NoSuchBucket"})
    r = client.get("/health")
    assert r.status_code == 200
    assert r.json()["status"] == "ok"


def test_the_degraded_answer_still_names_nothing_a_stranger_may_not_know(
        client, monkeypatch):
    """/health is the one route reachable from the open internet. The new fields
    are a word and a boolean computed from variable NAMES the repository already
    publishes — nothing in them may widen what a stranger learns."""
    monkeypatch.delenv(VERSION_ENV, raising=False)
    monkeypatch.setattr(appmod, "_STORAGE_PREFLIGHT",
                        {"ok": False, "checked": True,
                         "bucket": "relational-fluency-study-data",
                         "region": "us-east-1", "readable": False,
                         "writable": False, "credentials": False,
                         "detail": "Unable to locate credentials",
                         "error_code": "NoCredentialsError"})
    flat = json.dumps(client.get("/health").json())
    for secret in ("relational-fluency-study-data", "us-east-1",
                   "Unable to locate credentials"):
        assert secret not in flat


# --- POST /api/consent: which of the two mistakes it was ---------------------

def test_a_missing_consent_version_is_named_rather_than_called_a_missing_record(
        client, monkeypatch, store):
    """Operator mistake one. The record exists; the deployment was never told
    which approved text the survey shows."""
    monkeypatch.delenv(VERSION_ENV, raising=False)
    run_id, pid = _arrive(client, "RFCONS01", qid="R_0123456789abcd")
    assert store.get_participant(pid) is not None, "the record is on disk"

    r = client.post("/api/consent", json={"code": "RFCONS01", "run_id": run_id,
                                          "participant_id": pid,
                                          "consent_given": True})

    assert r.status_code == 503, r.text
    body = r.json()
    assert body["reason"] == appmod.CONSENT_REFUSAL_VERSION_UNSET
    assert VERSION_ENV in body["detail"], body["detail"]
    assert "no such participant record" not in body["detail"]
    # And it is still a refusal: nothing was written.
    assert store.get_participant(pid)["consent_given"] is False


def test_an_entry_link_with_no_response_id_is_named_as_that(client, fielded,
                                                            store):
    """Operator mistake two, which used to wear the same sentence. The survey's
    redirect is not passing ?qid=${e://Field/ResponseID}, so there is no
    response to record a consent against."""
    run_id, pid = _arrive(fielded, "RFCONS02")
    assert store.get_participant(pid) is not None

    r = fielded.post("/api/consent", json={"code": "RFCONS02", "run_id": run_id,
                                           "participant_id": pid,
                                           "consent_given": True})

    assert r.status_code == 409, r.text
    body = r.json()
    assert body["reason"] == appmod.CONSENT_REFUSAL_NO_QID
    assert "qid" in body["detail"]
    assert VERSION_ENV not in body["detail"], (
        "named the wrong one of the two mistakes")
    assert store.get_participant(pid)["consent_given"] is False


def test_the_two_mistakes_do_not_share_a_sentence(client, monkeypatch, store):
    """The whole point. Two operators, two different things to go and do."""
    monkeypatch.delenv(VERSION_ENV, raising=False)
    run_a, pid_a = _arrive(client, "RFCONS03", qid="R_0123456789abcd")
    first = client.post("/api/consent", json={"code": "RFCONS03",
                                              "run_id": run_a,
                                              "participant_id": pid_a,
                                              "consent_given": True}).json()

    monkeypatch.setenv(VERSION_ENV, APPROVED)
    run_b, pid_b = _arrive(client, "RFCONS04")
    second = client.post("/api/consent", json={"code": "RFCONS04",
                                               "run_id": run_b,
                                               "participant_id": pid_b,
                                               "consent_given": True}).json()

    assert first["detail"] != second["detail"]
    assert first["reason"] != second["reason"]


def test_a_record_that_does_not_exist_is_told_exactly_what_it_was_before(
        fielded):
    """The line this may not cross. Which record ids EXIST is not a stranger's
    business, so an id that resolves to nothing gets the same 404 it always got
    — the new answers are reachable only where the old 404 already confirmed the
    record was the caller's."""
    r = fielded.post("/api/consent", json={"code": "RFNOBODY",
                                           "participant_id": "p_0000000000_ffffff",
                                           "consent_given": True})
    assert r.status_code == 404
    body = r.json()
    assert body["detail"] == "no such participant record"
    assert body["reason"] == appmod.CONSENT_REFUSAL_NO_RECORD


def test_a_stranger_presenting_a_real_record_learns_nothing_new(client,
                                                                monkeypatch):
    """The ownership refusal comes first and is unchanged, so the diagnosis
    cannot be used as an oracle for "does this id exist"."""
    monkeypatch.delenv(VERSION_ENV, raising=False)
    run_id, pid = _arrive(client, "RFOWNER1", qid="R_0123456789abcd")

    r = client.post("/api/consent", json={"code": "SOMEONE_ELSE",
                                          "participant_id": pid,
                                          "consent_given": True})

    assert r.status_code == 403
    flat = json.dumps(r.json())
    assert VERSION_ENV not in flat and "qid" not in flat


def test_a_legitimate_participant_is_still_consented(fielded, store, runs_mod):
    """THE POSITIVE CONTROL. Everything above is a refusal being made legible,
    and none of it is allowed to cost the person who did everything right."""
    run_id, pid = _arrive(fielded, "RFGOOD01", qid="R_0123456789abcd")

    r = fielded.post("/api/consent", json={"code": "RFGOOD01", "run_id": run_id,
                                           "participant_id": pid,
                                           "consent_given": True})

    assert r.status_code == 200, r.text
    assert r.json()["participant_id"] == pid
    rec = store.get_participant(pid)
    assert rec["consent_given"] is True
    assert rec["consent_source"] == storage.CONSENT_SOURCE_UPSTREAM
    assert rec["consent_reference"] == "R_0123456789abcd"
    assert rec["consent_upstream_verified"] is True


# --- the version the route claims to have written ----------------------------

def test_the_version_in_the_reply_is_the_version_on_the_record(fielded, store):
    """D4. The reply echoed config/consent.yaml's version while the record was
    stamped with UPSTREAM_CONSENT_VERSION — two different strings, and the one
    an auditor is handed was the wrong one. Nothing user-facing reads it, which
    is exactly why nothing ever contradicted it."""
    yaml_version = appmod._load_consent().get("version")
    assert yaml_version and yaml_version != APPROVED, (
        "this test needs the two to differ to mean anything")

    run_id, pid = _arrive(fielded, "RFVER001", qid="R_0123456789abcd")
    r = fielded.post("/api/consent", json={"code": "RFVER001", "run_id": run_id,
                                           "participant_id": pid,
                                           "consent_given": True})

    assert r.status_code == 200, r.text
    written = store.get_participant(pid)["consent_text_version"]
    assert written == APPROVED
    assert r.json()["consent_text_version"] == written
    assert r.json()["consent_text_version"] != yaml_version


def test_the_page_does_not_blame_the_participant_for_our_unset_variable():
    """static/v2.html branches on the reason, so the one refusal that is nobody's
    fault but ours stops being shown as "we could not confirm that YOU completed
    the consent step" over a Back-to-Qualtrics instruction that cannot help.

    Pinned as a source check because the constant is a contract between two
    files: rename it on the server and this page silently falls back to blaming
    the participant again.
    """
    from pathlib import Path

    page = (Path(appmod.STATIC_DIR) / "v2.html").read_text(encoding="utf-8")
    assert f"'{appmod.CONSENT_REFUSAL_VERSION_UNSET}'" in page, (
        "the page no longer recognises the server's word for this refusal")
    assert "not anything you have done" in page


def test_a_record_with_no_run_still_reports_what_it_wrote(fielded, store):
    """The other branch: a consent with no run behind it is recorded as
    direct_api against config/consent.yaml's version, and that is then the true
    answer for that record. The reply reads the record either way rather than
    guessing which branch it took."""
    r = fielded.post("/api/consent", json={"code": "RFDIRECT1",
                                           "consent_given": True})
    assert r.status_code == 200, r.text
    pid = r.json()["participant_id"]
    assert (r.json()["consent_text_version"]
            == store.get_participant(pid)["consent_text_version"])
