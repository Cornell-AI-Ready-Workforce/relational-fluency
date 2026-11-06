"""The analysis-database loader refreshes in place.

A rating_assignment or model_score references an encounter row, and a rating's
evidence_turn_ids name turn_ids. The loader used to delete the encounter and
every child row and re-insert them, which (a) renumbered every turn on each
refresh and (b) fails on the foreign key once a single assignment exists. Now
the encounter and its turns are upserted on their natural keys; only the
children nothing references are replaced. Offline, with the fake cursor
tests/test_review_fixes.py uses; tests/test_load_analysis_db_postgres.py runs
the same against a real Postgres when one is offered.
"""
from __future__ import annotations

import json
import re
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
for p in (ROOT, ROOT / "tests", ROOT / "tools"):
    if str(p) not in sys.path:
        sys.path.insert(0, str(p))

from server import encounter_record  # noqa: E402
from test_review_fixes import _Cursor, _encounter_dir  # noqa: E402

ladb = pytest.importorskip("load_analysis_db")


def _loaded(tmp_path, n_turns=3):
    sdir, _ = _encounter_dir(tmp_path)
    record = encounter_record.build(sdir)
    record["scenario"] = "S4A"
    record["transcript"] = [{"t": float(i), "role": "participant" if i % 2 == 0 else "agent",
                             "agent_id": None if i % 2 == 0 else "dan", "text": f"line {i}"}
                            for i in range(n_turns)]
    (sdir / "record.json").write_text(json.dumps(record), encoding="utf-8")
    cur = _Cursor()
    assert ladb.load_encounter(cur, sdir, "s3://x/")
    return [s for s, _ in cur.calls], cur.calls


def test_the_encounter_row_is_upserted_not_replaced(tmp_path):
    sqls, calls = _loaded(tmp_path)
    assert not any(re.search(r"DELETE FROM encounter\b", s) for s in sqls)
    (enc,) = [s for s in sqls if "INSERT INTO encounter (" in s]
    assert "ON CONFLICT (encounter_id) DO UPDATE SET" in enc
    # every column but the key is rewritten, so a refresh cannot leave a
    # stale value standing beside fresh ones
    cols = re.search(r"INSERT INTO encounter \(([^)]*)\)", enc).group(1)
    names = [c.strip() for c in cols.split(",") if c.strip() != "encounter_id"]
    for c in names:
        assert f"{c} = EXCLUDED.{c}" in enc, c
    assert tuple(names) == ladb.ENCOUNTER_COLUMNS


def test_turns_are_upserted_on_their_natural_key_and_trimmed(tmp_path):
    sqls, calls = _loaded(tmp_path, n_turns=3)
    turns = [s for s in sqls if "INSERT INTO turn (" in s]
    assert len(turns) == 3
    for s in turns:
        assert "ON CONFLICT (encounter_id, seq) DO UPDATE SET" in s and "RETURNING turn_id" in s
        # text and timing are rewritten; the key and turn_id are not touched
        assert "text = EXCLUDED.text" in s and "t = EXCLUDED.t" in s
        assert "turn_id = " not in s and "seq = EXCLUDED" not in s
    assert not any(re.fullmatch(r"DELETE FROM turn WHERE encounter_id = %s", s.strip()) for s in sqls)
    (trim_sql, trim_params), = [(s, p) for s, p in calls
                                if s.startswith("DELETE FROM turn WHERE encounter_id = %s AND seq >= %s")]
    assert trim_params[1] == 3


def test_only_unreferenced_children_are_replaced(tmp_path):
    sqls, _ = _loaded(tmp_path)
    replaced = {re.match(r"DELETE FROM (\w+) WHERE encounter_id = %s$", s.strip()).group(1)
                for s in sqls if re.match(r"DELETE FROM (\w+) WHERE encounter_id = %s$", s.strip())}
    assert replaced == {"event", "director_route", "trigger_firing", "stage_direction",
                        "encounter_cast", "media", "transcript_version"}
    assert "turn" not in replaced and "encounter" not in replaced


def test_the_recovered_path_upserts_too():
    src = (ROOT / "tools" / "load_analysis_db.py").read_text(encoding="utf-8")
    assert "DELETE FROM encounter" not in src
    # both INSERT INTO encounter statements carry a conflict clause
    assert src.count("INSERT INTO encounter (") == 2
    assert src.count("ON CONFLICT (encounter_id) DO UPDATE SET") == 2
