"""The two-survey join (Study 1 plan, 1.6): Survey 1 ↔ run ↔ Survey 2."""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def _resp(rid, **values):
    return {"responseId": rid, "values": {"finished": 1, "recordedDate": "2026-09-17", **values}}


@pytest.fixture
def two_runs(tmp_path, monkeypatch):
    from server import runs
    runs_dir = tmp_path / "runs"
    runs_dir.mkdir()
    monkeypatch.setattr(runs, "RUNS_DIR", runs_dir)
    for rid, qid, pid in (("r_1", "R_a", "P1"), ("r_2", "R_b", "P2")):
        (runs_dir / f"{rid}.json").write_text(json.dumps({
            "run_id": rid, "qualtrics_id": qid, "participant_id": pid,
            "cohort": "study", "index": 4, "scenarios": [1, 2, 3, 4], "completed": [],
        }), encoding="utf-8")
    return runs


def test_survey_two_is_joined_on_the_run_id_the_app_appended(two_runs):
    from server import qualtrics
    codes = {rid: two_runs.completion_code({"run_id": rid, "index": 4, "scenarios": [1, 2, 3, 4]}) for rid in ("r_1", "r_2")}
    s1 = [_resp("R_a"), _resp("R_b")]
    s2 = [_resp("R_x", run="r_1", code=codes["r_1"]),   # matched on run
          _resp("R_y", code=codes["r_2"]),               # run field did not pipe: code
          _resp("R_z", run="r_nobody")]                  # nobody's
    rows = qualtrics.join_two(s1, s2)
    real = [r for r in rows if "response_id" in r]
    assert [r["survey2"]["response_id"] for r in real] == ["R_x", "R_y"]
    assert [r["survey2"]["matched_by"] for r in real] == ["run", "code"]
    assert rows[-1] == {"orphans_survey2": ["R_z"]}


def test_a_run_with_no_survey_two_is_still_a_row(two_runs):
    from server import qualtrics
    rows = qualtrics.join_two([_resp("R_a")], [])
    assert rows[0]["run"]["run_id"] == "r_1" and rows[0]["survey2"] is None
    assert not any("orphans_survey2" in r for r in rows)
