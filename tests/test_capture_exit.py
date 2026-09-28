"""Leaving because the microphone will not work is not withdrawing (issue #41).

The capture-failure screen had one way out on a study link, "Stop and leave the
study", and that one is a withdrawal: it stamps every run and record of the
person, closes the study to them, and puts them in the IRB report as somebody
who refused to continue. A participant whose headset would not start is not
that person. POST /api/run/{id}/exit records the fact that actually happened
(`mic_failed` or `camera_failed`, with the browser's own name for the failure)
and changes nothing else, and the analysis export carries it beside
`withdrawn` so the two can be counted apart.

The page half — the notice's button, the card, the survey link carrying the
status — is driven in tests/test_participant_intro_page.py.
"""

from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient

from server import app as appmod

KEY = "test-session-key"
RUN_ID = "abcdef123456"
RECORD = "p_1790365076_2bb22c"


@pytest.fixture()
def runs_mod(tmp_path, monkeypatch):
    from server import runs, storage

    monkeypatch.setattr(runs, "DATA_DIR", tmp_path)
    monkeypatch.setattr(runs, "RUNS_DIR", tmp_path / "runs")
    monkeypatch.setattr(storage, "DATA_DIR", tmp_path)
    monkeypatch.setattr(storage, "SESSIONS_DIR", tmp_path / "sessions")
    monkeypatch.setattr(storage, "PARTICIPANTS_DIR", tmp_path / "participants")
    monkeypatch.setattr(storage, "DB_PATH", tmp_path / "index.db")
    storage.init_storage()
    (tmp_path / "runs").mkdir(parents=True, exist_ok=True)
    runs.save({
        "run_id": RUN_ID, "participant_id": "RF_TEST_1", "participant_record_id": RECORD,
        "cohort": "study", "created_at": 1790365000.0, "index": 1,
        "scenarios": [{"id": "S1A"}, {"id": "S2B"}, {"id": "S3A"}, {"id": "S4B"}],
        "completed": [{"id": "S1A", "session_id": "s_1790365100_aaaaaa"}],
    })
    return runs


@pytest.fixture()
def client(runs_mod, monkeypatch):
    monkeypatch.setattr(appmod, "SESSION_KEY", KEY)
    if appmod.ALLOWED_HOSTS and "testserver" not in appmod.ALLOWED_HOSTS:
        monkeypatch.setattr(appmod, "ALLOWED_HOSTS",
                            list(appmod.ALLOWED_HOSTS) + ["testserver"])
    return TestClient(appmod.app, raise_server_exceptions=False)


def _exit(client, **body):
    body.setdefault("participant_id", RECORD)
    body.setdefault("status", "mic_failed")
    return client.post(f"/api/run/{RUN_ID}/exit", json=body)


def test_a_microphone_that_would_not_start_is_recorded_as_that(client, runs_mod):
    r = _exit(client, capture_kind="denied")
    assert r.status_code == 200, r.text
    assert r.json()["recorded"] is True
    run = runs_mod.get(RUN_ID)
    [line] = run["exits"]
    assert line["status"] == "mic_failed"
    assert line["capture_kind"] == "denied"
    assert line["index"] == 1 and line["completed"] == 1, "where in the run they were is lost"
    assert line["at"] > 0


def test_it_is_not_a_withdrawal_and_the_run_stays_open(client, runs_mod):
    _exit(client, status="camera_failed", capture_kind="camera")
    run = runs_mod.get(RUN_ID)
    assert not run.get("withdrawn"), "a broken camera was recorded as a refusal to continue"
    # The same person on a machine that works carries on where they were.
    assert client.get(f"/api/run/{RUN_ID}").json()["withdrawn"] is None
    after = runs_mod.advance(RUN_ID, "s_1790365200_bbbbbb")
    assert after["index"] == 2
    from server.storage import get_participant
    rec = get_participant(RECORD)
    assert not (rec or {}).get("withdrawn"), "the participant record was stamped"


def test_the_export_tells_an_exit_from_a_withdrawal(client, runs_mod):
    _exit(client)
    rows = {r["run_id"]: r for r in client.get("/api/runs", params={"key": KEY}).json()}
    row = rows[RUN_ID]
    assert row["withdrawn"] is None
    assert [e["status"] for e in row["exits"]] == ["mic_failed"]


def test_only_the_runs_own_participant_may_write_on_it(client, runs_mod):
    r = _exit(client, participant_id="p_1790365355_5567bf")
    assert r.status_code == 403
    assert not runs_mod.get(RUN_ID).get("exits")
    # The researcher key may, as it may stop a run.
    assert client.post(f"/api/run/{RUN_ID}/exit", params={"key": KEY},
                       json={"status": "mic_failed"}).status_code == 200


def test_only_the_two_statuses_and_short_tokens_are_written(client, runs_mod):
    assert _exit(client, status="participant_withdrew").status_code == 400
    assert _exit(client, status="").status_code == 400
    assert client.post("/api/run/ffffffffffff/exit",
                       json={"status": "mic_failed", "participant_id": RECORD}).status_code == 404
    _exit(client, capture_kind="<script>alert(1)</script>", session_id="../../etc/passwd")
    [line] = runs_mod.get(RUN_ID)["exits"]
    assert line["capture_kind"] is None and line["session_id"] is None, line
    _exit(client, capture_kind="unanswered", session_id="s_1790365100_aaaaaa")
    assert runs_mod.get(RUN_ID)["exits"][-1]["session_id"] == "s_1790365100_aaaaaa"


def test_the_list_is_bounded(client, runs_mod):
    for _ in range(runs_mod._EXITS_KEPT + 5):
        runs_mod.note_exit(RUN_ID, "mic_failed")
    assert len(runs_mod.get(RUN_ID)["exits"]) == runs_mod._EXITS_KEPT
    with pytest.raises(ValueError):
        runs_mod.note_exit(RUN_ID, "withdrawn")
    json.dumps(runs_mod.get(RUN_ID))   # still a plain document
