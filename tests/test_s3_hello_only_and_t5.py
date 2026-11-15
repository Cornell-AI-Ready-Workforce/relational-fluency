"""Pipeline 2026-11-15c (#218, #219).

#218: an S3 room lead's first beat is t0_hello ("If they have only said hello,
stop there"), and its brief says a hello gets only a hello back, but the
room's FIRST REPLY note told it "Then, in the same reply, make your opening
move". The note now says the same as the cue when the lead's first beat is a
greeting that scores nothing; S4's leads open on a scored beat and keep it.

#219: S3A t5 asked Jordan "what breaks if nobody does the second pass", and in
sim s_1794711071_5ac3f6 he answered with a day and a clause type that no
setup, card or brief gives. The cue and probe now hand him his own facts.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
for p in (ROOT, ROOT / "tests"):
    if str(p) not in sys.path:
        sys.path.insert(0, str(p))

from server import llm  # noqa: E402

import test_participant_turn_integrity as T  # noqa: E402
from test_participant_opens import room_runner  # noqa: E402

MOVE = "Then, in the same reply, make your opening move."
HELLO_ONLY = "If all they have said is hello, that greeting is the whole of this reply"


@pytest.mark.parametrize("scenario,hello_only", [
    ("S3A", True), ("S3B", True), ("S4A", False), ("S4B", False)])
@T.in_a_loop
async def test_the_first_reply_note_matches_the_leads_first_beat(scenario, hello_only):
    runner, session, page, tl = room_runner(scenario)
    lead = runner._resolve_agents()[0]
    assert runner._lead_opens_on_a_hello(lead) is hello_only
    assert runner._fold_opening(lead, group=True) is True
    brief = runner._instructions_for(lead)
    assert "FIRST REPLY" in brief and "The participant speaks first." in brief
    if hello_only:
        assert MOVE not in brief and "opening move" not in brief
        assert HELLO_ONLY in brief
        first = runner._triggers()[0]
        assert first["id"] == "t0_hello"
        assert "If they have only said hello, stop there." in first["cue"]
    else:
        assert MOVE in brief and HELLO_ONLY not in brief


@T.in_a_loop
async def test_only_the_lead_is_read_as_opening_on_a_hello():
    runner, session, page, tl = room_runner("S3A")
    others = runner._resolve_agents()[1:]
    assert others and not any(runner._lead_opens_on_a_hello(a) for a in others)


def test_jordans_t5_gives_him_his_own_facts_and_invites_no_new_ones():
    import yaml
    doc = yaml.safe_load((ROOT / "scenarios" / "v3" / "S3A_after_resignations.yaml")
                         .read_text(encoding="utf-8"))
    (t5,) = [t for t in doc["interactions"][0]["triggers"]
             if t["id"] == "t5_a_fact_left_on_the_table"]
    assert t5["agent"] == "jordan"
    assert t5["esci"] == ["brings_out_best", "inspires", "builds_pride"]
    for text in (t5["cue"], t5["on_silence"]):
        assert "what breaks" not in text
        assert "nobody reading behind them" in text
        assert "comes back on the name on it" in text
        assert "no contract, clause, day or number" in text
    # Both facts are Jordan's own (his brief's "Your facts, and no others").
    brief = " ".join(doc["agents"]["jordan"]["system_prompt"].split())
    assert "nobody reading behind them" in brief
    assert "whose name is on a review when it comes back wrong" in brief


def test_the_generator_writes_the_committed_s3_files():
    from tools.scenarios import s3_briefs
    for sid, form in s3_briefs.FORMS.items():
        path = ROOT / "scenarios" / "v3" / form["file"]
        assert s3_briefs.render(sid) == path.read_text(encoding="utf-8"), sid


def test_the_pipeline_version_names_this_change():
    assert llm.PIPELINE_VERSION == "2026-11-15c"
    src = (ROOT / "server" / "llm.py").read_text(encoding="utf-8")
    assert "#   2026-11-15c  S3 rooms (#218, #219)." in src
