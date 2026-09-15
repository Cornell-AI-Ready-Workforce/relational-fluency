"""S2C is the third form of influence, and it has to be the same instrument as
the other two.

The bank-wide files already hold what is common to every spec: that every beat
carries a silence probe (test_scenario_probes), that the probe wrapper is
form-blind (test_on_silence_probe), that a cast is on its family's roster
(test_voice_casting), that the composed prompt states the length rule once
(test_prompt_assembly). None of them holds the thing that makes a THIRD form
legitimate rather than merely present: that a participant scored on S2C is
scored on the same skeleton, by the same voice, at the same beats, against the
same anchors, as one scored on S2A or S2B.

So the assertions below are written against BOTH siblings rather than against a
literal copied out of one of them. "S2C has six planted beats" goes stale the
day somebody changes the ladder; "S2C has as many beats as S2A and as S2B, with
the same items at the same positions" fails on the day the three stop matching,
which is the only day anybody needs to hear about it. With two forms every check
was A-vs-B and transitivity was free. With three it is not: B-vs-C is not
implied by A-vs-B unless someone checks it, so each comparison is run against
each sibling separately.

Three things here hold S2C to a rule its siblings are only partly held to, and
each says so where it is written:

  * the help-desk ban appears ONCE in this brief. S2A repeats it inside its
    agreement bullet because that bullet creates a slot the ban then has to
    defend — the self-contradiction measured at 23 banned phrases in 700 turns
    on A against 0 in 700 on B. This form's deflection mechanic is written so
    there is no slot to ban, so there is nothing to repeat.
  * no cue and no probe anywhere in the file names this form's precedent. Both
    siblings had to have that removed from a cue once, after it reached the
    actor through _trigger_instruction on the one turn where it costs most.
  * the probes are this form's own words, checked against both siblings. A
    probe is the only fixed line in an S2 spec and therefore the one most
    likely to be pasted from next door.

No network and no credentials. Everything here reads the compiled spec; the
live measurement that justified the content decisions is quoted in the spec's
own header, where it can be re-driven.
"""

from __future__ import annotations

import logging
import re
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from server import runs  # noqa: E402
from server import scenarios_v3 as v3  # noqa: E402
from server.identity import NAMES as PARTICIPANT_NAMES  # noqa: E402
from server.realtime_voice_session import RealtimeVoiceSessionRunner  # noqa: E402
from server.voice import realtime as rt_mod  # noqa: E402

SID = "S2C"
SIBLINGS = ("S2A", "S2B")
ALL_S2 = ("S2A", "S2B", "S2C")
AGENT = "imani"
SPEC_PATH = ROOT / "scenarios" / "v3" / "S2C_weekly_report.yaml"

GEMINI = "nto.gemini-live-2.5-flash"
GPT = "gpt-realtime-2.1"

# The precedent each form hides from its actor until the participant plays it.
PRECEDENT = {"S2A": "alex", "S2B": "rivera", "S2C": "delgado"}


def spec(sid=SID):
    return v3.load_spec(sid)


def brief(sid=SID, aid=AGENT):
    return spec(sid)["agents"][aid]["system_prompt"]


def flat(text):
    """One line, single-spaced, lower case.

    The briefs are YAML block scalars hard-wrapped at 78 columns, so every
    phrase worth asserting on spans a newline somewhere; matching the raw text
    would pass or fail on where the author pressed return."""
    return " ".join((text or "").split()).lower()


def triggers(sid=SID):
    """(interaction, trigger) for every planted beat, in spec order."""
    for inter in spec(sid).get("interactions", []):
        for trig in inter.get("triggers", []):
            yield inter, trig


def actor_facing(sid=SID):
    """Every string in this spec an ACTOR is ever handed.

    Deliberately not the scored anchors: `scores.high/low` are sample answers
    written for the rater and the judge, they are quoted on purpose in all three
    forms, and they are never composed into an actor's prompt."""
    out = [a["system_prompt"] for a in spec(sid)["agents"].values()]
    for inter in spec(sid).get("interactions", []):
        out.append(str(inter.get("opening") or ""))
        for trig in inter.get("triggers", []):
            out.append(str(trig.get("cue") or ""))
            out.append(str(trig.get("on_silence") or ""))
    return out


# --------------------------------------------------------------------------
# 1. It is in the bank, and it is a third form rather than a renamed second.
# --------------------------------------------------------------------------

def test_the_third_form_loads_from_the_bank():
    """Through available(), not off the disk: scenarios_v3 skips a spec missing
    one of _REQUIRED_KEYS with a log line and nothing else, so a spec that
    exists and a spec that loads are two different facts."""
    assert SID in v3.available()
    forms = sorted(sid for sid in v3.available()
                   if v3.load_spec(sid)["construct"] == "influence")
    assert forms == list(ALL_S2)
    assert spec()["variant"] == "C"
    assert spec()["id"] == SID


def test_influence_stays_whole_inside_the_one_to_one_arm():
    """A construct joins an arm only when EVERY form of it qualifies
    (server/runs.ARMS), so one group interaction in this file would not merely
    mis-file this form — it would take influence out of the 1:1 arm for
    everybody, leaving that arm one construct."""
    modes = runs._interaction_modes(SID)
    assert modes == ["one_to_one", "one_to_one"]
    assert runs._all_one_to_one(modes)
    for sib in SIBLINGS:
        assert runs._interaction_modes(sib) == modes
    assert "influence" in runs.arm_constructs("one_to_one")[0]


def test_the_restricted_arm_now_leaves_an_influence_form_in_reserve(tmp_path, monkeypatch):
    """What the third form is FOR, measured over the draw rather than asserted.

    The 1:1 arm has two constructs and four slots, so influence fills two of
    them. While it carried two forms, filling two slots spent the construct and
    `parallel_forms_spent` was true: there was no unseen encounter left for a
    second attempt. With three, one is left over, and runs.create records which
    one. This is the run document's own statement that a retest is possible."""
    # Both globals are repointed, and by ATTRIBUTE rather than by environment
    # variable. server.storage resolves DATA_DIR at import and server.runs binds
    # RUNS_DIR off it at import, so monkeypatch.setenv("DATA_DIR", ...) inside a
    # test is inert: the run files land in the repository's own data/runs next
    # to real collection data, a few dozen per suite run, and nothing fails.
    # DATA_DIR is repointed as well as RUNS_DIR because the completion-code
    # secret is written there.
    monkeypatch.setattr(runs, "DATA_DIR", tmp_path)
    monkeypatch.setattr(runs, "RUNS_DIR", tmp_path / "runs")
    seen_reserve = 0
    for seed in range(30):
        run = runs.create(f"S2C_reserve_{seed}", seed=seed, arm="one_to_one")
        pool = run["construct_pool"]
        served = [s["id"] for s in run["scenarios"] if s["construct"] == "influence"]
        assert len(served) == 2, served
        assert len(set(served)) == 2, (
            f"the same influence form was served twice in one run: {served}")
        left = pool["forms_in_reserve"].get("influence") or []
        assert set(left) == set(ALL_S2) - set(served), (
            f"served {served} but the run says {left} is in reserve")
        seen_reserve += bool(left)
    assert seen_reserve == 30
    assert not pool["parallel_forms_spent"], (
        "the run still reports every parallel form spent, which tells the "
        "researcher no retest is possible when one is")


def test_the_form_letter_is_a_legal_pin():
    """`?variant=C` in a Qualtrics redirect has to mean something for influence
    too. known_variants is computed from the specs, so the letter arrives by
    the spec being written; if it has not, the loader dropped the file."""
    assert "C" in runs.known_variants()
    assert runs.normalize_variant("c") == "C"
    with pytest.raises(ValueError):
        runs.normalize_variant("Z")


# --------------------------------------------------------------------------
# 2. Interchangeable with BOTH siblings, checked three ways round.
# --------------------------------------------------------------------------

def _shape(sid):
    """Everything a score from this form is compared across.

    Trigger ids are deliberately absent: S2's per-form rungs are named after
    their own obstacle (t2_rung1_no_budget against t2_rung1_company_wide), and
    that is checked separately. What must match is the ordered map — the mode,
    the kind, the ESCI items at each position, whether a beat carries a probe,
    and whether it carries rater anchors."""
    s = v3.load_spec(sid)
    return {
        "construct": s["construct"],
        "skill_measured": " ".join(s["skill_measured"].split()),
        "esci_items": s["esci_items"],
        "duration_minutes": s["duration_minutes"],
        "has_assets": bool(s.get("assets")),
        "has_private_setup": bool(s.get("private_setup")),
        "cast_size": len(s["agents"]),
        "interactions": [
            (i["mode"], i["kind"], i["label"],
             [(tuple(t["esci"]), bool(t.get("on_silence")),
               tuple(sorted((t.get("scores") or {}).keys())))
              for t in i["triggers"]])
            for i in s["interactions"]
        ],
    }


@pytest.mark.parametrize("sib", SIBLINGS)
def test_the_shape_matches_each_sibling_separately(sib):
    """Three forms is where pairwise stops being enough. A check that only ever
    compares the new form against the one its `parallel_form` scalar names
    leaves the third comparison to an argument."""
    assert _shape(SID) == _shape(sib)


def test_all_three_forms_are_one_instrument():
    shapes = {sid: _shape(sid) for sid in ALL_S2}
    assert len({repr(s) for s in shapes.values()}) == 1, (
        "the three forms of influence are no longer one instrument: "
        + repr({k: v["interactions"] for k, v in shapes.items()}))


def test_the_shared_beat_ids_are_shared_and_the_rung_ids_are_this_forms_own():
    """t1 and the two rung-4 beats are the same beat in all three forms and
    keep the same ids — verify_record._expected_triggers and the rater packet
    key on them. The three ladder rungs are named for the obstacle they carry,
    which differs per form by construction, and reusing a sibling's rung id
    would put two different obstacles behind one name in the evidence trace."""
    ids = [t["id"] for _, t in triggers()]
    assert len(ids) == 6
    shared = {"t1_the_opening", "t5_rung4_the_package", "t6_rung4_fairness"}
    for sib in SIBLINGS:
        sib_ids = [t["id"] for _, t in triggers(sib)]
        assert [i for i in ids if i in shared] == [i for i in sib_ids if i in shared]
        assert set(ids) & set(sib_ids) == shared, (
            f"{SID} shares {set(ids) & set(sib_ids)} with {sib}")
    assert ids[0] == "t1_the_opening"
    assert ids[-2:] == ["t5_rung4_the_package", "t6_rung4_fairness"]


def test_the_authored_scalar_is_provenance_and_says_so():
    """`parallel_form` cannot name two siblings, so it names the one this form
    was written to match and nothing routes on it. It stays a scalar and keeps
    that spelling — tools/gen_scenario_map.py subscripts it, and
    tests/test_rater_packet forbids the leak into the rater packet by this
    literal name, so a rename un-blinds the packet silently."""
    assert spec()["parallel_form"] in SIBLINGS
    head = SPEC_PATH.read_text(encoding="utf-8").split("parallel_form:")[0]
    assert "parallel_forms()" in head, (
        "nothing above the scalar tells the next reader that the routing "
        "authority is derived from `construct`; that reader will route on the "
        "scalar, and the scalar names one of three")


def test_the_title_is_this_forms_own_and_not_a_near_miss_of_s4as():
    """tests/test_demo_honesty counts titles equal to S4A's exactly, and
    requires every v3 title to be distinct. A near-miss title leaves that test
    passing while making it meaningless."""
    title = spec()["title"]
    others = {v3.load_spec(sid)["title"] for sid in v3.available() if sid != SID}
    assert title not in others
    assert "rollout" not in title.lower()
    s4a = v3.load_spec("S4A")["title"]
    assert set(title.lower().split()) & set(s4a.lower().split()) <= {"a", "an", "the"}


# --------------------------------------------------------------------------
# 3. It is not a third telling of a sibling's situation.
# --------------------------------------------------------------------------

# The two hooks already spent by this construct. Not a thesaurus: these are the
# words that would mean somebody had moved this form back onto a sibling's
# situation, at which point a participant who draws two influence encounters on
# an arm run negotiates the same thing twice and the second is not a new
# measurement.
S2A_HOOK = ("a raise", "the raise", "salary", "base pay", "off-cycle",
            "competing offer", "counter-offer", "pay rise", "compensation")
S2B_HOOK = ("remote", "hybrid", "return-to-office", "rto", "in the office",
            "days in", "work from home", "compliance numbers")


@pytest.mark.parametrize("hook,owner", [(S2A_HOOK, "S2A"), (S2B_HOOK, "S2B")])
def test_this_form_does_not_negotiate_a_siblings_subject(hook, owner):
    text = " ".join([spec()["setup"], *actor_facing(),
                     *[a for a in spec()["assets"]]]).lower()
    found = [w for w in hook if w in text]
    assert not found, (
        f"{SID} has drifted onto {owner}'s subject ({found}); on the 1:1 arm "
        "influence fills two slots, so two forms sharing a subject means the "
        "participant negotiates the same thing twice")


def test_the_ask_is_to_stop_something_and_the_manager_is_exposed_by_it():
    """The single fact that makes this a different instrument.

    Both siblings ask the manager to GIVE or LET KEEP, and in both the manager
    is sympathetic and constrained, which leaves a participant the cheap route
    of being sympathetic back. Here the ask is to STOP a commitment, and what
    stands in the way is the manager's own exposure upstairs. If the brief
    stops saying she is the one who answers for it, the scenario quietly
    becomes another form where warmth is enough."""
    low = flat(brief())
    assert "monday call" in low
    assert "you are the one standing" in low, (
        "the manager is no longer the person who answers for the report, so "
        "nothing costs her anything and the ladder is decorative")


def test_the_cast_name_collides_with_nobody():
    """Participants are issued a name from identity.NAMES and raters watch four
    encounters of one participant back to back. Two characters sharing a name
    across the bank is a continuity error in the data, not just on the day."""
    mine = {a["name"] for a in spec()["agents"].values()}
    assert mine == {"Imani"}
    assert not (mine & set(PARTICIPANT_NAMES))
    others = {a["name"] for sid in v3.available() if sid != SID
              for a in v3.load_spec(sid)["agents"].values()}
    assert not (mine & others), f"name already cast elsewhere: {mine & others}"


# --------------------------------------------------------------------------
# 4. Casting: one voice, the siblings', on both families.
# --------------------------------------------------------------------------

@pytest.mark.parametrize("model", [GEMINI, GPT])
@pytest.mark.parametrize("sib", SIBLINGS)
def test_it_is_cast_exactly_as_both_siblings_are(monkeypatch, model, sib):
    """A participant meets ONE of the three forms, so which manager's voice
    refused them is otherwise an uncontrolled difference between attempt 1 and
    attempt 2. Morgan and Sasha were pitched 50 Hz apart once and corrected to
    a single voice for the pair; a third form is a third chance to lose it.

    Resolved through the runner rather than read off the YAML, because the map
    is resolved against the family REALTIME_MODEL names at compile time and a
    spec that reads right can still resolve wrong."""
    monkeypatch.setattr(rt_mod, "MODEL", model)
    from server.scenarios import load_scenario

    def voices(sid):
        scenario = load_scenario(sid, "p_test")
        return [getattr(a, "realtime_voice", None) or a.voice_id
                for a in scenario.cast]

    mine = voices(SID)
    assert None not in mine and "" not in mine
    assert mine == voices(sib), f"on {model}: {SID} {mine} vs {sib} {voices(sib)}"


def test_both_families_are_named_and_the_legacy_scalar_agrees():
    rosters = {name: set(caps.voices) for name, caps in rt_mod.REALTIME_FAMILIES.items()}
    for aid, a in spec()["agents"].items():
        mapping = a["realtime_voice"]
        assert set(mapping) == set(rosters), f"{aid} is cast for {sorted(mapping)}"
        for family, voice in mapping.items():
            assert voice in rosters[family], f"{aid}: {voice} is not on {family}"
        assert mapping["gemini-live"] != mapping["gpt-realtime"], (
            f"{aid}: the rosters are disjoint, so one name in both columns is "
            "a wish rather than a measurement")
        assert a["voice"] == mapping["gemini-live"], (
            f"{aid}: the v1 fallback field disagrees with the map")


# --------------------------------------------------------------------------
# 5. The participant's leverage is declared private, and stays private.
# --------------------------------------------------------------------------

def test_private_setup_is_declared_and_actually_withholds_something():
    """An entry that is not a substring of `setup` is silently a no-op: the
    word-overlap guess is skipped because the key is present, and nothing is
    withheld. That failure looks like success from every angle."""
    private = spec().get("private_setup") or []
    assert private, f"{SID} has assets but declares no private_setup"
    setup = spec()["setup"]
    for entry in private:
        assert entry.lower() in setup.lower(), (
            f"private_setup entry {entry!r} does not occur in `setup`")


def test_the_leverage_leaves_the_actor_scene_and_the_guess_never_runs(caplog):
    """The scene compiled here is pasted verbatim into the actor's system
    prompt, and the same text unredacted goes to the judge and the steering
    controller. Check both ends, and check no WARNING fired — a WARNING means
    the word-overlap guess ran, which is what the declaration exists to
    prevent."""
    with caplog.at_level(logging.INFO, logger="server.scenarios_v3"):
        sc = v3.compile_scenario(SID)
    scene = sc.scene.lower()
    assert PRECEDENT[SID] not in scene, "the precedent reached the actor scene"
    assert "open logs" not in scene, "the evidence reached the actor scene"
    # The judge and the steering controller need the leverage; only the actor
    # loses it. This is the half a blunt "strip the assets" fix breaks.
    assert PRECEDENT[SID] in sc.analysis_scene.lower()
    assert "open logs" in sc.analysis_scene.lower()
    warnings = [r.getMessage() for r in caplog.records if r.levelno >= logging.WARNING]
    assert not warnings, f"compiling {SID} still warns: {warnings}"


def test_the_precedent_is_in_the_brief_as_a_thing_she_is_holding_back():
    """Withheld, not absent. The actor has to KNOW it to concede it when the
    participant plays it at rung 3, and must never reach for it first. Both
    siblings carry the same two sentences about their own precedent."""
    low = flat(brief())
    assert PRECEDENT[SID] in low
    assert "it is not an example, it is not a hint" in low
    assert "does not come out of your mouth first" in low
    for sib, aid in (("S2A", "morgan"), ("S2B", "sasha")):
        sib_low = flat(brief(sib, aid))
        assert "does not come out of your mouth first" in sib_low, sib


@pytest.mark.parametrize(
    "iid,trig",
    [(i["id"], t) for i, t in triggers() if t["id"] != "t5_rung4_the_package"],
    ids=[t["id"] for _, t in triggers() if t["id"] != "t5_rung4_the_package"])
def test_no_cue_and_no_probe_anywhere_names_the_precedent(iid, trig):
    """_trigger_instruction pastes the whole cue into the stage direction, so a
    cue that carries the precedent hands it to the actor on the one turn where
    it costs most: rung 3 exists to score whether the PARTICIPANT found it. Both
    siblings had exactly this removed from their rung-3 cue once. A negation
    would be no better — it still puts the name in front of her — so the
    conditional logic lives in the brief and the name appears in neither key.

    t5 is the one exception all three forms make, and it is excluded here and
    held to a stricter rule of its own in the test below: there the name may
    appear only inside a condition on what the participant has already done."""
    for field in ("cue", "on_silence"):
        assert PRECEDENT[SID] not in (trig.get(field) or "").lower(), (
            f"{iid} {trig['id']}: the precedent is named in `{field}`")


def test_the_one_cue_that_may_mention_the_precedent_keeps_it_conditional():
    """t5 is the exception both siblings make and for the same reason: triggers
    fire strictly in order, one per participant turn, with nothing testing what
    the participant said, so an unconditional concession makes the manager
    volunteer the precedent on a fixed beat even in the runs where it was never
    found — and then t4 measures nothing."""
    t5 = next(t for _, t in triggers() if t["id"] == "t5_rung4_the_package")
    cue = flat(t5["cue"])
    assert "if they have already put" in cue
    assert "if they have not, do not raise" in cue
    for sib in SIBLINGS:
        sib_t5 = next(t for _, t in triggers(sib) if t["id"] == "t5_rung4_the_package")
        assert "if they have already put" in flat(sib_t5["cue"]), sib


# --------------------------------------------------------------------------
# 6. Every beat is in the OPENING brief, not only in its cue.
# --------------------------------------------------------------------------
# A mid-session session.update is inert on the configured realtime model, so a
# beat that depends on its cue arriving is a beat that does not happen. The cue
# starts a beat the brief already carries; it is not the beat.

BEAT_IN_BRIEF = {
    "t2_rung1_only_look": ("only look", "mistake"),
    "t3_rung2_how_it_reads": ("nothing arrives", "keep up"),
    "t4_rung3_the_coo_asks": ("coo asks for it by name", "monday call"),
    "t5_rung4_the_package": ("obstacle 4, the package", "who builds it"),
    "t6_rung4_fairness": ("standing send who comes asking", "lose sleep over"),
}


@pytest.mark.parametrize("tid", sorted(BEAT_IN_BRIEF))
def test_the_beat_is_carried_by_the_brief_as_well_as_by_the_cue(tid):
    assert tid in {t["id"] for _, t in triggers()}
    low = flat(brief())
    for needle in BEAT_IN_BRIEF[tid]:
        assert needle in low, (
            f"the brief does not carry {tid} ({needle!r} missing); on the "
            "configured model a beat that only exists in its cue does not "
            "happen")


def test_the_ladder_is_four_obstacles_in_order_with_the_hand_back_attached():
    """The shape all three forms share, because the brief IS the instrument
    here. Without the one-per-turn discipline the actor delivers the ladder as
    a list in its second turn and the encounter has nowhere left to go."""
    low = flat(brief())
    assert "the four obstacles, in this order, one per push" in low
    assert "put one of these in front of them per turn" in low
    assert "never two obstacles at once, never a list" in low
    assert "then hand the floor straight back" in low
    for sib, aid in (("S2A", "morgan"), ("S2B", "sasha")):
        sib_low = flat(brief(sib, aid))
        assert "the four obstacles, in this order, one per push" in sib_low, sib


def test_rung_three_names_somebody_the_participant_could_go_and_get():
    """behind_scenes and key_people are scored at rung 3 in all three forms.

    S2A's rung 3 is the one in this construct with nobody in it — a door that
    cannot be reclosed — so there is no one for the participant to develop
    support with, and two of the six items are scored off a beat that does not
    invite them. S2B's has the director and this one has the COO. If a later
    edit takes the person back out of this rung, the two items go with it."""
    t4 = next(t for _, t in triggers() if t["id"] == "t4_rung3_the_coo_asks")
    assert set(t4["esci"]) >= {"behind_scenes", "key_people"}
    assert "coo" in flat(t4["cue"])
    assert "coo" in flat(brief())
    sib = next(t for _, t in triggers("S2B") if t["id"].startswith("t4_"))
    assert set(sib["esci"]) == set(t4["esci"])


def test_the_concession_is_gated_on_the_participant_playing_the_precedent():
    """Rung 3 is the beat that scores whether they found it. A manager who
    concedes on the turn rather than on the move has answered her own trigger,
    and the two items at that position measure nothing."""
    low = flat(brief())
    assert "obstacle 3 is the one you cannot actually defend" in low
    assert "if they put delgado's team in front of you at obstacle 3" in low
    assert "conceding is not agreeing" in low
    assert "it moves you to obstacle 4 and nowhere else" in low


# --------------------------------------------------------------------------
# 7. Nothing recitable, no help-desk register, and no slot for one.
# --------------------------------------------------------------------------

BANNED_QUOTES = {'"i appreciate"', '"i hear you"', '"i understand"',
                 '"that\'s a great point"', '"fair point"'}


def test_the_only_quoted_strings_an_actor_is_handed_are_the_banned_phrases():
    """Recitation across this bank fell from 24% of turns to 4% when beats were
    respecified as INTENT and PRESSURE. A quoted fragment in a brief is carried
    on every turn of the encounter and comes back word for word — both siblings
    had their quoted tempo fragments removed for exactly that, after one of
    them opened 77% of its turns on one of five stock words.

    The one deliberate exception is the ban itself: banning the move alone
    leaves the actor the strings, and banning the strings alone leaves the
    actor the move, measured both ways on the siblings."""
    for text in actor_facing():
        quoted = re.findall(r'"[^"]*"', " ".join(text.split()))
        leftover = [q for q in quoted if q.lower() not in BANNED_QUOTES]
        assert not leftover, f"an actor is handed a line to recite: {leftover}"
        for ch in ("“", "”"):
            assert ch not in text


def test_the_help_desk_ban_is_word_for_word_the_siblings():
    """Whatever is banned by name in one brief and not another migrates to the
    character it is not named for: with "fair point" named only in Morgan's
    brief, Sasha said it twice in 66 turns and Morgan none. The list and the
    clause after it are the same words in all three files.

    THIS IS A PARITY CLAIM INSIDE THE INFLUENCE FAMILY, not a rule about the
    bank. tests/test_s4c.py::test_no_character_is_written_in_help_desk_register
    holds the opposite for Teamwork's third form — no help-desk phrase may
    appear in any S4C brief, the ban written as a description of the noise — and
    both are right for the same reason: what interchangeability requires is that
    the three forms of ONE construct treat the register identically, which S2
    does by quoting and S4C does by describing. Measured across all twelve forms
    at eight replicates each, help-desk phrases in what the actors actually
    said: 0.00 to 0.38 hits per run everywhere, S2 0.12/0.00/0.00, S4 0.38/0.12/
    0.00 — the quoted list does not cause the leak and the description does not
    permit it, so neither claim may be promoted into a bank-wide rule at this n.
    """
    ban = ('Nothing you say should sound like a help desk. Never "I appreciate" '
           'anything, never "I hear you", never "I understand", never "that\'s a '
           'great point" or "fair point" — not as an opener, and not in front of '
           'a concession.')
    for sid, aid in (("S2A", "morgan"), ("S2B", "sasha"), (SID, AGENT)):
        assert ban in " ".join(brief(sid, aid).split()), sid


def test_the_ban_is_stated_once_because_this_brief_digs_no_hole_for_it():
    """The S2A self-contradiction, which this form must not reproduce.

    S2A asks for a one-word agreement fused onto the front of every refusal and
    then forbids every word that could fill that slot; an actor handed a slot it
    may not fill fills it anyway, and the measured result was 23 banned phrases
    in 700 turns on A against 0 in 700 on B — the register regression and the
    parallel-form failure in one number. The repair was to delete the slot, not
    to repeat the ban, and Morgan's brief still carries the repetition from
    before. This form's mechanic is substance with no fixed position in the
    sentence, so a second statement of the ban would be guarding a hole that
    does not exist."""
    low = flat(brief())
    assert low.count("help desk") == 1, (
        "the help-desk ban is stated twice, which in this construct is the "
        "signature of a brief that has created a slot it then has to defend")
    for phrase in ('in front of it', 'fused', 'one word', 'a word in front'):
        assert phrase not in low, (
            f"{phrase!r} is the shape of S2A's contradiction: a fixed position "
            "in the sentence for a word the brief also bans")


def test_the_deflection_mechanic_is_bounded_so_it_cannot_become_a_refrain():
    """Each bound is a failure this construct has already had: an unbounded
    mechanic becomes a refrain the participant cannot close, a mechanic that is
    a move of its own makes a third move in a turn budgeted for two, and a
    mechanic that concedes a little every turn is a ladder with no top."""
    low = flat(brief())
    assert "never the same one twice" in low
    assert "not a move of its own" in low
    assert "most turns have none of it in them" in low
    assert "it is not a concession and never the start of one" in low


@pytest.mark.parametrize("rule", [
    "No speeches.",
    "Two sentences a turn at the outside, and short ones. If a third is "
    "starting, the turn was over.",
    "TWO moves a turn, at the outside: the thing you are saying, and the thing "
    "you hand back.",
    "You do not say the same sentence twice.",
    "You do not answer pressure with somebody else's name.",
])
def test_the_turn_discipline_is_identical_across_the_three_forms(rule):
    """Manager airtime is the inverse of the dependent variable in this
    instrument: a participant who draws the long manager gets less floor of
    their own, and the difference between forms is reported as skill change.
    These are the sentences that hold the three level, so they are held
    byte-identical rather than paraphrased."""
    for sid, aid in (("S2A", "morgan"), ("S2B", "sasha"), (SID, AGENT)):
        assert rule in " ".join(brief(sid, aid).split()), f"{sid} has reworded: {rule!r}"


def test_the_brief_does_not_narrate_its_character_in_the_third_person():
    """The composed prompt tells the actor never to refer to itself in the
    third person. A brief that does it first is the instruction losing an
    argument with its own example."""
    name = spec()["agents"][AGENT]["name"]
    body = brief().split("\n", 1)[1]
    assert name not in body, f"the brief names {name} outside its opening line"


def test_the_brief_is_not_materially_longer_than_the_longest_sibling():
    """A proxy, not the thing — what has to match is what the characters DO,
    and that is measured by driving them. But brief length is where the last
    round's airtime gap started, so it is worth a ratchet: this form carries one
    mechanic bullet the siblings do not, and nothing else."""
    words = {sid: len(brief(sid, aid).split())
             for sid, aid in (("S2A", "morgan"), ("S2B", "sasha"), (SID, AGENT))}
    assert words[SID] <= max(words["S2A"], words["S2B"]) * 1.05, words


# --------------------------------------------------------------------------
# 8. Probes: present, this form's own, speakable, and verbatim through the
#    wrapper.
# --------------------------------------------------------------------------

def test_every_beat_carries_a_probe_that_is_not_its_cue():
    """Nothing in a 1:1 speaks first, so a participant who freezes stalls the
    encounter: the watchdog probes only where the next unfired trigger has an
    on_silence, and _maybe_advance will not move the scene past an unfired
    beat. A beat without one is dead air, a silent WAV and an empty
    transcript."""
    for _, trig in triggers():
        probe = (trig.get("on_silence") or "").strip()
        assert probe, f"{trig['id']}: no probe"
        assert probe != (trig.get("cue") or "").strip()


@pytest.mark.parametrize("sib", SIBLINGS)
def test_the_probes_are_this_forms_own_words(sib):
    """A probe is the only fixed line in an S2 spec, so it is the line most
    likely to be pasted from next door — and it is the one place a participant
    could hear another form's encounter."""
    mine = {(t["on_silence"] or "").strip().lower() for _, t in triggers()}
    theirs = {(t["on_silence"] or "").strip().lower() for _, t in triggers(sib)}
    assert not (mine & theirs), f"probe shared with {sib}: {mine & theirs}"


@pytest.mark.parametrize("iid,trig", [(i["id"], t) for i, t in triggers()],
                         ids=[t["id"] for _, t in triggers()])
def test_the_probe_is_a_line_this_character_could_say_out_loud(iid, trig):
    """S1 and S2 write their probes as speakable lines, deliberately and pinned
    (see _trigger_instruction's docstring): a probe fires at the one moment the
    encounter has no signal of its own, so every participant who freezes at a
    beat should meet the same sentence and what differs between them should be
    their answer. S3's "that ..." complements are the other legitimate style and
    this form must not drift into them halfway."""
    probe = trig["on_silence"].strip()
    assert probe[0].isupper(), f"{trig['id']}: not a sentence: {probe!r}"
    assert probe.endswith((".", "?", "!")), f"{trig['id']}: {probe!r}"
    assert not probe.lower().startswith("that "), (
        f"{trig['id']}: this is S3's complement style, not S1/S2's line")
    for narration in ("they have gone", "the silence", "let the pause",
                      "she presses", "the participant"):
        assert narration not in probe.lower(), (
            f"{trig['id']}: the probe narrates rather than speaks, and "
            "_trigger_instruction hands whatever is here to the actor as its "
            "next move")


def test_cue_and_probe_reach_the_actor_verbatim_through_the_wrapper():
    """The wrapper may be rephrased; the beat may not. Duplicated from
    test_prompt_assembly deliberately: that file checks the wrapper's manners,
    this one is the contract a future rewording is measured against, and a
    contract in another file is one a rewrite does not read."""
    import types

    scenario = v3.compile_scenario(SID)
    by_id = {a.id: a for a in scenario.cast}
    for inter in scenario.interactions:
        agent = by_id[inter["agent"]]
        for trig in inter["triggers"]:
            runner = types.SimpleNamespace(agent=agent)
            probe = RealtimeVoiceSessionRunner._trigger_instruction(
                runner, trig, probing=True)
            cue = RealtimeVoiceSessionRunner._trigger_instruction(
                runner, trig, probing=False)
            assert trig["on_silence"] in probe, trig["id"]
            assert trig["cue"] in cue, trig["id"]
            assert f"Your next move, as {agent.name}" in probe
            assert f"Your next move, as {agent.name}" in cue


# --------------------------------------------------------------------------
# 9. The anchors a rater reads, and the openings that make them reachable.
# --------------------------------------------------------------------------

@pytest.mark.parametrize("tid", ["t1_the_opening", "t4_rung3_the_coo_asks",
                                 "t5_rung4_the_package", "t6_rung4_fairness"])
def test_the_scored_anchors_are_things_a_person_says_out_loud(tid):
    """The anchors are read by raters and handed to the judge. A parenthetical
    stage direction is not a thing anyone said into a microphone; one of these
    had to be rewritten in a sibling for exactly that. And the low anchor may
    not be a capitulation: at a beat scoring multiple_approaches a participant
    who simply folds maps to none of the items, which is what made the two
    siblings score different behaviour at the same position for a while."""
    trig = next(t for _, t in triggers() if t["id"] == tid)
    scores = trig["scores"]
    for band in ("high", "low"):
        answer = scores[band]["answer"].strip()
        assert answer.endswith((".", "?", "!")), f"{tid}/{band}: {answer!r}"
        assert not answer.startswith("("), f"{tid}/{band} is a stage direction"
        assert scores[band]["why"].strip()
    for sib in SIBLINGS:
        sib_trig = next((t for _, t in triggers(sib) if t["id"] == tid), None)
        if sib_trig:
            assert sorted(sib_trig["scores"]) == sorted(scores), tid


def test_the_opening_gets_in_ahead_of_the_ask_and_then_gets_out_of_the_way():
    """In a 1:1 nothing speaks first, so the interaction's own opening is the
    only line the participant has to answer, and t1 is the manager's REPLY to
    whatever they open with rather than a line she delivers. An opening that
    does not hand the floor over leaves the participant with nothing to do, and
    the low anchor at t1 — an ultimatum instead of a case — is by construction
    an answer to an invitation."""
    opening = spec()["interactions"][0]["opening"].strip()
    assert opening.endswith(("Go on.", "Go on", "?")), opening
    assert '"' not in opening
    t1 = next(t for _, t in triggers() if t["id"] == "t1_the_opening")
    cue = flat(t1["cue"])
    assert "get in ahead of the ask" in cue
    assert "let them put it to you" in cue
    for sib in SIBLINGS:
        sib_t1 = next(t for _, t in triggers(sib) if t["id"] == "t1_the_opening")
        assert "get in ahead of the ask" in flat(sib_t1["cue"]), sib


# --------------------------------------------------------------------------
# 10. It compiles into something the runner can actually drive.
# --------------------------------------------------------------------------

def test_the_actor_scene_survives_the_third_person_rewrite():
    """`setup` is written to the participant in the second person and is
    rewritten for the actor by a pronoun pass. A setup the pass cannot read
    leaves broken grammar pasted verbatim into the actor's prompt in every
    encounter, behind nothing but a warning nobody reads."""
    scene = v3.compile_scenario(SID, "p_test").scene
    assert scene
    for bad in (" you ", " your ", "them'", "them're", "them've"):
        assert bad not in f" {scene.lower()} ", f"mangled actor scene: {scene!r}"
    assert "participant" in scene.lower()


def test_the_composed_prompt_carries_the_brief_and_the_scene():
    """A character whose prompt does not compose arrives as the gateway's stock
    assistant, fluently, and the recording looks normal."""
    from server.engine import AgentEngine
    from server.scenarios import load_scenario

    scenario = load_scenario(SID, "p_test")
    agent = next(a for a in scenario.cast if a.id == AGENT)
    engine = AgentEngine(agent, scenario, scenario.initial_personas()[AGENT])
    prompt = engine._system_prompt([], None, group=False)
    assert agent.name in prompt
    assert "The four obstacles" in prompt
    assert scenario.scene in prompt
    assert PRECEDENT[SID] not in prompt.split("## Scene")[1].split("You are **")[0].lower()
    assert len(prompt) > 2000
