"""Casting survives the choice of realtime model, or it is not casting.

Every character in scenarios/v3 has a measured voice, and for four rounds that
measurement was written down twice: once as `realtime_voice: Fenrir`, which the
runner reads, and once as a trailing YAML comment "on gpt-realtime: verse
(~120 Hz)", which nothing read at all. The rosters are disjoint, so on a gpt
model the first of those is a name the family refuses: the runner fell back to
the character's POSITION in the gpt roster and S4's room opened on
alloy/ash/ballad — confirmed live, three realtime_voice_unusable events an
encounter, each offered the gpt roster in order.

The fix is to make the second half machine-readable: `realtime_voice` is a map
from family to voice, server/scenarios_v3.py resolves it against the family
REALTIME_MODEL names, and the answer lands on Agent.realtime_voice, the first
field realtime_voice_session._voice_of asks for.

What these tests hold, in order of what would hurt most if it broke:

  1. On gpt the bank opens on its MEASURED voices, not on roster order, and the
     encounter records no unusable voice.
  2. On Gemini nothing moved. Four rounds of casting still sound the same.
  3. Every A/B pair is cast identically on gpt, as it already is on Gemini. A
     participant on form B is argued with by the same voices as one on form A;
     that was corrected deliberately once and must not be lost on the other
     family.
  4. Every voice named anywhere in the bank is on its family's roster — the
     rosters in REALTIME_FAMILIES, each name re-confirmed on the Cornell
     gateway on 2026-09-12 (session.updated acked, audio returned).
  5. A voice that is NOT on the roster stops the scenario compiling, loudly,
     instead of being quietly replaced by whatever sits at that cast position.

No network and no credentials here. The live evidence is quoted, not re-run.

REALTIME_MODEL is never written. Every test that needs a model patches the
bridge module's MODEL the way the runner's own tests do, which is the reason
the runner reads it at call time.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from server import realtime_voice_session as rvs  # noqa: E402
from server import scenarios_v3 as v3  # noqa: E402
from server.scenarios import load_scenario  # noqa: E402
from server.voice import realtime as rt_mod  # noqa: E402

GEMINI = "nto.gemini-live-2.5-flash"
GPT = "gpt-realtime-2.1"

# What the runner used to produce on gpt: the first three entries of that
# family's roster, handed out by cast position. Kept as a literal so this file
# fails if the bug comes back in a form that merely looks different.
ROSTER_ORDER_ON_GPT = ["alloy", "ash", "ballad"]

# The casting, as the specs record it. Second column is the Gemini name the
# bank has used for four rounds; third is the gpt voice measured against it.
CASTING = {
    "S1A": [("riley", "Fenrir", "verse"), ("sam", "Charon", "ash")],
    "S1B": [("mel", "Fenrir", "verse"), ("drew", "Charon", "ash")],
    "S2A": [("morgan", "Kore", "coral")],
    "S2B": [("sasha", "Kore", "coral")],
    "S3A": [("alex", "Fenrir", "verse"), ("jordan", "Charon", "ash"),
            ("casey", "Leda", "marin")],
    "S3B": [("toni", "Fenrir", "verse"), ("lee", "Charon", "ash"),
            ("ari", "Leda", "marin")],
    "S4A": [("dan", "Fenrir", "verse"), ("priya", "Aoede", "coral"),
            ("chris", "Charon", "ash")],
    "S4B": [("dan", "Fenrir", "verse"), ("priya", "Aoede", "coral"),
            ("chris", "Charon", "ash")],
}


@pytest.fixture
def on_model(monkeypatch):
    """Run as though REALTIME_MODEL named this model, without changing it."""
    def _set(model):
        monkeypatch.setattr(rt_mod, "MODEL", model)
        return model
    return _set


# --- the smallest session the runner's voice resolution needs ---------------

class FakeStore:
    def __init__(self):
        self.events = []
        self.started_at = 0.0

    def event(self, type_, **fields):
        self.events.append(dict(type=type_, **fields))

    def of(self, type_):
        return [e for e in self.events if e["type"] == type_]


class FakeEngine:
    def __init__(self, agent):
        self.agent = agent

    def _system_prompt(self, branches, note, group=False):
        return f"SYSTEM PROMPT for {self.agent.id}"


class FakeSession:
    def __init__(self, scenario_id):
        self.scenario = load_scenario(scenario_id, "p_test")
        self.is_group = self.scenario.mode == "group"
        self.engines = {a.id: FakeEngine(a) for a in self.scenario.cast}
        self.store = FakeStore()
        self.director = None
        self.triggered_branches = []
        self.shared_history = []
        self.steering_log = []
        self.broadcasts = []

    def append_agent(self, agent_id, text):
        self.shared_history.append({"speaker": agent_id, "text": text})

    async def broadcast(self, payload):
        self.broadcasts.append(payload)


class FakeWS:
    def __init__(self):
        self.json = []

    async def send_json(self, payload):
        self.json.append(payload)

    async def send_bytes(self, payload):
        pass


def make_runner(scenario_id):
    """A runner over a freshly compiled scenario.

    Compiled AFTER the model is patched, deliberately: resolving a cast is the
    one thing that has to know which family it is for, and a scenario compiled
    for the other one is the defect this file is about.
    """
    session = FakeSession(scenario_id)
    return rvs.RealtimeVoiceSessionRunner(session, FakeWS()), session


# --------------------------------------------------------------------------
# 1. The defect: on gpt the bank must open on its measured voices.
# --------------------------------------------------------------------------

@pytest.mark.parametrize("scenario_id", sorted(CASTING))
def test_gpt_opens_the_measured_voice_for_every_character(on_model, scenario_id):
    on_model(GPT)
    runner, _ = make_runner(scenario_id)
    want = [gpt for _, _, gpt in CASTING[scenario_id]]
    got = [runner._voice_for(a) for a in runner.cast]
    assert got == want, (
        f"{scenario_id} opens on {got} instead of the measured {want}"
    )


def test_the_group_room_is_not_cast_by_roster_position(on_model):
    """The failure exactly as it was seen live, named so it cannot come back.

    S4A's three characters were measured at Charon ~100 Hz, Fenrir ~115 Hz and
    Aoede ~166 Hz, three separable people in one room. On gpt they arrived as
    the first three entries of that family's roster in cast order, which is a
    different room by accident."""
    on_model(GPT)
    runner, _ = make_runner("S4A")
    got = [runner._voice_for(a) for a in runner.cast]
    assert got != ROSTER_ORDER_ON_GPT
    assert got == ["verse", "coral", "ash"]
    assert len(set(got)) == 3, f"the room shares voices: {got}"


@pytest.mark.parametrize("scenario_id", sorted(CASTING))
def test_nothing_in_the_bank_is_unusable_on_gpt(on_model, scenario_id):
    """The record is the proof, because the record is where this was visible.

    Every fallback the runner makes writes a realtime_voice_unusable row. A
    bank that is cast for the family it runs on produces none."""
    on_model(GPT)
    runner, session = make_runner(scenario_id)
    for agent in runner.cast:
        runner._voice_for(agent)
    assert session.store.of("realtime_voice_unusable") == []


# --------------------------------------------------------------------------
# 2. And Gemini is untouched.
# --------------------------------------------------------------------------

@pytest.mark.parametrize("scenario_id", sorted(CASTING))
def test_gemini_casting_is_exactly_what_it_was(on_model, scenario_id):
    on_model(GEMINI)
    runner, session = make_runner(scenario_id)
    want = [gem for _, gem, _ in CASTING[scenario_id]]
    assert [runner._voice_for(a) for a in runner.cast] == want
    assert session.store.of("realtime_voice_unusable") == []


def test_the_unqualified_field_still_carries_the_gemini_name():
    """Agent.voice_id is the v1 cascade's field and other tests pin it.

    It is now only a fallback, but it is read by anything that has not been
    taught about families, so it must not start changing under the model."""
    for model in (GEMINI, GPT):
        scenario = load_scenario("S1A", "p_test")
        assert [a.voice_id for a in scenario.cast] == ["Fenrir", "Charon"], (
            f"voice_id moved while compiling for {model}"
        )


# --------------------------------------------------------------------------
# 3. The A/B pairs stay matched on the other family too.
# --------------------------------------------------------------------------

PAIRS = [("S1A", "S1B"), ("S2A", "S2B"), ("S3A", "S3B"), ("S4A", "S4B")]


@pytest.mark.parametrize("model", [GEMINI, GPT])
@pytest.mark.parametrize("a_id,b_id", PAIRS)
def test_each_ab_pair_is_cast_identically(on_model, model, a_id, b_id):
    """A participant on form B must be argued with by form A's voices.

    This was corrected deliberately once: S3B's challenger was 110 Hz off
    S3A's, so the one voice a participant is publicly challenged by was the one
    voice that differed between the two forms they might be assigned. Adding a
    second family is a second chance to lose it."""
    on_model(model)
    a_runner, _ = make_runner(a_id)
    b_runner, _ = make_runner(b_id)
    a_voices = [a_runner._voice_for(x) for x in a_runner.cast]
    b_voices = [b_runner._voice_for(x) for x in b_runner.cast]
    assert a_voices == b_voices, (
        f"on {model} {a_id} is cast {a_voices} and {b_id} {b_voices}; the two "
        "forms are scored against each other and must sound the same"
    )


# --------------------------------------------------------------------------
# 4. Every name is on a real roster, for every family, not just the one running.
# --------------------------------------------------------------------------

def test_the_whole_bank_is_castable_on_both_families():
    """One call, because a typo in the family that is NOT running is invisible
    until the PI switches model, which is the worst moment to find it."""
    assert v3.voice_casting_problems() == []


def test_every_named_voice_is_on_its_family_roster():
    """Held against REALTIME_FAMILIES by name rather than by the loader, so it
    still fails if the loader is taught to be forgiving.

    The gpt names were re-confirmed on the gateway on 2026-09-12: one short
    socket each for verse, coral, ash and marin, session.updated acked in
    333-1330 ms and audio returned on every one."""
    rosters = {name: set(caps.voices)
               for name, caps in rt_mod.REALTIME_FAMILIES.items()}
    for sid in v3.available():
        spec = v3.load_spec(sid)
        for aid, a in spec["agents"].items():
            mapping = a.get("realtime_voice")
            assert isinstance(mapping, dict), (
                f"{sid}/{aid} keeps its casting as {type(mapping).__name__}; "
                "a bare name is a name for one family and a silent recast on "
                "the other, which is the defect this field replaced"
            )
            assert set(mapping) == set(rosters), (
                f"{sid}/{aid} is cast for {sorted(mapping)}, and the table "
                f"covers {sorted(rosters)}"
            )
            for family, voice in mapping.items():
                assert voice in rosters[family], (
                    f"{sid}/{aid} is cast in {voice!r} on {family}, which is "
                    "not on that family's roster"
                )


def test_the_two_families_are_cast_with_different_names():
    """A guard against the map being filled in by copying one column.

    The rosters are disjoint, so a family's voice appearing in the other
    family's row means somebody wrote down a wish rather than a measurement."""
    for sid in v3.available():
        for aid, a in v3.load_spec(sid)["agents"].items():
            m = a["realtime_voice"]
            assert m["gemini-live"] != m["gpt-realtime"], f"{sid}/{aid}"


# --------------------------------------------------------------------------
# 5. An unknown voice fails where somebody can see it.
# --------------------------------------------------------------------------

def _doctored(monkeypatch, scenario_id, agent_id, mapping):
    """Serve one spec with one character's casting replaced."""
    real = v3.load_spec

    def fake(sid):
        spec = real(sid)
        if sid == scenario_id:
            spec["agents"][agent_id]["realtime_voice"] = mapping
        return spec

    monkeypatch.setattr(v3, "load_spec", fake)


def test_an_off_roster_voice_refuses_to_compile(on_model, monkeypatch):
    """Not a substitution, and not a warning nobody reads.

    A voice the family refuses costs the session its whole session.update, and
    with it the character brief: the actor then answers the participant as the
    gateway's stock assistant. There is no version of continuing that is worth
    more than stopping."""
    on_model(GPT)
    _doctored(monkeypatch, "S1A", "riley",
              {"gemini-live": "Fenrir", "gpt-realtime": "Fenrir"})
    with pytest.raises(v3.UnknownScenarioVoice) as exc:
        v3.compile_scenario("S1A", "p_test")
    said = str(exc.value)
    assert "S1A/riley" in said and "Fenrir" in said
    assert "verse" in said, "the message must name what the family does accept"


def test_the_refusal_reaches_the_log_as_well(on_model, monkeypatch, caplog):
    """list_scenarios swallows every compile exception by design, so the log
    line is the only thing an operator sees for a scenario that has quietly
    vanished from the listing."""
    on_model(GPT)
    _doctored(monkeypatch, "S1A", "riley",
              {"gemini-live": "Fenrir", "gpt-realtime": "Puck"})
    with caplog.at_level("ERROR", logger="server.scenarios_v3"):
        with pytest.raises(v3.UnknownScenarioVoice):
            v3.compile_scenario("S1A", "p_test")
    assert any("S1A/riley" in r.getMessage() for r in caplog.records)


def test_a_scenario_that_cannot_be_cast_leaves_the_listing(on_model,
                                                           monkeypatch):
    """The house rule for a spec that cannot be compiled, applied to this one:
    a participant is never offered a scenario that would fail when they picked
    it."""
    from server import scenarios as sc

    on_model(GPT)
    monkeypatch.setattr(sc, "_list_cache", None)
    before = {r["id"] for r in sc.list_scenarios()}
    assert "S1A" in before
    _doctored(monkeypatch, "S1A", "riley",
              {"gemini-live": "Fenrir", "gpt-realtime": "Kore"})
    monkeypatch.setattr(sc, "_list_cache", None)
    assert "S1A" not in {r["id"] for r in sc.list_scenarios()}


def test_a_character_cast_for_no_family_falls_back_and_says_so(on_model,
                                                              monkeypatch,
                                                              caplog):
    """The one case that is NOT fatal: a map with no row for this family.

    The character can still be cast by position — that is what an uncast
    character has always done, and it costs a measurement rather than the
    brief. It still may not be silent."""
    on_model(GPT)
    _doctored(monkeypatch, "S1A", "riley", {"gemini-live": "Fenrir"})
    with caplog.at_level("ERROR", logger="server.scenarios_v3"):
        scenario = v3.compile_scenario("S1A", "p_test")
    assert not getattr(scenario.cast[0], "realtime_voice", "")
    assert any("gpt-realtime" in r.getMessage() for r in caplog.records)


def test_a_model_outside_the_table_is_cast_by_nobody_here(on_model):
    """An unknown model gets no voice from this module at all.

    connect() refuses such a model by name a moment later; inventing a voice
    for it in the meantime is the one thing that could turn that loud refusal
    into a session that runs on a persona nobody chose."""
    on_model("some-model-nobody-has-a-row-for")
    scenario = v3.compile_scenario("S1A", "p_test")
    assert [getattr(a, "realtime_voice", "") for a in scenario.cast] == ["", ""]
