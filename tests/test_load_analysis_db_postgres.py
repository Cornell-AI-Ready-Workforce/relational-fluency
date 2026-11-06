"""The loader against a real Postgres: a refresh keeps turn_ids and survives a
rating_assignment pointing at the encounter.

Runs only with RF_ANALYSIS_TEST_DSN set, e.g. after

    docker run -d --name rf-analysis-db -p 5433:5432 -e POSTGRES_PASSWORD=rf \\
        -e POSTGRES_DB=rf postgres:16-alpine
    docker exec -i rf-analysis-db psql -q -U postgres -d rf -f - < docs/db-schema.sql
    RF_ANALYSIS_TEST_DSN=postgresql://postgres:rf@localhost:5433/rf pytest tests/test_load_analysis_db_postgres.py

CI has no Postgres and skips it; tests/test_load_analysis_db_refresh.py pins
the same statements offline.
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
for p in (ROOT, ROOT / "tests", ROOT / "tools"):
    if str(p) not in sys.path:
        sys.path.insert(0, str(p))

DSN = os.environ.get("RF_ANALYSIS_TEST_DSN")
pytestmark = pytest.mark.skipif(not DSN, reason="RF_ANALYSIS_TEST_DSN not set")
psycopg = pytest.importorskip("psycopg")
pytest.importorskip("yaml")

from server import encounter_record  # noqa: E402
from test_review_fixes import _encounter_dir  # noqa: E402
import load_analysis_db as ladb  # noqa: E402

SID = "s_1_ab"  # what _encounter_dir names its session


def _write(tmp_path, texts):
    sdir, _ = _encounter_dir(tmp_path) if not (tmp_path / SID).exists() else ((tmp_path / SID), None)
    record = encounter_record.build(sdir)
    record["scenario"] = "S4A"
    record["transcript"] = [{"t": float(i), "role": "participant" if i % 2 == 0 else "agent",
                             "agent_id": None if i % 2 == 0 else "dan", "text": t}
                            for i, t in enumerate(texts)]
    (sdir / "record.json").write_text(json.dumps(record), encoding="utf-8")
    return sdir


@pytest.fixture
def conn():
    with psycopg.connect(DSN) as c:
        c.execute("SET search_path TO rf, public")
        with c.cursor() as cur:
            # a clean slate for this encounter and anything hanging off it
            for t in ("rating", "rating_assignment", "model_score", "event", "director_route",
                      "trigger_firing", "stage_direction", "turn", "encounter_cast", "media",
                      "transcript_version"):
                cur.execute(f"DELETE FROM {t} WHERE {'assignment_id IN (SELECT assignment_id FROM rating_assignment WHERE encounter_id = %s)' if t == 'rating' else 'encounter_id = %s'}", (SID,))
            cur.execute("DELETE FROM encounter WHERE encounter_id = %s", (SID,))
            cur.execute("DELETE FROM rater WHERE rater_id = 'R-test'")
            ladb.load_scenarios(cur, ROOT / "scenarios" / "v3")
        c.commit()
        yield c


def _turns(cur):
    cur.execute("SELECT seq, turn_id, text FROM turn WHERE encounter_id = %s ORDER BY seq", (SID,))
    return cur.fetchall()


def test_refresh_keeps_turn_ids_and_survives_a_rating_assignment(conn, tmp_path):
    sdir = _write(tmp_path, ["hello", "hi there", "so about the rollout"])
    with conn.cursor() as cur:
        assert ladb.load_encounter(cur, sdir, "s3://x/") == SID
        first = _turns(cur)
        assert [t for _, _, t in first] == ["hello", "hi there", "so about the rollout"]
        ids = [i for _, i, _ in first]
        # a rater has pointed at this encounter and at its second line
        cur.execute("INSERT INTO rater (rater_id, display_name) VALUES ('R-test', 'Test')")
        cur.execute("INSERT INTO rating_assignment (encounter_id, rater_id) VALUES (%s, 'R-test') "
                    "RETURNING assignment_id", (SID,))
        aid = cur.fetchone()[0]
        cur.execute("INSERT INTO rating (assignment_id, construct, item_key, score, evidence_turn_ids) "
                    "VALUES (%s, 'teamwork', 'supportive', 4, %s)", (aid, [ids[1]]))
    conn.commit()

    # the record grows by a line and one line is corrected: a normal refresh
    sdir = _write(tmp_path, ["hello", "hi there, Dan", "so about the rollout", "one more"])
    with conn.cursor() as cur:
        assert ladb.load_encounter(cur, sdir, "s3://x/") == SID   # no FK error
        second = _turns(cur)
    conn.commit()
    assert [i for _, i, _ in second[:3]] == ids, "turn_ids changed on refresh"
    assert [t for _, _, t in second] == ["hello", "hi there, Dan", "so about the rollout", "one more"]
    with conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM rating_assignment WHERE encounter_id = %s", (SID,))
        assert cur.fetchone()[0] == 1
        cur.execute("SELECT evidence_turn_ids FROM rating WHERE assignment_id = %s", (aid,))
        assert cur.fetchone()[0] == [ids[1]]
        cur.execute("SELECT text FROM turn WHERE turn_id = %s", (ids[1],))
        assert cur.fetchone()[0] == "hi there, Dan", "the cited line is the corrected one"

    # a shorter record: the trailing turns go, the cited one stays
    sdir = _write(tmp_path, ["hello", "hi there, Dan"])
    with conn.cursor() as cur:
        ladb.load_encounter(cur, sdir, "s3://x/")
        assert [i for _, i, _ in _turns(cur)] == ids[:2]
    conn.commit()
