"""Pre-launch copy fixes in the Phase 1 specs and the survey docs.

#220: a Phase 1 run serves S2A and S3A, so S2A's peer cannot share a name with
S3A's Alex (or with anyone else in any spec). #267: S1A tells the participant
and the characters one time for the last incident (this morning) and one span
(two months). #276: the survey docs say Phase 1 links out before they describe
the embedded design.
"""
import re
from pathlib import Path

import yaml

from server import scenarios_v3

ROOT = Path(__file__).resolve().parents[1]
SPECS = ROOT / "scenarios" / "v3"


def _spec(name: str) -> dict:
    return yaml.safe_load((SPECS / name).read_text(encoding="utf-8"))


def test_s2a_peer_is_jamie_and_no_other_spec_uses_the_name():
    text = (SPECS / "S2A_promised_raise.yaml").read_text(encoding="utf-8")
    assert not re.search(r"\bAlex\b", text)
    spec = _spec("S2A_promised_raise.yaml")
    assert "your peer Jamie" in spec["intro_card"]["situation"]
    assert any("Jamie" in a for a in spec["intro_card"]["assets"])
    assert any("Jamie" in a for a in spec["assets"])
    assert "Jamie got an off-cycle adjustment" in spec["agents"]["morgan"]["system_prompt"]
    others = [p.name for p in SPECS.glob("*.yaml")
              if p.name != "S2A_promised_raise.yaml"
              and re.search(r"\bJamie\b", p.read_text(encoding="utf-8"))]
    assert others == []


def test_s1a_gives_one_time_and_one_span_for_the_last_incident():
    spec = _spec("S1A_taken_credit.yaml")
    setup, card = spec["setup"], spec["intro_card"]["situation"]
    for text in (setup, card, scenarios_v3._actor_scene(spec)):
        assert "this morning" in text and "two months" in text, text
        for stale in ("yesterday", "last week", "few months"):
            assert stale not in text, (stale, text)
    assert "this morning" in spec["agents"]["riley"]["system_prompt"]
    assert "Two hours ago" in spec["agents"]["sam"]["system_prompt"]


def test_the_survey_docs_open_with_the_phase1_link_out():
    for doc in ("qualtrics-embed.md", "study1-plan.md"):
        head = (ROOT / "docs" / doc).read_text(encoding="utf-8")[:2500]
        assert "SV_bClj80jCRO4Dmdw" in head and "qid=<ResponseID>" in head, doc
        assert "qualtrics_id" in head and "python -m server.qualtrics join" in head, doc
