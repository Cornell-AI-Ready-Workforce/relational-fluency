"""The dashboard's tables: what a sim report and a judge's verdict flatten to.

tools/sim/dashboard_data.py turns the sim check's reports into the CSV a QA
dashboard charts. A chart is believed, so the flattening is pinned: which run
is a row, what a scenario the judge did not read looks like (blank, not zero),
and that nothing a person said in a recorded encounter can reach a file that
leaves this repository.
"""
from __future__ import annotations

import csv
import io

from tools.sim import dashboard_data as D

VERDICT = {
    "session_id": "s_1794253712_581349", "scenario": "S4A", "build": "0e27778", "passed": False,
    "rubric": "r", "model": "m", "spec_changed": None,
    "beats": {"y": 6, "p": 3, "n": 1, "x": 2},
    "counts": {"FACT": 1, "ROLE": 0, "SAFE": 0, "RULE": 1, "LOOP": 0, "STOCK": 2},
    "chances": {"met": 4, "of": 6}, "usage": {"calls": 3, "in": 9000, "out": 300},
    "flags": [{"c": "FACT", "i": 20, "agent": "priya", "line": "That's when it peaks.",
               "q": "peaks in the first two weeks", "source": "Support wants Monday the 23rd."},
              {"c": "STOCK", "i": 4, "agent": "chris", "line": "I hear you.", "q": "i hear you",
               "source": None}],
    "disputed": [{"c": "ROLE", "i": 9, "agent": "dan", "line": "Dan sighs.", "q": "Dan sighs",
                  "source": None}],
    "missed": [{"i": 23, "agent": "chris", "trigger": "t9_dates", "line": "That week is out."}],
}
METRICS = {"lines_said": 34, "lines_heard": 34, "lines_answered": 30, "phantom_turns": 0,
           "voice_error": 0, "reply_missing": 0, "triggers_fired": 10,
           "speech_end_to_first_played_s": {"p50": 2.532, "p90": 3.115, "n": 34}}
REPORTS = {
    "old1234": {"build": "old1234", "generated_at": "2026-10-01T10:00:00Z",
                "health": {"realtime_model": "gpt-realtime-2.1", "pipeline_version": "2026-09-30a"},
                "scenarios": {"S4A": {"metrics": METRICS}, "S3A": {"passed": False, "error": "x"}}},
    "new5678": {"build": "new5678", "generated_at": "2026-11-13T21:29:03Z",
                "health": {"realtime_model": "cascade-argon-elevenlabs",
                           "pipeline_version": "2026-11-06a"},
                "scenarios": {"S4A": {"metrics": METRICS, "judge": VERDICT},
                              "S1A": {"metrics": METRICS, "judge": {"error": "gateway down"}}}},
}


def test_a_run_is_one_row_per_scenario_that_ran_oldest_first():
    rows = D.sim_rows(REPORTS)
    assert [r["key"] for r in rows] == ["old1234|S4A", "new5678|S1A", "new5678|S4A"]
    old, unread, judged = rows
    assert (old["day"], old["model"], old["answered_pct"], old["wait_p50"]) == (
        "2026-10-01", "gpt-realtime-2.1", 88.2, 2.532)
    # Not read by the judge: blank cells, never zeros a chart would plot.
    assert old["judged"] == 0 and old["verdict"] == "" and "beats_made" not in old
    assert unread["judged"] == 0 and unread["verdict"] == "no verdict" and "hard" not in unread
    assert {k: judged[k] for k in ("judged", "verdict", "beats_made", "beats_judged", "beats_part",
                                   "beats_lost", "made_pct", "hard", "rule", "stock", "disputed",
                                   "esci_met", "esci_of", "judge_calls", "judge_tokens")} == {
        "judged": 1, "verdict": "FAIL", "beats_made": 9, "beats_judged": 10, "beats_part": 3,
        "beats_lost": 2, "made_pct": 90.0, "hard": 1, "rule": 1, "stock": 2, "disputed": 1,
        "esci_met": 4, "esci_of": 6, "judge_calls": 3, "judge_tokens": 9300}


def test_a_sim_runs_findings_carry_the_line_and_what_it_was_held_against():
    rows = D.finding_rows(REPORTS)
    assert [(r["kind"], r["fails"], r["turn"], r["character"]) for r in rows] == [
        ("FACT", "yes", 20, "priya"), ("stock", "", 4, "chris"), ("disputed ROLE", "", 9, "dan"),
        ("beat not made", "", 23, "chris")]
    assert rows[0]["against"] == "Support wants Monday the 23rd." and rows[1]["against"] == "i hear you"
    assert rows[3]["trigger"] == "t9_dates" and rows[3]["line"] == "That week is out."
    assert len({r["id"] for r in rows}) == 4 and all(r["run"] == "new5678" for r in rows)


def test_a_recorded_encounter_is_counts_and_places_never_a_line():
    unknown = dict(VERDICT, session_id="s_1792089076_b68b39", build=None,
                   spec_changed="no build recorded")
    lost = {"session_id": "s_1792080662_a3e4c4", "scenario": "S1A", "build": None,
            "error": "gateway down"}
    rows = D.encounter_rows([VERDICT, unknown, lost])
    assert [r["session"] for r in rows] == [
        "s_1794253712_581349", "s_1792089076_b68b39", "s_1792080662_a3e4c4"]
    first, second, third = rows
    assert (first["day"], first["build"], first["verdict"], first["hard"], first["note"]) == (
        "2026-11-09", "0e27778", "FAIL", 1, "")
    assert second["build"] == "" and second["note"] == "FACT, rule not judged: no build recorded"
    assert third["verdict"] == "no verdict" and third["note"] == "no verdict: gateway down"
    out = io.StringIO()
    w = csv.writer(out)
    for r in rows:
        w.writerow([r.get(c) for c in D.ENCOUNTER_COLUMNS])
    for said in ("That's when it peaks.", "I hear you.", "Dan sighs.", "That week is out.",
                 "peaks in the first two weeks"):
        assert said not in out.getvalue()
    assert not {"line", "against", "q", "source"} & set(D.ENCOUNTER_COLUMNS)


def test_the_selftest_is_one_row_per_kind_of_defect():
    payload = {"score": {"lines": 61, "codes": {"FACT": {"planted": 6, "found": 5, "false": 1},
                                                "RULE": {"planted": 3, "found": 3, "false": 0}},
                         "beats": {"none_planted": 9, "none_found": 8, "made": 31,
                                   "made_called_none": 2}},
               "results": [{"rubric": "97e0ab8d", "model": "m"}, {"error": "x"}]}
    rows = D.selftest_rows(payload)
    assert [(r["defect"], r["planted"], r["found"], r["false_alarms"]) for r in rows] == [
        ("FACT", 6, 5, 1), ("rule", 3, 3, 0), ("beat not made", 9, 8, 2)]
    assert all((r["encounters"], r["lines"], r["rubric"], r["model"]) == (2, 61, "97e0ab8d", "m")
               for r in rows)


def test_the_files_are_lf_utf8_and_blank_where_a_row_has_nothing(tmp_path):
    D.write_csv(tmp_path / "t.csv", ("a", "b", "c"), [{"a": 1, "b": None}, {"a": "’", "c": 0}])
    raw = (tmp_path / "t.csv").read_bytes()
    assert raw == "a,b,c\n1,,\n’,,0\n".encode("utf-8")


def test_every_committed_report_flattens_and_the_command_writes_the_tables(tmp_path, capsys):
    reports = D.load_reports()
    rows = D.sim_rows(reports)
    assert len(reports) >= 16 and len(rows) >= 50
    assert all(r["run"] in reports and r["scenario"] and r["run_at"] for r in rows)
    assert [r["run_at"] for r in rows] == sorted(r["run_at"] for r in rows)
    assert D.main([str(tmp_path)]) == 0
    assert sorted(p.name for p in tmp_path.iterdir()) == ["findings.csv", "sim_runs.csv"]
    with open(tmp_path / "sim_runs.csv", encoding="utf-8", newline="") as f:
        table = list(csv.DictReader(f))
    assert tuple(table[0]) == D.SIM_COLUMNS and len(table) == len(rows)
    assert "sim_runs.csv" in capsys.readouterr().out
