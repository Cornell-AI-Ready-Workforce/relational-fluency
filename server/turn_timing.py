"""Where the time goes between a participant stopping and a character starting.

Issue #25. Every latency number this project had was one of two things: the
runner's own `latency_s` on assistant_turn (turn end to the turn being WRITTEN,
which is after the transcript grace and says nothing about sound), or a model
of playback built from byte counts. Neither says when the participant heard the
character, and the gateway delivers a reply about 2.5x faster than it plays, so
"sent" and "heard" differ by most of a turn. This writes the stages down as
they happen, one `turn_timing` event per agent turn, every stage in seconds
from encounter start on the store's own clock (store.started_at, the clock
every event's `t` is on):

    vad_speech_end            the runner's VAD marked the participant's turn end
    commit_sent               the participant's buffer was committed (1:1
                              commit_turn, or the room scribe's commit); null
                              where the gateway started its own reply
    transcript_arrived        the participant transcript for that commit
    director_decided          rooms: the director_route was written
    grant_sent                rooms: this character was given the floor
    first_audio_from_gateway  the first audio delta of this reply reached the
                              bridge (a held room reply: when it was held)
    first_audio_to_client     the first chunk of this turn was sent to the page
    assistant_done            the turn was closed on the server
    first_audio_played        the page says the first chunk started playing
    play_end                  the page says this turn's scheduled audio ended
                              (or was stopped: play_end_interrupted)

The participant-side stages belong to the participant turn the reply answered
and are shared by every reply to it; `reply_index` says which one this is (0 is
the first voice the participant heard after speaking, and the one a latency
figure is about). An opener, a probe or a follow-up before any participant
speech has no participant stages at all.

The page's two stages arrive as acks (`playback` frames; see static/v2.html)
stamped on receipt and corrected by the lag the page reports between the audio
clock reaching the moment and the ack being sent, so a throttled background
tab does not move them. Each ack is also written on its own as a `play_start`
/ `play_end` event, the per-turn playback record the room-pacing work (#24)
needs measured rather than modelled. A turn_timing event is written at its
play_end, or at encounter end for any turn still open (then with whatever the
page never confirmed left null, which is itself the finding for a page that
sent nothing). At most MAX_OPEN turns wait for their acks; the oldest is
written early rather than held without bound.

Instrumentation only. Nothing reads these back to decide anything, and a
fault in here must never reach the audio path: every entry point swallows its
own errors (a missing stage is a null, not a lost encounter).
"""

from __future__ import annotations

import time
from typing import Optional

# Turns awaiting the page's play_end before the oldest is written without it.
MAX_OPEN = 16
# The largest lag an ack may claim. The page schedules at most a reply's worth
# of audio ahead; anything larger is a clock fault, not a measurement.
MAX_ACK_LAG_S = 60.0


class _ParticipantTurn:
    __slots__ = ("stages", "grants", "replies")

    def __init__(self) -> None:
        self.stages: dict = {}
        self.grants: dict = {}
        self.replies = 0


def _guard(fn):
    def wrapped(self, *args, **kwargs):
        try:
            return fn(self, *args, **kwargs)
        except Exception:  # noqa: BLE001 - instrumentation never breaks a turn
            return None
    wrapped.__name__ = fn.__name__
    wrapped.__doc__ = fn.__doc__
    return wrapped


class TurnTimer:
    """One encounter's turn clock. Driven by the runner; see the module doc."""

    def __init__(self, store) -> None:
        self.store = store
        base = getattr(store, "started_at", None)
        self._t0 = float(base) if isinstance(base, (int, float)) else time.time()
        self._seq = 0
        self._pt: Optional[_ParticipantTurn] = None
        # The last few participant turns, newest last, so a transcript that
        # lands after the participant has started again is still matched to
        # the commit it came from rather than to the turn now forming.
        self._recent: list = []
        # The turn the page is filling right now: opened by assistant_started,
        # closed by any assistant_done or assistant_interrupted, exactly as the
        # page's own `currentTurn` is, because the page ties every chunk it is
        # sent to that turn and so must this.
        self._current: Optional[dict] = None
        self._open: dict = {}

    def _t(self, when: Optional[float] = None) -> float:
        return round((time.time() if when is None else when) - self._t0, 3)

    # ── the participant's side ──────────────────────────────────────────
    @_guard
    def speech_end(self) -> None:
        pt = _ParticipantTurn()
        pt.stages["vad_speech_end"] = self._t()
        self._pt = pt
        self._recent = (self._recent + [pt])[-4:]

    def _mark(self, stage: str) -> None:
        if self._pt is not None:
            self._pt.stages.setdefault(stage, self._t())

    @_guard
    def commit_sent(self) -> None:
        self._mark("commit_sent")

    @_guard
    def director_decided(self) -> None:
        self._mark("director_decided")

    @_guard
    def transcript_arrived(self) -> None:
        # The oldest committed turn still waiting for its transcript; else the
        # current one (a family whose gateway closes the turn itself commits
        # nothing, and its transcript is the current turn's).
        for pt in self._recent:
            if "commit_sent" in pt.stages and "transcript_arrived" not in pt.stages:
                pt.stages["transcript_arrived"] = self._t()
                return
        self._mark("transcript_arrived")

    @_guard
    def grant_sent(self, agent_id: str) -> None:
        if self._pt is not None:
            self._pt.grants[agent_id] = self._t()

    # ── the character's side ────────────────────────────────────────────
    @_guard
    def started(self, agent_id: Optional[str]) -> Optional[int]:
        """assistant_started is going to the page. Returns the turn number the
        page is told, which is what its acks name."""
        self._seq += 1
        pt = self._pt
        rec = {
            "turn": self._seq, "agent_id": agent_id, "pt": pt,
            "reply_index": pt.replies if pt is not None else None,
            "grant_sent": pt.grants.pop(agent_id, None) if pt is not None else None,
            "first_audio_from_gateway": None, "first_audio_to_client": None,
            "assistant_done": None, "first_audio_played": None,
            "play_end": None, "play_end_interrupted": None,
            "ack_lag_s": None, "output_latency_s": None,
        }
        if pt is not None:
            pt.replies += 1
        self._current = rec
        self._open[self._seq] = rec
        while len(self._open) > MAX_OPEN:
            oldest = next(iter(self._open))
            self._write(self._open.pop(oldest), reason="evicted")
        return self._seq

    @_guard
    def audio_to_client(self, gateway_first_audio_at: Optional[float] = None) -> None:
        rec = self._current
        if rec is None or rec["first_audio_to_client"] is not None:
            return
        rec["first_audio_to_client"] = self._t()
        if isinstance(gateway_first_audio_at, (int, float)) and gateway_first_audio_at > 0:
            rec["first_audio_from_gateway"] = self._t(gateway_first_audio_at)

    @_guard
    def done(self, agent_id: Optional[str]) -> None:
        for rec in reversed(list(self._open.values())):
            if rec["agent_id"] == agent_id and rec["assistant_done"] is None:
                rec["assistant_done"] = self._t()
                break
        self._current = None

    @_guard
    def interrupted(self) -> None:
        self._current = None

    # ── the page's side ─────────────────────────────────────────────────
    @_guard
    def ack(self, msg: dict) -> None:
        """One `playback` frame from the page: {phase: start|end, turn, lag_s,
        interrupted, output_latency_s}."""
        phase = msg.get("phase")
        if phase not in ("start", "end"):
            return
        turn = msg.get("turn")
        if not isinstance(turn, int) or isinstance(turn, bool):
            return
        lag = msg.get("lag_s")
        lag = float(lag) if isinstance(lag, (int, float)) and not isinstance(lag, bool) else 0.0
        lag = min(max(lag, 0.0), MAX_ACK_LAG_S)
        out_lat = msg.get("output_latency_s")
        out_lat = (round(float(out_lat), 4)
                   if isinstance(out_lat, (int, float)) and not isinstance(out_lat, bool)
                   and 0 <= out_lat < 5 else None)
        at = self._t(time.time() - lag)
        rec = self._open.get(turn)
        agent_id = rec["agent_id"] if rec is not None else None
        if phase == "start":
            self.store.event("play_start", turn=turn, agent_id=agent_id, at=at,
                             lag_s=round(lag, 3), output_latency_s=out_lat)
            if rec is not None and rec["first_audio_played"] is None:
                rec["first_audio_played"] = at
                rec["ack_lag_s"] = round(lag, 3)
                rec["output_latency_s"] = out_lat
            return
        cut = bool(msg.get("interrupted"))
        self.store.event("play_end", turn=turn, agent_id=agent_id, at=at,
                         lag_s=round(lag, 3), interrupted=cut)
        if rec is not None:
            rec["play_end"] = at
            rec["play_end_interrupted"] = cut
            self._write(self._open.pop(turn), reason="play_end")

    # ── writing ─────────────────────────────────────────────────────────
    @_guard
    def flush(self) -> None:
        """Write every turn still open. Called once, as the encounter ends."""
        for turn in list(self._open):
            self._write(self._open.pop(turn), reason="encounter_end")
        self._current = None

    def _write(self, rec: dict, *, reason: str) -> None:
        pt = rec.get("pt")
        stages = pt.stages if pt is not None else {}
        if rec is self._current:
            self._current = None
        self.store.event(
            "turn_timing",
            turn=rec["turn"], agent_id=rec["agent_id"],
            reply_index=rec["reply_index"],
            vad_speech_end=stages.get("vad_speech_end"),
            commit_sent=stages.get("commit_sent"),
            transcript_arrived=stages.get("transcript_arrived"),
            director_decided=stages.get("director_decided"),
            grant_sent=rec["grant_sent"],
            first_audio_from_gateway=rec["first_audio_from_gateway"],
            first_audio_to_client=rec["first_audio_to_client"],
            assistant_done=rec["assistant_done"],
            first_audio_played=rec["first_audio_played"],
            play_end=rec["play_end"],
            play_end_interrupted=rec["play_end_interrupted"],
            ack_lag_s=rec["ack_lag_s"],
            output_latency_s=rec["output_latency_s"],
            # Why it was written now: "play_end" (complete), "encounter_end"
            # or "evicted" (the page never confirmed the end).
            written_at=reason,
        )
