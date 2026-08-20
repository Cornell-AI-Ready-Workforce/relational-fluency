"""The name and role a participant is given for an encounter.

The IRB document has participants play an assigned character — "You are [NAME],
a [ROLE] at a mid-sized company" — and asks them to use the names the scenario
provides rather than their own. That only works if the app actually issues a
name, so it is assigned here, deterministically from the participant key: the
same person keeps the same identity across all four encounters, which matters
because raters watch four videos of one participant.

Names are chosen to be common, short, easy to say aloud, and distinct from the
agent names in every scenario.
"""

from __future__ import annotations

import hashlib
from typing import Dict

# Deliberately gender-ambiguous, and none collide with agent names
# (Riley, Sam, Mel, Drew, Morgan, Sasha, Alex, Jordan, Casey, Toni, Lee, Ari,
#  Dan, Priya, Chris).
NAMES = [
    "Avery", "Reese", "Quinn", "Rowan", "Emerson", "Finley",
    "Hayden", "Kendall", "Marlowe", "Peyton", "Sloane", "Tatum",
]

ROLES: Dict[str, str] = {
    "conflict_management": "senior analyst",
    "influence": "senior analyst",
    "inspirational_leadership": "interim team lead",
    "teamwork": "project coordinator",
}

ORG = "a mid-sized company"


def assign(participant_key: str, construct: str) -> dict:
    """Stable identity for this participant. Same key, same name every time."""
    seed = hashlib.sha256((participant_key or "anon").encode()).hexdigest()
    name = NAMES[int(seed[:8], 16) % len(NAMES)]
    return {"name": name, "role": ROLES.get(construct, "team member"), "org": ORG}
