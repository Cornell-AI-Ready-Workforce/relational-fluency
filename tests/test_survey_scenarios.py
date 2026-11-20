"""GET /api/survey/scenarios: the Phase 1 survey's rf_scenario_1..4.

The run's order is counterbalanced, so the survey cannot assume conversation n
was Sn. The page after the code box asks the app with its own ResponseID.
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tests"))

from test_links import client, runs_mod  # noqa: E402,F401  (fixtures)

QID = "R_1abcDEF234ghiJK"


def test_returns_the_runs_order_not_s1_to_s4(client, runs_mod):
    run = runs_mod.create("P_SURVEY_ORDER", qualtrics_id=QID, order_row=3)
    run = runs_mod.advance(run["run_id"], session_id="s_1772460600_eeeeee")
    r = client.get("/api/survey/scenarios", params={"qid": QID})
    assert r.status_code == 200
    body = r.json()
    want = [sc["id"] for sc in run["scenarios"]]
    assert [body[f"rf_scenario_{i}"] for i in range(1, 5)] == want
    assert want[0].startswith("S4")          # row 3 starts with teamwork
    assert body["rf_completed"] == 1
    # Only scenario ids and a count: nothing about the participant.
    assert set(body) == {"rf_scenario_1", "rf_scenario_2", "rf_scenario_3",
                         "rf_scenario_4", "rf_completed"}


def test_unknown_or_template_response_id_is_404(client, runs_mod):
    runs_mod.create("P_SURVEY_OTHER", qualtrics_id=QID)
    for qid in ("R_nobodyHasThis1", "${e://Field/ResponseID}", ""):
        assert client.get("/api/survey/scenarios", params={"qid": qid}).status_code == 404


def test_readable_from_qualtrics_only(client, runs_mod):
    runs_mod.create("P_SURVEY_CORS", qualtrics_id=QID)
    ok = client.get("/api/survey/scenarios", params={"qid": QID},
                    headers={"Origin": "https://cornell.yul1.qualtrics.com"})
    assert ok.headers.get("access-control-allow-origin") == "https://cornell.yul1.qualtrics.com"
    for origin in ("https://evil.example", "http://cornell.qualtrics.com",
                   "https://qualtrics.com.evil.example"):
        r = client.get("/api/survey/scenarios", params={"qid": QID},
                       headers={"Origin": origin})
        assert r.headers.get("access-control-allow-origin") != origin
