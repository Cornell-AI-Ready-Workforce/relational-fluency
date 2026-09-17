"""Construct order across participants is a balanced Latin square, not a shuffle.

Decision 2026-09-17 (docs/study1-plan.md, E2.8): a Williams 4×4 square, rows
assigned in rotation per cohort. What it buys, and what these tests pin:

  * every construct appears in every position equally often across a wave;
  * every construct is followed by every other construct equally often;
  * a seeded run rebuilds its own order, and a second attempt keeps attempt 1's
    row, so a pre/post delta is not confounded with a change of position;
  * internal test traffic does not consume the study cohort's rows.
"""
from collections import Counter

import pytest

from server import runs
from server.runs import CONSTRUCT_ORDER, WILLIAMS_4


@pytest.fixture(autouse=True)
def _data_dir(tmp_path, monkeypatch):
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    monkeypatch.setattr(runs, "RUNS_DIR", tmp_path / "runs", raising=False)
    (tmp_path / "runs").mkdir(parents=True, exist_ok=True)
    monkeypatch.delenv("DEFAULT_RUN_VARIANT", raising=False)


def _order(run):
    return [s["construct"] for s in run["scenarios"]]


def test_the_square_is_a_williams_design():
    """Each treatment once per position, each ordered adjacent pair once."""
    n = len(WILLIAMS_4)
    for pos in range(n):
        assert sorted(row[pos] for row in WILLIAMS_4) == list(range(n)), pos
    pairs = Counter((row[i], row[i + 1]) for row in WILLIAMS_4 for i in range(n - 1))
    assert all(v == 1 for v in pairs.values()), pairs
    assert len(pairs) == n * (n - 1)


def test_a_hundred_participants_put_every_construct_in_every_position_25_times():
    positions = Counter()
    rows = Counter()
    for i in range(100):
        run = runs.create(f"P_LSQ{i}", qualtrics_id=f"R_LSQ{i}")
        assert run["order"]["scheme"] == "williams_4x4"
        rows[run["order"]["row"]] += 1
        for pos, construct in enumerate(_order(run)):
            positions[(construct, pos)] += 1
    assert set(rows) == {0, 1, 2, 3} and all(v == 25 for v in rows.values()), rows
    assert all(v == 25 for v in positions.values()), positions
    assert len(positions) == 16


def test_each_construct_follows_each_other_construct_equally_often():
    follows = Counter()
    for i in range(40):
        o = _order(runs.create(f"P_FOL{i}", qualtrics_id=f"R_FOL{i}"))
        for a, b in zip(o, o[1:]):
            follows[(a, b)] += 1
    assert all(v == 10 for v in follows.values()), follows
    assert len(follows) == 12


def test_the_row_maps_onto_construct_order():
    run = runs.create("P_ROW", order_row=2)
    assert run["order"] == {"scheme": "williams_4x4", "row": 2}
    assert _order(run) == [CONSTRUCT_ORDER[i] for i in WILLIAMS_4[2]]


def test_a_seeded_run_rebuilds_its_own_order_without_touching_the_counter():
    a = runs.create("P_SEED_A", seed=7)
    b = runs.create("P_SEED_B", seed=7)
    assert _order(a) == _order(b) and a["order"] == b["order"]
    assert not (runs.RUNS_DIR / ".order_counter.study").exists()


def test_the_second_attempt_keeps_the_first_attempts_row():
    first = runs.create("P_SIB", qualtrics_id="R_SIB")
    second = runs.sibling_run(first["run_id"])
    assert second["order"]["row"] == first["order"]["row"]
    assert _order(second) == _order(first)


def test_internal_traffic_rotates_its_own_rows():
    study = [runs.create(f"P_S{i}", qualtrics_id=f"R_S{i}")["order"]["row"] for i in range(4)]
    internal = [runs.create(f"P_I{i}", cohort="internal")["order"]["row"] for i in range(4)]
    assert study == [0, 1, 2, 3] and internal == [0, 1, 2, 3]
    assert (runs.RUNS_DIR / ".order_counter.study").read_text(encoding="utf-8") == "4"


def test_the_counter_file_is_not_mistaken_for_a_run():
    runs.create("P_CNT", qualtrics_id="R_CNT")
    assert (runs.RUNS_DIR / ".order_counter.study").exists()
    assert runs.find_for_participant("P_CNT") is not None   # scans RUNS_DIR/*.json
