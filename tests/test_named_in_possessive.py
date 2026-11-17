"""#297/#299: a possessive names someone without addressing them."""
from types import SimpleNamespace

import pytest

from server.realtime_voice_session import RealtimeVoiceSessionRunner as R

AGENTS = [SimpleNamespace(id="priya", name="Priya"), SimpleNamespace(id="chris", name="Chris"),
          SimpleNamespace(id="dan", name="Dan"), SimpleNamespace(id="casey", name="Casey")]


def named(text):
    fake = SimpleNamespace(_resolve_agents=lambda: AGENTS)
    return R._named_in(fake, text)


@pytest.mark.parametrize("text,who", [
    ("Priya, what do you think about the timeline for Chris's team?", "priya"),
    ("Priya, what about Chris’s team", "priya"),            # curly apostrophe
    ("What did you make of Casey's numbers, Dan?", "dan"),
    ("Sorry Priya, but Chris, how are you?", "chris"),      # last address still wins
    ("Chris, so what's the plan", "chris"),                 # "so" is not a possessive
    ("I liked Casey's point", None),
    ("Dan", "dan"),
    ("abundant caution", None),
])
def test_possessives_do_not_take_the_floor(text, who):
    assert named(text) == who
