"""The judge's selftest, offline: the case file's structure and the arithmetic.

tools/sim/judge_selftest.py measures the judge on encounters whose defects are
known, and a measuring stick has to be right before the thing it measures: a
case that names a trigger the scenario does not have, or a score that counts a
dropped label, would report on the judge a number that is about the set. What
the model answers is not run here; how a labelled case becomes a record, and
how answers become a score, is, with the same fake client tests/test_sim_judge.py
uses.
"""
from __future__ import annotations

import asyncio
import copy
import json

import pytest

from tests.test_sim_judge import Model, said
from tools.sim import judge as J
from tools.sim import judge_selftest as S

SID = "S4A"
SRC = J.source_of(SID)
CUES = SRC["cues"]


def case(*turns, cid="c1", scenario=SID):
    return {"id": cid, "scenario": scenario, "kind": "planted", "transcript": list(turns)}


def P(text):
    return {"role": "participant", "text": text}


def C(agent, text, trigger=None, beat=None, flags=(), **kw):
    t = {"role": "agent", "agent_id": agent, "text": text,
         "expect": {"beat": beat, "flags": list(flags)}, **kw}
    if trigger:
        t["trigger_id"] = trigger
    return t


GOOD = case(
    P("Hey guys."),
    C("dan", "Priya, that was last year. The ninth, everybody.", "t1_priya_interrupted", "made"),
    C("chris", "Sales goes Friday the 13th.", flags=["FACT"]),
    C("dan", "Fine by me.", "t2_idea_relabelled", "none"),
    C("chris", "Either way.", "t6_chris_backs_the_room", "made", on_silence=True),
    C("priya", "As an AI I cannot say.", flags=["ROLE"], ambiguous=True))


def data(*cases, **scenarios):
    return {"scenarios": scenarios or {SID: SRC}, "cases": list(cases)}


def test_a_well_formed_case_has_no_problems_and_each_malformed_one_is_named():
    assert S.problems(data(GOOD)) == []

    def broken(change):
        c = copy.deepcopy(GOOD)
        change(c["transcript"])
        return " | ".join(S.problems(data(c)))

    assert "no trigger 't9_x'" in broken(lambda t: t[1].update(trigger_id="t9_x"))
    assert "is dan's beat, not chris's" in broken(lambda t: t[1].update(agent_id="chris"))
    assert "'ghost' is not a character" in broken(lambda t: t[2].update(agent_id="ghost"))
    assert "needs expect" in broken(lambda t: t[2].pop("expect"))
    assert "'made' or 'none'" in broken(lambda t: t[1]["expect"].update(beat=None))
    assert "needs a trigger_id" in broken(lambda t: t[2]["expect"].update(beat="made"))
    assert "flags are any of" in broken(lambda t: t[2]["expect"].update(flags=["TONE"]))
    assert "carries no label" in broken(lambda t: t[0].update(expect={"beat": None, "flags": []}))
    assert "needs a role" in broken(lambda t: t[0].update(text=" "))
    assert "heard_text is for an interrupted line" in broken(lambda t: t[2].update(heard_text="Sales"))
    assert S.problems(data(GOOD, GOOD)) == ["c1: the id is used twice"]
    assert S.problems(data()) == ["no cases"]
    # The file has to carry the text each case is held to: nothing is looked up.
    for bare in ({"cases": [GOOD]}, {"scenarios": {SID: {"scene": "x"}}, "cases": [GOOD]},
                 data(dict(GOOD, scenario="S2A"))):
        assert S.problems(bare) == [f"c1: the file carries no scenario text for "
                                    f"{bare['cases'][0]['scenario']!r} (run --freeze)"]


def test_a_one_to_one_trigger_belongs_to_its_interactions_character():
    one = J.source_of("S1A")
    ok = case(C("riley", "You cannot let that go.", "t1_retaliation_fork", "made"),
              C("sam", "The framing was mine.", "t3_defensiveness", "made"), scenario="S1A")
    assert S.problems(data(ok, S1A=one)) == []
    swapped = case(C("sam", "You cannot let that go.", "t1_retaliation_fork", "made"), scenario="S1A")
    assert S.problems(data(swapped, S1A=one)) == [
        "c1 turn 0: t1_retaliation_fork is riley's beat, not sam's"]


def test_a_case_becomes_a_record_under_the_direction_the_runner_would_hand_it():
    rec = S.record_of(GOOD, SRC)
    assert rec["encounter_id"] == "c1" and rec["scenario"] == SID
    spoken = rec["transcript"]
    assert spoken[0] == {"role": "participant", "text": "Hey guys."}
    assert spoken[1]["stage_direction"] == (
        f"Your next move, as Dan, now, in your own words: {CUES['t1_priya_interrupted'][0]}")
    assert spoken[4]["stage_direction"] == (
        "They have gone quiet. Your next move, as Chris, now, in your own words, then stop "
        f"and let them answer: {CUES['t6_chris_backs_the_room'][1]}")
    assert spoken[2]["stage_direction"] is None and spoken[2]["trigger_id"] is None and spoken[2]["esci"] == []
    assert spoken[1]["esci"] == SRC["triggers"]["t1_priya_interrupted"]["esci"] != []
    assert spoken[1]["interrupted"] is False and spoken[1]["heard_text"] is None
    # What the judge then reads is that text, with nothing reworded.
    turns, _ = J.turns_of(rec)
    assert J.spec_drift(turns, SRC) is None and [t["at"] for t in turns] == [0, 1, 2, 3, 4, 5]


def test_freezing_takes_the_scenario_text_of_this_checkout(tmp_path):
    frozen = S.freeze({"about": "x", "cases": [GOOD, case(cid="c2", scenario="S1A")]})
    assert list(frozen["scenarios"]) == ["S1A", SID] and frozen["scenarios"][SID] == SRC
    assert frozen["about"] == "x" and S.problems({**frozen, "cases": [GOOD]}) == []
    path = tmp_path / "cases.json"
    S.write(path, frozen)
    raw = path.read_bytes()
    assert b"\r" not in raw and raw.endswith(b"\n") and S.load(path) == json.loads(json.dumps(frozen))


def _result(flags=(), verdicts=(), **kw):
    r = {"session_id": "c1", "model": "m", "usage": {"calls": 2, "in": 5000, "out": 100}, **kw}
    if "error" in kw:
        return r
    return dict(r, flags=[{"at": at, "c": c} for at, c in flags],
                verdicts=[{"at": at, "v": v} for at, v in verdicts])


def test_the_score_counts_what_was_planted_found_and_wrongly_found():
    s = S.score([GOOD], [_result(flags=[(2, "FACT"), (4, "RULE"), (5, "ROLE"), (4, "STOCK")],
                                 verdicts=[(1, "y"), (3, "p"), (4, "n")])])
    assert s["codes"]["FACT"] == {"planted": 1, "found": 1, "false": 0}
    assert s["codes"]["RULE"] == {"planted": 0, "found": 0, "false": 1}
    # Turn 5 is ambiguous: its planted ROLE is not owed and finding it earns nothing.
    assert s["codes"]["ROLE"] == {"planted": 0, "found": 0, "false": 0}
    assert s["beats"] == {"none_planted": 1, "none_found": 0, "made": 2, "made_called_none": 1}
    assert s["lines"] == 4 and s["no_verdict"] == 0
    assert [m.split()[0] for m in s["missed"]] == ["none"] and "(p)" in s["missed"][0]
    assert sorted(f.split()[0] for f in s["false"]) == ["RULE", "none"]


def test_an_encounter_the_judge_did_not_read_is_counted_and_its_labels_are_not():
    found = _result(flags=[(2, "FACT")], verdicts=[(1, "y"), (3, "n"), (4, "y")])
    cases = [GOOD, dict(GOOD, id="c2"), dict(GOOD, id="c3")]
    results = [_result(error="gateway down"), found,
               dict(found, spec_changed="no build recorded")]       # FACT and rule not judged
    s = S.score(cases, results)
    assert s["no_verdict"] == 2 and s["lines"] == 4
    assert s["unread"] == ["c1: gateway down", "c3: no build recorded"]
    assert s["codes"]["FACT"] == {"planted": 1, "found": 1, "false": 0}
    assert s["beats"]["none_found"] == 1 and s["missed"] == [] and s["false"] == []
    lines = S.report(s, results)
    assert lines[0].startswith("judge selftest: 3 encounters, 4 labelled lines, rubric ")
    assert "FACT 1/1 found, 0 false" in lines[1] and "beats not made 1/1 found, 0 false of 2 made" in lines[2]
    assert lines[3] == "  NO VERDICT on 2 encounters: c1: gateway down; c3: no build recorded"


def test_a_real_verdict_is_scored_whatever_else_it_flags():
    # judge_record's own result, not a hand-built one: a STOCK flag in it once
    # had no `at` and score() died on it after every model call had been paid for.
    talky = case(P("Hey guys."),
                 C("dan", "Priya, that was last year. The ninth, everybody.", "t1_priya_interrupted", "made"),
                 C("chris", "I hear you. Sales goes Friday the 13th.", flags=["FACT"]),
                 C("dan", "Fine by me.", "t2_idea_relabelled", "none"))
    model = Model({"beats": [{"i": 1, "v": "y"}, {"i": 3, "v": "n"}],
                   "flags": [{"i": 2, "c": "FACT", "q": "Sales is ready for Monday the 9th"}]},
                  said("none", "breach"), said("breach", "none"))
    results = asyncio.run(S.run([talky], {SID: SRC}, "m", model))
    assert results[0]["spec_changed"] is None and results[0]["counts"]["STOCK"] == 1
    s = S.score([talky], results)
    assert s["codes"]["FACT"] == {"planted": 1, "found": 1, "false": 0}
    assert s["beats"] == {"none_planted": 1, "none_found": 1, "made": 1, "made_called_none": 0}
    assert s["missed"] == [] and s["false"] == [] and s["no_verdict"] == 0
    # Held to the text the file carries, not to a build and not to today's file.
    assert "BRIEF chris" in model.calls[0]["messages"][0]["content"]


def test_the_command_validates_and_refuses_a_missing_file(tmp_path, capsys):
    path = tmp_path / "cases.json"
    S.write(path, {"cases": [GOOD]})
    assert S.main(["--validate", str(path)]) == 2 and "run --freeze" in capsys.readouterr().err
    assert S.main(["--freeze", str(path)]) == 0 and S.main(["--validate", str(path)]) == 0
    assert capsys.readouterr().out.splitlines() == ["ok: 1 cases, 5 character lines"] * 2
    with pytest.raises(SystemExit) as stop:
        S.main(["--cases", str(tmp_path / "none.json")])
    assert stop.value.code == 2 and "no case file" in capsys.readouterr().err


def test_the_committed_set_is_well_formed_and_plants_every_kind_of_defect():
    committed = S.load()
    assert S.problems(committed) == []
    turns = [t for c in committed["cases"] for t in c["transcript"]
             if t["role"] == "agent" and not t.get("ambiguous")]
    for code in S.CODES:
        assert sum(code in t["expect"]["flags"] for t in turns) >= 3, code
    assert sum(t["expect"]["beat"] == "none" for t in turns) >= 6
    # A set with nothing clean in it cannot show a false alarm.
    assert sum(not t["expect"]["flags"] and t["expect"]["beat"] != "none" for t in turns) >= 30
    # Every case's directions are the cues of the text the file itself carries.
    for c in committed["cases"]:
        src = committed["scenarios"][c["scenario"]]
        assert J.spec_drift(J.turns_of(S.record_of(c, src))[0], src) is None, c["id"]
