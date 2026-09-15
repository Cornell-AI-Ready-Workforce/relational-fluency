"""The blinded packet: one encounter, as a human rater is allowed to see it.

Phase 2 turns recorded encounters into gold labels. Two or three independent
raters score each encounter on all 22 ESCI Relationship Management items and
reliability is computed before anything is modelled. That only measures what it
claims to measure if the raters are blind — the rating instrument's own design
note says raters must be blind to condition and to the scenario's
primary-competency designation — and an encounter on disk is not blind. It
carries the participant key, the actor system prompts, the stage directions the
director wrote, the planted beats with their ESCI tags, and (once the offline
judge has run) a model's score for the same conversation. A rater who saw any of
those would be scoring the instrument's expectations rather than the
participant.

So this module is a deliberate subtraction. It reads the full record and builds
a new dict field by field, an allowlist rather than a copy-and-delete: a field
reaches a rater because it is named here, not because nobody remembered to strip
it. When the record gains a field — and it has, twice — the packet does not gain
it. tests/test_rater_packet.py asserts the absence of each forbidden field by
name and by value, because that test is the thing standing between a blinded
rating and a biased one.

What a rater legitimately needs, and gets:

  the rating code    an opaque, stable handle for the encounter, so a rating can
                     be joined back to it without the session id ever being
                     rater-visible
  the construct      which competency the assignment is for
  the situation      what the PARTICIPANT was told before they started: the same
                     briefing the participant page rendered, minus the identity
                     block, and never the actor briefs
  the transcript     speaker-labelled turns, with the per-turn flags that say
                     where the capture failed — a truncated or untranscribed
                     agent line, a garbled participant turn, the point at which
                     the participant stopped being heard at all
  the duration       how long the encounter ran
  the media          a playback URL for the webcam recording, which is the
                     rateable artefact (it carries the mixed conversation audio;
                     the per-channel WAVs cannot be played back as a
                     conversation). It points at this app's own rater route
                     rather than at storage, so it neither expires nor names the
                     encounter.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import random
from pathlib import Path
from typing import Any, Dict, List, Optional

from . import esci, video
from .encounter_record import build as build_record
from .raters import _ASSIGNMENT_ID_RE
from .runs import _run_code_secret
from .scenarios_v3 import compile_scenario
from .storage import SESSION_ID_RE, SESSIONS_DIR

log = logging.getLogger(__name__)

# Same shape check the storage layer mints and app.py's _session_dir enforces —
# now literally the same object, not a second, looser transcription of it.
# Rating codes and packets are reached from rater-facing routes, so a session id
# is untrusted input by the time it arrives here.
#
# The old [A-Za-z0-9_-]{1,64} accepted uppercase, and rating_code HMACs the id
# STRING: on Windows or a default macOS volume "S_1772460300_44C9A2" and
# "s_1772460300_44c9a2" open the one encounter directory but mint two different
# RC- codes, so the handle a rater quotes and a researcher joins on stops being
# one-per-encounter. The minted shape is lowercase by construction, so pinning
# it removes the ambiguity on every platform rather than papering over it here.
_SESSION_ID_RE = SESSION_ID_RE

# Where the console fetches the recording. The app serves the bytes itself now,
# so this is a route on this server rather than a presigned S3 URL, and three
# things the packet used to have to carry fall away with the signature: there is
# no expiry to count down, no link to re-mint when it runs out, and no object
# key — hence no session id — in a URL the rater's own network tab and browser
# history record.
#
# App-relative on purpose. The console fetches it from whatever origin served
# the console, which is the only origin the rater's token is good against. An
# absolute URL would be fixed at packet-build time and wrong the moment the same
# packet was opened against a researcher's laptop, staging and Fargate in turn.
#
# The token is deliberately NOT interpolated here. The route wants one, but a
# packet is exportable and is written to disk by tooling that has no idea it is
# holding a bearer credential; the console already has the rater's token and
# appends it to this URL itself.
VIDEO_ROUTE = "/api/rater/video/{assignment_id}"

# Reproduced verbatim from the header of studies/study1/qualtrics/
# rating-instrument.md. The ESCI items are somebody else's instrument, and the
# warning has to travel with them: it is attached to every packet because a
# packet is what a rater has open while the items are on screen next to it, and
# because a packet is exportable. Do not drop it to tidy the payload.
INSTRUMENT_NOTICE = (
    "ESCI item source: ESCI item bank (Boyatzis, Goleman & Korn Ferry). "
    "Proprietary instrument — items reproduced for research reference only; "
    "confirm licensing/permission before fielding."
)

# The scale's N/A option, restated where the rater sees the encounter, because
# the instrument requires it: a single encounter cannot exhibit every behaviour,
# and a rater with no way to say so guesses a number instead, which is the one
# thing the reliability computation cannot detect.
SCALE_NOTE = (
    "Rate the participant, not the other speakers, and only on what this "
    "encounter shows. Where the encounter gives you nothing to judge an item "
    "on, choose \"Not enough information to judge\" rather than a middle score."
)

# Neutral, non-evaluative wording for the per-turn flags the runner records.
# The runner writes them specifically so a rater can tell a truncated or lost
# delivery from a bad one; carrying them this far and then describing them as
# failures would trade one rating error for another.
#
# The first two are about the AI character's line. The last two are about the
# platform's hearing of the participant, and they are the harder case: a turn
# the transcriber garbled and a stretch of encounter after the participant's
# transcription channel died both look, on the page, like a participant who
# performed badly or fell quiet. So they say what was lost and point the rater
# at the video, and none of the four is phrased as a hint about how the
# participant did.
NOTE_INTERRUPTED = (
    "The participant began speaking while this line was still being delivered, "
    "so the text shown is what was being said, not all of what was heard."
)
NOTE_NO_TRANSCRIPT = (
    "This line was spoken aloud but its text was not captured. Watch this "
    "moment in the video; do not read the empty text as silence."
)
NOTE_SCRIPT_MISMATCH = (
    "The automatic transcript of this turn is known to be unreliable: the "
    "participant's speech was written out in the wrong writing system, so the "
    "text below is not what was said. Watch this moment in the video, and use "
    "\"Not enough information to judge\" where it is all you have."
)
NOTE_CHANNEL_LOST = (
    "The system stopped receiving the participant's transcript before this "
    "point, so anything they said from here on is missing from the text. "
    "Watch the video; do not read the absence of participant turns as the "
    "participant falling silent."
)
# The same loss, seen from the last turn that was still being heard, for the
# encounters where nothing was spoken after it — see _transcript. The wording
# has to point FORWARD out of the transcript rather than back into it, because
# the thing being described is the part that is not there.
NOTE_CHANNEL_LOST_AT_END = (
    "The system stopped receiving the participant's transcript after this turn "
    "and never got it back, so the transcript ends here whether or "
    "not the participant kept speaking. Watch the video; do not read the end of "
    "the transcript as the participant falling silent."
)
NOTE_CHANNEL_GAP = (
    "The system stopped receiving the participant's transcript after this turn "
    "and only got it back later, so anything the participant said in "
    "between is missing from the text. Watch the video; do not read the gap as "
    "the participant falling silent."
)
# The same fact with no position claim in it, for the encounter where no turn
# sits on the near side of the loss at all. Saying "after this turn"
# there would be a confident false statement about where the hole is, which is
# the failure this whole file is defending against; saying nothing would hide a
# hole the record knows about. This says only what is true.
NOTE_CHANNEL_GAP_SOMEWHERE = (
    "For part of this encounter the system was not receiving the participant's "
    "transcript, so some of what they said is missing from the text. Watch the "
    "video; do not read the missing stretch as the participant falling silent."
)


def _packet_secret() -> bytes:
    """Key for the rating-code HMAC.

    The same server-side secret runs.completion_code uses, rather than a second
    one. A rating code and a completion code are the same kind of object — an
    opaque token derived from an internal id that a participant or a rater
    carries around outside the system — and the failure they share is worse than
    any benefit from separating them: if the key is lost or regenerated, every
    code minted under the old key stops matching, and for rating codes that
    means submitted ratings can no longer be joined to the encounters they
    describe. One key is one thing to persist, back up and carry across a
    redeploy. The domain prefix below keeps the two families from colliding.
    """
    return _run_code_secret()


def rating_code(session_id: str) -> str:
    """Opaque, stable handle for one encounter.

    A rater never sees a session id. They do need something to quote in a
    support email, and the study needs something to print on an export that a
    researcher can join back to the encounter — so the handle has to be stable
    for the life of the study and useless to anyone without the server secret.

    HMAC, not a hash of the session id: a session id is `s_{epoch}_{6 hex}`, so
    an unkeyed digest could be brute-forced back to the id (the epoch is known
    to within a day and only 24 bits follow it) and a rater could then tell
    which encounters share a wave, or a participant. Keyed, the code is a
    dead end without the secret.

    Domain-separated from runs.completion_code by the "rating-code:v1" prefix,
    so the two can never be made to collide by choosing an id, and so a future
    change to the code format can bump the version without re-minting the run
    codes.
    """
    if not _SESSION_ID_RE.fullmatch(session_id or ""):
        raise ValueError("bad session_id")
    msg = f"rating-code:v1:{session_id}".encode()
    digest = hmac.new(_packet_secret(), msg, hashlib.sha256).hexdigest()
    return f"RC-{digest[:10].upper()}"


def _session_dir(session_id: str) -> Optional[Path]:
    if not _SESSION_ID_RE.fullmatch(session_id or ""):
        return None
    sdir = SESSIONS_DIR / session_id
    return sdir if sdir.is_dir() else None


def _manifest(session_dir: Path) -> Dict[str, Any]:
    try:
        return json.loads((session_dir / "manifest.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def _duration_s(manifest: Dict[str, Any], turns: List[dict]) -> Optional[float]:
    """How long the encounter ran, in seconds.

    Preferred from the manifest, which is written by SessionStore.close from the
    real clock. Falls back to the last event's elapsed time for an encounter
    whose manifest never got its ended_at (a crashed session), because a rater
    reading "unknown length" cannot tell a five-minute encounter from a
    thirty-second one, and the last turn is a floor rather than a guess.
    """
    started, ended = manifest.get("started_at"), manifest.get("ended_at")
    if isinstance(started, (int, float)) and isinstance(ended, (int, float)):
        if ended >= started:
            return round(float(ended - started), 1)
    last = max((t.get("t") or 0) for t in turns) if turns else 0
    return round(float(last), 1) if last else None


def _clock(seconds: Optional[float]) -> Optional[str]:
    if seconds is None:
        return None
    total = int(round(seconds))
    return f"{total // 60}:{total % 60:02d}"


def _situation(scenario_id: Optional[str]) -> Dict[str, Any]:
    """The orientation the participant was given, and nothing beyond it.

    Built by the platform's own compiler, so this is literally the briefing the
    participant page rendered rather than a second, drifting retelling of it.
    Two deliberate departures:

    1. compile_scenario is called with an empty participant key. The scenario's
       setup text is filled with {role} and {org}, which come from fixed tables
       and carry no participant information, and with {name} — the in-scene
       identity, which identity.assign derives from the participant key and
       keeps stable across all four of that participant's encounters. A rater
       who saw it could group a participant's packets together and carry an
       impression from one into the next, which is exactly the independence the
       three-rater design is buying. No current spec's setup uses {name}, so
       today this changes no text; it is here so that the day one does, the
       packet stays blind instead of quietly leaking.

    2. Only four fields are taken from the briefing, by name. The compiled
       scenario also holds the actor system prompts, the director prompt, the
       scene retellings and the planted triggers with their ESCI tags. Naming
       what is wanted means a field added to the briefing later does not arrive
       here on its own.
    """
    if not scenario_id:
        return {}
    try:
        scenario = compile_scenario(scenario_id, participant_key="")
    except Exception:  # noqa: BLE001 — a missing or malformed spec must not
        # sink the packet: the transcript and the video are still rateable, and
        # a packet with no situation is visibly incomplete rather than silently
        # wrong.
        log.warning("rater packet: no compiled spec for scenario %s", scenario_id)
        return {}
    brief = getattr(scenario, "briefing", {}) or {}
    return {
        # The few sentences the participant read before speaking. Second person,
        # because that is how they read it.
        "text": brief.get("situation") or "",
        # Facts the participant was holding. Withholding these from the rater
        # would make a participant who used the precedent they were given look
        # like they invented it.
        "assets": list(brief.get("assets") or []),
        "people": [
            {"name": p.get("name"), "role": p.get("role")}
            for p in (brief.get("people") or [])
        ],
        # Scene labels only. The spec's `observe:` line for each interaction —
        # "Reply-all in kind, let it slide, or take it to Drew directly" — is
        # the measurement hint and is not copied.
        "parts": [
            {"label": p.get("label"), "mode": p.get("mode"),
             "with": list(p.get("with") or [])}
            for p in (brief.get("parts") or [])
        ],
    }


def _turn_note(turn: dict) -> Optional[str]:
    parts = []
    if turn.get("interrupted"):
        parts.append(NOTE_INTERRUPTED)
    if turn.get("transcript_missing"):
        parts.append(NOTE_NO_TRANSCRIPT)
    if turn.get("script_mismatch"):
        parts.append(NOTE_SCRIPT_MISMATCH)
    # Only the first turn after the loss carries the channel note; the flag is
    # stamped on every subsequent turn, and repeating the same paragraph down
    # the rest of the transcript is how a marker stops being read. `_transcript`
    # decides which turn is first and clears the flag on the others.
    if turn.get("participant_channel") == "lost":
        parts.append(NOTE_CHANNEL_LOST)
    return " ".join(parts) if parts else None


def _transcript(record: Dict[str, Any]) -> List[dict]:
    """Speaker-labelled turns, with the delivery flags carried through.

    Speakers are named, not identified: the AI characters get their cast names
    (which the rater already sees in the situation) and the participant gets
    "Participant". The record's agent_id, voice, stage_direction, trigger_id,
    esci, probing and instructions_sha256 are all dropped — the first two
    because a rater has no use for them and the rest because they are the
    answer key.

    The flags are why this is a loop and not a slice. `interrupted` means
    the participant spoke over the character, so the text is what the character
    was saying rather than all of what was heard; `transcript_missing` means the
    line was spoken but its text never arrived, so the empty string is a gateway
    failure and not the character falling silent. A rater who scores a truncated
    or empty line as a weak exchange has made precisely the error those flags
    were recorded to prevent, and the record only just started carrying them
    this far. They are surfaced as a neutral marker on the turn, never as a
    quality judgement.

    The other two say the same thing about the participant's side, which is the
    side being scored and therefore the side where getting it wrong is
    expensive. `script_mismatch` means the transcriber wrote a real utterance
    out in the wrong script, so the text is gibberish attributed to a person;
    `participant_channel == "lost"` means the room's only participant
    transcription channel had died by this turn, so every reply from there on is
    absent from the transcript. Both look, unmarked, exactly like a participant
    who did poorly — and the rater is being asked for 22 judgements about
    precisely that. The channel note is attached once, at the turn where the
    loss begins, because a paragraph repeated on every remaining turn is a
    paragraph nobody reads; the flag itself stays on all of them so the counts
    below are honest.

    When the loss ended the encounter there is no turn after it to carry any of
    that, so the last block reads the record's own `participant_channel` state
    and marks the last turn that was still being heard. See the comment there:
    that is the encounter this whole mechanism is for, and it was the one it said
    nothing about.
    """
    names = {a.get("id"): a.get("name") for a in (record.get("cast") or [])}
    out: List[dict] = []
    channel_noted = False
    for turn in record.get("transcript") or []:
        role = turn.get("role")
        lost = turn.get("participant_channel") == "lost"
        if role == "participant":
            speaker = "Participant"
        else:
            # An unrecognised agent id becomes a generic label rather than the
            # raw id: internal ids are not for raters, and "mel" tells them
            # nothing "The other speaker" does not.
            speaker = names.get(turn.get("agent_id")) or "The other speaker"
        out.append({
            # Seconds from the start of the encounter. This is the event
            # timeline, which for the webcam recording is also roughly the
            # playback position (the capture is continuous wall-clock, unlike
            # the per-channel WAVs, which are gapless and do not line up). Good
            # enough to scrub to a moment; not frame-accurate, and nothing has
            # verified the recorder started at t=0, so do not present it as a
            # synchronised caption track.
            "t": turn.get("t"),
            "role": "participant" if role == "participant" else "agent",
            "speaker": speaker,
            "text": turn.get("text") or "",
            "interrupted": bool(turn.get("interrupted")),
            "transcript_missing": bool(turn.get("transcript_missing")),
            "script_mismatch": bool(turn.get("script_mismatch")),
            "participant_channel_lost": lost,
            "note": _turn_note(
                turn if not lost or not channel_noted
                else {**turn, "participant_channel": None}
            ),
        })
        channel_noted = channel_noted or lost

    # The loss that leaves no turn behind it.
    #
    # Everything above learns about the channel from the turns the runner
    # stamped, and those are agent turns spoken AFTER the loss. An encounter that
    # ended at the loss has none — neither does a bounded hole that no agent turn
    # happened to fall inside — so the packet said nothing whatever about it,
    # while record["participant_channel"]["state"] sat in the record reading
    # "lost". The rater was then handed a transcript that simply stops and asked
    # for 22 judgements about the person whose turns are missing from it, which is
    # precisely the reading ("they disengaged") every note above exists to
    # prevent. So read the record's own statement of the loss rather than
    # inferring it from the loss's downstream effects.
    #
    # The note goes on the last turn that was still being heard, because that is
    # where a rater reading down the transcript arrives at the silence; the top of
    # the packet is where it becomes a fact about the file. The turn's own
    # `participant_channel_lost` stays False and the counts are untouched — that
    # turn WAS heard, and the count is of turns, not of encounters.
    if out and not channel_noted:
        channel = record.get("participant_channel") or {}
        state = channel.get("state")
        if state in ("lost", "restored"):
            lost_at = channel.get("lost_at")
            before = [i for i, t in enumerate(out)
                      if lost_at is None or (t["t"] or 0) <= lost_at]
            if before:
                idx = before[-1]
                if state == "restored":
                    note = NOTE_CHANNEL_GAP
                elif idx == len(out) - 1:
                    note = NOTE_CHANNEL_LOST_AT_END
                else:
                    # A loss the record calls terminal with turns still rendered
                    # below the marked one: the 1:1 writer
                    # (realtime_voice_session._finalize_turn) omits
                    # participant_channel entirely, so a group segment whose
                    # scribe died followed by a one_to_one_series — S3A and S3B
                    # — arrives here with further turns below. "The transcript
                    # ends here" is false there, so use the wording that claims
                    # no position.
                    note = NOTE_CHANNEL_GAP_SOMEWHERE
            else:
                # Nothing in the transcript sits on the near side of the loss —
                # the channel died before anything was recorded and came back
                # later. Both notes above claim a position ("after this
                # turn") that would be false here, so the marker goes on the
                # first turn in the wording that claims none.
                idx = 0
                note = NOTE_CHANNEL_GAP_SOMEWHERE
            out[idx]["note"] = " ".join(
                p for p in (out[idx]["note"], note) if p
            )
    return out


def _last_upload_event(session_id: str) -> Optional[Dict[str, Any]]:
    """The last thing the confirm endpoint wrote about this session's video.

    Not the same question as video.exists, which asks storage "are there bytes
    to play?" and is what _media now branches on. This answers "was a recording
    ever made and reported?", which storage cannot answer at all and which is
    the only way the server can tell a participant who never turned a camera on
    from one whose recording was captured and then lost on the way to the
    bucket. The browser posts the confirmation either way and it carries
    `status` ("ok" or "failed") and a short `error`; an older event has neither,
    and `bytes: 0` says the same thing about it.

    Every event is taken, whatever its status or byte count, because this is a
    question about the ATTEMPT. video.upload_receipt filters the zero-byte and
    failed ones out — it is answering about the object — and reading a receipt
    where this reads the trail is how a lost recording came to be reported to
    raters as an encounter that had no camera.

    Last one wins, matching upload_receipt and encounter_record.build: a
    repeated confirmation is the later, better-informed one.
    """
    if not _SESSION_ID_RE.fullmatch(session_id or ""):
        return None
    events_path = SESSIONS_DIR / session_id / "events.jsonl"
    if not events_path.is_file():
        return None
    latest = None
    try:
        with events_path.open(encoding="utf-8") as fh:
            for line in fh:
                # Cheap prefilter: events.jsonl is hundreds of lines per
                # encounter and only a couple are ever this type.
                if '"video_uploaded"' not in line:
                    continue
                try:
                    ev = json.loads(line)
                except ValueError:
                    continue
                if ev.get("type") == "video_uploaded":
                    latest = ev
    except OSError:
        return None
    return latest


def _blinded_code(value: Any, session_id: str) -> Optional[str]:
    """A reported failure, trimmed to something a rater can quote.

    Two constraints meet here. The rater is the person who will report this, so
    a short code ("put 403") turns "the video is missing" into something the
    study team can act on. But the packet is blinded, and this string came from
    a browser rather than from this module — so it is capped, and dropped
    outright if the session id has found its way into it (a URL in an error
    message is the obvious route), because the whole point of the rating code is
    that a rater cannot link two packets to one participant.
    """
    if not value:
        return None
    # The blinding check runs on the WHOLE string, before the cap. Truncating
    # first defeated it: an id at offset 110 of a 130-character message is cut
    # in half, `session_id in text` is then False, and the surviving prefix —
    # `s_<unix timestamp>` — is exactly the start-time-to-the-second that lets a
    # rater tell which packets belong to one participant (see _media below).
    # A partial id leaking is the same leak as a whole one; only the cap is
    # about length, and it is applied to a string already cleared.
    raw = str(value).strip()
    if not raw or session_id in raw:
        return None
    return raw[:120]


def _upload_error(event: Dict[str, Any], session_id: str) -> Optional[str]:
    """What broke, as far as anyone can say: this server's finding, then the
    browser's.

    Two fields, two authors, and the second one was being thrown away. `error`
    is what this server's own HEAD found, and on a failed upload it is ALWAYS
    set — "not_found" when S3 answered plainly that the object is absent. That
    is a true statement and a nearly useless one: it says the recording is not
    there, which the rater can already see, and nothing about why. `client_error`
    is the browser's own diagnosis (put_http_403, confirm_timeout, timeout,
    abandoned), it is the only account of the leg that actually broke, and it
    reached neither a rater, a researcher nor verify_record — so the one fact
    that says whether this is a permissions problem, a network problem or a
    participant who closed the tab was written down and never read.

    Both are shown when both exist, because they answer different questions and
    an "AccessDenied / put_http_403" pair is a diagnosis where either half alone
    is a guess.
    """
    ours = _blinded_code(event.get("error"), session_id)
    theirs = _blinded_code(event.get("client_error"), session_id)
    if ours and theirs and theirs != ours:
        return f"{ours}; browser reported {theirs}"
    return ours or theirs


def _video_route(assignment_id: Optional[str]) -> Optional[str]:
    """The URL the console plays, or None when this packet cannot name one.

    The id is shape-checked before it is interpolated, because this string ends
    up as the `src` of a <video> element and the id reaches build() from a
    rater-facing route path. One carrying "/" or "?" or "#" would silently move
    the element's request to a different route, or graft a query string onto
    this one and displace the token the console appends — a recording that
    plays for nobody, or worse, a request aimed somewhere it was not meant to
    go. Imported from raters rather than transcribed, for the same reason
    SESSION_ID_RE is: two copies of one shape drift, and the copy that drifts is
    always the one nothing mints against.
    """
    aid = (assignment_id or "").strip()
    if not _ASSIGNMENT_ID_RE.fullmatch(aid):
        return None
    return VIDEO_ROUTE.format(assignment_id=aid)


def _media(session_id: str, assignment_id: Optional[str] = None) -> Dict[str, Any]:
    """The rateable artefact: the webcam recording, behind this app's own route.

    Four states, kept distinct on purpose. A video that exists and can be
    played; an encounter that has no video at all (two of the twenty-seven in
    the reference wave); a recording that was made and could not be stored or
    confirmed, which is the deployment fault that actually happens at scale (a
    403 on the PUT, a region redirect, a presign that 500ed); and a video that
    exists but that this packet cannot address. Collapsing either fault into "no
    video" would show a rater "no video recorded" for an encounter that had one,
    and they would rate it from the transcript alone with nothing anywhere
    saying the video had been lost.

    The fourth is the only one a rater cannot reach, and it covers two unlike
    situations: a call given no assignment to address the recording through (an
    operator inspecting an encounter), and a call given an id that is not the
    minted shape (a real fault). Only the second is anything to report, and only
    the second's note asks for it — see the branch. Saying "report it" to the
    first sends a reader to chase a fault that does not exist, and teaches them
    that this state means a damaged recording, which is the reading the 'failed'
    branch exists to keep separate from it.

    WHICH STATE APPLIES IS DECIDED BY ASKING STORAGE, NOT BY READING A RECEIPT.
    These branches used to turn on video.upload_receipt — the `video_uploaded`
    event a browser's confirmation POST leaves behind — and an encounter whose
    confirmation leg failed once was then permanently unrateable even though its
    bytes had been sitting in the bucket the whole time: nothing re-checked, and
    nothing rewrote the event. A browser event is a claim ABOUT storage; it is
    not storage. video.exists() asks storage, so a recording that is there is
    rateable however badly its confirmation went, and a receipt over bytes that
    are not there stops promising a rater something to play.

    The event trail is still read, but only for the question storage cannot
    answer: an absent object does not say whether a camera was ever on. "The
    camera was never on" and "the camera was on and we lost it" are different
    facts about the encounter. A rating made from the transcript because there
    was nothing to watch and one made from the transcript because the upload
    broke land in the same ratings table and feed the same reliability figure,
    so the difference has to be visible at the moment of rating, not
    reconstructed afterwards from a bucket listing that no longer has anything
    to list.

    Asking costs an existence check per packet where reading the receipt was a
    file read. That trade is deliberate and it is cheaper than it was: the check
    is local when the recording is local, and a packet is built one per route
    call rather than once per row of an assignment list.

    Audio is deliberately absent. The WAVs are per-channel — mic on one, each
    agent on another — so there is no file on disk that plays back as a
    conversation. The video is the one artefact that does.

    The blinding caveat this docstring used to carry is CLOSED, and closing it
    is the second reason for the route. The playback URL was a presigned S3 URL
    whose object key is encounters/{session_id}/webcam.webm, so the one session
    id anywhere in the packet sat in a field a rater could read out of their own
    network tab and their browser history kept. A session id carries the
    encounter's start time to the second, which is enough to tell that two
    packets recorded twelve minutes apart belong to one participant — exactly
    the cross-linking the rating code exists to prevent. The URL now names the
    ASSIGNMENT, which is already the rater's own handle and links nothing back
    to a participant, and the app resolves it to an encounter server-side.
    """
    # exists() is contractually total — no credentials, an unreachable endpoint
    # or a refused HEAD all answer False rather than raising — so there is
    # deliberately nothing caught around it. A raise here would 500 the whole
    # packet, transcript and all, over a question about one field.
    if video.exists(session_id):
        url = _video_route(assignment_id)
        if url is None:
            # There are bytes, and no URL that reaches them. The note this
            # branch used to send ran two unlike statements together — "there
            # is no playable recording" and "this call cannot build a URL for
            # one" — and the first of them is FALSE here every time: exists()
            # said yes. Only the second is what happened, and it happens for two
            # different reasons that deserve different sentences.
            #
            #   An id was given and refused — something minted, stored or typed
            #   an assignment id that is not the minted shape. A console would
            #   show a present recording it cannot play, and nothing else in the
            #   system will notice, so this one IS a fault and the note asks for
            #   it to be reported.
            #
            #   No id was given at all — build() called with neither
            #   assignment_id nor order_seed, which in this repository is an
            #   operator inspecting one encounter rather than a rater rating it.
            #   Nothing is broken. The recording is there and playable; this
            #   call just has no assignment to address it through, which is a
            #   fact about the call and not about the encounter. The old note
            #   told that reader to report it too, so an operator checking
            #   whether a recording had survived was handed the words the
            #   LOST-recording state uses and would reasonably conclude it had
            #   not — and would go and chase a fault that does not exist.
            #
            # Both notes still say do not rate FROM THIS PACKET, and that part
            # is not the bug: a packet with no playable link would be rated from
            # the transcript alone whichever reason put it in this state, and
            # that rating cannot be taken back. What only one of them may say is
            # "report it". `asked` is the whole distinction — a caller that
            # tried to address the recording and failed, against one that never
            # tried. Drop it and the false alarm comes straight back.
            asked = bool((assignment_id or "").strip())
            # Same status string on both, deliberately. 'unsigned' is a fossil
            # name kept because the rating console keys off it, and both
            # readings of it are "there are bytes and this packet cannot address
            # them" — a consumer branching on the status still gets one honest
            # answer, and a fifth value would be a state static/rater.html has
            # never heard of. What differs is the sentence, because the sentence
            # is the part a person acts on. Nothing signs anything any more, and
            # /api/rater/packet/{assignment_id} always passes the id it just
            # authorised, so a rater reaches neither reading.
            #
            # It stays a branch rather than an assertion because the alternative
            # is emitting a URL that 404s and letting the console report a
            # present recording as a missing one.
            return {
                "video_url": None,
                "video_available": True,
                "video_status": "unsigned",
                "upload_error": None,   # same key on every branch; see 'absent'
                "expires_in": None,
                "note": (
                    "This encounter has a webcam recording, but a playback "
                    "link could not be issued for the assignment id this "
                    "packet was given. Do not rate it yet — report it."
                    if asked else
                    "This encounter has a webcam recording and there are "
                    "bytes to play, but a playback link could not be issued: "
                    "this packet was built outside a rating assignment, and a "
                    "link is addressed to one. Do not rate it from this "
                    "packet — nothing is wrong with the recording, this packet "
                    "simply cannot reach it. Build the packet for an "
                    "assignment to get a playable link."
                ),
            }
        return {
            "video_url": url,
            "video_available": True,
            "video_status": "ok",
            "upload_error": None,   # same key on every branch; see 'absent'
            # Null on every branch now, this one included. The app serves the
            # bytes for as long as the rater's own token is good for, so there
            # is no deadline for a console to count down and nothing to re-mint
            # when it passes. The key stays because every branch carries the
            # same key set; the value is the fact that there is no expiry.
            "expires_in": None,
            "note": None,
        }
    attempt = _last_upload_event(session_id)
    if attempt is not None:
        # Storage has nothing and a confirmation was posted anyway: the browser
        # held a recording of known size and it could not be stored, or it was
        # stored somewhere this server cannot see. The confirmation's own status
        # is not consulted — an event saying "ok" over bytes that are not there
        # is precisely the receipt this function stopped trusting. The note
        # stops short of "it never reached storage": that is a claim about the
        # bucket this process is in no position to make, and overclaiming about
        # the video is exactly how this state came to be reported as "no
        # camera".
        # `video_available` stays True because it is what tells the rating
        # console this is a fault to report rather than an encounter to rate
        # from the transcript — the same treatment the unaddressable state gets,
        # for the same reason: a rating made against a missing video cannot be
        # undone afterwards, and a blocked one can.
        err = _upload_error(attempt, session_id)
        return {
            "video_url": None,
            "video_available": True,
            "video_status": "failed",
            "upload_error": err,
            "expires_in": None,
            "note": "This encounter WAS recorded, but the recording could "
                    "not be stored or confirmed, so there is nothing to "
                    "play. Do not rate it from the transcript — report it, "
                    "so the study team can tell this apart from an "
                    "encounter that had no camera."
                    + (f" (reported: {err})" if err else ""),
        }
    return {
        "video_url": None,
        "video_available": False,
        "video_status": "absent",
        # Present and null, not absent. Every branch above carries the same key
        # set, so a Python consumer can read media["upload_error"] on any state
        # without a KeyError deciding, at import time, whether an encounter had
        # an upload fault. An analysis that crashes on the 'absent' rows is an
        # analysis that gets run on the others.
        "upload_error": None,
        "expires_in": None,
        "note": "No webcam recording was captured for this encounter. "
                "Rate it from the transcript, and use the "
                "\"Not enough information to judge\" option where the "
                "transcript alone cannot support an item.",
    }


def _rating_items(order_seed: str) -> List[Dict[str, Any]]:
    """The 22 items a rater answers, in this rater's own order.

    All 22, not just the encounter's focal construct: the instrument is explicit
    that every transcript is rated on every item regardless of the scenario's
    competency, because that is what preserves the multitrait-multimethod
    structure the reliability analysis rests on. Rating only the focal five or
    six would collapse it.

    Order is randomised per rater, per administration note 1: items shuffled
    within their competency group, and the groups themselves shuffled. Fixed
    order invites a rater to settle into a rhythm and answer position rather
    than content, and it makes any order effect indistinguishable from a real
    one because it lands identically on everybody. The seed is the assignment,
    so a rater who reloads the page sees the same order they started with —
    reshuffling mid-rating would be worse than not shuffling at all.
    """
    rng = random.Random(f"item-order:{order_seed}")
    groups = []
    for construct in esci.CONSTRUCTS:
        block = [dict(i) for i in esci.items_for(construct)]
        rng.shuffle(block)
        groups.append(block)
    rng.shuffle(groups)
    out: List[Dict[str, Any]] = []
    for block in groups:
        for it in block:
            # The rater is never shown which competency an item belongs to, nor
            # that it is reverse-scored: both are cues about the expected answer.
            # Reverse scoring is applied server-side by esci.score_value.
            out.append({"id": it["id"], "text": it["text"]})
    return out


def build(session_id: str, *, order_seed: Optional[str] = None,
          assignment_id: Optional[str] = None) -> Dict[str, Any]:
    """The blinded packet for one encounter, or {} when there is no such encounter.

    Returns {} rather than raising so a rater-facing route can turn an unknown
    or unreachable encounter into a 404 without leaking the difference between
    "no such session" and "a session you are not assigned to".

    The record is rebuilt from events.jsonl rather than read from the stored
    record.json, because the stored copy is written when the session closes and
    the browser confirms the webcam upload a moment later — so record.json on
    disk says the encounter has no video even when it has one. The packet's
    whole point is the video.

    `order_seed` should be the assignment id. It fixes this rater's item order
    (see _rating_items) and keeps it stable across reloads. Omitted, the items
    come back in bank order, which is fine for an operator inspecting a packet
    and wrong for a rater.

    `assignment_id` is what the playback URL is addressed to (see _media), and
    it defaults to `order_seed` because the only caller in this repository
    already passes the assignment id there and this docstring has always said
    that is what it is. Naming it separately means a caller that seeds the item
    order with something else does not thereby publish a playback URL that
    resolves to nothing; omit both and the packet reports its video as present
    but unaddressable — a fact about how it was asked for, not a claim that the
    recording is damaged — rather than inventing a link.
    """
    sdir = _session_dir(session_id)
    if sdir is None:
        return {}
    record = build_record(sdir)
    if not record:
        return {}
    manifest = _manifest(sdir)

    turns = _transcript(record)
    duration = _duration_s(manifest, record.get("transcript") or [])

    return {
        # The only identifier in the packet. Not the session id, not the run id,
        # not the participant key, not the participant record id.
        "rating_code": rating_code(session_id),
        # Which competency this assignment is for. The scenario id and its
        # variant are NOT here: "S1B" names the scenario's primary-competency
        # designation, which the instrument requires raters to be blind to, and
        # it would also point a curious rater straight at the spec file holding
        # every planted beat and its scoring anchors.
        "construct": _construct_for(record),
        "situation": _situation(record.get("scenario")),
        "transcript": turns,
        "duration_s": duration,
        "duration_display": _clock(duration),
        "counts": {
            "participant_turns": sum(1 for t in turns if t["role"] == "participant"),
            "agent_turns": sum(1 for t in turns if t["role"] == "agent"),
            # Surfaced as counts as well as per-turn markers so a console can
            # warn once at the top instead of hoping the rater notices a marker
            # halfway down a ten-turn transcript.
            "interrupted_turns": sum(1 for t in turns if t["interrupted"]),
            "untranscribed_turns": sum(1 for t in turns if t["transcript_missing"]),
            # The participant-side pair. `unheard_turns` is the number of agent
            # turns spoken after the participant's transcription channel died,
            # which is the only number on the packet that distinguishes a short
            # transcript from a truncated one — and a rater who sees a
            # transcript whose last third has no participant in it will
            # otherwise conclude the participant disengaged.
            #
            # It is 0 for an encounter that ENDED at the loss: nothing was spoken
            # after it, so there is nothing to count. That encounter is marked on
            # the turn instead (see _transcript), and a console must not read
            # this zero as "the channel held".
            "script_mismatch_turns": sum(1 for t in turns if t["script_mismatch"]),
            "unheard_turns": sum(1 for t in turns if t["participant_channel_lost"]),
        },
        "media": _media(session_id, assignment_id or order_seed),
        # The items themselves. Without these the console has nothing to render
        # and Phase 2 collects nothing through its own working path, which is
        # exactly what happened before this was added: each side of this
        # boundary was tested against a stub of the other, so both suites passed
        # over a packet that could never be rated.
        "items": _rating_items(order_seed or session_id),
        "scale": {
            "min": esci.SCALE_MIN,
            "max": esci.SCALE_MAX,
            "labels": esci.SCALE_LABELS,
            "na_label": esci.NA_LABEL,
        },
        "scale_note": SCALE_NOTE,
        "instrument_notice": INSTRUMENT_NOTICE,
    }


def _construct_for(record: Dict[str, Any]) -> Optional[str]:
    """The competency this encounter measures, from the scenario spec.

    Derived rather than taken from the record, which carries the scenario id but
    not its construct. Reading the spec for one field is cheap (the specs are
    cached by scenarios_v3) and keeps the packet from having to trust a caller
    to tell it which construct an encounter belongs to.
    """
    scenario_id = record.get("scenario")
    if not scenario_id:
        return None
    try:
        from .scenarios_v3 import load_spec
        return load_spec(scenario_id).get("construct")
    except Exception:  # noqa: BLE001 — an archived or v2 scenario has no v3
        # spec; the packet is still rateable and the assignment already names a
        # construct.
        return None
