"""The study-wide survey code (SURVEY_COMPLETION_CODE).

Qualtrics checks one fixed code that participants enter after the app. The
page must show it only on a run that is finished: a withdrawal or a half-done
run keeps its own RF-PARTIAL- code, which does not pass the survey's check. The
value is a secret because the repo is public, so it must not leak through the
unauthenticated config endpoint either. Exports keep the per-run HMAC code,
which stays verifiable.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tests"))

from test_links import client, runs_mod  # noqa: E402,F401  (fixtures)

CODE = "QX-TEST-7431"


def _finish(runs, run):
    for i in range(len(run["scenarios"])):
        run = runs.advance(run["run_id"], session_id=f"s_17724603{i:02d}_cccccc")
    return run


def test_a_finished_run_shows_the_survey_code(runs_mod, monkeypatch):
    monkeypatch.setenv("SURVEY_COMPLETION_CODE", f"  {CODE}\n")
    run = _finish(runs_mod, runs_mod.create("P_SURVEY_DONE", seed=3))
    view = runs_mod.view(run)
    assert view["done"] is True
    assert view["completion_code"] == CODE
    # The export's per-run code is untouched and still the verifiable one.
    own = runs_mod.completion_code(run)
    assert own.startswith("RF-") and own != CODE


def test_an_unfinished_run_never_shows_it(runs_mod, monkeypatch):
    monkeypatch.setenv("SURVEY_COMPLETION_CODE", CODE)
    run = runs_mod.create("P_SURVEY_HALF", seed=4)
    run = runs_mod.advance(run["run_id"], session_id="s_1772460500_dddddd")
    code = runs_mod.view(run)["completion_code"]
    assert code.startswith("RF-PARTIAL-") and code != CODE


@pytest.mark.parametrize("value", [None, "", "   "])
def test_unset_keeps_the_per_run_code(runs_mod, monkeypatch, value):
    if value is None:
        monkeypatch.delenv("SURVEY_COMPLETION_CODE", raising=False)
    else:
        monkeypatch.setenv("SURVEY_COMPLETION_CODE", value)
    run = _finish(runs_mod, runs_mod.create("P_SURVEY_UNSET", seed=5))
    code = runs_mod.view(run)["completion_code"]
    assert code == runs_mod.completion_code(run)
    assert code.startswith("RF-") and "PARTIAL" not in code


def test_the_api_serves_it_only_with_the_finished_run(client, runs_mod, monkeypatch):
    monkeypatch.setenv("SURVEY_COMPLETION_CODE", CODE)
    run = runs_mod.create("P_SURVEY_API", seed=6)
    assert CODE not in client.get("/api/run/config").text
    assert CODE not in client.get(f"/api/run/{run['run_id']}").text
    _finish(runs_mod, run)
    assert client.get(f"/api/run/{run['run_id']}").json()["completion_code"] == CODE
