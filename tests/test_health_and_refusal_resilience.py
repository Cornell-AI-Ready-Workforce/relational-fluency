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


# --- a refusal may not stutter the encounters that still work ------------------

# --- and the page the participant reads --------------------------------------

def _v2() -> str:
    return (appmod.STATIC_DIR / "v2.html").read_text(encoding="utf-8")

