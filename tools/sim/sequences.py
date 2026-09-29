"""What the simulated participant does in each scenario, as a list of steps.

The four defaults are the sequences the 2026-09-24 verification runs used
against gpt-realtime-2.1 and the native-audio route (S2A and S4A from the gpt
runs, S1A and S3A from the native ones), with two changes forced by the
stimulus being committed: the recorded clips those runs used (a hesitant long
S1A turn, a short S2A line) are replaced by synthetic lines with the same words
(s1a_long, s2a_race), and a 25 s recorded S4A clip whose content was never
transcribed is dropped from the S4A cycle.

Each covers what went wrong on the live service: long and hesitant turns and a
silence past the S1A timebox; the full 1:1 negotiation to the encounter's own
end in S2A; talking over a reply (bargeplay), pausing mid-sentence (resume) and
a quick two-line burst in both group rooms.

Every sequence begins with the participant speaking. Since pipeline
2026-09-28b nobody else speaks first, in 1:1 or in a room: the room tone that
used to lead S3A and S4A was the lead's time to open the scene.

Steps, comma-separated, each `kind:argument`:

  tone:N          N s of room tone (tools/sim/stim.py)
  zeros:N         N s of digital silence
  until:T         room tone until T s after the session started
  say:F           say line F, then wait for the reply to finish playing
  bargeplay:F+G:D say F; D s after a reply that started after F is PLAYING,
                  talk over it with G
  resume:F+G:D    say F, D s of quiet, then G (one sentence with a pause)
  burst:F         say F and 1.2 s of quiet, without waiting for a reply
  cycle:F1|F2|..:MAX  say each in turn (14 s of tone after every fourth, for
                  the silence probes) until the encounter completes or MAX s
                  after the session started

F is a line name from tools/sim/stim/lines.txt.
"""
from __future__ import annotations

from typing import Dict, List, Tuple

DEFAULT_SEQUENCES: Dict[str, str] = {
    # Riley (a colleague) sets the scene, then Sam (the peer who took the
    # credit); i1 has a 120 s timebox, and the until: runs room tone past the
    # end so the last stretch is the server's clock, not the participant's.
    "S1A": ("say:s6,say:s2,"
            "cycle:s1a_long|s5|s2|s1a_long|s5|s2|s1a_long:124,"
            "say:s3,say:s5,tone:14,say:s3,until:215"),
    # The raise conversation, cycled to the encounter's own end (the floor and
    # the ceiling are the server's; 720 s is only the sim's give-up point).
    # Since pipeline 2026-09-28a (#34) nothing ends the encounter before the
    # 12:00 ceiling unless the participant moves on, which this sequence never
    # does, so the run lasts the full twelve minutes and the server's ceiling
    # (encounter_complete, reason ceiling) lands at the give-up mark: a line
    # started just before 720 s may be cut off by it. The S2A baseline was
    # recorded on 24c, when the floor ended it at about 451 s.
    "S2A": ("say:a1,tone:20,say:a2,say:a3,"
            "cycle:s1|a4|a5|a6|s2a_race|a7|a3|a2|a8:720,tone:3"),
    # A team meeting: talk over Alex's long answer, then pause mid-sentence.
    "S3A": ("say:c1,say:c2,bargeplay:c9+c7:2.0,say:c3,tone:14,"
            "say:c4,resume:c5+c6:1.0,say:c8,tone:3"),
    # The planning meeting, where chiming in is hardest (#24): barge, a paused
    # sentence and a burst twice over, then cycled to the end.
    "S4A": ("say:s4,bargeplay:b2+b3:2.0,resume:b4+b5:1.0,burst:b6,say:b7,"
            "tone:14,say:b8,bargeplay:b2+b3:2.0,resume:b4+b5:0.95,burst:b6,say:b7,"
            "cycle:b9|b10|b11|s4|b8|b2|b12:780,tone:3"),
}

KINDS = {"tone", "zeros", "until", "say", "bargeplay", "resume", "burst", "cycle"}


class StepError(ValueError):
    pass


def parse(steps: str) -> List[Tuple[str, str]]:
    """[(kind, argument), ...], or StepError naming the step that is wrong."""
    out = []
    for raw in steps.split(","):
        raw = raw.strip()
        if not raw:
            continue
        kind, sep, arg = raw.partition(":")
        if not sep or kind not in KINDS:
            raise StepError(f"bad step {raw!r}: expected one of {sorted(KINDS)} as kind:argument")
        out.append((kind, arg))
    return out


def lines_used(steps: str) -> List[str]:
    """Every stimulus line a sequence can say, in order of first use."""
    names: List[str] = []

    def add(n):
        if n not in names:
            names.append(n)

    for kind, arg in parse(steps):
        if kind in ("say", "burst"):
            add(arg)
        elif kind in ("bargeplay", "resume"):
            pair = arg.rsplit(":", 1)[0]
            for n in pair.split("+"):
                add(n)
        elif kind == "cycle":
            for n in arg.rsplit(":", 1)[0].split("|"):
                add(n)
    return names
