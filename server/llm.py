"""One place where model clients are built.

Clients are constructed with an explicit base URL and key rather than letting
the SDK read ambient environment variables. A stray ANTHROPIC_BASE_URL in the
shell (the desktop app exports one) would otherwise silently redirect study
traffic to a different provider, encounters would fail, or worse, quietly run
somewhere other than the configured gateway. For a measurement instrument the
endpoint has to be deliberate and recorded.
"""

from __future__ import annotations

import os
import re
from typing import Optional

from anthropic import AsyncAnthropic
from dotenv import dotenv_values

from .build_info import build_sha

# .env wins over ambient environment for these, deliberately.
_FILE = dotenv_values()


def _cfg(name: str, default: str = "") -> str:
    value = _FILE.get(name) or os.getenv(name) or default
    return value.strip()


def setting(name: str, default: str = "") -> str:
    """Public accessor with the same file-wins-over-ambient precedence."""
    return _cfg(name, default)


def setting_if_set(name: str) -> Optional[str]:
    """The value of `name` as configured, or None where it is set nowhere.

    Same precedence as setting(), but a blank value comes back as "" instead
    of being replaced by a default: for a knob where blank means "off" (e.g.
    REALTIME_MAX_OUTPUT_TOKENS=, no reply cap at all), "unset" and "set to
    nothing" are different instructions."""
    if name in _FILE:
        return (_FILE.get(name) or "").strip()
    value = os.getenv(name)
    return None if value is None else value.strip()


def gateway_base_url() -> str:
    # LLM_BASE_URL is this project's own explicit var (fine to read from .env or
    # ambient env). ANTHROPIC_BASE_URL, however, is resolved from .env / the
    # hardcoded default ONLY: a stray value in the ambient shell (the desktop
    # app exports one) must never silently redirect study traffic and leak the
    # gateway key to an unintended host. preflight() flags such an ambient value
    # instead of honoring it.
    explicit = _cfg("LLM_BASE_URL")
    if explicit:
        return explicit
    env_val = _FILE.get("ANTHROPIC_BASE_URL")
    if env_val and env_val.strip():
        return env_val.strip()
    return "https://api.ai.it.cornell.edu"


def gateway_api_key() -> str:
    return _cfg("LITELLM_API_KEY") or _cfg("ANTHROPIC_API_KEY")


_REDACTED = "<redacted>"

# The floor applies to EVERY needle, including a whole key. It used to apply
# only to the whitespace-split parts, so a degenerate dev value like
# LITELLM_API_KEY=x substituted on every letter x in the message: "max retries
# exceeded" came back as "ma<redacted> retries e<redacted>ceeded". That is the
# one line an operator reads off /health to find out why the gateway is down,
# and a placeholder key is exactly when they are reading it. A value under six
# characters is not a LiteLLM credential (those are "sk-" plus a long random
# tail), so there is nothing there to protect and mangling the diagnostic is
# pure loss - while a real key has no needle short enough to reach this branch.
_MIN_NEEDLE = 6

# Every variable a live credential is kept in on some deployment of this app.
# Read at CALL time, never captured at import: on the lab machine the key is
# pasted into the shell after the server is already running, so a needle set
# built at import would be empty for exactly the process that has something to
# protect. os.environ is a few dozen entries and this only runs on an error
# path, so the lookup is not worth caching.
_SECRET_ENV_NAMES = (
    "LITELLM_API_KEY",
    "ANTHROPIC_API_KEY",
    "ANTHROPIC_AUTH_TOKEN",
    "OPENAI_API_KEY",
    "GEMINI_API_KEY",
    "GOOGLE_API_KEY",
)

# The AWS_ prefix alone is not enough to make a needle: AWS_REGION is
# "us-east-1" and AWS_PROFILE is a word, and substituting either out of a
# diagnostic is the same pure loss the length floor exists to prevent. Only the
# names that hold something secret count.
_AWS_SECRET_NAME = re.compile(r"^AWS_.*(KEY|SECRET|TOKEN|CREDENTIAL|PASSWORD)", re.I)

# Credential SHAPES, for the secret this process cannot look up: a key rotated
# an hour ago, a colleague's key out of a shared .env, or the gateway quoting
# the upstream provider's own credential back at us. Each of these is anchored
# on a vendor prefix or on an HTTP landmark rather than on entropy alone,
# because a rule loose enough to match any long token also matches hostnames,
# model ids and build paths.
_CRED_CHARS = r"A-Za-z0-9_\-.+/="
_SHAPES = (
    # websockets' InvalidHeaderValue stringifies to "invalid Authorization
    # header: Bearer <the whole credential>" - the transport hands back the
    # header it refused, key included. After the scheme word there is by
    # construction nothing but the credential, so the scheme is enough of a
    # landmark to redact a value of a shape we have never seen. The scheme
    # itself stays: an operator still needs to know which header was rejected.
    #
    # The wrapped paste - a key broken across two lines, which survives _cfg()'s
    # strip(), reaches the header, and comes back with the credential split at
    # the newline - used to be handled by a repeated group bolted onto the end
    # of THIS rule. It is _CONTINUATION's job now, because the tail of a wrapped
    # key outlives every rule in this table and not only this one; see the
    # comment there for the body that proved it.
    (re.compile(rf"(?i)\b(bearer|basic)\s+[{_CRED_CHARS}]{{8,}}"),
     r"\1 " + _REDACTED),
    # A gateway that quotes the credential in its own JSON error body -
    # 'Invalid API key: sk-...', {"api_key": "..."}, x-api-key=... - which is
    # the likelier of the two vectors and the one reproduced in the audit.
    (re.compile(rf"(?i)((?:x-)?api[-_ ]?key[\"']?\s*[:=]\s*[\"']?)[{_CRED_CHARS}]{{8,}}"),
     r"\1" + _REDACTED),
    (re.compile(r"\bsk-[A-Za-z0-9_\-]{10,}"), _REDACTED),
    (re.compile(r"\b(?:AKIA|ASIA|AROA|AIDA|AGPA|AIPA|ANPA|ANVA|ABIA)[0-9A-Z]{12,}\b"),
     _REDACTED),
    (re.compile(r"\bgh[pousr]_[A-Za-z0-9]{16,}\b"), _REDACTED),
    (re.compile(r"\bAIza[0-9A-Za-z_\-]{20,}\b"), _REDACTED),
)

# The catch-all: a long base64url/base64 run, which is what an AWS secret access
# key, a session token and a JWT all look like and none of which carry a vendor
# prefix. Bounded by "not a credential character" rather than \b so that / and +
# are part of the run instead of splitting it.
_LONG_RUN = re.compile(rf"(?<![{_CRED_CHARS}])[A-Za-z0-9_\-+/=]{{28,}}(?![{_CRED_CHARS}])")


def _looks_like_a_secret(run: str) -> bool:
    """Whether one long token is credential material rather than a name.

    Length alone is not a signal: this repo's own build paths and scratch
    directories produce runs well over 28 characters
    ("C--Users-rf-Downloads-relational-fluency-main--1-"), and eating one out of
    a stack trace costs the reader the only line that says where the failure
    was. Separators are what tell them apart - a slug is mostly hyphens and a
    credential almost never is - and a credential mixes case with digits.
    """
    if run.count("-") + run.count("_") > 3:
        return False
    has_upper = any(c.isupper() for c in run)
    has_lower = any(c.islower() for c in run)
    has_digit = any(c.isdigit() for c in run)
    return (has_upper and has_lower and has_digit) or (len(run) >= 40 and has_digit)


# A credential that wrapped across a line break is still one credential, and the
# rules above only ever reach the half of it that carries the landmark - the
# vendor prefix, the "api-key" label, the Authorization scheme. The remainder
# starts the next line with nothing left to recognise it by: no prefix, too
# short for _LONG_RUN, and, for a key this process cannot look up, no needle to
# match either. Reproduced on this branch against a gateway body reading
#
#     Invalid API key: sk-aB3dEfGh1jKlMn0pQrSt
#     uVwXyZ2345aBcDeFgH
#
# which came back with the first line redacted and the second published whole -
# a live credential, just fewer characters, in a string app.py prints at boot
# and serves from the unauthenticated /health. The same body with the label
# changed to a bare `sk-` or to an Authorization header leaked the same tail.
#
# So the continuation is caught by its POSITION: a credential-charset token at
# column 0 on the line immediately after something already redacted. Position
# alone is only half a test, because an ordinary next log line sits in exactly
# that place, so the token must ALSO look like the rest of a key - mixed case
# with digits (_looks_like_a_secret), or a bare token that ENDS its line, which
# is what a wrapped key's tail does and what "Traceback (most recent call
# last):" does not.
#
# The ends-its-line half of that used to live inside the Authorization rule as a
# repeated group. It is one rule for every landmark now, applied last, by which
# point every other rule has left a <redacted> behind for this one to anchor on
# - _LONG_RUN's included.
_CONTINUATION = re.compile(rf"({_REDACTED}[ \t]*\r?\n)([{_CRED_CHARS}]{{4,}})")
_ENDS_ITS_LINE = re.compile(r"[ \t]*(?:\r?\n|\Z)")


def _redact_continuations(text: str) -> str:
    """Redact the far side of a line break from a credential just redacted."""
    def one(m):
        tail = m.group(2)
        ends_its_line = _ENDS_ITS_LINE.match(m.string, m.end()) is not None
        if ends_its_line or _looks_like_a_secret(tail):
            return m.group(1) + _REDACTED
        return m.group(0)

    out = text
    # Looped because re.sub does not rescan what it just wrote, so a key wrapped
    # over three lines needs a second pass to reach the third. Bounded because
    # this runs inside an exception handler on a path that has already cost a
    # participant part of an encounter, and a key wrapped over ten lines is not
    # a thing that happens; each pass that changes anything replaces a token
    # with <redacted>, which cannot match again, so it terminates on its own.
    for _ in range(10):
        new = _CONTINUATION.sub(one, out)
        if new == out:
            break
        out = new
    return out


def _needles() -> list:
    """Exact credential values this process can look up, longest first.

    Longest first so redacting a fragment cannot strand the remainder of a
    longer needle.
    """
    values = set()
    for name in _SECRET_ENV_NAMES:
        for raw in (_FILE.get(name), os.getenv(name)):
            if raw:
                values.add(raw)
                values.add(raw.strip())
    for name, raw in os.environ.items():
        if raw and _AWS_SECRET_NAME.match(name):
            values.add(raw)
            values.add(raw.strip())
    out = set()
    for raw in values:
        out.add(raw)
        # h11 and the bytes reprs in this stack quote the offending header as a
        # repr, in which a newline inside a wrapped key shows up as the two
        # characters \ and n, so the raw value never matches. Redact the escaped
        # form and each line of the key as well.
        out.add(raw.encode("unicode_escape").decode("ascii", "replace"))
        out.update(raw.split())
    return sorted(
        (n for n in out if len(n) >= _MIN_NEEDLE and n != _REDACTED),
        key=len,
        reverse=True,
    )


def redact_key(text: str) -> str:
    """Strip anything key-shaped out of a string that is about to leave here.

    Three sinks make this a disclosure rather than log hygiene. preflight()'s
    `detail` is public by contract: app.py prints it at boot (CloudWatch, 90-day
    retention) and returns it from GET /health, which is unauthenticated. The
    director's routing fallback writes `detail` into the encounter's
    events.jsonl, which is archived per encounter and shipped whole in the
    per-session download.zip an IRB reviewer reads, and into a warning line
    beside it. And Session.auto_steer writes the steering failure into the same
    file. All three carry a string the gateway wrote.

    Two shapes put key material into those strings, and both were reproduced
    against the real libraries rather than assumed. A gateway echoes the
    credential it was sent back in its error body, and anthropic's
    AuthenticationError stringifies to that body verbatim. And a key pasted
    wrapped across two lines survives _cfg()'s strip(), so the newline reaches
    the Authorization header, where the transport rejects it by quoting the
    ENTIRE header value into its exception message - h11's "Illegal header
    value" line in this repo's own logs, and websockets' InvalidHeaderValue on
    the realtime socket.

    So the string is scrubbed four ways, cheapest first: by exact value, for
    every credential variable this process can read; by vendor shape, for a
    credential it cannot (a key rotated an hour ago is still live, and the
    gateway will happily quote one that was never ours); by the scheme word of
    an Authorization header, which is the only landmark left when the value is
    opaque; and then, once every rule above has run, by position - the far side
    of a line break from something already redacted, which is where the tail of
    a wrapped key sits with no landmark of its own (see _CONTINUATION).

    Value substitution is by bare substring, which is only safe for a needle
    long enough that it cannot plausibly occur in ordinary prose - hence
    _MIN_NEEDLE. The shape rules are anchored for the same reason.

    It is idempotent (nothing in "<redacted>" matches any rule, so a string that
    passes two sinks reads the same at both), and it does not raise. That last
    one is not politeness: every caller is already inside an exception handler
    for a failure that cost a participant part of an encounter, and a redactor
    that throws there turns a recoverable gateway error into a lost session. If
    anything below goes wrong the message is withheld rather than published,
    because the caller cannot be trusted to have a safe fallback of its own.
    """
    try:
        if not isinstance(text, str):
            # Callers hand this str(exc); an exception whose __str__ returns a
            # non-str, or a caller that forgot the str(), must not crash the
            # handler it is running inside.
            text = str(text)
        if not text:
            return text
        out = text
        for needle in _needles():
            out = out.replace(needle, _REDACTED)
        for pattern, replacement in _SHAPES:
            out = pattern.sub(replacement, out)
        out = _LONG_RUN.sub(
            lambda m: _REDACTED if _looks_like_a_secret(m.group(0)) else m.group(0),
            out,
        )
        return _redact_continuations(out)
    except Exception:  # noqa: BLE001 - see the docstring: never raise from here
        return "<redaction failed; message withheld>"


def text_client() -> AsyncAnthropic:
    """Client for the text models: director and steering."""
    return AsyncAnthropic(base_url=gateway_base_url(), api_key=gateway_api_key())


# Stamps for what the live pipeline does to a participant's audio and to the
# turn-taking in a room, on every realtime_session_started and so on every
# record. Bump the matching string in the SAME commit as any change that
# alters what a participant hears or what the record contains (input rate,
# transcriber, reply cap, resampler, transcript gate, cancel handling...) —
# PIPELINE_VERSION for the 1:1 and shared path, ROOM_PACING_VERSION for how a
# group room chooses when the next character speaks. They exist because the
# fixes for issues #21-#25 land mid-study, and an analyst has to be able to
# split the archive at each one without reconstructing deploy dates.
#
#   2026-09-23a  instrumentation only (turn_timing, play_start/play_end,
#                client_audio_settings, cap_truncated); behaviour identical
#                to d6f319d.
#   2026-09-23b  gpt route: participant audio sent at 24 kHz (was 16 kHz,
#                read by the gateway as 24 kHz, i.e. heard 1.5x fast);
#                live transcriber gpt-4o-transcribe (was whisper-1); reply
#                cap 1200 tokens (was 380, which cut replies at 10.5-14 s);
#                page end-of-encounter drain up to 45 s (was 12). Each is
#                also in its own provenance field; INPUT_TRANSCRIPTION_MODEL
#                and REALTIME_MAX_OUTPUT_TOKENS can undo two of them, and the
#                record says which values ran whatever this string says.
#                Room pacing logic unchanged (rooms get the same audio fixes).
#   2026-09-23c  participant-turn integrity (issues #21, #24): a silence
#                probe's pad is committed on a cleared buffer and its
#                transcript suppressed (user_turn_suppressed probe_pad); a
#                transcript of fillers/punctuation over <= 80 ms of voice is
#                suppressed (no_speech); a turn with < 600 ms of voice is
#                recorded but low_confidence and kept out of steering (and of
#                the director unless it names someone); the gateway buffer is
#                cleared on the first speech_started after a commit, keeping
#                a 600 ms pre-roll (input_buffer_cleared); the room's
#                near-duplicate filter needs 90% of the shorter line (was
#                60%); a replayed line's transcript is not a second turn.
#                Every value is in `turn_gate` and every suppression in the
#                events with its text. ALSO: 2026-09-23b's 24 kHz gpt input
#                reached only sessions built with an explicit model; the
#                runner and the room build theirs without one and went on
#                sending 16 kHz (read as 24 kHz) while provenance said 24000.
#                From 2026-09-23c the rate follows each session's model, so a
#                23b record's input_rate is not what its gpt sessions sent.
#                The same held on the NATIVE-AUDIO route, which this entry
#                did not say until 2026-09-23g: runner- and room-built
#                native-audio sessions sent 16 kHz raw on every version
#                before 23c while 23a/23b provenance said input_rate 24000
#                and input_resampler audioop.ratecv, and from 23c they send
#                24 kHz resampled. Measured 2026-09-24 through the bridge's
#                own send_audio, on a session built as the runner builds it:
#                the gateway reads that route
#                at 24 kHz too, 157 input audio tokens at 24 kHz against 108
#                for the same audio sent 16 kHz (0.69, i.e. 2/3), so pre-23c
#                native-audio encounters were heard 1.5x fast as well. The
#                transcript came back either way ("Oh yeah. So I'm actually
#                having a competing offer." / "Yeah, so I'm ..."), with the
#                same onset of the reply (6.2 s).
#   2026-09-23d  bridge correctness (issue #23): on the gpt route, what the
#                gateway still sends for a reply after a barge-in cancel is
#                no longer played or made into a second turn (that was the
#                phantom duplicate turn, the false agent_audio_short and the
#                blip after the stop); it is written as
#                cancelled_output_dropped with its text and audio_ms, and an
#                interrupted turn's text is what arrived before the cancel.
#                A reply delivered as several output items is recorded (and
#                captioned) as all of them joined by a space; before, the
#                last item's line replaced the others. Fields
#                `cancelled_output` and `agent_transcript_items`;
#                CANCELLED_OUTPUT_DISCARD=0 restores the relay. Rooms: the
#                floor holder's tail after a barge-in is dropped the same way;
#                a suppressed (held) reply keeps its tail, as before.
#   2026-09-23e  reply lifecycle and clocks (issues #23, #24). Record: a
#                room's heard_seconds / playback_cut total_seconds are per
#                turn (they accumulated across a member's turns: 159.5,
#                284.4 s), and a barge-in on a floor holder whose reply has
#                not reached the page is written as the cut of the line that
#                was playing (it wrote the holder's previous turn); a room
#                assistant_turn carries play_clock_start / play_clock_end;
#                an adopted completed hold records the audio
#                it relayed (was audio_ms 0) and its text once (a whole-line
#                transcript no longer follows the deltas it replaces). 1:1
#                and rooms: the silence probe counts from when the last reply
#                finished PLAYING, looked at every 1 s (was: from generation
#                end, every 12 s, so 12-24 s late on its own clock and ~4 s of
#                real silence). Knobs and values in `pacing`.
#   2026-09-23f  record accuracy (issue #23). A reply read as turn-taking
#                narration ("I'll wait for Casey.") is blanked only when none
#                of its audio was relayed; a spoken one keeps its text and is
#                flagged `deferral` (DEFERRAL_BLANK_AUDIBLE=1 blanks it, as
#                before). The deferral match's name slots are case-sensitive,
#                so "That's for you to set.", "Go ahead and tell me..." and
#                "You asked me for a number..." are no longer blanked. An
#                interrupted or cap-truncated assistant_turn carries
#                generated_text, heard_text (the words that fit in the audio
#                relayed for it) and heard_estimate; `text` is unchanged.
#                Knobs and values in `record`.
#   2026-09-23g  review fixes. gpt rooms: the near-duplicate filter no
#                longer runs where the scribe is the only transcriber (it
#                deleted "Priya, are you there?" as a copy of "Priya?");
#                turn_gate.room_dedupe_second_source says where it runs.
#                1:1 and rooms: an interrupted turn that played audio and got
#                no transcript before the cancel takes its text from the
#                dropped tail (interrupted_text_from_cancelled_output) and is
#                no longer transcript_missing; a barge-in inside a retry's
#                window discards the retry's reply (and a resumed head) as
#                cancelled_output instead of playing it. Record: record.json
#                provenance carries turn_gate, cancelled_output,
#                agent_transcript_items and record; a floor holder's
#                announced mark no longer outlives its turn (the stale
#                playback_cut of 23e by the adopt path); turn_timing binds a
#                reply to the participant turn current at its grant, and
#                commit_sent is marked only for a commit that went out.
#                PARTICIPANT_LOW_CONFIDENCE_DIRECTOR (default "named", as
#                before) can give the director every short turn.
#   2026-09-24a  voice and rate gates (issues #21, #24), calibrated on the
#                tester's S3A session s_1790217895_4025d8. A participant turn
#                the VAD ends with < PARTICIPANT_COMMIT_MIN_VOICED_MS (300)
#                of voice since the last commit is not committed: buffer
#                cleared, participant_turn_discarded, no reply (1:1 and the
#                room's scribe). A transcript with more than
#                PARTICIPANT_MAX_WORDS_PER_VOICED_S (8) words per voiced
#                second over < PARTICIPANT_RATE_GATE_MAX_VOICED_MS (1500) of
#                voice is user_turn_suppressed{implausible_rate} (the
#                phantoms ran 47-63, real lines 1.4-6.1); in 1:1 its reply
#                is cancelled if none of it has played
#                (suppressed_turn_reply_cancelled), else kept and written as
#                reply_to_suppressed_turn. S1 hand-off: once the timebox has
#                run out, HANDOFF_IDLE_S (3) of silence after playback
#                briefs the closing line and probes for it (was: briefed at
#                the participant's next turn, probed 12 s after that).
#                Record: verify_record counts a planted beat as reached only
#                when performed; stage_direction_unperformed and
#                trigger_undelivered are netted out and named. Knobs in
#                `turn_gate` and `pacing`.
#   2026-09-24b  P6 review. The rate gate divides by the voiced SPAN (first
#                voiced frame to last since the commit; voiced_span_ms on
#                the tag, user_turn and user_turn_suppressed), not the voiced
#                count, and PARTICIPANT_MAX_WORDS_PER_VOICED_S defaults to 16
#                (was 8): the count shrank on a 2.5 dB quieter replay of the
#                tester's WAV until "Does that sound good?" read 8.7 w/s; over
#                the span every real line stays <= 6.2 from +6 to -6 dB (and
#                is kept down to -11 dB) and the phantoms >= 44
#                (turn_gate.participant_rate_over). 1:1:
#                a suppressed turn's late transcript no longer cancels the
#                reply to a later commit (reply_to_suppressed_turn,
#                kept="later_commit"); a withdrawn reply gives its planted
#                beat back (trigger_undelivered, reason "reply_withdrawn",
#                re-fired at the next turn; stage_direction_unperformed where
#                it cannot go back); an unnamed cancel the gateway answered
#                with response_cancel_not_active is sent again once the reply
#                is named (cancelled_output_dropped.recancelled). S1: the
#                proactive hand-off waits out a reply still being finalized,
#                the timebox advances only once the closing-line note has
#                been taken by the reply that speaks it, and a refused
#                advance keeps the hand-off flags (no second closing line).
#
# ROOM_PACING_VERSION:
#   2026-09-23a  as d6f319d.
#   2026-09-23b  participant turns queued behind a held floor are routed
#                together as soon as the floor frees (was: a full
#                ROUTE_TRANSCRIPT_WAIT and then routed on nothing);
#                ROOM_MERGE_QUEUED_TURNS=0 routes on the latest alone.
#   2026-09-23c  on the gpt route a floor grant is the commit alone, with a
#                response.create only when nothing started within
#                ROOM_GRANT_UNANSWERED_S (grant_fallback_create); it was
#                commit + create, which drew "the extra response.create was
#                refused" on nearly every grant and sometimes a second reply.
#                A held reply with no audio, or one the suppression's cancel
#                cut short (gpt), is not adopted (held_reply_refused, text
#                kept). A participant resuming within ROOM_SPLIT_TURN_S of
#                their own commit no longer cancels the reply to it
#                (split_turn_extended). Plus the probe clock under 23e.
#   2026-09-23d  review fixes. A refused hold no longer swallows the fresh
#                reply's done when that reply ends empty (the floor was held
#                for the whole group turn timeout); the commit-only grant no
#                longer takes a cancelled reply's kept tail as the commit's
#                answer, so its one fallback create goes out; on the Gemini
#                routes an utterance left unrouted before this turn's speech
#                began no longer routes the turn on its own (the room ran one
#                utterance behind): the turn waits for its own transcript and
#                routes on both.
#   2026-09-24a  a room turn whose only arrivals were withheld from the
#                director (suppressed, or low_confidence naming nobody) is
#                not routed or answered: group_turn_skipped with the text
#                (a turn for which nothing arrived routes as before); a turn
#                under the voice floor is never a group turn at all; the
#                commit-only grant waits ROOM_GRANT_UNANSWERED_S 6 s (was
#                3; a gpt reply took 5.09 s to first audio and the fallback
#                create at 3.03 s was refused) and holds the bridge's own
#                REQUEST_UNANSWERED_S off for that window.
#   2026-09-24b  a low_confidence line that names nobody no longer skips the
#                room turn: 24a left a real short answer ("Yes.", "Two.",
#                "Thank you.", "Casey?" heard as "TC?") unanswered. It
#                routes as before 24a, on context; only a gate suppression
#                (implausible_rate, no_speech, probe_pad) skips the turn.
#   2026-09-28a  the post-turn steering review runs after the floor is
#                released, as a tracked task and one review at a time, so the
#                next routed turn no longer waits 0.8-1.2 s for it (#25); rooms
#                get the turn_open cue (#49) and the 2026-09-28 end policy
#                (#34; a member's held end_conversation is answered with its
#                output alone). Who speaks next, and when, is unchanged.
#
# PIPELINE_VERSION, continued:
#   2026-09-24c  a transcript with no letter or digit ("..." / "." / "```")
#                is suppressed as no_speech at any voiced level
#                (PARTICIPANT_DROP_WORDLESS), and a gateway
#                response_cancel_not_active with no reply in flight is
#                recorded but no longer shown to the participant as an error.
#   2026-09-28a  the researchers' decisions of 2026-09-28. End policy (#34):
#                from 7:00 the participant may move on (End unlocks on every
#                link type, with a notice; move_on_open), and nothing ends an
#                encounter by itself before 12:00 (warning 11:00, stop 12:00;
#                were 12:00 and 13:00): the last interaction's auto-advance and
#                end_conversation are held (auto_end_held), a held call is
#                answered (tool_call_answered) and on gpt the character asked to
#                go on (held_call_reply), and the clock runs on the watchdog's
#                tick. Turn cue (#49): turn_open once the last line has played
#                and nothing is queued or being generated. Issue #21: a
#                transcript of sound tags alone ("(laughter)") is no_speech
#                (PARTICIPANT_DROP_ANNOTATIONS), and in 1:1 a no_speech
#                commit's reply is withdrawn.
PIPELINE_VERSION = "2026-09-28a"
ROOM_PACING_VERSION = "2026-09-28a"


def _voice_style_provenance():
    try:
        from .voice_style import provenance as _vp
        return _vp()
    except Exception:  # noqa: BLE001 - provenance must never break a session
        return None


def provenance(model: Optional[str] = None) -> dict:
    """Recorded with each session so the record shows what served it.

    `model` is the realtime model the session actually opened; the audio
    fields (input rate, transcriber, reply cap, resamplers) are read for it
    from the bridge's own capability table. Omitted, they are read for the
    configured REALTIME_MODEL, which is what /health reports.
    """
    # Imported here, not at the top: server.voice.realtime imports this module.
    from .voice.realtime import (audio_provenance, bridge_provenance,
                                 pacing_provenance, record_provenance,
                                 turn_gate_provenance)

    realtime = _cfg("REALTIME_MODEL", "nto.gemini-live-2.5-flash-native-audio")
    return {
        "gateway": gateway_base_url(),
        "text_model": _cfg("CLAUDE_MODEL", "nto.gemini-3.1-flash-lite"),
        "realtime_model": realtime,
        # The two live text models, so a change to either is visible on every
        # encounter it touched. Steering is the manipulated variable; its model
        # moved from nto.gemini-3.1-flash-lite to nto.gemini-3.5-flash-lite on
        # 2026-09-23, and without this field the record could not say which
        # controller a given encounter ran under. Defaults duplicated from
        # server/steering.py and server/director.py (pinned by
        # tests/test_final_preflight.py via _MODEL_ROLES).
        "steering_model": _cfg("STEERING_MODEL", "nto.gemini-3.5-flash-lite"),
        "director_model": _cfg("DIRECTOR_MODEL", "nto.gemini-3.5-flash-lite"),
        # input_rate, input_transcription_model, max_output_tokens,
        # resampler, input_resampler; see audio_provenance.
        **audio_provenance(model or realtime),
        # The participant-turn gate's knob values (pipeline 2026-09-23c); see
        # server/voice/realtime.py turn_gate_provenance.
        "turn_gate": turn_gate_provenance(model or realtime),
        # cancelled_output ("discard"/"relay") and agent_transcript_items
        # (pipeline 2026-09-23d); see bridge_provenance.
        **bridge_provenance(model or realtime),
        # Room grant, hold adoption, split turn and silence-probe clock knobs
        # (pipeline 2026-09-23e, room pacing 2026-09-23c); see
        # pacing_provenance.
        "pacing": pacing_provenance(),
        # The deferral rule and the heard_text estimate (pipeline
        # 2026-09-23f); see record_provenance.
        "record": record_provenance(),
        "pipeline_version": PIPELINE_VERSION,
        "room_pacing_version": ROOM_PACING_VERSION,
        # The commit the running image was built from (server/build_info.py),
        # None for a local checkout or an image built before BUILD_SHA. The
        # two versions above name what the pipeline was MEANT to do; this
        # names the code that did it, which is what an analyst needs when a
        # deploy went somewhere nobody intended: the four days on a
        # rolled-back image from 2026-09-24 are recognisable afterwards only
        # because that image happened to predate pipeline_version.
        "build": build_sha(),
        # None unless VOICE_STYLE_FILE is set (a listening test): an accent is
        # part of the stimulus, so a styled session must say so.
        "voice_style": _voice_style_provenance(),
    }


# Every model id this deployment will ask the gateway for: the variable that
# names it, the value that variable falls back to, what the model does, and
# whether getting it wrong stops an encounter.
#
# One table because the failure it exists to catch is silent. A mistyped
# DIRECTOR_MODEL crashes nothing: the gateway refuses it (403
# key_model_access_denied - measured, not a 404, which is why the model LIST and
# never a status code is the oracle here), director.py swallows the refusal into
# its cast[0] fallback, and the encounter completes with audio, video,
# transcript and full trigger coverage. Everything is there except the steering,
# which is the study's independent variable, and a whole wave collected that way
# is indistinguishable from a good one until the analysis shows no effect.
# tools/encounter_health.py exists to find that AFTER a wave; this finds it
# before anyone joins.
#
# `blocks` is the difference between "no encounter can run" and "a report will
# fail later". The first four are the encounter path and they turn `ok` false,
# which is what docs/OPERATIONS.md tells an operator `gateway.ok` means. The
# last one is offline tooling - the re-transcriber - and a typo there costs a
# re-transcription run rather than a wave, so
# it is named without taking the gateway "down" for something no participant
# will ever touch. A warning that fires for a harmless reason is a warning that
# gets ignored for the harmful one.
#
# The defaults are duplicated from the modules that read them, which is a real
# hazard: a default that drifts here silently checks a model the study does not
# run. tests/test_final_preflight.py pins every row against its source module,
# the same way tests/test_infra_scenarios.py pins the terraform defaults.
# Importing them instead would be better and is not available - server.director
# and server.steering both import this module.
_MODEL_ROLES = (
    ("CLAUDE_MODEL", "nto.gemini-3.1-flash-lite", "the actor's text engine", True),
    ("DIRECTOR_MODEL", "nto.gemini-3.5-flash-lite", "the director", True),
    ("STEERING_MODEL", "nto.gemini-3.5-flash-lite", "the steering reviewer", True),
    ("REALTIME_MODEL", "nto.gemini-live-2.5-flash-native-audio", "the voice socket", True),
    ("TRANSCRIBE_MODEL", "nto.gemini-3.8-flash", "the re-transcriber, offline", False),
)


def _served_model_ids(payload: object) -> set | None:
    """The model ids a /v1/models body names, or None when it names none.

    None means "no claim", and that is the important half. This gateway answers
    the OpenAI shape - {"object": "list", "data": [{"id": ...}, ...]}, 17
    entries as of this writing - but the check has to fail silent rather than
    fail loud: an operator pointed at a proxy, a stub, a recorded fixture or a
    future gateway that answers some other shape must not be told at boot that
    every model they configured is missing. Only a non-empty list of ids is
    evidence of anything.

    What is deliberately NOT read here is the `mode` field. It looks like the
    way to catch a REALTIME_MODEL pointed at a chat model, and on the live
    gateway it is populated for the gpt entries and null for every nto.gemini
    one - including nto.gemini-live-2.5-flash-native-audio, the model the study
    actually runs. A rule keyed on it would flag the correct configuration.
    """
    entries = payload.get("data") if isinstance(payload, dict) else payload
    if not isinstance(entries, list):
        return None
    ids = set()
    for entry in entries:
        value = entry.get("id") if isinstance(entry, dict) else entry
        if isinstance(value, str) and value.strip():
            ids.add(value.strip())
    return ids or None


def model_check(response: object) -> dict:
    """Whether every model this deployment is configured to use is served here.

    Takes the /v1/models response rather than a parsed body so that a 200 with
    an unreadable body is this function's problem and not preflight's.

    Never raises and never guesses: `models_checked` false is an honest "this
    could not be decided", which is a different answer from "everything checks
    out" and is published as one. `model_problems` stops encounters;
    `model_warnings` does not (see _MODEL_ROLES). Both are lists of sentences
    naming the variable, because "DIRECTOR_MODEL is set to X, which this gateway
    does not serve" is a thing an operator can act on and "gateway ok: false" is
    not.

    Model ids are not redacted on their way into those sentences. They are
    configuration rather than credentials - provenance() already publishes two
    of them verbatim on the same unauthenticated /health - and redacting the
    wrong value out of a message whose only job is to name the wrong value would
    leave nothing behind worth printing.
    """
    try:
        served = _served_model_ids(response.json())
        if served is None:
            return {"models_checked": False}
        # A LiteLLM key whose allowlist is a bare "*" serves everything, and a
        # gateway that says so must not be reported as serving one model called
        # "*". Cheap, and the cost of getting it wrong is the false alarm this
        # whole check has to avoid.
        if "*" in served:
            return {"models_checked": True}

        import difflib

        out = {"models_checked": True}
        for name, default, role, blocks in _MODEL_ROLES:
            value = _cfg(name, default)
            if not value or value in served:
                continue
            # The wave-killer shape is a typo, not an invention -
            # "nto.gemini-3.6-flsah" for "nto.gemini-3.6-flash" - so the nearest
            # served name is usually the whole fix. The cutoff is tight because
            # this catalog is full of near neighbours that differ by a real
            # version number: offering nto.gemini-3.5-flash to somebody who
            # meant 3.6 would send them to change the wrong thing, and no
            # suggestion at all still leaves them the variable and the value.
            near = difflib.get_close_matches(value, sorted(served), n=1, cutoff=0.8)
            hint = f" - did you mean {near[0]!r}?" if near else ""
            key = "model_problems" if blocks else "model_warnings"
            out.setdefault(key, []).append(
                f"{name} ({role}) is set to {value!r}, which this gateway does "
                f"not serve{hint}"
            )
        return out
    except Exception:  # noqa: BLE001 - a diagnostic may not become the failure
        return {"models_checked": False}


def preflight() -> dict:
    """Check the configured gateway answers, and serves what is configured.

    Called at startup. A misconfigured endpoint used to surface only as a 401
    buried in a session log, halfway through an encounter, by which point the
    participant's time is already spent. Better to say so before anyone joins.

    /v1/models is asked because it is the cheapest authenticated call, and the
    answer to the second question was always in the body: the list of models
    this key may use. Checking only the status code and throwing the list away
    is what let a mistyped DIRECTOR_MODEL boot green and cost a whole wave its
    manipulation - see _MODEL_ROLES.

    Everything this returns is treated as public (see redact_key), so `detail`
    describes the failure without ever repeating the credential.

    `status` is the HTTP status, and it is present when the HTTP exchange is
    what went wrong. It is deliberately ABSENT when the gateway answered
    perfectly and the configuration is what is broken, because app.py's boot
    warning prints `status or detail` and a bare "200" in that slot tells an
    operator nothing at all. The named sentence is the entire point of the
    check, so on that path it is what reaches the boot log; `detail` says the
    gateway is reachable in its own words so the line stays true.
    """
    import httpx

    result = {"ok": False, **provenance()}
    ambient = os.getenv("ANTHROPIC_BASE_URL", "")
    if ambient and ambient != gateway_base_url():
        result["ambient_override_ignored"] = ambient
    key = gateway_api_key()
    if key and any(c.isspace() for c in key):
        # A key pasted wrapped across two lines is the copy-paste failure that
        # produced this repo's own "Illegal header value" boot warning. Catch it
        # here rather than letting h11 catch it: h11's complaint quotes the whole
        # Authorization header back, key included, into a detail that is printed
        # at boot and served by the unauthenticated /health. Name the fault, and
        # never the value - the operator only needs to know to re-paste it.
        result["detail"] = (
            "the key contains whitespace (a newline, most likely from a paste "
            "that wrapped) - re-paste it as a single line"
        )
        return result
    try:
        r = httpx.get(
            f"{gateway_base_url().rstrip('/')}/v1/models",
            headers={"Authorization": f"Bearer {key}"},
            timeout=10,
        )
        result["ok"] = r.status_code == 200
        if r.status_code != 200:
            result["status"] = r.status_code
            # Redact before truncating: slicing first could cut the key in half
            # and leave a prefix that no longer matches anything to redact.
            result["detail"] = redact_key(r.text)[:200]
            return result
        result.update(model_check(r))
        if result.get("model_problems"):
            result["ok"] = False
            # Not truncated. Four broken variables is four sentences, and the
            # fourth is the one nobody would have found on their own.
            result["detail"] = ("the gateway is reachable, but "
                                + "; ".join(result["model_problems"]))
        else:
            result["status"] = r.status_code
    except Exception as exc:  # noqa: BLE001
        # The exception TYPE is the useful half and is always safe; the message
        # is not, so it goes through the redactor on its way out.
        result["detail"] = f"{type(exc).__name__}: {redact_key(str(exc))}"
    return result
