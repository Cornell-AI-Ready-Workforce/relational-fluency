"""The judge's offline half: what decides a verdict once the model has answered.

tools/sim/judge.py asks a model what the characters did, so the asking cannot
run in CI. Everything around it can, and is where a judge goes wrong quietly:
which words of a cut line are judged, which scenario text a line is held to,
whether a quoted rule is really in the brief it is attributed to, what readings
that disagree add up to, what a model that did not answer is taken to have
said. All of it is pinned here with a client that returns what the test hands
it, against the real S4A scenario file, so a renamed loader or a reworded brief
breaks a test, not an audit.
"""
from __future__ import annotations

import asyncio
import json
import shutil
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from tools.sim import analyze as A
from tools.sim import check
from tools.sim import judge as J

ROOT = Path(__file__).resolve().parent.parent
SID = "S4A"
SRC = J.source_of(SID)
RULE_OF_DANS = "You never hand credit over before you are corrected."
# Chris speaks for Operations; the Sales date is Dan's.
FACT_OF_CHRISS = "Operations wants Monday the 30th"


def P(text):
    return {"role": "participant", "text": text}


def C(agent, text, trigger=None, **kw):
    """A character turn; with `trigger`, under today's cue for it."""
    direction = kw.pop("direction", None)
    if trigger and direction is None:
        direction = f"Your next move, now, in your own words: {SRC['cues'][trigger][0]}"
    return {"role": "agent", "agent_id": agent, "text": text, "trigger_id": trigger,
            "stage_direction": direction, "esci": kw.pop("esci", ["respectful"] if trigger else []),
            **kw}


def record(*turns, build=None, scenario=SID, **kw):
    return {"encounter_id": "s_x", "scenario": scenario, "participant_key": None,
            "provenance": {"build": build}, "transcript": list(turns), **kw}


class Model:
    """Answers in order; an Exception is raised, a dict is a tool answer."""

    def __init__(self, *answers):
        self.answers, self.calls, self.messages = list(answers), [], self

    async def create(self, **kw):
        self.calls.append(kw)
        a = self.answers.pop(0)
        if isinstance(a, Exception):
            raise a
        content = ([SimpleNamespace(type="text", text="no tool")] if a.get("prose") else
                   [SimpleNamespace(type="tool_use", name=kw["tool_choice"]["name"], input=a)])
        return SimpleNamespace(stop_reason=a.get("stop", "tool_use"), content=content,
                               usage=SimpleNamespace(input_tokens=1000, output_tokens=50))


def run(rec, *answers, working_tree=True, **kw):
    """One encounter, held to this checkout's scenario file unless told otherwise."""
    model = Model(*answers)
    return asyncio.run(J.judge_record(rec, client=model, model="m",
                                      working_tree=working_tree, **kw)), model


def said(*answers):
    """A second reading: answers for items 1, 2, ... in the order it was shown them."""
    return {"answers": [{"k": k, "a": a} for k, a in enumerate(answers, 1)]}


ENCOUNTER = record(
    P("Hey guys."),
    C("dan", "Priya, last year's timeline doesn't apply here, and Monday the ninth is the date.",
      "t1_priya_interrupted", esci=["solicits_input", "respectful"]),
    P("Chris, what do you think?"),
    C("chris", "One team first. Sales is ready for Friday the 13th."),
    C("dan", "As I was saying, the pilot was my point.", "t2_idea_relabelled"),
)
CLEAN = {"beats": [{"i": 1, "v": "y"}, {"i": 4, "v": "p"}], "flags": []}


def _turns():
    return J.turns_of(ENCOUNTER)[0]


# --- the encounter as the judge reads it ---------------------------------------

def test_a_cut_line_is_judged_on_what_was_heard_and_a_lost_beat_is_counted():
    rec = record(
        P("Go on."),
        C("dan", "The ninth is the date and nobody moves it.", interrupted=True,
          heard_text="The ninth is"),
        C("dan", "Nobody heard this.", "t1_priya_interrupted", interrupted=True, heard_text=""),
        C("priya", "", "t5_a_smaller_second_try", transcript_missing=True),
        C("chris", "Fine.", cap_truncated=True, heard_text=None),
        P("   "))
    turns, lost = J.turns_of(rec)
    assert [(t["i"], t["at"], t["who"], t["text"]) for t in turns] == [
        (0, 0, "P", "Go on."), (1, 1, "dan", "The ninth is"), (2, 4, "chris", "Fine.")]
    assert turns[1]["cut"] and turns[2]["cut"] and lost == 2
    # A direction the record counts that no line carries (a reply withdrawn, a
    # character who never spoke) is lost too; a count the transcript already
    # accounts for adds nothing.
    assert J.turns_of(dict(rec, counts={"stage_directions": 5}))[1] == 5
    assert J.turns_of(dict(rec, counts={"stage_directions": 2}))[1] == 2
    assert J.turns_of(dict(rec, counts={"stage_directions": 1}))[1] == 2


# --- grounding -------------------------------------------------------------------

BRIEF = ("You know two teams' dates: Sales is ready for\nMonday the 9th and would go first. "
         "You say them in a few dry words, and never unasked. No speeches, ever.")


def test_a_quote_is_found_verbatim_whatever_the_punctuation_and_wrapping():
    span = J.locate("sales is ready for monday the 9th", BRIEF)
    assert span and BRIEF[span[0]:span[1]] == "Sales is ready for\nMonday the 9th"
    assert J.locate("“never unasked”", BRIEF) and J.locate("You know two teams dates", BRIEF)


def test_a_loose_quote_needs_four_words_and_most_of_them_in_one_place():
    assert J.locate("Sales is ready on Monday the 9th", BRIEF)        # one word swapped
    assert J.locate("never asked", BRIEF) is None                      # short: verbatim or nothing
    assert J.locate("Sales would say dates ever unasked", BRIEF) is None   # scattered words


def test_an_invented_quote_is_not_found():
    assert J.locate("Operations wants Monday the 30th", BRIEF) is None
    assert J.locate("", BRIEF) is None and J.locate("anything at all", "") is None


def test_the_source_is_the_sentence_holding_the_quote_and_says_where_it_is_from():
    span = J.locate("never unasked", BRIEF)
    assert J.sentence_around(BRIEF, *span) == "You say them in a few dry words, and never unasked."
    assert J.ground("never unasked", [("the scene", "Nothing here."), ("chris's brief", BRIEF)]) == (
        "chris's brief", "You say them in a few dry words, and never unasked.")
    assert J.ground("never unasked", [("the scene", "Nothing here.")]) is None
    origin, sentence = J.ground(FACT_OF_CHRISS, J._world(SRC))
    assert origin == "chris's brief, in which \"you\" is chris" and FACT_OF_CHRISS in sentence


# --- what code decides alone -------------------------------------------------------

def test_a_repeated_line_is_a_loop_only_for_the_same_character():
    turns, _ = J.turns_of(record(
        C("dan", "Monday the ninth is the date every team goes live."),
        C("chris", "Monday the ninth is the date every team goes live."),
        C("dan", "The ninth, Monday, is the date every team goes live!"),
        C("dan", "Fine."), C("dan", "Fine.")))
    assert J.loops(turns) == [{"i": 2, "of": 0}]


def test_the_stock_phrases_are_the_ones_every_brief_bans():
    from server import scenarios_v3

    prompt = scenarios_v3.compile_scenario(SID).cast[0].system_prompt.lower()
    talk = " ".join(
        prompt[prompt.index("## how you talk"):prompt.index("## keep the scene alive")].split())
    for phrase in J.STOCK_PHRASES:
        assert phrase in talk, f"{phrase!r} is no longer banned in _render_prompt"
    turns, _ = J.turns_of(record(P("Fair point."), C("chris", "That's a great point, Dan."),
                                 C("dan", "I appreciated nothing."), C("priya", "I hear you.")))
    assert J.stock(turns) == [{"i": 1, "q": "great point"}, {"i": 3, "q": "i hear you"}]


# --- the scenario it is held to ----------------------------------------------------

def test_the_scenario_source_names_every_character_and_every_trigger():
    from server import scenarios_v3

    spec = scenarios_v3.load_spec(SID)
    assert set(SRC["briefs"]) == set(spec["agents"]) and all(b["text"] for b in SRC["briefs"].values())
    planted = {t["id"]: t for i in spec["interactions"] for t in i["triggers"]}
    assert set(SRC["cues"]) == set(planted) == set(SRC["triggers"])
    assert all(SRC["triggers"][k] == {"speakers": [t["agent"]], "esci": t["esci"]}
               for k, t in planted.items())
    assert SRC["esci"] == list(spec["esci_items"]) and SRC["facts"] and SRC["scene"]
    json.dumps(SRC)                                       # plain data: the selftest stores it
    # A one-to-one interaction names its character itself; its triggers do not.
    one = J.source_of("S1A")
    assert one["triggers"]["t1_retaliation_fork"]["speakers"] == ["riley"]
    assert one["triggers"]["t3_defensiveness"]["speakers"] == ["sam"]
    with pytest.raises(FileNotFoundError):
        J.source_of("presentation_pileon")


def test_a_recorded_direction_is_held_to_the_scenarios_cue():
    assert J.spec_drift(_turns(), SRC) is None
    silence = f"They have gone quiet. Your next move: {SRC['cues']['t2_idea_relabelled'][1]}"
    assert J.spec_drift(J.turns_of(record(C("dan", "x", "t2_idea_relabelled",
                                            direction=silence)))[0], SRC) is None
    reworded, _ = J.turns_of(record(C("dan", "x", "t1_priya_interrupted",
                                      direction="Cut Priya off and ask for a vote on the date.")))
    assert J.spec_drift(reworded, SRC) == (
        "t1_priya_interrupted is worded differently in the scenario file")
    gone, _ = J.turns_of(record(C("dan", "x", "t99_dropped", direction="Do something.")))
    assert J.spec_drift(gone, SRC) == "t99_dropped is not in the scenario file"


def test_a_keep_going_probe_is_one_of_the_scenarios_own_lines_not_a_changed_scenario():
    # The runner records an `after_beats` move as keep_going_<n> (it cycles the
    # list, so the number does not say which line): a planted-trigger lookup
    # alone called every S3A encounter with a quiet participant "changed".
    s3 = J.source_of("S3A")
    assert s3["after"], "S3A lists after_beats; this test needs them"
    probe = f"They have gone quiet. Your next move, as Alex, now: {s3['after'][3]}"
    turns, _ = J.turns_of(record(C("alex", "What would you need in writing?", "keep_going_10",
                                   direction=probe), scenario="S3A"))
    assert J.spec_drift(turns, s3) is None
    other, _ = J.turns_of(record(C("alex", "x", "keep_going_1", direction="Sing a song."),
                                 scenario="S3A"))
    assert "worded differently" in J.spec_drift(other, s3)
    # S4A lists after_beats too (#214); S2A lists none.
    assert J.spec_drift(other, J.source_of("S2A")) == "keep_going_1 is not in the scenario file"


@pytest.mark.skipif(shutil.which("git") is None, reason="git is not installed")
def test_the_scenario_is_read_as_the_encounters_build_had_it_or_not_at_all():
    head = subprocess.run(["git", "rev-parse", "--short", "HEAD"], cwd=ROOT, text=True,
                          capture_output=True, check=True).stdout.strip()
    spec, unknown = J.spec_for(SID, head)
    assert spec["id"] == SID and unknown is None
    assert J.spec_for(SID, "0000000")[1] == "build 0000000 is not in this checkout"
    assert J.spec_for(SID, None)[1] == "no build recorded"
    # Never handed to git: a build is a commit id, not whatever a record says.
    for odd in ("HEAD", "main", "--help", "abc1234-dirty"):
        assert J.spec_for(SID, odd)[1] == f"build {odd} is not in this checkout"
    assert J.spec_for(SID, "0000000", working_tree=True)[1] is None


def test_an_encounter_whose_scenario_text_is_not_known_is_not_held_to_todays():
    answer = {"beats": [{"i": 1, "v": "y"}, {"i": 4, "v": "y"}],
              "flags": [{"i": 3, "c": "FACT", "q": FACT_OF_CHRISS}]}
    for build, why in ((None, "no build recorded"),
                       ("0000000", "build 0000000 is not in this checkout")):
        r, model = run(dict(ENCOUNTER, provenance={"build": build}), answer, working_tree=False)
        call = model.calls[0]
        assert r["spec_changed"] == why and r["passed"] and r["counts"]["FACT"] == 0
        assert "BRIEF" not in call["messages"][0]["content"] and "FACT" not in call["system"]
        assert call["tools"][0]["input_schema"]["properties"]["flags"]["items"][
            "properties"]["c"]["enum"] == ["ROLE", "SAFE"]
        assert r["beats"]["y"] == 2 and r["dropped"]["ungrounded"] == 1
        assert f"FACT, rule not judged: {why}" in J.summary(r)
        assert r["chances"]["of"] == len(SRC["esci"])       # the items are still the scenario's


def test_a_session_held_to_a_reworded_working_tree_is_judged_on_beats_and_the_fiction_only():
    rec = record(P("Hi."), C("dan", "Vote now.", "t1_priya_interrupted",
                             direction="Cut Priya off and call a vote on the date."))
    r, model = run(rec, {"beats": [{"i": 1, "v": "y"}], "flags": []})
    assert "BRIEF" not in model.calls[0]["messages"][0]["content"]
    assert r["passed"] and "worded differently" in r["spec_changed"]


# --- the first reading, checked against the text -----------------------------------

def test_a_flag_is_kept_only_when_its_quote_is_where_it_says():
    answer = {"beats": [{"i": 1, "v": "y"}, {"i": "4", "v": "n"}, {"i": 3, "v": "n"},
                        {"i": 9, "v": "y"}, {"i": 1, "v": "n"}],
              "flags": [
                  {"i": 3, "c": "FACT", "q": FACT_OF_CHRISS},             # in a brief
                  {"i": 3, "c": "FACT", "q": FACT_OF_CHRISS},             # said twice
                  {"i": 3, "c": "RULE", "q": RULE_OF_DANS},               # Dan's brief, not Chris's
                  {"i": 4, "c": "RULE", "q": RULE_OF_DANS},               # Dan's own
                  {"i": 4, "c": "ROLE", "q": "as an AI"},                 # not in the line
                  {"i": 4, "c": "SAFE", "q": "my point"},                 # in the line
                  {"i": 0, "c": "ROLE", "q": "Hey guys"},                 # the participant
                  {"i": 3, "c": "TONE", "q": "One team first"},           # no such code
                  "nonsense"]}
    cand = J.candidates(answer, _turns(), SRC)
    assert cand["first"] == {1: "y", 4: "n"}              # first verdict wins; 3 has no direction
    assert [(f["i"], f["c"]) for f in cand["flags"]] == [(3, "FACT"), (4, "RULE"), (4, "SAFE")]
    fact, rule, safe = cand["flags"]
    assert FACT_OF_CHRISS in fact["source"] and fact["origin"].startswith("chris's brief")
    assert rule["source"] == RULE_OF_DANS and rule["origin"] == "dan's own brief"
    assert safe["source"] is None and safe["origin"] is None and cand["ungrounded"] == 5


@pytest.mark.parametrize("answer", [{}, {"beats": []}, {"flags": []},
                                    {"beats": None, "flags": []}, {"beats": [], "flags": "none"}])
def test_an_answer_without_its_lists_is_no_verdict_not_nothing_found(answer):
    with pytest.raises(J.JudgeError, match="no beats or flags list"):
        J.candidates(answer, _turns(), SRC)


def test_only_missed_or_unruled_beats_and_grounded_flags_get_a_second_reading():
    cand = {"first": {1: "y"}, "ungrounded": 0,
            "flags": [{"i": 3, "c": "FACT", "q": "q", "source": "s", "origin": "the facts"}]}
    items = J.to_confirm(_turns(), cand)
    assert items == [{"kind": "BEAT", "i": 4},
                     {"kind": "FACT", "i": 3, "source": "s", "origin": "the facts"}]
    text = J.confirm_prompt(items, _turns(), "A scene\n in two lines.")
    assert text.startswith("SCENE: A scene in two lines.\n\n1 BEAT\nCONTEXT:\n  dan: Priya")
    assert "  P: Hey guys." not in text.split("2 FACT")[0]       # CONTEXT_TURNS back, no further
    assert "DIRECTION (to dan): Your next move" in text and "LINE (dan): As I was saying" in text
    # A flag gets the same context a beat does: "Yes. That's the date." means
    # nothing without the question it answers.
    assert text.endswith(
        "2 FACT  Does the LINE state as true something that contradicts the SOURCE?\n"
        "SOURCE (the facts): s\nCONTEXT:\n  P: Hey guys.\n  dan: Priya, last year's timeline "
        "doesn't apply here, and Monday the ninth is the date.\n  P: Chris, what do you think?\n"
        "LINE (chris): One team first. Sales is ready for Friday the 13th.")
    assert not J.confirm_prompt(items[:1], _turns(), "A scene.").startswith("SCENE")
    # The second reading is shown them the other way round and numbers them itself.
    assert J.confirm_prompt(items[::-1], _turns()).startswith("1 FACT")


def test_a_second_reading_is_read_by_the_order_it_was_shown():
    items = [{"kind": "BEAT", "i": 4}, {"kind": "FACT", "i": 3}]
    assert J.read_answers(said("none", "breach"), items) == {("BEAT", 4): "none", ("FACT", 3): "breach"}
    assert J.read_answers(said("ok", "done"), items[::-1]) == {("FACT", 3): "ok", ("BEAT", 4): "done"}
    twice = {"answers": [{"k": 1, "a": "none"}, {"k": "1", "a": "done"}, {"k": 7, "a": "ok"},
                         {"k": True, "a": "ok"}, "junk", {"k": 2}]}
    assert J.read_answers(twice, items) == {("BEAT", 4): "none"}
    for broken in ({}, {"answers": {"1": "breach"}}, {"answers": None}):
        with pytest.raises(J.JudgeError, match="no answers list"):
            J.read_answers(broken, items)


@pytest.mark.parametrize("first, second, cut, want", [
    ("n", ["none", "none"], False, "n"), ("n", ["none", "part"], False, "p"),
    ("n", ["done", "none"], False, "p"), (None, ["done", "done"], False, "y"),
    (None, ["none", "none"], False, "n"), (None, ["none", "done"], False, "p"),
    (None, ["part", "part"], False, "p"), ("n", ["none", "none"], True, "x"),
    (None, ["none", "none"], True, "x"), ("y", [], False, "y"), ("p", [], True, "p")])
def test_a_beat_is_not_made_only_when_every_reading_says_so(first, second, cut, want):
    turns, _ = J.turns_of(record(P("Go."), C("dan", "A line.", "t1_priya_interrupted",
                                             interrupted=cut)))
    cand = {"first": {1: first} if first else {}, "flags": [], "ungrounded": 0}
    assert J.settle(turns, cand, [{("BEAT", 1): a} for a in second])["beats"] == {1: want}


def test_a_flag_stands_only_when_every_second_reading_says_breach():
    flag = {"i": 3, "c": "FACT", "q": "q", "source": "s", "origin": "o"}
    cand = {"first": {1: "y", 4: "y"}, "flags": [flag], "ungrounded": 0}

    def settled(*answers):
        got = J.settle(_turns(), cand, [{("FACT", 3): a} for a in answers])
        assert got["beats"] == {1: "y", 4: "y"}
        return got["flags"], got["disputed"], got["unconfirmed"]

    assert settled("breach", "breach") == ([flag], [], 0)
    # Some say so and some do not: neither a finding nor nothing.
    assert settled("breach", "ok") == settled("ok", "breach") == ([], [flag], 0)
    assert settled("ok", "ok") == ([], [], 1)


@pytest.mark.parametrize("readings, where", [
    ([], "beat on turn 4"),                                             # no second reading at all
    ([{("FACT", 3): "breach"}], "beat on turn 4"),                     # the beat was not answered
    ([{("BEAT", 4): "none", ("FACT", 3): "breach"}, {("BEAT", 4): "none"}], "fact on turn 3"),
    ([{("BEAT", 4): "none", ("FACT", 3): "none"}], "fact on turn 3"),   # a beat's word on a flag
    ([{("BEAT", 4): "breach", ("FACT", 3): "ok"}], "beat on turn 4"),   # a flag's word on a beat
    ([{("BEAT", 4): "none", ("FACT", 3): None}], "fact on turn 3")])
def test_an_item_a_second_reading_did_not_answer_is_no_verdict_for_a_flag_as_for_a_beat(readings, where):
    cand = {"first": {1: "y"}, "ungrounded": 0,
            "flags": [{"i": 3, "c": "FACT", "q": "q", "source": "s", "origin": "o"}]}
    with pytest.raises(J.JudgeError, match=f"no second answer on the {where}"):
        J.settle(_turns(), cand, readings)


def test_chances_count_the_beats_that_were_made_per_esci_item():
    turns = [{"i": k, "who": "dan", "esci": e} for k, e in enumerate(
        [["a", "b"], ["a"], ["a", "a"], ["a"], ["b"], ["b"]])]
    got = J.chances(turns, {0: "y", 1: "p", 2: "y", 3: "n", 4: "x", 5: "y"}, ["a", "b", "c"])
    assert got == {"met": 1, "of": 3, "per_item": {"a": 3, "b": 2, "c": 0}}


# --- one encounter, end to end ------------------------------------------------------

def test_one_call_when_there_is_nothing_to_confirm():
    r, model = run(ENCOUNTER, CLEAN)
    assert r["passed"] and r["usage"] == {"calls": 1, "in": 1000, "out": 50}
    assert r["beats"] == {"y": 1, "p": 1, "n": 0, "x": 0} and r["flags"] == [] and r["missed"] == []
    assert r["disputed"] == [] and "disputed" not in J.summary(r)
    assert r["rubric"] == J.rubric_id() and r["model"] == "m" and r["lines"] == 3
    assert r["spec_changed"] is None
    call = model.calls[0]
    assert call["extra_body"] == {"temperature": 0} and "temperature" not in call
    assert call["tool_choice"] == {"type": "tool", "name": "report"}
    shown = call["messages"][0]["content"]
    for part in ("SCENE\n", "FACTS\n- ", "BRIEF dan (Dan", "BRIEF chris (", "CONVERSATION\n0 P: Hey guys.",
                 "3 chris: One team first.", "BEATS\n1 dan: Your next move", "4 dan: Your next move"):
        assert part in shown, part
    assert shown.count("Sales is ready for Friday the 13th") == 1


FOUND = {"beats": [{"i": 1, "v": "y"}, {"i": 4, "v": "n"}],
         "flags": [{"i": 3, "c": "FACT", "q": FACT_OF_CHRISS},
                   {"i": 4, "c": "RULE", "q": RULE_OF_DANS}]}


def test_a_confirmed_hard_flag_fails_the_encounter_and_a_beat_not_made_is_named():
    # Items: the beat on 4, the FACT on 3, the RULE on 4; the second of the two
    # readings is shown them in reverse.
    r, model = run(ENCOUNTER, FOUND, said("none", "breach", "breach"),
                   said("breach", "breach", "none"))
    assert not r["passed"] and r["usage"]["calls"] == 3
    assert r["counts"] == {"FACT": 1, "ROLE": 0, "SAFE": 0, "RULE": 1, "LOOP": 0, "STOCK": 0}
    assert [m["trigger"] for m in r["missed"]] == ["t2_idea_relabelled"] and r["beats"]["n"] == 1
    assert [c["tool_choice"]["name"] for c in model.calls] == ["report", "confirm", "confirm"]
    first, second = (c["messages"][0]["content"] for c in model.calls[1:])
    assert first.index("1 BEAT") < first.index("2 FACT") < first.index("3 RULE")
    assert second.index("1 RULE") < second.index("2 FACT") < second.index("3 BEAT")
    assert first.startswith("SCENE: ") and "the pilot was my point" in second
    assert all({"i", "at", "agent", "line", "c", "q", "source"} <= set(f) for f in r["flags"])
    line = J.summary(r)
    assert "\n" not in line and "FAIL  beats 1/2" in line and "FACT 1 ROLE 0 SAFE 0  rule 1" in line
    assert [d.split()[0] for d in J.details(r)] == ["FACT", "none"]
    assert [d.split()[0] for d in J.details(r, verbose=True)] == ["FACT", "RULE", "none"]
    # And it fails the sim scenario it came from, by name.
    assert J.failures(r)[0].startswith("judge: FACT t3 chris: \"One team first.")
    assert FACT_OF_CHRISS in J.failures(r)[0] and len(J.failures(r)) == 1


def test_a_soft_flag_alone_passes_and_a_flag_the_readings_dispute_is_listed_not_failed():
    found = {"beats": [{"i": 1, "v": "y"}, {"i": 4, "v": "y"}],
             "flags": [{"i": 3, "c": "FACT", "q": FACT_OF_CHRISS},
                       {"i": 4, "c": "RULE", "q": RULE_OF_DANS},
                       {"i": 4, "c": "ROLE", "q": "invented words"},
                       {"i": 4, "c": "SAFE", "q": "my point"}]}
    # Items: FACT on 3, RULE on 4, SAFE on 4 (the ROLE quote is not in the line).
    r, _ = run(ENCOUNTER, found, said("breach", "breach", "ok"), said("ok", "breach", "ok"))
    assert r["passed"] and r["counts"]["RULE"] == 1 and r["counts"]["FACT"] == 0
    assert [(f["c"], f["i"], f["at"]) for f in r["disputed"]] == [("FACT", 3, 3)]
    assert r["dropped"] == {"ungrounded": 1, "unconfirmed": 1} and J.failures(r) == []
    assert "rule 1 loop 0 stock 0  disputed 1  [" in J.summary(r)
    assert [d.split()[0] for d in J.details(r)] == ["?FACT"]
    assert [d.split()[0] for d in J.details(r, verbose=True)] == ["RULE", "?FACT", "dropped:"]


@pytest.mark.parametrize("answers, why", [
    ([RuntimeError("502 from the gateway")], "RuntimeError: 502"),
    ([{"beats": [], "flags": [], "stop": "max_tokens"}], "cut off"),
    ([{"prose": True}], "without the report tool"),
    ([{}], "no beats or flags list"),
    ([FOUND, {"answers": []}, {"answers": []}], "no second answer on the beat on turn 4"),
    ([FOUND, said("none", "breach", "breach"), {}], "no answers list"),
    ([FOUND, said("none", "breach", "breach"), said("breach", "none", "none")],
     "no second answer on the fact on turn 3"),
    ([FOUND, said("none", "breach", "breach"), RuntimeError("timed out")], "RuntimeError: timed out")])
def test_a_judge_that_cannot_answer_is_no_verdict_never_a_pass(answers, why):
    r, _ = run(ENCOUNTER, *answers)
    assert not r["passed"] and why in r["error"] and "beats" not in r
    assert "NO VERDICT" in J.summary(r) and J.details(r) == []
    assert J.failures(r) == [f"judge: no verdict: {r['error']}"]


def test_a_gateway_fault_reaches_the_report_without_its_key():
    r, _ = run(ENCOUNTER, RuntimeError("401 Invalid API key: sk-aB3dEfGh1jKlMn0pQrStUv"))
    assert "sk-aB3d" not in r["error"] and "401" in r["error"]


def test_an_encounter_with_no_character_line_or_no_scenario_file_is_no_verdict():
    r, model = run(record(P("Hello?")))
    assert "no character line" in r["error"] and model.calls == []
    r, model = run(record(P("Hi."), C("dan", "Hello."), scenario="presentation_pileon"))
    assert "no v3 scenario file" in r["error"] and model.calls == []


def test_a_lost_beat_and_a_repeated_or_stock_line_are_in_the_result_with_their_place():
    rec = record(
        P("Go on."),
        C("dan", "Monday the ninth is the date every team goes live."),
        C("dan", "Nobody heard this.", "t1_priya_interrupted", interrupted=True, heard_text=""),
        C("dan", "The ninth, Monday, is the date every team goes live."),
        C("chris", "I hear you, Dan."),
        counts={"stage_directions": 3})
    r, model = run(rec, {"beats": [], "flags": []})
    assert r["beats"] == {"y": 0, "p": 0, "n": 0, "x": 3} and len(model.calls) == 1
    assert [(f["c"], f["i"], f["at"], f["q"]) for f in r["flags"]] == [
        ("LOOP", 2, 3, "turn 1"), ("STOCK", 3, 4, "i hear you")]
    assert r["passed"] and "beats 0/0 lost 3" in J.summary(r) and "loop 1 stock 1" in J.summary(r)


def test_the_participants_line_is_capped_and_a_characters_is_not():
    long = " ".join(f"w{k}" for k in range(200))
    turns, _ = J.turns_of(record(P(long), C("dan", long)))
    text = J.find_prompt(turns, None)
    assert f"w{J.PARTICIPANT_WORD_CAP - 1} ..." in text and "0 P: w0 w1" in text
    assert f"1 dan: {long}" in text


def test_the_judge_is_the_one_named_else_the_directors(monkeypatch):
    from server import llm

    monkeypatch.setattr(llm, "_FILE", {})
    for name in ("JUDGE_MODEL", "DIRECTOR_MODEL"):
        monkeypatch.delenv(name, raising=False)
    assert J.judge_model() == J.DEFAULT_MODEL
    monkeypatch.setenv("DIRECTOR_MODEL", "nto.gemini-3.1-flash-lite")
    assert J.judge_model() == "nto.gemini-3.1-flash-lite"
    monkeypatch.setenv("JUDGE_MODEL", "gpt-6-astra")
    assert J.judge_model() == "gpt-6-astra"


# --- holding a sim run to it ----------------------------------------------------------

def _verdict(first, *second):
    return run(ENCOUNTER, {"beats": [{"i": 1, "v": first[0]}, {"i": 4, "v": first[1]}],
                           "flags": []}, *second)[0]


def test_a_sim_scenario_is_held_to_the_beats_its_baseline_made():
    both = _verdict("yy")
    entry = J.baseline_entry(both)
    assert entry == {"beats_made": 2, "beats_judged": 2, "rubric": J.rubric_id(), "model": "m"}
    assert J.failures(both, entry) == [] and J.failures(both) == []
    none = _verdict("nn", said("none", "none"), said("none", "none"))
    assert J.baseline_entry(none)["beats_made"] == 0 and none["passed"]
    assert J.failures(none, entry) == []                              # two fewer: the tolerance
    worse = J.failures(none, dict(entry, beats_made=3))
    assert worse == ["judge: 0 beats made against limit 1 (baseline 3); not made: "
                     "t1 t1_priya_interrupted, t4 t2_idea_relabelled; lost: 0"]
    assert J.failures(none, entry, made_drop=1) != [] and J.failures(none, entry, made_drop=3) == []
    # Lost beats shrink the count exactly as beats a character flubbed do.
    lost = dict(both, beats={"y": 1, "p": 0, "n": 0, "x": 7}, missed=[])
    assert J.failures(lost, dict(entry, beats_made=8)) == [
        "judge: 1 beats made against limit 6 (baseline 8); not made: none; lost: 7"]


def test_a_baseline_with_no_judge_entry_or_another_judges_cannot_pass_a_run():
    both = _verdict("yy")
    assert "has no judge entry: re-record" in J.failures(both, None, baselined=True)[0]
    assert J.failures(both, None, baselined=False) == []     # no baseline at all: compare() fails it
    entry = J.baseline_entry(both)
    for other in (dict(entry, rubric="deadbeef"), dict(entry, model="gpt-6-astra"),
                  {"beats_made": 2, "beats_judged": 2}):
        why = J.failures(both, other)
        assert len(why) == 1 and "re-record it" in why[0] and J.rubric_id() in why[0]


def test_a_sim_run_whose_scenario_text_was_not_judged_fails():
    # The sim check holds a run to this checkout, where the text is always
    # known: FACT and rule going unjudged there is a fault, not a caveat.
    r, _ = run(record(P("Hi."), C("dan", "Vote now.", "t1_priya_interrupted",
                                  direction="Cut Priya off and call a vote on the date.")),
               {"beats": [{"i": 1, "v": "y"}], "flags": []})
    assert r["passed"] and J.failures(r) == [
        "judge: FACT and rule were not judged: t1_priya_interrupted is worded differently "
        "in the scenario file"]


def _encounter_metrics():
    from tests.test_sim_analysis import HEALTH, _baseline_file, _encounter

    return A.summarize(*_encounter()), HEALTH, _baseline_file()


def _report(judged, base, m, health):
    return check.build_report("abc1234", "--build", "", health,
                              {"S2A": {"metrics": m, "judge": judged}}, base)


def test_the_report_carries_the_verdict_and_fails_with_it():
    m, health, base = _encounter_metrics()
    clean, _ = run(ENCOUNTER, CLEAN)
    base["scenarios"]["S2A"]["judge"] = J.baseline_entry(clean)
    ok = _report(clean, base, m, health)
    assert ok["passed"] and ok["scenarios"]["S2A"]["judge"]["beats"]["y"] == 1

    flagged, _ = run(ENCOUNTER, FOUND, said("part", "breach", "ok"), said("ok", "breach", "part"))
    bad = _report(flagged, base, m, health)
    assert flagged["counts"]["FACT"] == 1 and not bad["passed"] and not bad["scenarios"]["S2A"]["passed"]
    assert len(bad["failures"]) == 1 and bad["failures"][0].startswith("S2A: judge: FACT t3 chris")

    lost, _ = run(ENCOUNTER, RuntimeError("gateway down"))
    assert _report(lost, base, m, health)["failures"] == [f"S2A: judge: no verdict: {lost['error']}"]

    base["scenarios"]["S2A"]["tolerances"] = {"judge_made_drop": 0}
    missed, _ = run(ENCOUNTER, {"beats": [{"i": 1, "v": "y"}, {"i": 4, "v": "n"}], "flags": []},
                    said("none"), said("none"))
    worse = _report(missed, base, m, health)
    assert not worse["passed"] and "1 beats made against limit 2 (baseline 2)" in worse["failures"][0]

    del base["scenarios"]["S2A"]["judge"]
    unbaselined = _report(clean, base, m, health)
    assert not unbaselined["passed"] and "has no judge entry" in unbaselined["failures"][0]
    plain = check.build_report("abc1234", "--build", "", health, {"S2A": {"metrics": m}}, base)
    assert plain["passed"] and "judge" not in plain["scenarios"]["S2A"]


def test_the_judge_tolerance_is_in_the_committed_baseline():
    base = json.loads((ROOT / "tools" / "sim" / "baseline.json").read_text(encoding="utf-8"))
    assert base["tolerances"]["judge_made_drop"] == A.DEFAULT_TOLERANCES["judge_made_drop"] == J.MADE_DROP


def test_a_baseline_is_not_recorded_without_the_judge(capsys):
    with pytest.raises(SystemExit) as stop:
        check.main(["--write-baseline", "--no-judge"])
    assert stop.value.code == 2 and "cannot take --no-judge" in capsys.readouterr().err


EVENTS = [
    {"type": "session_start", "t": 0.0, "scenario": SID},
    # A build the record names is NOT what a sim run is held to: its server ran
    # this checkout, uncommitted edits included.
    {"type": "realtime_session_started", "t": 0.1, "build": "0000000"},
    {"type": "user_turn", "t": 2.0, "text": "Hey guys.", "spoken_at": 1.0},
    {"type": "stage_direction", "t": 2.5, "trigger_id": "t1_priya_interrupted"},
    {"type": "steering_pair", "t": 5.0, "page_turn": 0,
     "actor": {"agent_id": "dan", "text": "Monday the ninth.", "interrupted": False},
     "direction": {"stage_direction": f"Now: {SRC['cues']['t1_priya_interrupted'][0]}",
                   "trigger_id": "t1_priya_interrupted", "esci": ["respectful"]}},
    {"type": "stage_direction", "t": 6.0, "trigger_id": "t2_idea_relabelled"},
    {"type": "session_end", "t": 9.0}]


def test_the_sim_check_judges_the_record_a_rater_would_read_against_this_checkout():
    rec = J.record_of_events(EVENTS)
    assert rec["scenario"] == SID and [t["who"] for t in J.turns_of(rec)[0]] == ["P", "dan"]
    assert rec["provenance"]["build"] == "0000000"
    model = Model({"beats": [{"i": 1, "v": "y"}], "flags": []})
    r = J.judge_events(EVENTS, session_id="s_sim", client=model, model="m")
    assert r["passed"] and r["session_id"] == "s_sim" and r["spec_changed"] is None
    assert "BRIEF dan" in model.calls[0]["messages"][0]["content"]
    assert r["beats"] == {"y": 1, "p": 0, "n": 0, "x": 1}      # the second direction never spoken
    broken = J.judge_events([{"type": "session_start", "scenario": SID}], client=Model())
    assert not broken["passed"] and "no character line" in broken["error"]


# --- the command ------------------------------------------------------------------------

def _session(root, name, events=EVENTS):
    d = root / name
    d.mkdir(parents=True)
    (d / "events.jsonl").write_text("".join(json.dumps(e) + "\n" for e in events),
                                    encoding="utf-8")
    return d


def test_the_command_says_one_line_per_encounter_and_exits_on_the_verdict(tmp_path, capsys):
    d = _session(tmp_path, "s_one")
    out = tmp_path / "out.json"
    clean = {"beats": [{"i": 1, "v": "y"}], "flags": []}
    assert J.main([str(d), "--working-tree", "--json", str(out)], client=Model(clean)) == 0
    printed = capsys.readouterr().out.splitlines()
    assert len(printed) == 1 and printed[0].startswith("S4A  s_one  pass  beats 1/1 lost 1")
    assert json.loads(out.read_text(encoding="utf-8"))[0]["session_id"] == "s_one"

    breach = {"beats": [{"i": 1, "v": "y"}], "flags": [{"i": 1, "c": "SAFE", "q": "Monday"}]}
    assert J.main([str(d), "--working-tree"],
                  client=Model(breach, said("breach"), said("breach"))) == 1
    printed = capsys.readouterr().out.splitlines()
    assert "FAIL" in printed[0] and printed[1].strip().startswith("SAFE t1 dan")

    # Held to the build the record names by default, and that one is not here.
    assert J.main([str(d)], client=Model(clean)) == 0
    assert "FACT, rule not judged: build 0000000 is not in this checkout" in capsys.readouterr().out


def test_a_sweep_skips_short_encounters_and_judges_a_session_once(tmp_path, capsys):
    d = _session(tmp_path, "s_one")
    clean = {"beats": [{"i": 1, "v": "y"}], "flags": []}
    # One character line: under a sweep's floor of MIN_TURNS, so nothing is judged.
    assert J.main(["--all", str(tmp_path)], client=Model()) == 1
    assert "skipped 1" in capsys.readouterr().out
    # Named and swept: one encounter, one model call.
    model = Model(clean)
    assert J.main([str(d), "--all", str(tmp_path), "--min-turns", "1"], client=model) == 0
    assert len(model.calls) == 1 and len(capsys.readouterr().out.splitlines()) == 1
    # An explicit floor holds for a named session too.
    assert J.main([str(d), "--min-turns", "10"], client=Model()) == 1


@pytest.mark.parametrize("argv, why", [
    (["{tmp}"], "no events.jsonl"), (["--all", "{tmp}"], "no session under"),
    ([], "name a session directory"), (["--all", "{tmp}", "--limit", "-1"], "cannot be negative")])
def test_the_command_refuses_what_it_cannot_judge(argv, why, tmp_path, capsys):
    with pytest.raises(SystemExit) as stop:
        J.main([a.format(tmp=tmp_path) for a in argv], client=Model())
    assert stop.value.code == 2 and why in capsys.readouterr().err


def test_the_judge_calls_the_pinned_sdk_with_arguments_it_accepts():
    """Every argument _ask passes must be one the installed SDK's
    messages.create takes. anthropic 1.0.0 dropped `temperature`, and a judge
    that passed it was no verdict on every encounter."""
    import inspect

    from anthropic import AsyncAnthropic

    accepted = set(inspect.signature(AsyncAnthropic(api_key="x").messages.create).parameters)
    model = Model(CLEAN)
    asyncio.run(J.judge_record(ENCOUNTER, client=model, model="m", working_tree=True))
    assert set(model.calls[0]) <= accepted, set(model.calls[0]) - accepted
