"""What the final prompt is allowed to say twice.

An actor's brief is assembled from layers: the scene, the character's own file,
the persona knobs, the situational updates, one universal block about how anyone
speaks out loud, and — last — the note about this moment. Each layer has one
subject, and nothing belongs in two of them.

It did not start that way. A single S1A/Sam prompt carried SIX length rules at
once: one in the YAML brief, a second from the v3 composer's "## Manner", a
third from the persona's mid-band verbosity fragment, a fourth from the mode
line in engine.py, a fifth from the VOICE block in realtime_voice_session.py,
and a sixth in the other mode branch for 1:1. They disagreed — "one to three
sentences", "natural-length for speech", "at most about 25 words" — and the cost
was measured on the gateway rather than assumed. Same brief, same three
participant turns: the shipped stack produced 27/37/26-word turns; the tightest
rule ALONE produced 11/24/25; no rule at all produced 29/51/53. Six rules landed
almost exactly halfway between one clear rule and no rule, which is to say the
duplication bought nothing and broke the cap it was trying to enforce.

Three of those six are in files this module's tests cover (engine, persona,
realtime). The other three are in the scenario layer, which is authored
elsewhere; the assembly's job is to state its own rule once, state it tight, and
state it as a SUBSET of what a brief says, so the layers never contradict even
while the bank is being rewritten. That is what these tests hold down — nobody
noticed six rules accumulating because nothing ever counted them.

The prompts are built through the real call path, including the runner's own
_instructions, because the duplication lived in the seam between the two files
and not inside either one.
"""

from __future__ import annotations

import re
import sys
import types
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from server import engine as engine_mod                        # noqa: E402
from server.engine import AgentEngine, MEETING_RULES, SPEECH_RULES  # noqa: E402
from server.persona import Persona                             # noqa: E402
from server.realtime_voice_session import (                    # noqa: E402
    END_SEGMENT_TOOL,
    RealtimeVoiceSessionRunner,
)
from server.scenarios_v3 import available, compile_scenario     # noqa: E402


# Every way the layers this module owns have ever expressed "keep it short".
# A new one appearing in engine/persona/realtime is the regression: it means
# the rule is being said in a second place again, in a second set of words.
_LENGTH_RULES = (
    r"one or two sentences",
    r"one to three sentences",
    r"\bone sentence, two at most\b",
    r"\b25 words\b",
    r"twenty-five words",
    r"natural-length",
    r"match the length",
    r"keep (?:it|replies|every turn) short",
)


def _assembled(scenario_id="S1A", agent_id=None, *, group=False, persona=None,
               director_note=""):
    """The exact string the realtime runner hands the gateway, for one character.

    Built through RealtimeVoiceSessionRunner._instructions rather than
    engine._system_prompt alone: the runner is where the VOICE and MEETING
    duplicates lived, so a test that stopped at the engine would not have seen
    any of them.
    """
    scenario = compile_scenario(scenario_id)
    agent = (next(a for a in scenario.cast if a.id == agent_id) if agent_id
             else scenario.cast[0])
    persona = persona or Persona(**(getattr(agent, "defaults", None) or {}))
    eng = AgentEngine(agent, scenario, persona, client=object())
    runner = types.SimpleNamespace(
        session=types.SimpleNamespace(engines={agent.id: eng},
                                      triggered_branches=[]),
        agent=agent,
        agent_id=agent.id,
        _scene_note="",
        is_group=lambda: group,
    )
    return RealtimeVoiceSessionRunner._instructions(runner, director_note), agent


def _assembly_only(prompt: str, agent) -> str:
    """The prompt minus the layer this module does not own.

    The character's brief is written in the scenario files and may say what it
    likes about its own character; these tests are about what the ASSEMBLY adds
    around it.
    """
    brief = (agent.system_prompt or "").strip()
    return prompt.replace(brief, "\n[CHARACTER BRIEF]\n") if brief else prompt


# --- one rule, one place -----------------------------------------------------

@pytest.mark.parametrize("scenario_id", available())
def test_the_assembly_states_the_length_rule_exactly_once(scenario_id):
    """Across every character in the bank, in both modes."""
    scenario = compile_scenario(scenario_id)
    for agent in scenario.cast:
        for group in (False, True):
            prompt, _ = _assembled(scenario_id, agent.id, group=group)
            body = _assembly_only(prompt, agent)
            where = f"{scenario_id}/{agent.id} group={group}"
            assert body.count(SPEECH_RULES) == 1, (
                f"{where}: engine.SPEECH_RULES appears "
                f"{body.count(SPEECH_RULES)} times"
            )
            # One rule may of course use more than one phrase to say itself.
            # What must not exist is a SECOND site saying it again elsewhere.
            rest = body.replace(SPEECH_RULES, "")
            hits = [m.group(0) for pat in _LENGTH_RULES
                    for m in re.finditer(pat, rest, re.I)]
            assert not hits, (
                f"{where}: the assembly states the length of a turn a second "
                f"time, outside SPEECH_RULES — {hits}. One rule, in one place, "
                "and the tight one; see this module's docstring for what the "
                "extra copies were measured to cost."
            )


def test_the_one_rule_is_the_tight_one():
    """A cap the model obeys, not a cap that sounds reasonable.

    "natural-length for speech" and "one to three sentences" were both in the
    shipped stack and both are looser than what the model does unprompted, so
    they could only ever be decoration. The word count is the version that
    measurably moved the replies.
    """
    assert "twenty-five words" in SPEECH_RULES
    assert "one or two sentences" in SPEECH_RULES


def test_the_universal_rules_do_not_come_back_in_the_voice_layer():
    """The VOICE block was a verbatim second copy with a different number.

    It is gone; SPEECH_RULES is emitted by the engine for both the text and the
    voice paths. This asserts the seam stays closed, since re-adding a block in
    _instructions is the natural way to "just make the voice path stricter".
    """
    prompt, agent = _assembled("S1A")
    assert prompt.count(SPEECH_RULES) == 1
    assert "VOICE:" not in prompt
    assert "MEETING:" not in prompt


def test_group_mode_adds_the_floor_rules_and_no_second_length_rule():
    prompt, _ = _assembled("S4A", group=True)
    assert MEETING_RULES in prompt
    assert prompt.count(SPEECH_RULES) == 1
    # The floor rules are about who talks, never about how long for.
    for pat in _LENGTH_RULES:
        assert not re.search(pat, MEETING_RULES, re.I), pat


def test_a_one_to_one_segment_of_a_group_scenario_is_not_told_others_may_speak():
    """The hard-won mode flag. A scenario is stamped mode="group" if ANY of its
    interactions is, so S3's 1:1 series would otherwise be handed the meeting
    framing and every actor in it would wait for colleagues who are not there.
    The runner passes the CURRENT interaction's mode; this is that contract.
    """
    scenario = compile_scenario("S3A")
    assert scenario.mode == "group", "S3A should still be a mixed scenario"
    solo, _ = _assembled("S3A", group=False)
    assert MEETING_RULES not in solo
    room, _ = _assembled("S3A", group=True)
    assert MEETING_RULES in room


# --- the persona layer -------------------------------------------------------

def test_a_neutral_persona_says_nothing():
    """Five mid-band sentences, byte-identical for all sixteen characters, used
    to be the last content block in every prompt ever assembled. Two of them
    were wrong there: one was a fourth length rule, and the other told an actor
    briefed to talk over a colleague to leave a little space."""
    assert Persona().tone_fragments() == []
    prompt, _ = _assembled("S1A")
    assert "## Tone and manner" not in prompt
    assert "Match the length the person seems to want" not in prompt
    assert "Leave a little space" not in prompt


def test_a_knob_off_neutral_still_speaks():
    """Suppressing the mid band must not suppress the dial itself."""
    warm = Persona(warmth=0.9, restraint=0.1)
    fragments = warm.tone_fragments()
    assert len(fragments) == 2, fragments
    prompt, _ = _assembled("S1A", persona=warm)
    assert "## Tone and manner" in prompt
    for f in fragments:
        assert f in prompt


def test_the_incivility_arm_is_never_told_it_is_an_experiment():
    """"## Incivility behaviors (active, research dial)" rendered ONLY when a
    knob was up — that is, only in the incivility arm, the one arm where an
    actor stepping outside the fiction costs the most. A character brief is not
    the place to name the manipulation."""
    rude = Persona(condescension=0.9, sarcasm=0.8)
    prompt, _ = _assembled("S1A", persona=rude)
    assert rude.incivility_fragments(), "the dial must still produce behaviour"
    lowered = prompt.lower()
    for leak in ("research dial", "incivility", "manipulation", "condition",
                 "experiment"):
        assert leak not in lowered, f"the actor is told it is an experiment: {leak!r}"


def test_the_persona_speaks_about_a_person_not_a_user():
    """"the user" is the clinical noun for whoever is on the other side. It sat
    inside instructions to be condescending and dismissive, where the register
    clash is loudest."""
    rude = Persona(condescension=0.9, dismissiveness=0.9, sarcasm=0.9,
                   passive_aggression=0.9)
    for fragment in rude.incivility_fragments():
        assert "user" not in fragment.lower(), fragment


# --- this moment: the director note -----------------------------------------

def _runner_for(scenario_id="S1A", agent_id=None):
    prompt, agent = _assembled(scenario_id, agent_id)
    return types.SimpleNamespace(agent=agent), prompt


def test_the_moment_note_arrives_last_and_wins():
    """A note about THIS turn has to outrank the standing rules above it, and on
    nto.gemini-live-2.5-flash it has exactly one chance to arrive: a mid-session
    session.update is never acknowledged there, so the opening prompt is the
    whole of the instruction and position in it is the only emphasis available.
    """
    runner, prompt = _runner_for()
    note = RealtimeVoiceSessionRunner._director_note(runner, "say the thing")
    assert (prompt + note).endswith(note)
    assert "say the thing" in note
    # The marker the runner's own tests and the steering log are keyed to.
    assert "DIRECTOR NOTE" in note


def test_the_engine_puts_its_own_director_note_after_the_standing_rules():
    """The text path's equivalent. It used to sit ABOVE the mode line, i.e. the
    last thing the actor read before speaking was a generic rule about speech
    rather than the thing it was supposed to do."""
    prompt, _ = _assembled("S1A", director_note="press on the authorship")
    assert prompt.index("press on the authorship") > prompt.index(SPEECH_RULES)


def test_the_moment_note_is_addressed_to_the_actor_in_the_second_person():
    """"Bring about this beat now" over a cue like "Sam defends: 'I did most of
    the legwork anyway'" handed Sam a note about Sam in the third person —
    twenty lines under a brief forbidding both third-person self-reference and
    reading stage directions aloud. The beat still fired in most samples, but
    the actor was resolving that contradiction on the one turn the encounter is
    scored on. The wrapper now frames the cue as this character's own next move,
    which reads correctly whether the cue is written as "Sam defends: '...'" or
    as "Defend yourself before you have thought about it" — the cues live in the
    scenario bank and this layer does not rewrite them.
    """
    scenario = compile_scenario("S1A")
    agent = scenario.cast[0]
    runner = types.SimpleNamespace(agent=agent)
    trigger = scenario.interactions[0]["triggers"][0]

    cue_note = RealtimeVoiceSessionRunner._trigger_instruction(
        runner, trigger, probing=False)
    assert trigger["cue"] in cue_note, "the beat's own words must survive verbatim"
    assert agent.name in cue_note, "the actor is told the note is about them"
    for craft in ("beat", "director", "stage direction"):
        assert craft not in cue_note.lower(), f"production vocabulary: {craft}"


def test_the_silence_probe_does_not_call_the_other_person_a_participant():
    """A research subject is who they are to the study, not who they are to the
    character standing in front of them. It was the most frequent noun in the
    prompt."""
    scenario = compile_scenario("S1A")
    runner = types.SimpleNamespace(agent=scenario.cast[0])
    trigger = scenario.interactions[0]["triggers"][0]
    probe = RealtimeVoiceSessionRunner._trigger_instruction(
        runner, trigger, probing=True)
    assert trigger["on_silence"] in probe, "the probe's own words, verbatim"
    assert "participant" not in probe.lower()


@pytest.mark.parametrize("scenario_id", available())
def test_every_planted_beat_survives_the_wrapper(scenario_id):
    """The wrapper may be rephrased; the beat may not be paraphrased.

    The cue and the on_silence line ARE the measurement — a rater scores what
    the participant did with them — so whatever the assembly wraps around them
    has to pass them through byte for byte, in both the cue and probe forms.
    """
    scenario = compile_scenario(scenario_id)
    agent_by_id = {a.id: a for a in scenario.cast}
    for inter in scenario.interactions:
        for trig in inter.get("triggers", []):
            named = trig.get("agent")
            agent = agent_by_id.get(named, scenario.cast[0])
            runner = types.SimpleNamespace(agent=agent)
            cue = RealtimeVoiceSessionRunner._trigger_instruction(
                runner, trig, probing=False)
            assert trig["cue"] in cue, f"{scenario_id} {trig.get('id')}"
            if trig.get("on_silence"):
                probe = RealtimeVoiceSessionRunner._trigger_instruction(
                    runner, trig, probing=True)
                assert trig["on_silence"] in probe, f"{scenario_id} {trig.get('id')}"


# --- the one tool the actor holds -------------------------------------------

def test_the_end_tool_does_not_invite_what_the_brief_forbids():
    """The actor holds this tool and a brief saying "never end it yourself"
    at the same time. The old description invited the opposite — "reached its
    natural end, the matter has been addressed" — so an actor could truncate a
    scored interaction the moment the argument felt settled, several beats
    before the beats were spent, and nothing in the record would say why the
    encounter was short. One rule now: the other person ends it, or it does not
    end.
    """
    description = END_SEGMENT_TOOL["description"].lower()
    assert "natural end" not in description
    assert "matter has been addressed" not in description
    assert "the other person has ended the conversation" in description


# --- the shape of the thing --------------------------------------------------

def test_the_universal_block_is_short_enough_to_be_read():
    """It is the last thing before the moment note and it is in every prompt
    sixteen characters ever get. Six overlapping rules were long; one rule that
    grows back into a policy document is the same failure with fewer headings.
    """
    assert len(SPEECH_RULES.split()) <= 90, SPEECH_RULES
    assert len(MEETING_RULES.split()) <= 70, MEETING_RULES


def test_the_layers_are_still_named_in_the_engine():
    """These tests hold the invariant; the comment in engine.py is what tells
    the next person which layer a new rule belongs in. Losing it is how the
    duplication came back the first time.
    """
    source = Path(engine_mod.__file__).read_text(encoding="utf-8")
    assert "SPEECH_RULES" in source and "MEETING_RULES" in source
    assert "said ONCE" in source
