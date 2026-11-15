"""#225, pipeline 2026-11-15d: S3 and S4 text cleanups.

- A brief can opt out of the shared "about half your turns end on a question"
  and "do not accept a brief answer" rules (`reticent: true`); S3A Jordan and
  Casey and S3B Lee and Ari do, and the two forms stay parallel.
- S3A's observe line no longer asks the rater to "probe beneath Jordan's
  'fine'": the beat is what he stopped doing.
- S4A's opening is Dan's position, not a line nobody heard. (Ben's t1 point,
  Dan cutting off a Priya who has not spoken when t1 rides on his first reply,
  is left open: a branched t1 cue was tried and the sims got worse, #225.)
- S4A names the risk section in Priya's brief and t7 cue, not only in the
  observe line and probe; Dan's example reason is a month-end close.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from server import llm  # noqa: E402
from server import scenarios_v3 as v3  # noqa: E402

HALF_QUESTIONS = "About half your turns end on a question"
PRESS_BRIEF = "If they are brief or non-committal, do not accept it"


def _prompt(sid: str, key: str) -> str:
    sc = v3.compile_scenario(sid)
    return next(a for a in sc.cast if a.id == key).system_prompt


def _norm(text: str) -> str:
    return " ".join(str(text or "").split())


def _beat(sid: str, tid: str) -> dict:
    spec = v3.load_spec(sid)
    for i in spec["interactions"]:
        for t in i.get("triggers") or []:
            if t["id"] == tid:
                return t
    raise AssertionError(f"{sid}: no beat {tid}")


@pytest.mark.parametrize("sid,key", [
    ("S3A", "jordan"), ("S3A", "casey"), ("S3B", "lee"), ("S3B", "ari")])
def test_a_reticent_brief_drops_the_two_shared_rules_that_make_it_ask_and_press(sid, key):
    prompt = _norm(_prompt(sid, key))
    assert HALF_QUESTIONS not in prompt
    assert PRESS_BRIEF not in prompt
    # Everything else in the shared block stays.
    assert "## How you talk" in prompt and "## Keep the scene alive" in prompt
    assert "You do not say the same sentence twice" in prompt
    assert "Do not resolve the situation for them" in prompt


@pytest.mark.parametrize("sid,key", [
    ("S3A", "alex"), ("S3B", "toni"), ("S4A", "dan"), ("S4A", "priya"),
    ("S1A", next(iter(v3.load_spec("S1A")["agents"])))])
def test_every_other_brief_keeps_both_rules(sid, key):
    prompt = _norm(_prompt(sid, key))
    assert HALF_QUESTIONS in prompt
    assert PRESS_BRIEF in prompt


def test_the_two_s3_forms_opt_out_the_same_roles():
    def reticent_roles(sid):
        spec = v3.load_spec(sid)
        return sorted(a["role"].split()[-1] for a in spec["agents"].values()
                      if a.get("reticent"))
    assert reticent_roles("S3A") == reticent_roles("S3B") == ["junior", "performer"]
    for sid in ("S3A", "S3B"):
        cynic = [a for a in v3.load_spec(sid)["agents"].values()
                 if "cynic" in a["role"] or "cynical" in a["role"]]
        assert len(cynic) == 1 and not cynic[0].get("reticent"), sid


def test_s3a_observe_names_the_withdrawal_not_a_fine():
    meeting = v3.load_spec("S3A")["interactions"][0]
    assert "'fine'" not in meeting["observe"]
    assert "re-engage Jordan with something real" in meeting["observe"]


def test_s4a_opens_on_a_position_not_a_line_nobody_heard():
    a = _norm(v3.load_spec("S4A")["interactions"][0]["opening"])
    assert "mid-flow" not in a and "His position is" in a


def test_s4a_names_the_risk_section_wherever_priya_is_handed_work():
    spec = v3.load_spec("S4A")
    i2 = spec["interactions"][1]
    assert "the risk section" in i2["observe"]
    t7 = _beat("S4A", "t7_priya_waits_to_be_handed_work")
    assert "the risk section" in t7["cue"] and "the risk section" in t7["on_silence"]
    assert "the risk section" in _norm(spec["agents"]["priya"]["system_prompt"])


def test_s4a_has_no_close_beside_the_part_called_the_close():
    text = (ROOT / "scenarios" / "v3" / "S4A_internal_rollout.yaml").read_text(encoding="utf-8")
    assert "once the close is done" not in text
    dan = _norm(v3.load_spec("S4A")["agents"]["dan"]["system_prompt"])
    assert "a close" not in dan


def test_the_issue_greps_come_back_empty():
    s3a = (ROOT / "scenarios" / "v3" / "S3A_after_resignations.yaml").read_text(encoding="utf-8")
    s4a = (ROOT / "scenarios" / "v3" / "S4A_internal_rollout.yaml").read_text(encoding="utf-8")
    for gone in ("whole review queue", "thank them for answering",
                 "one problem, not a list", "That helps. Or, okay"):
        assert gone not in s3a, gone
    for gone in ("Opens mid-flow", "once the close is done", "support collapsed"):
        assert gone not in s4a, gone


def test_the_pipeline_version_names_this_change():
    assert llm.PIPELINE_VERSION >= "2026-11-15d"
    src = (ROOT / "server" / "llm.py").read_text(encoding="utf-8")
    assert "#   2026-11-15d  S3 and S4 briefs (#225)." in src
