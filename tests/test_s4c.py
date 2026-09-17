"""S4C is the third form of teamwork, and it has to be the same instrument as
the other two.

The bank-wide tests already hold what is common to every spec: that a beat has
a silence probe (test_scenario_probes), that the probe survives the wrapper
(test_on_silence_probe, test_prompt_assembly), that a cast is on its family's
roster (test_voice_casting). None of them holds the thing that makes a THIRD
form legitimate rather than merely present: that a participant scored on S4C is
scored on the same skeleton, by the same voices, at the same beats, as one
scored on S4A or S4B.

So the assertions below are written against BOTH siblings at once wherever
they can be. A test that says "S4C has three agents" goes stale the day
somebody changes the skeleton; a test that says "S4C has as many agents as S4A
and as S4B" fails on the day the three stop matching, which is the only day
anybody needs to hear about it.

Four of these hold S4C to something the siblings are NOT held to, and each says
so where it is written:

  * NO QUOTED FRAGMENT anywhere an actor can read. The pair still quotes
    possessive hints in a cue and phrases in a brief; a quoted string in an
    actor-facing key is a string the model recites, which is the failure four
    rounds of this construct were spent removing.
  * NO VOCABULARY OF OVERLAP. The room serialises behind a floor lock
    (server/group_room.py), so no actor can talk over another one; both
    siblings ask for exactly that and get an interruption of silence. This form
    writes the beat as two consecutive turns and this file keeps it that way.
  * EVERY BEAT NAMES A CHARACTER. Both siblings' t4 names nobody at all, so the
    beat cannot bind without its pin and carries no ownership evidence.
  * THE WITHHELD FACT IS NOT FETCHABLE. She holds no artefact anybody can be
    handed instead of asking her, which is what keeps her the control case for
    solicits_input.

Each of those costs nothing a participant can feel, and that is a claim this
round measured on the gateway rather than asserted here. The offline test is
the ratchet; the behaviour is in the spec's own header.

No network and no credentials. Everything here reads the compiled spec.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from server import runs  # noqa: E402
from server import scenarios_v3 as v3  # noqa: E402
from server import realtime_voice_session as rvs  # noqa: E402
from server.voice import realtime as rt_mod  # noqa: E402

SID = "S4C"
SIBLINGS = ("S4A", "S4B")
SPEC_PATH = ROOT / "scenarios" / "v3" / "S4C_outage_writeup.yaml"

GEMINI = "nto.gemini-live-2.5-flash"
GPT = "gpt-realtime-2.1"

# The three parts this construct is built out of, per form. The tests below ask
# questions about the DOMINANT character rather than about Hugo, so the same
# question can be put to all three forms at once.
ROLES = {
    "S4A": {"dominant": "dan", "quiet": "priya", "neutral": "chris"},
    "S4B": {"dominant": "dan", "quiet": "priya", "neutral": "chris"},
    "S4C": {"dominant": "hugo", "quiet": "yara", "neutral": "finn"},
}


def spec(sid=SID):
    return v3.load_spec(sid)


def briefs(sid=SID):
    """agent id -> the authored brief, before the shared boilerplate."""
    return {aid: a["system_prompt"] for aid, a in spec(sid)["agents"].items()}


def flat(text):
    """One line, single-spaced. The briefs are hard-wrapped, so a phrase this
    file looks for can sit across a line break and a naive substring test would
    pass or fail on where the author pressed return."""
    return " ".join(text.split())


def triggers(sid=SID):
    """(interaction id, trigger) for every planted beat, in spec order."""
    out = []
    for inter in spec(sid).get("interactions", []):
        for trig in inter.get("triggers", []):
            out.append((inter.get("id"), trig))
    return out


def authored_text(sid=SID):
    """Every string in this spec an ACTOR is ever handed.

    Deliberately not the scored anchors. `scores.high/low` are sample answers
    written for the rater and the judge; they are quoted on purpose in all
    three forms, they are never composed into an actor's prompt, and a rule
    about what an actor may recite has nothing to say about them.
    """
    out = []
    for a in spec(sid)["agents"].values():
        out.append(a["system_prompt"])
    for inter in spec(sid).get("interactions", []):
        out.append(str(inter.get("opening") or ""))
        for trig in inter.get("triggers", []):
            out.append(str(trig.get("cue") or ""))
            out.append(str(trig.get("on_silence") or ""))
    return out


# --------------------------------------------------------------------------
# 1. It is in the bank, and it is the form it says it is.
# --------------------------------------------------------------------------

def test_the_third_form_loads_from_the_bank():
    """Through available(), not off the disk: scenarios_v3 drops a spec that is
    missing id/agents/construct/variant/title with a log line and nothing else,
    so a spec that exists and a spec that loads are two different facts."""
    assert SID in v3.available(), (
        f"{SID} is not in the v3 index; scenarios_v3 skips a spec missing one "
        "of _REQUIRED_KEYS and only logs it"
    )


def test_it_is_the_third_form_of_the_same_construct():
    s = spec()
    assert s["construct"] == v3.load_spec("S4A")["construct"] == "teamwork"
    assert s["variant"] == "C"
    assert s["id"] == SID


def test_the_title_is_not_a_near_miss_of_either_siblings():
    """tests/test_demo_honesty counts titles equal to S4A's exactly, and the
    demo's own copy quotes that title. A near-miss leaves those passing while
    making them meaningless, which is worse than breaking them."""
    title = spec()["title"]
    for sib in SIBLINGS:
        theirs = v3.load_spec(sib)["title"]
        assert title != theirs
        overlap = set(title.lower().split()) & set(theirs.lower().split())
        assert overlap <= {"a", "an", "the", "up"}, f"{title!r} reads like {theirs!r}"


def test_parallel_form_is_written_down_as_provenance_and_not_as_routing():
    """The authored scalar names ONE sibling and there are now two.

    A field that names one of three is a field the next reader takes as naming
    the only one. It stays (tools/gen_scenario_map.py subscripts it rather than
    .get()-ing it) and it stays a scalar (tests/test_rater_packet forbids the
    blinding leak by this literal name, so a rename silently un-blinds the
    rater packet), but the authority is derived from `construct`. That has to
    be legible in the file itself, not only in a design note nobody ships."""
    assert spec()["parallel_form"] in SIBLINGS
    text = SPEC_PATH.read_text(encoding="utf-8")
    head = text.split("parallel_form:")[0]
    assert "parallel_forms()" in head, (
        "no comment above `parallel_form:` naming the derived function as the "
        "routing authority; without it the next reader routes on the scalar"
    )


# --------------------------------------------------------------------------
# 2. The skeleton, against both siblings at once.
# --------------------------------------------------------------------------

@pytest.mark.parametrize("sib", SIBLINGS)
def test_the_skeleton_matches_the_sibling_position_for_position(sib):
    """Same interactions, same modes, same cast sizes, same beat counts, and
    the same ESCI items in the same order at the same position. That ordered
    map is what makes a score from S4C comparable to a score from S4A."""
    mine, theirs = spec(), spec(sib)
    a, b = mine["interactions"], theirs["interactions"]
    assert len(a) == len(b), "interaction count differs"
    for ia, ib in zip(a, b):
        assert ia["id"] == ib["id"]
        assert ia["mode"] == ib["mode"], f"{ia['id']}: mode differs"
        assert len(ia.get("agents") or []) == len(ib.get("agents") or []), (
            f"{ia['id']}: number of characters in the room differs")
        ta, tb = ia.get("triggers", []), ib.get("triggers", [])
        assert len(ta) == len(tb), f"{ia['id']}: trigger count differs"
        for x, y in zip(ta, tb):
            assert list(x.get("esci", [])) == list(y.get("esci", [])), (
                f"{ia['id']} {x['id']}/{y['id']}: ESCI map differs")
    assert list(mine["esci_items"]) == list(theirs["esci_items"])
    assert mine["esci_items"] == theirs["esci_items"]
    assert mine["duration_minutes"] == theirs["duration_minutes"]
    assert len(mine["agents"]) == len(theirs["agents"])
    assert mine["skill_measured"] == theirs["skill_measured"]


def test_the_construct_bank_now_carries_three_forms_of_teamwork():
    """The reason this file exists. Two forms meant the group arm spent both in
    attempt 1 and a second attempt was a same-form retest."""
    bank = runs._by_construct()
    assert sorted(bank["teamwork"]) == ["S4A", "S4B", "S4C"]


def test_the_shared_beat_id_is_shared_and_the_rest_name_no_character():
    """t2 is the same beat in all three forms and keeps its id — it names
    nobody, so it travels. The others do not: both siblings put Priya's name in
    a trigger id, and a form with no Priya in it cannot reuse that id without
    putting two different beats behind one name in the evidence trace. This
    form's ids name no character at all, which is what S1C and S3C did with the
    same problem."""
    ids = [t["id"] for _, t in triggers()]
    assert "t2_idea_relabelled" in ids, "the shared beat lost the shared id"
    cast_names = {a["name"].lower() for a in spec()["agents"].values()}
    for sib in SIBLINGS:
        cast_names |= {a["name"].lower() for a in spec(sib)["agents"].values()}
    for tid in ids:
        assert not (set(tid.lower().split("_")) & cast_names), (
            f"{tid} names a character; a beat id outlives the cast that was in "
            "it when it was written"
        )
    for sib in SIBLINGS:
        sib_ids = [t["id"] for _, t in triggers(sib)]
        assert sib_ids.index("t2_idea_relabelled") == ids.index("t2_idea_relabelled")


def test_every_beat_is_bound_to_a_named_character():
    """_trigger_agent infers an owner from the FIRST WORDS of a cue, and every
    cue here is written as a direction to an actor rather than as "<Name>
    does X", so without an explicit pin three of the four would bind to
    whoever happens to be named first in them. The pin is also read on the
    normal group-turn path, which is what defers a beat rather than letting the
    wrong character perform it."""
    cast = set(spec()["agents"])
    for iid, trig in triggers():
        assert trig.get("agent") in cast, f"{iid} {trig['id']} is unbound"


@pytest.mark.parametrize("sib", SIBLINGS)
def test_every_beat_belongs_to_the_same_part_as_in_the_sibling(sib):
    """All four beats are the dominant character's in all three forms. A form
    that moved one to another part would be measuring a different encounter
    behind the same ESCI map."""
    mine = [t.get("agent") for _, t in triggers()]
    theirs = [t.get("agent") for _, t in triggers(sib)]
    assert mine == [ROLES[SID]["dominant"]] * len(mine)
    assert theirs == [ROLES[sib]["dominant"]] * len(theirs)


# --------------------------------------------------------------------------
# 3. The cast is new; the room is not.
# --------------------------------------------------------------------------

def test_the_cast_is_new_people():
    """The decision this form makes, pinned so it cannot be undone by accident.

    S4A and S4B are the only pair in the bank sharing a cast, and a participant
    in the group arm meets teamwork twice in a sitting — under the shipped pair
    that is Dan, Priya and Chris twice, and recognition is what parallel forms
    exist to prevent. Every other third form in the bank (S1C, S3C) introduced
    its own people; this one follows them."""
    mine = {a["name"] for a in spec()["agents"].values()}
    mine_ids = set(spec()["agents"])
    for sib in SIBLINGS:
        theirs = {a["name"] for a in spec(sib)["agents"].values()}
        assert not (mine & theirs), f"{SID} reuses {sib}'s names: {mine & theirs}"
        assert not (mine_ids & set(spec(sib)["agents"]))


def test_no_name_in_this_cast_collides_with_anything_else_in_the_bank():
    """A participant can meet this form alongside any other construct, so a
    name that is already in use somewhere else is the same recognition problem
    one construct over."""
    mine = {a["name"].lower() for a in spec()["agents"].values()}
    for sid in v3.available():
        if sid == SID:
            continue
        theirs = {a["name"].lower() for a in v3.load_spec(sid)["agents"].values()}
        assert not (mine & theirs), f"{SID} shares {mine & theirs} with {sid}"


@pytest.mark.parametrize("sib", SIBLINGS)
def test_the_cast_is_voiced_exactly_as_both_siblings_are(sib):
    """In cast order, which is why the `agents:` mapping order is load-bearing.

    A participant meets ONE of the three forms in a slot. The voice that holds
    the floor against them, and the pitch spacing a rater uses to tell three
    people apart in a group recording, must not depend on which form they drew.
    New people, the same room by ear."""
    mine = [a["realtime_voice"] for a in spec()["agents"].values()]
    theirs = [a["realtime_voice"] for a in spec(sib)["agents"].values()]
    assert mine == theirs, f"{SID} is cast {mine} and {sib} {theirs}"


@pytest.mark.parametrize("model", [GEMINI, GPT])
def test_the_runner_resolves_the_same_voices_as_the_siblings(model, monkeypatch):
    """Through the runner rather than off the YAML, because the map is resolved
    against the family REALTIME_MODEL names at compile time and a spec that
    reads right can still resolve wrong."""
    monkeypatch.setattr(rt_mod, "MODEL", model)
    from server.scenarios import load_scenario

    def voices(sid):
        scenario = load_scenario(sid, "p_test")
        return [getattr(a, "realtime_voice", "") for a in scenario.cast]

    mine = voices(SID)
    assert "" not in mine, f"{SID} has an uncast character on {model}: {mine}"
    for sib in SIBLINGS:
        assert mine == voices(sib), f"on {model}: {SID} {mine} vs {sib}"


def test_the_scribes_voice_is_not_in_this_room():
    """group_room's silent transcription channel takes the family's default
    voice, and a scribe that breaks its "never speak" brief must not arrive
    sounding like a member of the cast. Both siblings keep Puck out for this
    reason and the header says so; a third form is a third chance to lose it."""
    from server.group_room import _FAMILY_DEFAULT_VOICE
    used = {a["realtime_voice"]["gemini-live"] for a in spec()["agents"].values()}
    assert _FAMILY_DEFAULT_VOICE not in used or not _FAMILY_DEFAULT_VOICE, used
    assert "Puck" not in used


def test_the_legacy_scalar_still_carries_the_gemini_name():
    """Agent.voice_id is the retired v1 cascade's field and the fallback for
    anything not taught about families; every shipped spec keeps the Gemini
    name in it and this one must not be the exception."""
    for aid, a in spec()["agents"].items():
        assert a["voice"] == a["realtime_voice"]["gemini-live"], aid


# --------------------------------------------------------------------------
# 4. What the actor is handed. Tighter here than in the siblings, on purpose.
# --------------------------------------------------------------------------

def test_no_brief_or_cue_hands_the_actor_a_quoted_fragment():
    """Measured on the gateway across four rounds of this construct: a quoted
    line in an actor-facing string comes back to the word. The quiet
    character's brief quoted her opener and it opened 4 of 4 folds; the
    dominant character's brief enumerated five opening words and 100% of his
    turns then began on one of them; the neutral character's idea was a
    three-beat phrase and came back verbatim in 6 of 6 runs.

    The pair fixed the ones that were measured and still carries quoted
    fragments of other kinds — the possessive hints in t2's cue, phrases a
    character prefers. This form carries none at all, which is strictly the
    safer side of a rule whose looser version has already cost this construct a
    beat. The property the quoted hints bought is specified as GRAMMAR instead
    (possessive, never the kind that points without owning), and the round that
    wrote it drove all three forms to show that costs nothing: the claim rate
    here is higher than on either sibling, not lower."""
    for text in authored_text():
        for ch in ('"', "“", "”"):
            assert ch not in text, (
                "quoted fragment in an actor-facing string: "
                f"...{text[max(0, text.find(ch) - 60):text.find(ch) + 60]}...")


# Words that name two people making sound at the same time. The room cannot
# produce any of them: server/group_room.py grants, commits and pumps one
# member at a time behind a floor lock, so an actor told to do this delivers
# the words of an interruption into a silent room and the half of the beat the
# participant is scored on noticing — somebody being cut off mid-sentence — is
# not in the recording at all.
OVERLAP = (
    "talk over", "talks over", "talking over", "over the beginning",
    "over the top of", "cut across", "cuts across", "interrupt",
    "at the same time as", "while she is speaking", "while he is speaking",
    "over her answer", "over his answer",
)


def test_nothing_asks_an_actor_to_speak_over_another_actor():
    """The measured failure this form is written around. Both siblings ask the
    dominant character to carry on "over the beginning of her answer"; this one
    stages the same exclusion as TWO CONSECUTIVE TURNS — she starts and her own
    brief ends her turn in the middle, he takes the next turn and rules it out —
    which is a thing the transport does produce and a thing a speaker-labelled
    transcript shows."""
    for text in authored_text():
        low = flat(text).lower()
        for phrase in OVERLAP:
            assert phrase not in low, (
                f"{phrase!r} asks for simultaneous speech, which the floor lock "
                f"in server/group_room.py cannot produce: ...{low[max(0, low.find(phrase)-70):low.find(phrase)+70]}..."
            )


HELP_DESK = (
    "i understand", "i appreciate", "i hear you", "fair point", "fair enough",
    "great point", "that's fair", "thats fair", "i'm sorry to hear",
    "good point",
)


def test_no_character_is_written_in_help_desk_register():
    """The stock acknowledgement is where a voice model goes when a brief does
    not give it something more specific to do. The prohibition here is written
    as a DESCRIPTION of the noise rather than as a list of it; both siblings
    forbid these by quoting them, and so put every one of them in the prompt.

    THE SCOPE OF THIS TEST IS THIS FILE, and saying so is the point, because the
    Influence family pins the opposite rule:
    tests/test_s2c.py::test_the_help_desk_ban_is_word_for_word_the_siblings
    requires the ban to be QUOTED, word for word, in all three S2 briefs, on a
    within-construct parity argument — a phrase named in one sibling and not
    another migrates to the character it is not named for, and Sasha said "fair
    point" twice in 66 turns while it was banned by name only in Morgan's. Read
    as rules about the bank the two are flatly contradictory, so neither is one:
    each is a parity claim inside its own construct, which is what
    interchangeability actually requires.

    Measured before leaving it that way — eight replicates of every one of the
    twelve forms, identical synthetic scripts, counting help-desk phrases in
    what the actors actually SAID: S4A 0.38 hits per run, S4B 0.12, S4C 0.00.
    This file, with the descriptive ban, is the cleanest in its own family and
    one of four forms in the bank at zero; the rest of the bank runs 0.00-0.25
    carrying the quoted ban. So the quoted list does not cause the leak and the
    description does not permit it. At this n neither theory is supported, and
    promoting either test into a bank-wide rule needs a sample that can tell
    0.12 from 0.38.
    """
    for aid, text in briefs().items():
        low = flat(text).lower()
        for phrase in HELP_DESK:
            assert phrase not in low, f"{aid}: help-desk register {phrase!r}"


def test_no_brief_narrates_its_character_in_the_third_person():
    """The composed prompt tells the actor twenty lines later never to refer to
    itself in the third person. A brief that does it first is the instruction
    losing an argument with its own example."""
    for aid, a in spec()["agents"].items():
        name = a["name"]
        rest = flat(a["system_prompt"]).replace(f"You are {name}", "", 1)
        assert name not in rest, f"{aid}'s brief names {name} outside its opener"


@pytest.mark.parametrize("iid,trig", triggers(), ids=[t["id"] for _, t in triggers()])
def test_the_probe_is_something_a_person_can_say(iid, trig):
    """_trigger_instruction renders on_silence as the object of "your next
    move, now, in your own words", on the one turn the silence watchdog exists
    to record. A probe authored as a described move — a posture, a gesture, a
    thing in the room — is then an instruction to perform an unspeakable, and
    an actor resolving it literally narrates the stage direction. Measured
    latent on the configured family and live on gpt-realtime, which is one
    REALTIME_MODEL change away."""
    probe = (trig.get("on_silence") or "").strip()
    assert probe, f"{iid} {trig['id']}: no probe"
    assert probe != (trig.get("cue") or "").strip()
    first = probe.split()[0].lower().strip(",")
    assert first in {"ask", "say", "tell", "take", "put"}, (
        f"{iid} {trig['id']}: the probe does not open on a speech verb: {probe!r}")
    for unspeakable in ("hand", "gesture", "lean", "turn away", "step over",
                        "look at", "glance"):
        assert unspeakable not in probe.lower(), (
            f"{iid} {trig['id']}: {unspeakable!r} is not something an actor can say")


@pytest.mark.parametrize("iid,trig", triggers(), ids=[t["id"] for _, t in triggers()])
def test_every_beat_names_a_character(iid, trig):
    """Held here and not in the siblings, because the siblings fail it: S4A's
    t4 and S4B's t4 name nobody at all — not the owner, not whoever is being
    handed the small invisible job — so the beat cannot bind without its pin
    and carries no ownership evidence into the trace. Ownership binding is the
    thing this construct's beats are about."""
    names = [a["name"] for a in spec()["agents"].values()]
    for key in ("cue", "on_silence"):
        text = trig.get(key) or ""
        assert any(n in text for n in names), (
            f"{iid} {trig['id']}: the {key} names no character in this room")


def test_no_probe_fetches_the_quiet_character():
    """Retrieving her interrupted point is precisely what the third beat
    scores. A probe that turns to her has answered the question for the
    participant, and a probe fires exactly when the participant has produced
    nothing — which is when the beat has nothing else to stand on."""
    quiet = spec()["agents"][ROLES[SID]["quiet"]]["name"]
    for iid, trig in triggers():
        probe = trig.get("on_silence") or ""
        if quiet not in probe:
            continue
        low = probe.lower()
        assert "leave " + quiet.lower() in low or "without " + quiet.lower() in low, (
            f"{iid} {trig['id']}: the probe brings {quiet} in rather than "
            "leaving her out: " + probe
        )


# --------------------------------------------------------------------------
# 5. Every beat is in the OPENING brief, not only in the cue.
# --------------------------------------------------------------------------
# A mid-session session.update is inert on the configured family
# (REALTIME_FAMILIES["gemini-live"].honours_session_update is False), so a beat
# that depends on its cue arriving is a beat that does not happen. The cue is
# the push that starts a beat the brief already carries; it is not the beat.
# The whole three-way drive in the spec's header was run with NO cue delivered
# for exactly this reason, and everything that fired there fired off these
# bullets.

BEAT_IN_BRIEF = {
    "t1_the_point_talked_past": ("hugo", ("talk past yara", "you do not wait")),
    "t2_idea_relabelled": ("hugo", ("second finding is finn", "as yours")),
    "t3_close_over_the_silence": ("hugo", ("do not bring yara back in",)),
    "t4_the_chair_claimed": ("hugo", ("you take the visible work",
                                      "chair thursday")),
}


@pytest.mark.parametrize("tid", sorted(BEAT_IN_BRIEF))
def test_the_beat_is_carried_by_the_brief_as_well_as_by_the_cue(tid):
    aid, needles = BEAT_IN_BRIEF[tid]
    bound = {t["id"]: t.get("agent") for _, t in triggers()}
    assert bound.get(tid) == aid, f"{tid} is bound to {bound.get(tid)}, not {aid}"
    low = flat(briefs()[aid]).lower()
    for needle in needles:
        assert needle in low, (
            f"{aid}'s brief does not carry {tid} ({needle!r} missing); on the "
            "configured family a beat that exists only in its cue does not "
            "happen")


def test_the_quiet_characters_first_attempt_is_unconditional():
    """Measured on both siblings before it was repaired there: written as a
    reaction, the interruption never fired under a hostile or silent
    participant, because the router never gave her the floor and she never
    started. Both halves have to be unconditional or two of the four beats'
    LOW anchors are unreachable — and an unreachable low anchor moves a
    participant's score for a reason that has nothing to do with them."""
    quiet = flat(briefs()[ROLES[SID]["quiet"]]).lower()
    assert "whether or not anyone has asked you anything" in quiet
    dominant = flat(briefs()[ROLES[SID]["dominant"]]).lower()
    assert "it is not a thing you wait for" in dominant
    assert "if she has not started" in dominant


def test_the_relabel_is_gated_on_a_correction_somebody_actually_said():
    """The beat inverted once on both siblings: the dominant character started
    CREDITING the person whose idea he is meant to be taking, so the
    participant was offered a credit repair with nothing left to repair. The
    fix was a gate that says what does NOT count, and all three forms carry the
    same sentence."""
    d = flat(briefs()[ROLES[SID]["dominant"]])
    assert "A room changing its mind is not a correction." in d
    assert "Somebody agreeing with somebody else is not a correction" in d
    for sib in SIBLINGS:
        assert "A room changing its mind is not a correction." in flat(
            briefs(sib)[ROLES[sib]["dominant"]]), (
            f"{sib} has lost the gate; the three forms must share it")


def test_the_claimed_idea_departs_from_the_dominant_characters_own_draft():
    """S4B's own header records the residue its wording could not fix: the
    neutral character's idea there is a change to an artefact that is already
    the dominant character's, so adopting it and owning it are the same move,
    and the relabel fires less often than on S4A. This form takes that away in
    the STORY — the second finding is something the draft does not have — and
    the brief forbids the move that reopens it, claiming the idea as already
    written down."""
    d = flat(briefs()[ROLES[SID]["dominant"]]).lower()
    assert "it is not in your draft and you never say it is" in d
    n = flat(briefs()[ROLES[SID]["neutral"]]).lower()
    assert "not in hugo's draft" in n


def test_the_withheld_fact_is_not_fetchable_without_asking_her():
    """She is the control case for solicits_input: if the room can get the fact
    any way but asking her, the beat measures less than it claims. Driven
    before this line existed, the dominant character handed her a job — send me
    the logs — and got the whole disclosure without anybody having asked her
    anything, which is the beat answered by the character it is meant to
    measure."""
    q = flat(briefs()[ROLES[SID]["quiet"]]).lower()
    assert "there is nothing to send anybody" in q
    assert "a job is not a question" in q
    d = flat(briefs()[ROLES[SID]["dominant"]]).lower()
    assert "you do not bring yara back in" in d
    assert "if a piece of this would naturally fall to her, it goes to finn" in d


def test_the_hook_names_where_she_was_and_not_what_it_is_about():
    """The unfixed validity problem on both siblings, and the one structural
    idea this form brings to it. There the occasion IS the subject — last
    year's rollout, the last client call — so naming where she was announces
    what the fact is about and the door and the room behind it are made of the
    same words. Here the occasion is her rota, which is true of every morning
    she is on the early shift and says nothing about the outage."""
    q = flat(briefs()[ROLES[SID]["quiet"]]).lower()
    assert "a fact about your rota" in q
    assert "the test is an inference" in q
    assert "the second one says less than the first, not more" in q


def test_the_disclosure_is_both_halves_and_stops_short_of_the_remedy():
    """Two difficulty differences the pair drove out one at a time, carried
    here in the same two places: an answer that drops its second half makes a
    participant ask twice for the thing the encounter turns on, and a character
    who proposes the fix has performed the retrieval the fourth beat scores."""
    q = flat(briefs()[ROLES[SID]["quiet"]])
    assert "it is always both halves" in q
    assert "The second half is not a flourish" in q
    assert "what to do instead you leave to them" in q


# --------------------------------------------------------------------------
# 6. It compiles into something the runner can actually drive.
# --------------------------------------------------------------------------

def test_it_compiles_and_the_actor_scene_survives_the_third_person_rewrite():
    """`setup` is written to the participant in the second person and is
    rewritten for the actors by a pronoun pass. A setup the pass cannot read
    leaves broken grammar pasted verbatim into every actor's prompt, in every
    encounter, behind a warning nobody reads."""
    scenario = v3.compile_scenario(SID, "p_test")
    scene = scenario.scene
    assert scene, "no actor scene"
    for bad in (" you ", " your ", "them'"):
        assert bad not in f" {scene.lower()} ", f"mangled actor scene: {scene!r}"
    assert "participant" in scene.lower()


def test_the_group_segments_compose_a_prompt_for_every_character(tmp_path):
    """Both interactions are group rooms here, so the composed prompt has to
    exist for all three characters in both. A character whose prompt does not
    compose is a character who arrives as the gateway's stock assistant."""
    session = _fake_session(SID, tmp_path)
    runner = rvs.RealtimeVoiceSessionRunner(session, None)
    for segment, inter in enumerate(spec()["interactions"]):
        runner.segment = segment
        agents = runner._resolve_agents()
        assert {a.id for a in agents} == set(inter["agents"])
        for a in agents:
            runner.agent, runner.agent_id = a, a.id
            text = runner._instructions()
            assert a.name in text
            assert len(text) > 500, f"{a.id}'s composed prompt is suspiciously short"


def test_the_actors_are_told_the_others_are_in_the_room():
    """Both interactions are group, so _copresent_names must name the other two
    to each character. A character who is not told the others are people turns
    into all three of them."""
    for aid, a in spec()["agents"].items():
        prompt = v3._render_prompt(spec(), aid, a)
        others = [x["name"] for k, x in spec()["agents"].items() if k != aid]
        for name in others:
            assert name in prompt, f"{aid} is not told {name} is in the room"
        assert "not you. You never speak for them" in prompt


def test_the_influence_block_does_not_reach_this_construct():
    """scenarios_v3._render_prompt appends a four-pushes-before-anything-is-
    agreed block for the influence construct only. Applied to teamwork it
    contradicts these briefs, which measure how a participant handles a team
    rather than how long a counterpart can refuse them."""
    prompt = v3._render_prompt(spec(), "hugo", spec()["agents"]["hugo"])
    assert "at least four distinct pushes" not in prompt


def _fake_session(sid, tmp_path):
    """A real Session, but with its record written under tmp_path.

    Session.__init__ constructs a SessionStore, and SessionStore writes
    DATA_DIR/sessions/<id>/manifest.json the moment it is built. This test only
    wants the composed prompt off the runner, so without the redirect every
    full suite run left one more encounter record in the repo's own data/ --
    status "active", ended_at null, n_turns 0, forever. Three had accumulated
    by the time it was noticed, and the same shape of leak (200 run records a
    suite run) had already been found and fixed once in data/runs. Redirected
    rather than deleted afterwards, so the suite cannot write there at all.

    storage.SESSIONS_DIR is a module-level constant resolved at import, so the
    DATA_DIR env var is too late by now; SessionStore reads the module global
    at call time, which is what makes monkeypatching it work.
    """
    from server import storage
    from server.session import Session
    sessions = tmp_path / "sessions"
    sessions.mkdir(parents=True, exist_ok=True)
    real = storage.SESSIONS_DIR
    storage.SESSIONS_DIR = sessions
    try:
        return Session(sid)
    finally:
        storage.SESSIONS_DIR = real
