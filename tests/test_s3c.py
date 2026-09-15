"""S3C is the third form of inspirational leadership, and it has to be the
same instrument as the other two.

The bank-wide tests already hold what is common to every spec: that a beat has
a silence probe (test_scenario_probes), that a probe is not a described move
(test_on_silence_probe), that a cast is on its family's roster
(test_voice_casting). What none of them hold is the thing that makes a THIRD
form legitimate rather than merely present: that a participant scored on S3C is
scored on the same skeleton, by the same voices, at the same beats, as one
scored on S3A or S3B.

So every assertion below is written against BOTH siblings at once rather than
against a literal copied out of one of them. A test that says "S3C has three
agents" goes stale the day somebody changes the skeleton; a test that says "S3C
has as many agents as S3A and as S3B" fails on the day the three stop matching,
which is the only day anybody needs to hear about it.

Three of these hold S3C to something its siblings are NOT held to, and each one
says so where it is written. They are places where four rounds of measured work
on S3A and S3B produced a rule that the siblings then only partly kept (quoted
fragments in a brief), and holding the new form to the rule fully costs nothing
a participant can feel — which is a claim this round measured on the gateway
rather than asserted here. The offline test is the ratchet; the behaviour is in
the spec's own header.

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

SID = "S3C"
SIBLINGS = ("S3A", "S3B")
SPEC_PATH = ROOT / "scenarios" / "v3" / "S3C_mandated_system.yaml"

GEMINI = "nto.gemini-live-2.5-flash"
GPT = "gpt-realtime-2.1"


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
    """Through available(), not off the disk: a spec missing one of
    _REQUIRED_KEYS is skipped with a log line and nothing else, so a spec that
    exists and a spec that loads are two different facts."""
    assert SID in v3.available(), (
        f"{SID} is not in the v3 index; scenarios_v3 skips a spec that is "
        "missing id/agents/construct/variant/title and only logs it"
    )


def test_it_is_the_third_form_of_the_same_construct():
    s = spec()
    assert s["construct"] == v3.load_spec("S3A")["construct"]
    assert s["variant"] == "C"
    assert s["id"] == SID


def test_the_title_is_not_a_near_miss_of_s4as():
    """tests/test_demo_honesty counts titles equal to S4A's exactly.

    A near-miss title ("Planning a system rollout") leaves that test passing
    while making it meaningless, which is worse than breaking it. The word
    itself is barred here so the next edit to this title cannot reintroduce the
    collision by accident."""
    title = spec()["title"]
    s4a = v3.load_spec("S4A")["title"]
    assert title != s4a
    assert "rollout" not in title.lower()
    overlap = set(title.lower().split()) & set(s4a.lower().split())
    assert overlap <= {"a", "an", "the"}, f"{title!r} reads like {s4a!r}"


def test_parallel_form_is_written_down_as_provenance_and_not_as_routing():
    """The authored scalar names ONE sibling and there are now two.

    With three forms the field cannot name them all, and a field that names one
    of three is a field the next reader will take as naming the only one. It
    stays (tools/gen_scenario_map.py subscripts it, not .get) and it stays a
    scalar (tests/test_rater_packet forbids the leak by this literal name), but
    the authority is derived from `construct`. That has to be legible in the
    file itself, not only in a design note nobody ships."""
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
    map is what makes a score from S3C comparable to a score from S3A."""
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
    assert mine["duration_minutes"] == theirs["duration_minutes"]
    assert len(mine["agents"]) == len(theirs["agents"])


@pytest.mark.parametrize("sib", SIBLINGS)
def test_the_pre_reading_is_the_same_length_as_the_siblings(sib):
    """Two lines in both siblings, one for the performer and one for the
    junior. A third line is context the other two forms' participants did not
    get, which is a difficulty difference nobody chose."""
    assert len(spec()["pre_reading"]) == len(spec(sib)["pre_reading"]) == 2


def test_the_arms_this_form_joins_are_the_arms_its_siblings_join():
    """A construct joins an arm only when EVERY form of it qualifies
    (server/runs.ARMS), so one interaction in the wrong mode does not just
    mis-file this form — it takes inspirational leadership out of the group arm
    for everybody."""
    modes = runs._interaction_modes(SID)
    assert runs._has_group(modes), "i1 is not a group room"
    assert not runs._all_one_to_one(modes)
    for sib in SIBLINGS:
        sib_modes = runs._interaction_modes(sib)
        assert runs._has_group(modes) == runs._has_group(sib_modes)
        assert runs._all_one_to_one(modes) == runs._all_one_to_one(sib_modes)


def test_the_shared_beat_id_is_shared_and_the_private_ones_are_private():
    """t1_public_challenge is the same beat in all three forms and keeps the
    same id. t2/t3 are per-form in the siblings already (S3A names Jordan and
    Casey in its ids, S3B names its own), and a third form reusing one of those
    ids would put two different beats behind one name in the evidence trace."""
    ids = [t["id"] for _, t in triggers()]
    assert ids[0] == "t1_public_challenge"
    for sib in SIBLINGS:
        sib_ids = [t["id"] for _, t in triggers(sib)]
        assert sib_ids[0] == ids[0]
        assert set(ids[1:]).isdisjoint(sib_ids[1:]), (
            f"{SID} reuses one of {sib}'s per-form beat ids")


def test_every_beat_is_bound_to_a_named_character():
    """Without an explicit `agent:` the binding is inferred from a cue that
    opens by naming somebody, and the cues here deliberately do not. An unbound
    beat drifts to whoever the director hands the floor to first."""
    cast = set(spec()["agents"])
    for iid, trig in triggers():
        assert trig.get("agent") in cast, f"{iid} {trig['id']} is unbound"


# --------------------------------------------------------------------------
# 3. Casting. The same room, by ear, as the other two forms.
# --------------------------------------------------------------------------

@pytest.mark.parametrize("sib", SIBLINGS)
def test_the_cast_is_voiced_exactly_as_both_siblings_are(sib):
    """In cast order, which is why the `agents:` mapping order is load-bearing.

    A participant meets ONE of the three forms. The voice that publicly
    challenges them in front of their team must not be a different voice
    depending on which form they drew — that was corrected deliberately once
    between S3A and S3B and a third form is a third chance to lose it."""
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


def test_the_legacy_scalar_still_carries_the_gemini_name():
    """Agent.voice_id is the retired v1 cascade's field and is the fallback for
    anything that has not been taught about families; all eight shipped specs
    keep the Gemini name in it and this one must not be the exception."""
    for aid, a in spec()["agents"].items():
        assert a["voice"] == a["realtime_voice"]["gemini-live"], aid


# --------------------------------------------------------------------------
# 4. What the actor is handed. Tighter here than in the siblings, on purpose.
# --------------------------------------------------------------------------

def test_no_brief_or_cue_hands_the_actor_a_quoted_fragment():
    """S3A's header established the rule and the pair kept it only for beats.

    Measured on the gateway, a quoted beat line in a brief came back to the
    word, and it came back on the WRONG STAGE — Jordan's one-on-one line spoken
    in the meeting — because a brief is carried on every turn of the encounter
    and the actor reaches for whatever unit it was handed the first time it
    nearly fits. Both siblings still carry quoted fragments of a different kind
    (words the character prefers, phrases the PARTICIPANT might use that do not
    land), which is defensible and is not what was measured to fail.

    This form carries none at all, which is strictly the safer side of a rule
    whose looser version has already cost this pair a beat. It is a tighter rule
    than the siblings keep, so the round that wrote it drove all three forms
    with identical scripts to show the difference is not behavioural; that
    evidence is in the spec's own header, not here."""
    for text in authored_text():
        for ch in ('"', "“", "”"):
            assert ch not in text, (
                f"quoted fragment in an actor-facing string: "
                f"...{text[max(0, text.find(ch) - 60):text.find(ch) + 60]}...")


HELP_DESK = (
    "i understand", "i appreciate", "i hear you", "fair point", "fair enough",
    "great point", "that's fair", "thats fair", "i'm sorry to hear",
)


def test_no_character_is_written_in_help_desk_register():
    """The stock acknowledgement is the failure mode a voice model falls into
    when a brief does not give it something more specific to do, and a brief
    that contains the phrase guarantees it."""
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
        assert name not in flat(a["system_prompt"]).replace(f"You are {name}", "", 1), (
            f"{aid}'s brief names {name} outside its opening line")


@pytest.mark.parametrize("iid,trig", triggers(), ids=[t["id"] for _, t in triggers()])
def test_the_probe_is_substance_and_cannot_be_spoken_as_it_stands(iid, trig):
    """_trigger_instruction takes on_silence as the OBJECT of "say this now in
    your own words", so a probe authored as a described move becomes an
    instruction to speak the description — stage directions and third-person
    narration about the participant included. A "that ..." complement cannot be
    a sentence, so the actor has to translate it before anything can be said."""
    probe = (trig.get("on_silence") or "").strip()
    assert probe, f"{iid} {trig['id']}: no probe"
    assert probe != (trig.get("cue") or "").strip()
    assert probe.startswith("that "), (
        f"{iid} {trig['id']}: probe is not a 'that ...' complement: {probe!r}")
    for word in ("they have not", "let the pause", "the silence"):
        assert word not in probe.lower(), (
            f"{iid} {trig['id']}: probe carries narration the actor will read out")


@pytest.mark.parametrize("iid,trig", triggers(), ids=[t["id"] for _, t in triggers()])
def test_no_probe_carries_an_instruction_to_the_actor(iid, trig):
    """"BOTH have to be said" is fine in a cue and is exactly what an actor on
    gpt-realtime reads out of a probe."""
    low = (trig.get("on_silence") or "").lower()
    for phrase in ("have to be said", "both halves", "in this turn", "however clumsily"):
        assert phrase not in low, f"{iid} {trig['id']}: {phrase!r} in the probe"


# --------------------------------------------------------------------------
# 5. Every beat is in the OPENING brief, not only in the cue.
# --------------------------------------------------------------------------
# A mid-session session.update is inert on the configured model, so a beat that
# depends on its cue arriving is a beat that does not happen. The cue is the
# push that starts a beat the brief already carries; it is not the beat.

BEAT_IN_BRIEF = {
    # trigger id -> (agent id, substrings that must all be in that brief)
    "t1_public_challenge": ("bex", ("in front of the others", "implement")),
    "t2_stopped_maintaining": ("rafa", ("stopped", "survives the switch")),
    "t3_worthless_and_blamed": ("noor", ("blamed", "worth anything")),
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
            "configured model a beat that only exists in its cue does not happen")


def test_the_mandate_is_an_established_fact_and_not_an_announcement():
    """The group segment's failure mode, named before it happened.

    If the irreversibility is something a character reveals, the participant
    spends the meeting litigating whether it is really fixed and the beat plan
    never runs. All three characters have to arrive already holding it, and the
    room's own opening has to say so."""
    opening = spec()["interactions"][0]["opening"].lower()
    assert "six weeks" in opening
    for marker in ("two levels up", "stopped"):
        assert marker in opening, f"the room's opening does not establish {marker!r}"
    for aid, text in briefs().items():
        assert "six weeks" in flat(text).lower(), (
            f"{aid} does not arrive holding the go-live date")


def test_the_latitude_the_leader_holds_is_written_as_a_trap_and_not_a_gift():
    """What keeps this form as hard as its siblings.

    S3A and S3B are losses already taken and the leader has nothing to offer
    but honesty and advocacy upward. Here the leader holds real local latitude,
    which would make the encounter easier unless the cynic tries to convert it
    into a promise to stall or quietly not comply — so a participant who offers
    latitude without conditions has given the LOW answer, not the high one."""
    bex = flat(briefs()["bex"]).lower()
    assert "slip" in bex or "quietly" in bex or "stall" in bex, (
        "the cynic does not try to convert latitude into non-compliance, so "
        "the one thing that keeps this form from being easier than A and B is "
        "not written down")


def test_the_cynic_and_the_quiet_one_are_bounded_as_their_counterparts_are():
    """Two places a difficulty difference hid between S3A and S3B and had to be
    driven out one at a time: the challenger's turn length, and the quiet
    character's satisfaction condition. A third form is a third chance to
    reintroduce both, so it carries the same two bounds in the same words."""
    for sib, cynic in (("S3A", "alex"), ("S3B", "toni")):
        assert "Two sentences at the outside" in flat(briefs(sib)[cynic])
    assert "Two sentences at the outside" in flat(briefs()["bex"])
    for sib, quiet in (("S3A", "jordan"), ("S3B", "lee")):
        assert "once is the whole of it" in flat(briefs(sib)[quiet])
    assert "once is the whole of it" in flat(briefs()["rafa"]), (
        "the quiet character's one-time ask is unbounded; on the B form an "
        "unbounded ask became a refrain the participant could not close"
    )


@pytest.mark.parametrize("sib", SIBLINGS)
def test_the_one_to_one_characters_do_not_arrive_as_strangers(sib):
    """The runner closes the group room at the i1/i2 boundary and opens a fresh
    gateway session per member, and no history can be replayed across that
    bridge. So the meeting has to be written into the briefs themselves, in all
    three forms, or a participant whose opening move is the good one — you were
    quiet in there — is talking to somebody who was never in the room."""
    for sid in (SID, sib):
        s = spec(sid)
        series = next(i for i in s["interactions"]
                      if i["mode"] == "one_to_one_series")
        for aid in series["agents"]:
            low = flat(s["agents"][aid]["system_prompt"]).lower()
            assert "meeting" in low and (
                "broke up" in low or "finished" in low), (
                f"{sid}/{aid} does not arrive holding the meeting")
            assert "greet" in low or "introduc" in low, (
                f"{sid}/{aid} is not told not to greet somebody they were "
                "speaking to a minute ago")


# --------------------------------------------------------------------------
# 6. It compiles into something the runner can actually drive.
# --------------------------------------------------------------------------

def test_it_compiles_and_the_actor_scene_survives_the_third_person_rewrite():
    """`setup` is written to the participant in the second person and is
    rewritten for the actors by a pronoun pass. A setup the pass cannot read
    leaves broken grammar pasted verbatim into every actor's prompt, in every
    encounter, and only a warning nobody reads."""
    scenario = v3.compile_scenario(SID, "p_test")
    scene = scenario.scene
    assert scene, "no actor scene"
    for bad in (" you ", " your ", "them'"):
        assert bad not in f" {scene.lower()} ", f"mangled actor scene: {scene!r}"
    assert "participant" in scene.lower()


def test_the_group_segment_composes_a_prompt_for_every_character(tmp_path):
    """The i1 brief is what the whole group segment runs on; a character whose
    prompt does not compose is a character who arrives as the gateway's stock
    assistant."""
    session = _fake_session(SID, tmp_path)
    runner = rvs.RealtimeVoiceSessionRunner(session, None)
    runner.segment = 0
    agents = runner._resolve_agents()
    assert {a.id for a in agents} == set(spec()["interactions"][0]["agents"])
    for a in agents:
        runner.agent, runner.agent_id = a, a.id
        text = runner._instructions()
        assert a.name in text
        assert len(text) > 500, f"{a.id}'s composed prompt is suspiciously short"


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
