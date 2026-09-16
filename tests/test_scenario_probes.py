"""Every planted beat in the v3 bank must carry a silence probe.

The runner's silence watchdog probes only where the NEXT unfired trigger has an
`on_silence` line (server/realtime_voice_session.py, both the group branch and
the 1:1 branch bail with `if trigger is None or not trigger.get("on_silence")`),
and `_maybe_advance` refuses to move the scene on while a planted beat is still
unfired. A 1:1 actor never speaks first either: the only two places a turn is
committed in a 1:1 are the participant's own turn and that probe. So a beat with
no `on_silence` is a beat where a participant who freezes produces dead air, a
silent WAV and an empty transcript — missing data exactly where the spec says
"silence is data, not a gap" (docs/scenario-spec-v3.md). Ten beats across six
specs were in that state; nothing checked, which is how they got there.

The assertions run through the real loader rather than reading YAML directly, so
a spec that compiles differently than it reads fails here rather than in front
of a participant. The parallel-form checks are here for the same reason: the
probes were added to six files at once and the two variants of a construct have
to stay interchangeable, beat for beat and item for item.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from server.scenarios_v3 import available, compile_scenario  # noqa: E402


def _triggers(scenario):
    """(interaction id, trigger dict) for every planted beat, in spec order."""
    for inter in scenario.interactions:
        for trig in inter.get("triggers", []):
            yield inter.get("id"), trig


@pytest.mark.parametrize("scenario_id", available())
def test_every_planted_trigger_has_a_silence_probe(scenario_id):
    scenario = compile_scenario(scenario_id)
    missing = [
        f"{scenario_id} {iid} {trig.get('id')}"
        for iid, trig in _triggers(scenario)
        if not (trig.get("on_silence") or "").strip()
    ]
    assert not missing, (
        "planted beats with no on_silence probe — the silence watchdog cannot "
        "probe there and a frozen participant becomes missing data: "
        + ", ".join(missing)
    )


@pytest.mark.parametrize("scenario_id", available())
def test_probe_is_not_a_copy_of_the_cue(scenario_id):
    """The probe replaces the cue for that beat; a verbatim copy is a placeholder.

    _trigger_instruction hands the actor "Probe now, in character, with the
    substance of: <on_silence>" instead of "Bring about this beat now ...
    <cue>", so a probe that is only the cue restated gives the actor a stage
    direction about itself rather than a line that hands the floor back.
    """
    scenario = compile_scenario(scenario_id)
    for iid, trig in _triggers(scenario):
        probe = (trig.get("on_silence") or "").strip()
        cue = (trig.get("cue") or "").strip()
        assert probe != cue, f"{scenario_id} {iid} {trig.get('id')}: probe repeats the cue"


def _pairs():
    seen = set()
    for sid in available():
        sc = compile_scenario(sid)
        other = getattr(sc, "parallel_form", None)
        if not other or other not in available():
            continue
        key = tuple(sorted((sid, other)))
        if key in seen:
            continue
        seen.add(key)
        yield key


@pytest.mark.parametrize("pair", list(_pairs()))
def test_parallel_forms_still_match_beat_for_beat(pair):
    """Attempt 2 serves the parallel form, so the two must stay interchangeable.

    Same number of interactions, same number of triggers in each, and the same
    ESCI items in the same order at the same position — that ordered map is what
    makes a score from S1B comparable to a score from S1A.
    """
    a, b = (compile_scenario(sid) for sid in pair)
    assert len(a.interactions) == len(b.interactions), f"{pair}: interaction count differs"
    for ia, ib in zip(a.interactions, b.interactions):
        ta, tb = ia.get("triggers", []), ib.get("triggers", [])
        assert len(ta) == len(tb), f"{pair} {ia.get('id')}: trigger count differs"
        for x, y in zip(ta, tb):
            assert list(x.get("esci", [])) == list(y.get("esci", [])), (
                f"{pair} {ia.get('id')} {x.get('id')}/{y.get('id')}: ESCI map differs"
            )
    assert a.esci_items.keys() == b.esci_items.keys(), f"{pair}: item bank differs"


@pytest.mark.parametrize("pair", list(_pairs()))
def test_parallel_forms_probe_in_the_same_places(pair):
    """A probe present in one variant and absent in its twin is a hidden
    difficulty difference: the participant who freezes on the A form gets a
    prompt and the one who freezes on the B form gets silence."""
    a, b = (compile_scenario(sid) for sid in pair)
    for ia, ib in zip(a.interactions, b.interactions):
        for x, y in zip(ia.get("triggers", []), ib.get("triggers", [])):
            assert bool(x.get("on_silence")) == bool(y.get("on_silence")), (
                f"{pair} {ia.get('id')} {x.get('id')}/{y.get('id')}: probe coverage differs"
            )
