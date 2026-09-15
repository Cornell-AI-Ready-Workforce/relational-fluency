"""The silence probe has to render a DESCRIBED MOVE as a move.

`on_silence` was once a quoted line everywhere in the bank, and the probe
wrapper said so: "As <name>, say this now in your own words ... : <on_silence>".
S1 and S2 still write theirs as speakable lines, on purpose and pinned — a probe
fires at the one moment the encounter has no signal of its own, so every
participant who freezes at a beat should meet the same sentence and the thing
that differs between them should be their answer, not the prompt. The
naturalness round then rewrote S3's and S4's probes into described moves ("Read
the silence as agreement and say that is the running order then"), and "say this
now: <description of a move>" is an instruction to speak the description.

Driven on the gateway against both realtime families with one identical silent
participant, the old wording leaked on 10 of 136 gpt-realtime-2.1 described-move
turns and on 0 of 136 nto.gemini-live-2.5-flash turns — latent on the configured
model, live on the family the study moves to if it wants mid-session steering
that works. What came back was the note itself, out loud: "They haven't
answered." opening a turn in the third person about the participant in the room,
"You've said nothing, so I'm not filling the silence", "the silence makes it
worse".

The measurement that decided the repair was parallel-form, not leakage. Driving
both forms of every pair with identical participant input, 272 probe turns per
wording: under "say this now" S1A's probes reached the actor less faithfully
than its twin S1B's by 0.115 of pinned-wording recall, 95% CI [-0.197, -0.033],
a gap excluding zero between two forms that are supposed to be interchangeable
and in a quantity no one reading the ESCI scores afterwards could recover.
Under the wording this file pins the gap is +0.045, CI spanning zero.

These tests are OFFLINE. They cannot see what a model says, so they check the
property that decided it: the probe direction must not name a mode of delivery
that only one of the two spec styles can satisfy. "Say this" is such a name;
"your next move" is not. The live numbers and the costs are in
_trigger_instruction's docstring, which is where the reasoning belongs — this
file is the ratchet.

One thing this file deliberately does NOT assert: that any particular
`on_silence` is a line or a move. S3's owner rewrote S3's probes into "that ..."
complements in the same round, so that they cannot be spoken as they stand; S4's
are still plain imperatives. Both styles are legitimate and the wrapper must not
care, so the assertions below are about the wrapper only. A test that pinned the
bank's style would be a test that the next content round has to delete.
"""

from __future__ import annotations

import sys
import types
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from server.realtime_voice_session import (  # noqa: E402
    END_SEGMENT_TOOL,
    RealtimeVoiceSessionRunner,
)
from server.scenarios_v3 import available, compile_scenario  # noqa: E402


def _probes():
    """(scenario id, trigger, the character the beat is bound to) per probe."""
    for sid in sorted(available()):
        scenario = compile_scenario(sid)
        by_id = {a.id: a for a in scenario.cast}
        for inter in scenario.interactions:
            for trig in inter.get("triggers", []):
                if not (trig.get("on_silence") or "").strip():
                    continue
                agent = (by_id.get(trig.get("agent"))
                         or by_id.get((inter.get("agents") or [None])[0])
                         or scenario.cast[0])
                yield sid, trig, agent


def _render(agent, trigger, *, probing):
    runner = types.SimpleNamespace(agent=agent)
    return RealtimeVoiceSessionRunner._trigger_instruction(
        runner, trigger, probing=probing)


ALL_PROBES = list(_probes())
IDS = [f"{sid}-{t['id']}" for sid, t, _ in ALL_PROBES]


# --- the finding ------------------------------------------------------------

# Any wording that tells the actor the probe is a thing to UTTER. On a probe
# authored as a described move, each of these renders the move's description as
# the line to speak, which is the bug this file exists to hold shut.
SPEAK_THIS = (
    "say this",
    "say it now",
    "say the following",
    "this line",
    "these words",
    "read this",
    "repeat this",
    "your line is",
    "speak this",
    "use this line",
)


@pytest.mark.parametrize("sid,trigger,agent", ALL_PROBES, ids=IDS)
def test_probe_never_tells_the_actor_to_utter_the_direction(sid, trigger, agent):
    """The probe may not name speech as the mode of delivery.

    Half the bank authors `on_silence` as a line and half as a move, and the
    wrapper cannot tell which it has: both arrive as a str off the same key.
    So the direction has to be true of both, and "say this" is true of only
    one. A described move rendered under "say this" is an instruction to speak
    the description — which is what gpt-realtime did.
    """
    probe = _render(agent, trigger, probing=True)
    body = probe.replace(trigger["on_silence"], " ")
    for phrase in SPEAK_THIS:
        assert phrase not in body.lower(), (
            f"{sid}/{trigger['id']}: the probe wrapper says {phrase!r}, which "
            f"only reads correctly if every on_silence in the bank is a quoted "
            f"line. This one is not: {trigger['on_silence'][:70]!r}"
        )


@pytest.mark.parametrize("sid,trigger,agent", ALL_PROBES, ids=IDS)
def test_probe_and_cue_frame_the_content_the_same_way(sid, trigger, agent):
    """One framing for both, because the bank writes both keys both ways.

    The cue wrapper already had to survive a cue written as "Sam defends:
    '...'" or as "Defend yourself before you have thought about it", and
    settled on "Your next move, as <name>, now, in your own words:". The probe
    was the one path left guessing. Holding the two to the same frame is what
    stops the next content round from reopening this: a probe rewritten into a
    move, or a cue rewritten into a line, lands on a wrapper that does not care
    which it got.
    """
    probe = _render(agent, trigger, probing=True)
    cue = _render(agent, trigger, probing=False)
    frame = f"Your next move, as {agent.name}, now, in your own words"
    assert frame in cue, f"{sid}/{trigger['id']}: cue frame moved"
    assert frame in probe, (
        f"{sid}/{trigger['id']}: the probe no longer uses the cue's framing, "
        f"so the two paths can drift apart again: {probe[:120]!r}"
    )


@pytest.mark.parametrize("sid,trigger,agent", ALL_PROBES, ids=IDS)
def test_probe_says_why_it_is_firing_and_hands_the_floor_back(sid, trigger, agent):
    """The two clauses the probe has that the cue does not.

    A probe fires because the recording has gone quiet, and the actor is owed
    that reason or it reads as an interruption of a conversation that was going
    fine. And it must return the floor: a probe carries that beat's own move and
    never the next beat's work, or one frozen participant spends two beats.
    """
    probe = _render(agent, trigger, probing=True)
    assert "gone quiet" in probe, f"{sid}/{trigger['id']}: no reason given"
    assert "let them answer" in probe, (
        f"{sid}/{trigger['id']}: the probe does not hand the floor back"
    )


@pytest.mark.parametrize("sid,trigger,agent", ALL_PROBES, ids=IDS)
def test_the_probe_reaches_the_actor_verbatim(sid, trigger, agent):
    """The probe IS the measurement; the wrapper may be rephrased, it may not.

    Duplicated from test_prompt_assembly deliberately. That file checks the
    wrapper's manners; this one is the contract a future rewording is measured
    against, and a contract that lives in another file is one a rewrite does
    not read.
    """
    probe = _render(agent, trigger, probing=True)
    assert trigger["on_silence"] in probe, f"{sid}/{trigger['id']}"


@pytest.mark.parametrize("sid,trigger,agent", ALL_PROBES, ids=IDS)
def test_probe_carries_no_production_vocabulary(sid, trigger, agent):
    """Same bar the cue is already held to.

    "beat", "director", "stage direction", "probe" are what this scene is to
    the study. To the character it is a pause in an argument. The actor is also
    told a few lines up never to read a stage direction out, so naming one in
    the direction is an invitation to read out the thing it names.
    """
    probe = _render(agent, trigger, probing=True)
    body = probe.replace(trigger["on_silence"], " ").lower()
    for craft in ("beat", "director", "stage direction", "probe", "trigger",
                  "participant", "scenario", "esci"):
        assert craft not in body, (
            f"{sid}/{trigger['id']}: production vocabulary {craft!r} in {body!r}"
        )


# --- parallel-form equivalence ----------------------------------------------

@pytest.mark.parametrize("pair", [("S1A", "S1B"), ("S2A", "S2B"),
                                  ("S3A", "S3B"), ("S4A", "S4B")])
def test_both_forms_of_a_construct_get_the_identical_probe_frame(pair):
    """A participant is assigned ONE form, so a wrapper that treats the two
    differently is undetectable in the data afterwards.

    The wrapper is form-blind by construction — one code path, no per-scenario
    branch — and this is what keeps it that way. The live check behind it drove
    both forms of every pair with identical synthetic participant input: with
    the old wording S4A leaked its description on 2 of 24 gpt turns while its
    twin S4B leaked 0 of 24, which is exactly the shape of asymmetry nobody can
    see in the scores. Both are 0 now.
    """
    a, b = pair
    if a not in available() or b not in available():
        pytest.skip(f"{a}/{b} not in the bank")
    frames = {}
    for sid in pair:
        for probe_sid, trig, agent in ALL_PROBES:
            if probe_sid != sid:
                continue
            rendered = _render(agent, trig, probing=True)
            # Strip the two things that are legitimately per-scenario: the
            # character's name and the beat's own words. What is left is the
            # frame, and it must be byte-identical across the pair.
            frame = (rendered
                     .replace(trig["on_silence"], "<PROBE>")
                     .replace(agent.name, "<NAME>"))
            frames.setdefault(sid, set()).add(frame)
    assert len(frames[a]) == 1, f"{a}: probe frame varies within the form"
    assert frames[a] == frames[b], (
        f"{a} and {b} render probes differently, so which form a participant "
        f"drew changes what their actor was told:\n  {a}: {frames[a]}\n"
        f"  {b}: {frames[b]}"
    )


# --- the one tool the actor holds -------------------------------------------

def test_the_end_tool_and_the_brief_say_one_thing_about_ending():
    """Both failure modes here are silent, so the two texts may not disagree.

    The brief says "Do not wrap it up early, and never end it yourself unless
    told to". If the tool also offers "the conversation reached its natural
    end" the actor holds two rules and neither wins reliably: under-calling
    stalls a segment until the pacing gate moves it, over-calling truncates a
    scored interaction the moment the argument feels settled — routinely
    several beats before the beats are spent. Neither leaves a mark a rater
    would notice.
    """
    desc = END_SEGMENT_TOOL["description"].lower()
    assert "only when the other person has ended" in desc, (
        "the tool no longer names the one event that ends a segment"
    )
    # Phrasings that AUTHORISE the actor to end on its own judgement. Each was
    # in the description this replaced, and each contradicts the brief.
    for invitation in ("natural end", "naturally", "has been addressed",
                       "when you are finished", "when the conversation is "
                       "complete", "if the conversation is over", "wrap up",
                       "wrap it up"):
        assert invitation not in desc, (
            f"the end tool authorises ending on {invitation!r}, which the "
            f"brief forbids; both failure modes are silent in the record"
        )
    # And it must still name the three temptations it was measured folding to,
    # each inside a refusal. A description that merely omits them leaves the
    # actor to infer, and inference is what the old one lost on.
    for refusal in ("feels resolved", "there is a pause", "nothing more"):
        assert refusal in desc, (
            f"the tool no longer refuses {refusal!r} by name"
        )
    assert "never call it because" in desc, (
        "the refusals are no longer stated as refusals"
    )
