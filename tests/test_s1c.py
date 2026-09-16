"""S1C is the third form of conflict management, and it has one job.

FORM_EXCLUSIONS bars conflict_management variant A from any run containing
teamwork, because S1A's hook — a colleague presenting the participant's
analysis as his own — is the same event S4 measures the participant's handling
of from the other side. Every full run contains teamwork, so S1A is never
served in the unrestricted arm and that arm had no A/B contrast for this
construct at all: every participant met S1B.

S1C exists to give the contrast back, and everything below is written against
the ways that can silently fail to happen:

  1. It must be SERVEABLE alongside teamwork. If it needed a second exclusion
     row the draw would halve again and the form would buy nothing, so this
     file measures the draw rather than reading the table.
  2. It must not BE S1A. The collision is a situation, not a word, and the
     firewall is that the participant here is genuinely half at fault. What can
     be held mechanically is that nobody in it claims anybody's work.
  3. It must stay interchangeable with BOTH siblings, not just the one its
     authored `parallel_form` scalar happens to name. A third form is where a
     pairwise check stops being enough: B-vs-C is not implied by A-vs-B unless
     someone checks it.
  4. Its beats must live in the OPENING BRIEF. On nto.gemini-live-2.5-flash a
     mid-session session.update is inert, so a beat whose precondition is
     written only into a cue does not happen. Both siblings lost this beat
     3/4-to-0/4 once already, and the cue looked fine throughout.

No network and no credentials here. The live measurement is quoted in the spec
file's header; this is what holds its shape still afterwards.
"""

from __future__ import annotations

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


# ---------------------------------------------------------------------------
# WHICH STUDY DESIGN THESE TESTS ARE ABOUT
#
# A FEW of the tests below measure the PER-SLOT DRAW: twelve forms, two of each
# construct's three used and the third held in reserve, FORM_EXCLUSIONS applied
# to the completed draw. `DEFAULT_RUN_VARIANT=random` is the setting that
# selects it, and those tests -- and only those -- carry
# `@pytest.mark.usefixtures("per_slot_draw")`.
#
# The merged DEFAULT is `A` -- origin/main's Phase 1 design, which pins
# S1A/S2A/S3A/S4A on every run. That is deliberate and it is the PI's call, not
# this file's. A pinned run has nothing to say about the draw: _apply_form_
# exclusions leaves a pinned slot alone BY DESIGN, so under the default the
# exclusion never fires and the reserve is never drawn, and an assertion about
# either would be testing a mechanism that is switched off rather than one that
# is broken. Those are the marked tests.
#
# EVERYTHING ELSE RUNS ON THE SHIPPED DEFAULT, which is the point of this note.
# The fixture was autouse in this module and four others until 2026-09-15, and
# that pinned 270 tests onto `random` when 20 of them are about the draw:
# stripped and re-run under the default, 15 failed and 255 passed. Entry links,
# arms, gates and cohort integrity are not about form selection, and pinning
# them meant the configuration Phase 1 will actually run had almost no coverage
# in this suite at all.
#
# The shipped default itself, and the fact that it turns the S1A/Teamwork
# exclusion off for the whole of Phase 1, is asserted head-on in
# tests/test_default_run_variant.py.
# ---------------------------------------------------------------------------

@pytest.fixture
def per_slot_draw(monkeypatch):
    """Ask for the per-slot draw, for the handful of tests that are ABOUT it.

    NOT autouse. It was, in all five of these modules, and that pinned 270
    tests off the configuration Phase 1 actually runs when only 20 of them
    need it: measured on 2026-09-15 by stripping the fixture and running the
    five modules under the shipped default -- 15 failed, 255 passed. Tests
    about entry links, arms, gates and cohort integrity are not about form
    selection and now run on the default a participant will meet.
    """
    monkeypatch.setenv("DEFAULT_RUN_VARIANT", "random")


SIBLINGS = ("S1A", "S1B")
ALL_S1 = ("S1A", "S1B", "S1C")


@pytest.fixture(scope="module")
def spec():
    return v3.load_spec("S1C")


def _strings(obj):
    """Every authored string in a spec, in no particular order."""
    if isinstance(obj, str):
        yield obj
    elif isinstance(obj, list):
        for x in obj:
            yield from _strings(x)
    elif isinstance(obj, dict):
        for x in obj.values():
            yield from _strings(x)


def _flat(text):
    """Whitespace-normalised lowercase.

    The briefs are YAML block scalars wrapped at 78 columns, so every phrase
    worth asserting on spans a newline somewhere. Matching the raw text would
    make these tests pass or fail on where the line happened to wrap."""
    return " ".join((text or "").split()).lower()


def _triggers(spec):
    for inter in spec.get("interactions", []):
        for trig in inter.get("triggers", []):
            yield inter, trig


# --------------------------------------------------------------------------
# 1. It is in the bank, and it is a third form rather than a renamed second.
# --------------------------------------------------------------------------

def test_s1c_is_the_third_form_of_conflict_management():
    assert "S1C" in v3.available()
    forms = sorted(sid for sid in v3.available()
                   if v3.load_spec(sid)["construct"] == "conflict_management")
    assert forms == list(ALL_S1)
    assert v3.load_spec("S1C")["variant"] == "C"
    assert "C" in runs.known_variants(), (
        "known_variants is computed from the specs, so a third letter should "
        "arrive by the spec being written; if it has not, the loader dropped "
        "the file"
    )


def test_the_form_letter_is_a_legal_pin_now_that_a_form_carries_it():
    """`?variant=C` in a Qualtrics redirect has to mean something.

    normalize_variant refuses a letter no form carries, and that refusal is
    what stops one wrong character in a link from stamping a whole wave with
    "form was pinned by the caller; exclusion not applied". C must now pass and
    the nonsense must still fail."""
    assert runs.normalize_variant("c") == "C"
    with pytest.raises(ValueError):
        runs.normalize_variant("Z")


# --------------------------------------------------------------------------
# 2. The whole point: it is serveable alongside teamwork.
# --------------------------------------------------------------------------

def test_no_second_exclusion_row_was_added():
    """A second row would halve the draw again, which is the thing this form
    was written to undo. If a future situation edit genuinely collides, the
    answer is a different situation, not another row."""
    assert runs.FORM_EXCLUSIONS == [("conflict_management", "A", "teamwork")]


@pytest.mark.usefixtures("per_slot_draw")
def test_the_unrestricted_arm_serves_s1c_alongside_teamwork(tmp_path, monkeypatch):
    """Measured over the draw, not read off the exclusion table.

    Before this form existed the unrestricted arm served S1B to every
    participant, because every full run contains teamwork and the only other
    form was the excluded one. The contrast is what the form buys.

    It used to be a SKEWED contrast and this docstring used to say so and stop
    there: _apply_form_exclusions replaced an excluded draw with the first
    permitted form in the construct's sorted list, always S1B, so the arm ran
    68/32 and 7 attempt1→attempt2 pairs in 10 were the same ordered pair. The
    replacement is now rotated on a digest of the run's own draw — still no
    randomness, still seed-reproducible — and the arm runs 50.7/49.3 over 2000
    seeds. The bounds below stay loose because they guard against a form being
    squeezed out; they are not pinning a ratio.
    """
    # Both globals are repointed, and by ATTRIBUTE rather than by environment
    # variable. server.storage resolves DATA_DIR at import and server.runs binds
    # RUNS_DIR off it at import, so monkeypatch.setenv("DATA_DIR", ...) here was
    # INERT: these 200 runs were written into the repository's own data/runs,
    # beside real collection data, on every invocation of the suite (measured
    # before/after one `pytest tests/test_s1c.py`: 5929 → 6129 records, exactly
    # +200, all green). DATA_DIR is repointed as well as RUNS_DIR because the
    # completion-code secret is persisted there.
    monkeypatch.setattr(runs, "DATA_DIR", tmp_path)
    monkeypatch.setattr(runs, "RUNS_DIR", tmp_path / "runs")
    served = {}
    for seed in range(200):
        run = runs.create(f"p{seed}", seed=seed)
        assert any(s["construct"] == "teamwork" for s in run["scenarios"]), (
            "a full run that does not contain teamwork would make this "
            "measurement meaningless"
        )
        for s in run["scenarios"]:
            if s["construct"] == "conflict_management":
                served[s["id"]] = served.get(s["id"], 0) + 1
    assert "S1A" not in served, "the exclusion stopped working"
    assert served.get("S1C", 0) >= 20, (
        f"S1C is served to {served.get('S1C', 0)} of 200 unrestricted runs; "
        "the construct is back to a single form in that arm"
    )
    assert served.get("S1B", 0) >= 20, f"S1B has been squeezed out: {served}"


def test_conflict_management_stays_in_the_one_to_one_arm(spec):
    """Both interactions are one_to_one, and that is structural.

    runs._all_one_to_one requires EVERY mode in a spec to be one_to_one for the
    encounter to be drawable into the 1:1 arm. One group interaction in this
    file would drop conflict management out of that arm entirely and leave it
    with a single construct."""
    assert [i["mode"] for i in spec["interactions"]] == ["one_to_one", "one_to_one"]
    assert runs._all_one_to_one([i["mode"] for i in spec["interactions"]])
    assert "conflict_management" in runs.arm_constructs("one_to_one")[0]


# --------------------------------------------------------------------------
# 3. It is not a fourth S1A.
# --------------------------------------------------------------------------

# The S1A hook, in the forms an author would actually write it in. Not a
# thesaurus: these are the phrases that would mean somebody had moved this
# situation back onto credit misattribution, which is where it collides with
# teamwork and needs the exclusion row that test above forbids.
CREDIT_HOOK = (
    "credit", "authorship", "as his own", "as her own", "as their own",
    "as my own", "passed it off", "took the credit", "claimed the work",
)


def test_nothing_in_this_form_turns_on_somebody_claiming_the_work(spec):
    """The firewall, stated where an edit would trip over it.

    S1A's participant is an innocent whose contribution was stolen; this one
    co-owns a failure and has been left holding all of it. The direction is
    inverted — a cost assigned, not a benefit taken — and that is the only
    reason this form may be served in a run containing teamwork."""
    text = " ".join(_strings(spec)).lower()
    found = [w for w in CREDIT_HOOK if w in text]
    assert not found, (
        f"S1C has drifted onto S1A's hook ({found}); a run containing teamwork "
        "may then no longer draw it, and the exclusion table needs a second "
        "row, which is what this form exists to avoid"
    )


def test_the_participant_is_half_at_fault_and_the_counterpart_may_say_so(spec):
    """The single fact that makes this a different instrument.

    The high-scoring move here is not "prove it was not me" — it is getting the
    record to say both of them while keeping the peer able to work with them on
    Friday, which requires conceding their own half out loud. Neither sibling
    ever asks for that. If the counterpart's brief stops saying the sign-off
    was shared, the scenario quietly becomes one where the participant is
    innocent again."""
    wes = _flat(spec["agents"]["wes"]["system_prompt"])
    assert "signed" in wes and "both" in wes
    assert "true" in wes, (
        "the counterpart must know his central claim is TRUE; a defence the "
        "actor privately believes is false is played as a bluff, and the "
        "participant then wins by contradicting it"
    )
    nadia = _flat(spec["agents"]["nadia"]["system_prompt"])
    assert "signed" in nadia, (
        "the ally must hold the shared sign-off too, or she drifts into "
        "offering an exoneration and the form becomes S1A with new names"
    )


def test_the_title_is_not_a_near_miss_of_the_teamwork_title():
    """test_demo_honesty counts titles equal to S4A's exactly, so a near-miss
    title is how that test stops meaning anything."""
    title = v3.load_spec("S1C")["title"]
    assert title == "Blamed in front of the manager"
    assert "rollout" not in title.lower()
    assert title != v3.load_spec("S4A")["title"]


def test_the_cast_names_collide_with_nobody(spec):
    """Participants are issued a name from identity.NAMES, and raters watch
    four encounters of one participant back to back. Two characters sharing a
    name across the bank is a continuity error in the data, not just on the
    day."""
    mine = {a["name"] for a in spec["agents"].values()}
    assert mine == {"Nadia", "Wes"}
    assert not (mine & set(PARTICIPANT_NAMES))
    others = {a["name"]
              for sid in v3.available() if sid != "S1C"
              for a in v3.load_spec(sid)["agents"].values()}
    assert not (mine & others), f"name already cast elsewhere in the bank: {mine & others}"


# --------------------------------------------------------------------------
# 4. Interchangeable with BOTH siblings, checked three ways round.
# --------------------------------------------------------------------------

def _shape(sid):
    """Everything a score from this form is compared across."""
    spec = v3.load_spec(sid)
    return {
        "construct": spec["construct"],
        "skill_measured": " ".join(spec["skill_measured"].split()),
        "esci_items": spec["esci_items"],
        "duration_minutes": spec["duration_minutes"],
        "pre_reading": bool(spec.get("pre_reading")),
        "assets": bool(spec.get("assets")),
        "interactions": [
            (i["mode"], i["kind"], [(t["id"], tuple(t["esci"]), bool(t.get("on_silence")),
                                     sorted((t.get("scores") or {}).keys()))
                                    for t in i["triggers"]])
            for i in spec["interactions"]
        ],
    }


@pytest.mark.parametrize("sibling", SIBLINGS)
def test_s1c_has_the_same_shape_as_each_sibling_separately(sibling):
    """Three forms is where pairwise stops being enough.

    With two forms every check is A-vs-B and transitivity is not needed. With
    three, a check that only ever compares each new form against the one its
    `parallel_form` scalar names leaves B-vs-C to an argument. This runs both
    comparisons directly."""
    assert _shape("S1C") == _shape(sibling)


def test_all_three_forms_agree_on_every_beat_id_and_item_map():
    """The ids are what verify_record._expected_triggers and the rater packet
    key on. A third form that renamed its beats would be scored as a different
    instrument at the same position."""
    shapes = {sid: _shape(sid) for sid in ALL_S1}
    assert len({repr(s) for s in shapes.values()}) == 1, (
        "the three forms of conflict management are no longer one instrument: "
        + repr({k: v["interactions"] for k, v in shapes.items()})
    )
    ids = [t["id"] for _, t in _triggers(v3.load_spec("S1C"))]
    assert ids == ["t1_retaliation_fork", "t2_the_opening",
                   "t3_defensiveness", "t4_half_concession"]


def test_the_authored_scalar_is_provenance_and_says_so():
    """`parallel_form` cannot name two siblings, so it names the one this form
    was written to match and nothing routes on it. The comment above it is half
    of that decision; the other half is that the derived grouping, not the
    scalar, is what the equivalence checks are built over."""
    assert v3.load_spec("S1C")["parallel_form"] == "S1A"
    src = (ROOT / "scenarios" / "v3" / "S1C_public_blame.yaml").read_text(encoding="utf-8")
    before = src.split("parallel_form:")[0]
    assert "parallel_forms()" in before, (
        "nothing above the scalar tells the next reader that the routing "
        "authority is derived from `construct`; that reader will route on the "
        "scalar, and the scalar names one of three"
    )


# --------------------------------------------------------------------------
# 5. Casting: the same two voices in the same two slots as the siblings.
# --------------------------------------------------------------------------

GEMINI = "nto.gemini-live-2.5-flash"
GPT = "gpt-realtime-2.1"


@pytest.mark.parametrize("model", [GEMINI, GPT])
@pytest.mark.parametrize("sibling", SIBLINGS)
def test_s1c_is_cast_like_its_siblings_on_both_families(monkeypatch, model, sibling):
    """A participant assigned C must be argued with by the forms' shared cast.

    This was corrected deliberately once — both of S1B's roles were originally
    pitched above their S1A counterparts, so the one voice a participant is
    publicly pushed by was the one voice that differed between the forms they
    might be assigned. A third form is a third chance to lose it, in cast
    order, which is why the comparison is a list and not a set."""
    monkeypatch.setattr(rt_mod, "MODEL", model)
    from server.scenarios import load_scenario

    def voices(sid):
        scenario = load_scenario(sid, "p_test")
        runner = RealtimeVoiceSessionRunner.__new__(RealtimeVoiceSessionRunner)
        return [getattr(a, "realtime_voice", None) or a.voice_id for a in scenario.cast]

    assert voices("S1C") == voices(sibling), (
        f"on {model} S1C is cast {voices('S1C')} and {sibling} {voices(sibling)}"
    )


def test_both_families_are_named_and_named_differently(spec):
    # The casting COLUMNS, not the row names: the native-audio row shares
    # the gemini-live roster and therefore its column
    # (realtime.casting_families). Adding it did not add a column.
    rosters = {caps.casting_key: set(caps.voices)
               for caps in rt_mod.REALTIME_FAMILIES.values()}
    for aid, a in spec["agents"].items():
        mapping = a["realtime_voice"]
        assert set(mapping) == set(rosters), f"{aid} is cast for {sorted(mapping)}"
        for family, voice in mapping.items():
            assert voice in rosters[family], f"{aid}: {voice} is not on {family}"
        assert mapping["gemini-live"] != mapping["gpt-realtime"], (
            f"{aid}: the rosters are disjoint, so one name in both columns is "
            "a wish rather than a measurement"
        )
        assert a["voice"] == mapping["gemini-live"], (
            f"{aid}: the v1 fallback field must not disagree with the map"
        )


# --------------------------------------------------------------------------
# 6. The beats are in the brief, because on the configured model the cue is not
#    read at all.
# --------------------------------------------------------------------------

def test_the_needle_precondition_is_an_event_in_the_pushers_brief(spec):
    """Written as a STATE it does not fire, and that was measured on both
    siblings before it was rewritten.

    "If they hesitate or go vague" made the actor re-decide every turn whether
    the state obtained; S1B's Mel decided it never did and the beat — the only
    carrier of fester_r at t1 — landed 0/4 against S1A's 3/4. It is now the
    FIRST time a named, observable thing happens, and it claims the whole of
    the next turn. The exclusion matters as much: driven against the older
    briefs, a participant who gave the high-scoring answer was needled anyway
    in 3 of 3 and 4 of 4 runs, firing the beat at the answer it exists to
    contrast with."""
    low = _flat(spec["agents"]["nadia"]["system_prompt"])
    assert "the first time they duck" in low, "the precondition is not an event"
    assert "your very next turn" in low, "the event does not claim a turn"
    assert "once in the whole conversation" in low, "nothing spends the needle"
    assert "a straight answer is not ducking" in low, (
        "nothing says what ducking is NOT, so the needle fires at the "
        "high-scoring answer"
    )


def test_the_concession_gate_is_in_the_counterparts_brief_and_needs_a_defence_first(spec):
    """t3 carries de_escalate and talk_openly; t4 carries de_escalate and
    resolve_not_fester. A counterpart who concedes before defending hands the
    participant credit for de-escalating something that folded on its own, and
    leaves no defence in the record to have been de-escalated. Measured on this
    form during authoring: with the gate written without the defence clause the
    concession landed on actor turn 2.4 against the siblings' 4.2 and 4.0, and
    t3 went undelivered in 2 of 5 encounters."""
    brief = _flat(spec["agents"]["wes"]["system_prompt"])
    assert "you give ground once" in brief
    assert "hold their ground without heat" in brief
    assert "after you have already defended yourself at least once" in brief, (
        "the gate does not require the defence first; the beat then fires "
        "early and t3 goes missing"
    )
    assert "not before the defence, and not twice" in brief
    assert "one-sided" in brief, (
        "the concession has no cheap word attached. A named absence is not a "
        "move: the sibling with a ready-made word conceded 0.90 of the time "
        "and the one with a negation 0.20"
    )


def test_the_counterpart_does_not_raise_the_grievance_himself(spec):
    """What the participant must put on the table is the whole of what t2
    scores. Both siblings are told the same thing about their own half — Sam
    the authorship question, Drew the tone — and each names the shared object
    instead, so the low anchor stays reachable."""
    wes = _flat(spec["agents"]["wes"]["system_prompt"])
    assert "you do not raise" in wes and "tuesday" in wes
    t2 = next(t for _, t in _triggers(spec) if t["id"] == "t2_the_opening")
    assert "do not raise tuesday's meeting yourself" in _flat(t2["cue"])
    assert "do not ask what is wrong" in _flat(t2["cue"])


def test_the_opening_carries_both_halves_a_name_and_an_ask(spec):
    """An opening is a NAME and an ASK, and it is the ask that makes the
    low-scoring anchor reachable at all — that anchor is by construction an
    answer to a request. Measured across the pair, one sibling named and asked
    in 8 of 8 encounters while the other named in 6 of 8 and asked in 0 of 8,
    so one form's t2 was scored against an anchor no participant could reach.
    Both halves belong to the same first turn, in the brief as well as the cue,
    because on the configured model the cue never arrives."""
    wes = _flat(spec["agents"]["wes"]["system_prompt"])
    assert "both of them go in your first turn" in wes
    assert "whether or not they speak first" in wes, "the opening is conditional again"
    t2 = next(t for _, t in _triggers(spec) if t["id"] == "t2_the_opening")
    low = _flat(t2["cue"])
    assert "both halves, in this one turn" in low
    assert "ask them straight out" in low
    assert (t2["scores"]["low"]["answer"] or "").strip().endswith("."), (
        "the low anchor must be something a person says out loud, not a stage "
        "direction; one sibling's was written as one and had to be rewritten"
    )


# --------------------------------------------------------------------------
# 7. No recitable lines, no help-desk register.
# --------------------------------------------------------------------------

def test_no_cue_and_no_opening_quotes_a_line(spec):
    """Recitation across this bank fell from 24% of turns to 4% when beats were
    respecified as INTENT and PRESSURE. A quoted line in a brief is a line the
    actor recites; a described move is a move the actor makes. Fragments are no
    safer than sentences — one character handed a quoted opener produced it in
    16 of 25 turns."""
    for inter, trig in _triggers(spec):
        for field in ("cue",):
            assert '"' not in trig[field], f"{trig['id']}: quoted text in {field}"
        assert '"' not in (inter.get("opening") or "")


def test_the_only_quoted_strings_in_a_brief_are_the_phrases_it_bans(spec):
    """One deliberate exception: the help-desk ban has to name the strings as
    well as the move, because banning the move alone leaves the actor the
    strings and banning the strings alone leaves the actor the move — measured
    both ways on the siblings."""
    banned = {'"i hear you"', '"i appreciate"', '"fair point"',
              '"that\'s a fair point"'}
    for aid, a in spec["agents"].items():
        quoted = re.findall(r'"[^"]+"', " ".join(a["system_prompt"].split()))
        leftover = [q for q in quoted if q.lower() not in banned]
        assert not leftover, f"{aid} hands the actor lines to recite: {leftover}"


def test_every_brief_bans_the_help_desk_move_and_not_only_its_strings(spec):
    """It migrated once already: banned by name in S2's managers and nowhere
    else, it moved to the characters that had no ban while the banned phrases
    went to zero. A ban on a string leaves the MOVE available and the actor
    makes the move in other words."""
    for aid, a in spec["agents"].items():
        brief = _flat(a["system_prompt"])
        assert "should sound like a help desk" in brief, aid
        assert "politer rewording of the same move" in brief, aid
        assert "acknowledging that you have heard them" in brief, aid


def test_no_brief_narrates_itself_in_the_third_person(spec):
    """The assembly tells every actor never to refer to themselves in the third
    person; a brief that does it anyway is an instruction fighting an
    instruction."""
    for aid, a in spec["agents"].items():
        name = a["name"]
        body = a["system_prompt"].split("\n", 1)[1]
        assert f"{name} " not in body, f"{aid} refers to itself by name"


# --------------------------------------------------------------------------
# 8. Probes: present, distinct, and this form's own words.
# --------------------------------------------------------------------------

def test_every_beat_carries_a_probe_that_is_not_the_cue(spec):
    """Nothing in a 1:1 speaks first, so a participant who freezes stalls the
    encounter: the watchdog probes only where the next unfired trigger has an
    on_silence, and _maybe_advance will not move the scene past an unfired
    beat. A beat without one is dead air, a silent WAV and an empty
    transcript."""
    for _, trig in _triggers(spec):
        probe = (trig.get("on_silence") or "").strip()
        assert probe, f"{trig['id']}: no probe"
        assert probe != trig["cue"].strip(), f"{trig['id']}: probe repeats the cue"


def test_the_probes_are_this_forms_own_words():
    """A probe copied from a sibling is the one place a participant could hear
    the other form's encounter. It is also the only fixed line in the file, so
    it is the line most likely to be pasted."""
    mine = {t["on_silence"].strip().lower()
            for _, t in _triggers(v3.load_spec("S1C"))}
    for sibling in SIBLINGS:
        theirs = {t["on_silence"].strip().lower()
                  for _, t in _triggers(v3.load_spec(sibling))}
        assert not (mine & theirs), f"probe shared with {sibling}: {mine & theirs}"


def test_the_probe_reaches_the_actor_verbatim_through_the_wrapper():
    """The probe IS the measurement; the wrapper may be rephrased, it may not."""
    import types

    scenario = v3.compile_scenario("S1C")
    by_id = {a.id: a for a in scenario.cast}
    for inter in scenario.interactions:
        agent = by_id[inter["agent"]]
        for trig in inter["triggers"]:
            runner = types.SimpleNamespace(agent=agent)
            probe = RealtimeVoiceSessionRunner._trigger_instruction(
                runner, trig, probing=True)
            assert trig["on_silence"] in probe, trig["id"]
            cue = RealtimeVoiceSessionRunner._trigger_instruction(
                runner, trig, probing=False)
            assert trig["cue"] in cue, trig["id"]
