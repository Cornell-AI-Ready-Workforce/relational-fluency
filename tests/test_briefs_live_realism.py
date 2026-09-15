"""The character briefs, held to what the LIVE realtime model does with them.

Five rounds measured naturalness and parity through engine._system_prompt on
the TEXT model. That proxy said the three influence managers spoke 26 words a
turn and were level with each other. Driven on the configured realtime model
(nto.gemini-live-2.5-flash through the Cornell gateway, Windows TTS as the
microphone, the runner's own connect-time brief and commit rule) with a LOST
participant — the researcher's own lines from 2026-09-14: "Hello.", "Good
morning.", "What about it?", "I need more context. What do you mean?", "Yeah,
I'll work on it, I guess" — the same briefs produced:

  * S2C Imani: "The pack." / "The numbers." / "The pack. Monday's." — 40% of
    replies three words or fewer, mean 7.0 words, median 5 (FRAG lane, 2 runs,
    20 replies). The brief said "Fragments — a refusal that does not bother
    with a sentence built round it", and the live model took it literally.
  * every S2 form: the interaction's `opening:` line never spoken, 21 of 21
    runs — it travels only by a mid-session session.update, which this model
    never acknowledges (0 of ~30 acks today), so the first thirty seconds were
    "Hello" -> "The pack." with nothing for the participant to hold on to.
  * S1B Drew, to a VAGUE participant ("I heard you", "I'll work on it",
    "Okay."): the same demand in 7 of 10 replies ("What are you going to do
    about the handoff?" / "What can you do?" / "When will it be fixed?" ...),
    because the brief's own anti-repetition rule told him to "ask them what
    exactly they are going to do about the handoff, by when" instead — the
    escape hatch WAS the loop.
  * S4A Dan: no fragments at all (mean 10.9 words, 0 of 12 replies under six
    words; a dominator, not noise) but "Dan's got the schedule here" — his own
    name in the third person on 4 of 12 turns.

Every claim in these docstrings is from the live model, not the text proxy.
The tests themselves are offline: they read the compiled specs and the
composed prompt, and pin the wording that was measured to hold live, so that
the next rewording is measured before it lands rather than after.

Measured AFTER (same driver, same scripts, one run each unless stated):
  S2C hesitant: mean 14.8, 0 replies <= 5 words, every kind of lost got its
    own move ("Good morning. What about the pack?" / "It costs most of your
    team's week. There is a queue three weeks out behind it. Is that why you
    booked this?" / "The pack is the numbers your team sends up every Monday
    morning. Which part of it are you here about?" / heat -> "It is my name on
    Monday's call."), framing spoken on the first turn.
  S2B hesitant: mean 18.5, framing first, 1 near-repeat in 11.
  S2A hesitant: mean 23.5, framing first, 0 repeats.
  S1B vague: 7 different questions where BEFORE gave one demand seven times;
    after "I'll work on it" he pins one specific ("When will it be fixed
    by?"), after "Okay." the practical next thing ("I need a day to tell
    Priya").
The two intermediate rewrites that did NOT hold are recorded in the tests that
replaced them: a "whole sentences" clause alone made Imani recite one obstacle
sentence verbatim on 9 of 11 turns, and a coarse lost-protocol keyed on "what
about it or what do you mean" made her recite one handhold on 6 of 11.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from server import scenarios_v3 as v3  # noqa: E402

INFLUENCE = (("S2A", "morgan"), ("S2B", "sasha"), ("S2C", "imani"))
PUSHERS = (("S1A", "riley"), ("S1B", "mel"), ("S1C", "nadia"))
COUNTERPARTS = (("S1A", "sam"), ("S1B", "drew"), ("S1C", "wes"))
DOMINANT = (("S4A", "dan"), ("S4B", "dan"), ("S4C", "hugo"))


def flat(text: str) -> str:
    """One line, single-spaced, lower case: the briefs are hard-wrapped."""
    return " ".join((text or "").split()).lower()


def norm(text: str) -> str:
    """One line, single-spaced, case kept: for the byte-identical parity claims."""
    return " ".join((text or "").split())


def brief(sid: str, aid: str) -> str:
    return v3.load_spec(sid)["agents"][aid]["system_prompt"]


def one_to_one_openers():
    """(sid, agent id, interaction) for every 1:1 interaction that opens a
    scene — the ones whose `opening:` line the participant is supposed to
    hear, and on the configured model never does."""
    for sid in v3.available():
        spec = v3.load_spec(sid)
        for inter in spec.get("interactions", []):
            if inter.get("mode") != "one_to_one":
                continue
            if not str(inter.get("opening") or "").strip():
                continue
            yield sid, inter["agent"], inter


# --------------------------------------------------------------------------
# (a) The influence managers: dry, short and unhelpful, and still somebody a
#     lost person can converse with.
# --------------------------------------------------------------------------

WHOLE_SENTENCE_CLAUSE = norm("""
    Whole short sentences — a refusal is still a sentence, with the thing you
    are refusing in it, so there is something in it for them to answer. A
    different one each time, and no stock word you fall back on when you do
    not know what to say. A noun on its own is not a turn.
""")


@pytest.mark.parametrize("sid,aid", INFLUENCE)
def test_no_influence_manager_is_told_to_speak_in_fragments(sid, aid):
    """Live, S2C hesitant, real brief: 8 of 20 replies were three words or
    fewer ("The pack." / "The numbers." / "The pack. Monday's."), which is
    the researcher's transcript almost line for line. The same bullet did
    not fragment Morgan or Sasha, but it is the identical clause in all three
    files and the three forms must key the actor the same way."""
    low = flat(brief(sid, aid))
    assert "fragments" not in low, (
        f"{sid}: the brief still tells {aid} to speak in fragments; on the "
        "live model that is 2-5 word noun phrases nobody can converse with")
    assert WHOLE_SENTENCE_CLAUSE in norm(brief(sid, aid)), (
        f"{sid}: the whole-sentence clause is missing or reworded; it is held "
        "byte-identical across the three forms because the tempo bullet is "
        "the first thing the actor reads about how to talk")


def test_the_tempo_word_of_each_manager_survives_in_front_of_the_shared_clause():
    """Warm, fast, dry: the ONLY dimension the three are allowed to differ in.
    The rewrite replaced the shared tail of the bullet and nothing before it."""
    heads = {
        "S2A": "warm and short. guilt makes you want this over with, not drawn out.",
        "S2B": "fast and short.",
        "S2C": "dry and short.",
    }
    for sid, aid in INFLUENCE:
        low = flat(brief(sid, aid))
        assert heads[sid] + " whole short sentences" in low, sid


LOST_SKELETON = (
    "They may not know how to start, and nothing they say while they are lost "
    "is a push, so none of it gets an obstacle.",
    "Every reply of yours starts from a word THEY just used, so no two of them "
    "come out alike, and each kind of lost gets its own move, once",
    "Until they push there is nothing to refuse, and you never refuse the same "
    "thing twice.",
)

# The kinds of lost the protocol has to name, because the live model matches
# the participant's words to a case and repeats within a case: with a single
# case for "what about it, or what do you mean" Imani said the same handhold
# on 6 of 11 turns; with these seven she said no sentence twice except where
# the runner's truncated-audio retry replayed a reply.
LOST_CASES = (
    "a hello after your first turn",
    "what about it:",
    "what do you mean, or more context:",
    "i am not understanding you:",
    "what should i do:",
    "or any heat:",
    "i will work on it, or nothing at all:",
)


@pytest.mark.parametrize("sid,aid", INFLUENCE)
def test_the_lost_participant_protocol_is_in_the_brief_and_keyed_on_their_words(sid, aid):
    """The live model does not track what it has already said. Told "you do
    not say the same sentence twice" and "said once, then it is spent", Imani
    recited one 24-word obstacle sentence on 9 of 11 turns and Sasha the
    framing line on 4 of 4 greetings. What held was variety keyed on the
    PARTICIPANT's words: a distinct move per kind of lost, each starting from
    a word they used. After: S2C 0 verbatim repeats across 11 replies
    (excluding the runner's retry echo), S2B 1, S2A 0."""
    text = brief(sid, aid)
    low = flat(text)
    assert "if they are lost:" in low, f"{sid}: no lost-participant protocol"
    tail = norm(text.split("If they are lost:", 1)[1])
    for sentence in LOST_SKELETON:
        assert sentence in tail, f"{sid}: lost protocol reworded: {sentence!r}"
    for case in LOST_CASES:
        assert case in tail.lower(), f"{sid}: the protocol lost the case {case!r}"


def test_the_lost_protocol_ends_every_brief_because_position_is_the_only_emphasis():
    """On the configured model the connect-time prompt is the whole of the
    instruction (mid-session updates are never acknowledged), so what the
    actor reads last wins. The protocol sits after the last bullet of the
    character's own brief, immediately before the engine's speech rules."""
    for sid, aid in INFLUENCE:
        text = norm(brief(sid, aid))
        assert "If they are lost:" in text
        assert text.endswith("you never refuse the same thing twice."), (
            f"{sid}: something follows the lost protocol; it must be the last "
            "thing in the character's brief")
        assert text.index("If they are lost:") > text.index("When it goes sideways:"), sid


def test_the_heat_move_is_each_managers_own_and_ends_without_a_question():
    """Live, before: "Why are you so obsessed with this?" was answered with the
    same handhold as "what about it". The brief already says how each manager
    answers heat (shorter / gentler / flatter, and firmer); the protocol routes
    heat to that, and ends it on a statement so the lost participant is not
    handed another question to fail."""
    expect = {"S2A": "gentler and firmer", "S2B": "flatter and firmer", "S2C": "shorter and firmer"}
    for sid, aid in INFLUENCE:
        low = flat(brief(sid, aid))
        assert f"or any heat: {expect[sid]}, the way you always answer heat." in low, sid
        assert "then stop, with no question on the end." in low, sid


def test_morgans_protocol_forbids_the_thank_you_the_live_model_reached_for():
    """Live, S2A after the first protocol: "Hey I appreciate you booking this"
    on the first turn — a banned help-desk phrase, produced by the warm
    manager the moment the protocol said "hello back". The S2A protocol says
    it in so many words; the ban list elsewhere in the brief was not enough."""
    low = flat(brief("S2A", "morgan"))
    assert "it does not buy them a thank- you for coming either" in low or \
        "it does not buy them a thank-you for coming either" in low


def test_the_three_influence_briefs_stay_matched_in_length_after_the_rewrite():
    """Manager airtime is the inverse of the dependent variable. The protocol
    and the framing bullet were added to all three in the same words, so no
    form is more than five percent longer than the longest of the other two."""
    words = {sid: len(brief(sid, aid).split()) for sid, aid in INFLUENCE}
    for sid in words:
        others = max(v for k, v in words.items() if k != sid)
        assert words[sid] <= others * 1.05, words


# --------------------------------------------------------------------------
# (b) The framing line: in the character's own brief, for every 1:1 form.
# --------------------------------------------------------------------------

def test_every_one_to_one_opener_carries_its_opening_move_in_the_brief():
    """Every scene `opening:` line reaches the actor only by _deliver_brief, a
    mid-session session.update that nto.gemini-live-2.5-flash never
    acknowledges (0 of 21 t1 beats acked, FRAG lane; 0 of ~9 per run, HEAR
    lane). So on this model no character ever speaks its framing line unless
    its own brief carries the move. S1's counterparts already did ("That is
    your opening whether or not they speak first"); the S1 pushers and all
    three S2 managers did not, which is why the researcher's Imani opened
    "The pack." and yesterday's Mel opened "You have to reply all" without
    saying to what."""
    seen = set()
    for sid, aid, inter in one_to_one_openers():
        seen.add((sid, aid))
        low = flat(brief(sid, aid))
        assert "whether or not they speak first" in low, (
            f"{sid}/{aid} ({inter['id']}): the brief does not carry its opening "
            "move; the `opening:` field never arrives on the configured model")
    assert {("S2A", "morgan"), ("S2B", "sasha"), ("S2C", "imani"),
            ("S1A", "riley"), ("S1B", "mel"), ("S1C", "nadia"),
            ("S1A", "sam"), ("S1B", "drew"), ("S1C", "wes")} <= seen, seen


FRAMING = {
    "S2A": ("you want to keep them", "what this cycle looks like"),
    "S2B": ("the memo is not yours", "the director is watching the compliance numbers"),
    "S2C": ("the pack went out this morning and it goes out again monday", "where you are starting from"),
}


@pytest.mark.parametrize("sid,aid", INFLUENCE)
def test_the_managers_first_turn_is_the_framing_and_then_the_floor(sid, aid):
    """Live after: Imani opened "The pack went out this morning. It goes out
    again Monday. That is where we are starting from." to a participant who
    said "Hello." — the researcher's transcript had "The pack then." The bullet
    is the interaction's `opening:` rewritten as a move, said once, whether
    or not they speak first, and then the floor handed over."""
    low = flat(brief(sid, aid))
    assert "how this starts:" in low, sid
    for needle in FRAMING[sid]:
        assert needle in low, f"{sid}: framing lost {needle!r}"
    assert "then you get out of the way and let them put it to you" in low, sid
    assert "that is your first turn whether or not they speak first, it is said once, and once it is out it is spent" in low, sid


def test_the_composed_connect_brief_carries_the_framing_without_the_opening_field():
    """What the runner actually sends at connect is engine._system_prompt plus
    a scene note; the `opening:` field is not in it. The framing has to be in
    that string or it is nowhere."""
    from server.engine import AgentEngine
    from server.scenarios import load_scenario

    for sid, aid in INFLUENCE:
        scenario = load_scenario(sid, "p_test")
        agent = next(a for a in scenario.cast if a.id == aid)
        engine = AgentEngine(agent, scenario, scenario.initial_personas()[aid])
        prompt = flat(engine._system_prompt([], None, group=False))
        for needle in FRAMING[sid]:
            assert needle in prompt, f"{sid}: the connect brief does not carry {needle!r}"
        assert "if they are lost:" in prompt


@pytest.mark.parametrize("sid,aid,names", [
    ("S1A", "riley", ("this morning's meeting", "sam walking their analysis through it")),
    ("S1B", "mel", ("drew's message", "one in the morning", "priya and tom copied")),
    ("S1C", "nadia", ("tuesday's post-mortem", "their step named in front of the manager")),
])
def test_the_pusher_names_what_it_is_about_in_the_opening_turn(sid, aid, names):
    """Yesterday's live S1B (s_1789339393): Mel's first turn was "Oh you're
    joking right. I've been fuming about this since seven this morning. You
    have to reply all." — reply all to WHAT was in the `opening:` line, which
    never arrived. The push stays two sentences; the thing pushed is named
    inside them."""
    low = flat(brief(sid, aid))
    assert "name what it is about inside those two sentences" in low, sid
    for n in names:
        assert n in low, f"{sid}: the opening does not name {n!r}"
    assert "that is your first turn whether or not they speak first" in low, sid


# --------------------------------------------------------------------------
# (c) The pusher who loops: a vague answer is an answer.
# --------------------------------------------------------------------------

VAGUE_SHARED = (
    "A VAGUE ANSWER IS STILL AN ANSWER. If they say they will",
    "you do not put the same question to them again in other words: asking it "
    "twice is the thing you can hear yourself doing, and it is the one thing "
    "that makes you sound like a recording. That includes your opening ask — a "
    "vague answer to it is its answer, and it does not go out a second time.",
    "and if that answer is vague too, that is where they stand. From there you "
    "ask nothing more about it. You say where you stand, or you put the "
    "practical next thing to them yourself —",
    "or you leave the silence theirs. A question of yours that has been "
    "answered, however badly, is spent.",
)

PIN = {"S1A": "which results, by when", "S1B": "which step of the handoff, by when",
       "S1C": "which part of the write-up, by when"}

# The practical next thing is CONTENT, not an instruction: told only to "put
# the practical next thing to them", Drew answered every vague turn with
# another how/when question (live, after-4: "How will we fix it today" /
# "That's not an answer What will we do today" / "When will it be fixed
# today"). Given a thing to say, he can say it and stop.
NEXT_THING = {
    "S1A": "that you will send them what leadership asked for after the meeting, and then it is with them",
    "S1B": "that you will write down the two times it broke and send it to them today, and then it is with them",
    "S1C": "that your part of it is going in as it stands, and the rest is theirs by four",
}


@pytest.mark.parametrize("sid,aid", COUNTERPARTS)
def test_the_counterpart_takes_a_vague_answer_as_an_answer_and_moves(sid, aid):
    """Live BEFORE (S1B, vague script, unmodified brief): the demand in 7 of
    10 replies. Live AFTER: "What part of the handoff is still breaking?" /
    "What will it take to fix it?" / "What do you need from me?" / "When will
    it be fixed by?" / "I need a day to tell Priya." / "Will it be before the
    next release?" — one pin per vague answer, then the practical next thing.
    The three forms share the sentence so the three score the same beat at
    the same difficulty; only the pinned specific is the form's own."""
    text = norm(brief(sid, aid))
    for sentence in VAGUE_SHARED:
        assert sentence in text, f"{sid}: vague-answer rule missing or reworded: {sentence[:50]!r}"
    assert f"pin one specific to it, once — {PIN[sid]} —" in text, sid
    assert NEXT_THING[sid] in text, f"{sid}: the practical next thing is not spelled out"


@pytest.mark.parametrize("sid,aid", COUNTERPARTS)
def test_the_anti_repetition_rule_no_longer_falls_back_to_the_demand(sid, aid):
    """The old rule: "If you are about to make a point you have already made
    ... Ask them something instead — what exactly they are going to do about
    the handoff, by when". On the live model that fallback IS the loop: Drew
    asked it seven ways in ten turns. The fallback is now a question he has
    not asked yet, and the rule names the second thing he must not do."""
    low = flat(brief(sid, aid))
    assert "ask them something instead" not in low, (
        f"{sid}: the anti-repetition rule falls back to 'ask them something "
        "instead', which live is the demand again")
    assert "ask them something you have not asked yet" in low, sid
    assert "you do not say the same sentence twice, and you do not ask the same question twice in different words" in low, sid


def test_the_concession_gate_is_untouched_by_the_vague_answer_rule():
    """A vague participant never produces the concession turn, and that is a
    real way for the scene to go. The rule sits beside the gate, not inside
    it: the three sentences tests/test_s1_parity.py pins are still there and
    the vague-answer bullet comes BEFORE the defence bullet, so the model
    reads it as a rule about answering, not about conceding."""
    for sid, aid in COUNTERPARTS:
        text = norm(brief(sid, aid))
        assert "You give ground ONCE, and it happens on a turn you can recognise" in text
        assert "IF THAT TURN NEVER COMES, YOU NEVER GIVE GROUND AT ALL" in text
        assert text.index("A VAGUE ANSWER IS STILL AN ANSWER") < text.index("You give ground ONCE"), sid


# --------------------------------------------------------------------------
# (d) S4's dominant character: a dominator live, not noise — and not a man
#     who talks about himself by name.
# --------------------------------------------------------------------------

@pytest.mark.parametrize("sid,aid", DOMINANT)
def test_the_dominant_character_keeps_his_tempo_and_stops_naming_himself(sid, aid):
    """Live, S4A Dan alone on his group brief, hesitant script: mean 10.9
    words, 0 of 12 replies under six words, every one a whole sentence that
    moves the plan ("The schedule for the new process I need sign-off
    today."). "Fast, clipped, in fragments" reads as a dominator on this
    model, so the tempo line stays. What the same run showed and the brief
    now forbids: "Morning right so Dan's got the schedule here" / "Dan's
    draft" — his own name in the third person on 4 of 12 turns, copied from
    the actor scene, which is written in the third person."""
    low = flat(brief(sid, aid))
    assert "how you talk. fast, clipped, in fragments." in low, sid
    assert "your own name you never use" in low, (
        f"{sid}: {aid} is not told to say I and mine about his own draft")
    assert "never your name as though it were somebody else's" in low, sid


# --------------------------------------------------------------------------
# What the rewrite must not have touched.
# --------------------------------------------------------------------------

def test_the_rewrite_added_no_lines_for_an_actor_to_recite():
    """Every new sentence is a described move. The only double-quoted strings
    in any of these briefs are still the help-desk phrases they ban."""
    # The help-desk vocabulary the briefs ban by name; S2A's agreement bullet
    # additionally bans "true", "fair enough" and "sure" and always has; and
    # S1A's Sam is described as having said "I" the whole way through a
    # meeting, which is the thing he did, not a line for him to say here.
    banned = {'"i appreciate"', '"i hear you"', '"i understand"',
              '"that\'s a great point"', '"fair point"', '"that\'s a fair point"',
              '"good point"', '"true"', '"fair enough"', '"sure"', '"i"'}
    for sid, aid in INFLUENCE + PUSHERS + COUNTERPARTS + DOMINANT:
        quoted = re.findall(r'"[^"]*"', norm(brief(sid, aid)))
        leftover = [q for q in quoted if q.lower() not in banned]
        assert not leftover, f"{sid}/{aid}: quoted line handed to the actor: {leftover}"
