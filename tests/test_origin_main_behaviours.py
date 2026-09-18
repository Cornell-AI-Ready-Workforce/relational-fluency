"""origin/main's half of the merge, held to what it was measured to do.

Twenty-two commits by jl3369 (7-11 Sep 2026) landed in this tree through a
merge, and at the end of that merge not one of the behaviours they added had a
test. They were all present and all working -- each was driven by hand against a
real session before this file existed -- but nothing in the suite would have
noticed if a later edit quietly took one away, and several of them are the only
thing standing between the DEPLOYED route and a room that says nothing at all.
That is the wrong way round: her half is the half measured against the model
production actually runs (`nto.gemini-live-2.5-flash-native-audio`, image
df1ab83), and ours is the half measured against plain flash.

So this file covers, one test each, the behaviours the merge resolution kept
from her:

  * the deployed route is its OWN family, not a near-miss of plain flash
    (16 kHz into it is a permanent, errorless silence)
  * the five per-model answers she reached for by name -- input rate, autofire
    wait, text items, "is this the OpenAI route", voice casting -- still answer,
    now out of the table
  * members get no tools on the deployed route (5a45420)
  * colleagues reach a member as TEXT there, not as fanned audio (ce8cdf4,
    df1ab83), and a told line counts as something heard
  * the floor is granted by injecting text and asking, not by padding and
    committing (169310c)
  * a member sitting in one of that route's empty auto-fired replies is STILL
    given the floor -- the fix of 2026-09-15, and the difference between a
    character speaking and a character silent for a whole turn
  * Gemini voice names still cast on the gpt fallback (210fbfc)
  * TRANSCRIPTION_LANG rides on every realtime session (cabc1dd)
  * the parroted context note is stripped whole, and a stage direction is not a
    reply (df1ab83)
  * her near-duplicate participant filter runs where a second transcriber
    exists, and nowhere else
  * BARGE_IN_MS, her name for the sustained-speech bar, is still honoured

Nothing here opens a socket: tests/conftest.py refuses outbound connections, and
the room tests ride the same MeasuredGateway harness as
tests/test_final_group_live.py, whose per-family answers come from the same
REALTIME_FAMILIES rows the code reads.
"""

from __future__ import annotations

import asyncio
import importlib
import sys
import time
import types
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from server import realtime_voice_session as rvs  # noqa: E402
from server.voice import realtime as rt_mod  # noqa: E402
from server.voice.realtime import (  # noqa: E402
    accepts_text_items, autofire_wait_for_model, capabilities_for, family_of,
    grants_via_text_prompt, input_rate_for_model, is_openai_realtime,
    member_tools_allowed, voice_for_model,
)

from test_final_group_live import (  # noqa: E402
    GEMINI, GPT, a_participant_speaks, run_room,
)
# The room-with-a-scripted-director harness, for the two anti-dominance tests:
# that decision is made inside _run_group_turn and there is no seam short of
# running the turn. Same module the director's own tests ride on.
from test_group_model_passthrough import _runner_with_room  # noqa: E402

# The model the terraform tfvars actually sets, and the reason this file exists.
NATIVE = "nto.gemini-live-2.5-flash-native-audio"


# --------------------------------------------------------------------------
# 1. The deployed route is its own family.
# --------------------------------------------------------------------------

def test_the_deployed_route_is_not_folded_into_plain_flash():
    """`"gemini" in name and "live" in name` matches the native-audio sibling
    too, and folding it in is not a near miss: it feeds that route 16 kHz, which
    it accepts and then ignores for ever -- session open, no transcription, no
    reply, no error (2026-09-08, after nine other config variants). The two rows
    differ in six columns, which is a family and not a member."""
    assert family_of(NATIVE) != family_of(GEMINI)
    assert capabilities_for(NATIVE) is not None, (
        "no row for the model production runs: require_capabilities() would "
        "resolve it into the wrong family"
    )
    assert input_rate_for_model(NATIVE) == 24000
    assert input_rate_for_model(GEMINI) == 16000


@pytest.mark.parametrize("model,rate,wait,text_items,openai", [
    (GEMINI, 16000, 1.5, True, False),
    (NATIVE, 24000, 4.5, True, False),
    # 0.0: server VAD is off on this family and the bridge replies only on
    # commit, so there is no auto-fired reply to wait for (2026-09-18).
    (GPT, 16000, 0.0, True, True),
])
def test_the_five_answers_she_reached_by_name_still_answer(
        model, rate, wait, text_items, openai, monkeypatch):
    """origin/main asked five separate substring questions of the model name.
    They are columns of one table now, and the names are still the door: the
    room and the runner import them. Every value here is hers."""
    monkeypatch.delenv("AUTOFIRE_WAIT", raising=False)
    assert input_rate_for_model(model) == rate
    assert autofire_wait_for_model(model) == wait
    assert accepts_text_items(model) is text_items
    assert is_openai_realtime(model) is openai


def test_the_autofire_wait_is_the_routes_own_and_the_env_still_overrides(monkeypatch):
    """Her knob still wins everywhere, because AUTOFIRE_WAIT is what the
    runner's single 1.5 s default was. Unset, each route gets its own."""
    monkeypatch.delenv("AUTOFIRE_WAIT", raising=False)
    assert autofire_wait_for_model(NATIVE) > autofire_wait_for_model(GEMINI)
    monkeypatch.setenv("AUTOFIRE_WAIT", "0.25")
    assert autofire_wait_for_model(NATIVE) == 0.25
    assert autofire_wait_for_model(GEMINI) == 0.25


def test_no_module_reads_the_autofire_knob_behind_the_tables_back():
    """The 1:1 wait read `os.getenv("AUTOFIRE_WAIT", "1.5")` directly until
    2026-09-15 while its group twin read the table, so on the deployed route the
    1:1 path committed ~1.8 s before that route's own reply fires -- two replies,
    both spoken and both transcribed, on every turn of S1 and S2. One reader."""
    for mod in ("server/realtime_voice_session.py", "server/group_room.py"):
        text = (ROOT / mod).read_text(encoding="utf-8")
        assert 'getenv("AUTOFIRE_WAIT"' not in text, (
            f"{mod} reads AUTOFIRE_WAIT itself; the per-family value in "
            f"autofire_wait_for_model is then unreachable from that call site"
        )


def test_the_replay_bar_is_never_shorter_than_the_routes_own_reply():
    """REPLAY_UNANSWERED_S is 4 s, measured on plain flash. The native-audio row
    says that route does not start speaking for 4.5 s. Unfloored, the replay
    path called a turn lost and rebuilt the session half a second before the
    reply was due."""
    sess = types.SimpleNamespace(
        model=NATIVE, _replay_in_flight=True,
        _absent_bar=None,
    )
    bar_native = rt_mod.RealtimeVoiceSession._absent_bar(sess)
    assert bar_native >= autofire_wait_for_model(NATIVE)
    sess.model = GEMINI
    assert rt_mod.RealtimeVoiceSession._absent_bar(sess) == rt_mod.REPLAY_UNANSWERED_S
    sess._replay_in_flight = False
    assert rt_mod.RealtimeVoiceSession._absent_bar(sess) == rt_mod.AUDIO_ABSENT_S


# --------------------------------------------------------------------------
# 2. The room, on the route that is deployed.
# --------------------------------------------------------------------------

def test_room_members_are_given_no_tools_on_the_deployed_route():
    """5a45420. That route calls end_conversation constantly and every call is
    an empty turn. The cost is real and recorded: END_SEGMENT_TOOL reaches no
    member there, so a group segment ends the way it did before the tool
    existed. Everywhere else the tool stays, because that is where it worked."""
    assert member_tools_allowed(NATIVE) is False
    assert member_tools_allowed(GEMINI) is True
    assert member_tools_allowed(GPT) is True

    async def nothing(room):
        return None

    room = run_room(NATIVE, nothing)
    for member in room.sessions.values():
        assert member.tools == [], (
            "a room member on the deployed route was handed tools; each call "
            "is an empty turn"
        )
    room = run_room(GEMINI, nothing)
    assert any(m.tools is not None for m in room.sessions.values())


def test_a_colleagues_line_reaches_relay_members_as_text_and_counts_as_heard():
    """ce8cdf4/df1ab83. Fanned colleague audio confused this route's turn
    detection, so the room tells the member what was said, once, when the line
    is finished -- as a context note, explicitly not a cue to speak.

    And the count matters as much as the note. give_floor asks "has this member
    heard anything since its last turn?" and takes the scene-open branch when
    the answer is no. On a relay family nothing is ever fanned, so an uncounted
    tell() would make every grant a scene open."""
    async def body(room):
        await room.tell("Priya", "We slipped the date.", exclude="priya")

    room = run_room(NATIVE, body)
    for aid, sess in room.sessions.items():
        if aid == "priya":
            assert sess.injected == [], "the speaker was told their own line"
            continue
        assert len(sess.injected) == 1, f"{aid} was not told the line"
        note = sess.injected[0]
        assert "We slipped the date." in note
        assert "not for you to repeat" in note
        assert room._fanned_since_grant.get(aid, 0) > 0, (
            f"{aid}'s told line was not counted as heard, so its next grant "
            f"would take the scene-open branch"
        )

    # Plain flash is untouched: colleagues are still audio there, which is the
    # route our own fan-out byte counters were measured against.
    room = run_room(GEMINI, body)
    assert all(not s.injected for s in room.sessions.values())


def test_the_floor_is_granted_by_text_on_the_deployed_route():
    """169310c. Pad-and-commit yields an EMPTY response here -- the route has
    already consumed the participant's audio with a reply of its own that was
    dropped, so the buffer the pad commits holds only the pad. A text item is
    new content, and it is answered. The same recipe as open_scene."""
    assert grants_via_text_prompt(NATIVE) is True
    assert grants_via_text_prompt(GEMINI) is False

    async def body(room):
        await a_participant_speaks(room)
        await room.give_floor("dan")

    room = run_room(NATIVE, body)
    dan = room.sessions["dan"]
    assert dan.injected, "nothing was put in front of the member at all"
    assert dan.responses_requested >= 1, "the member was never asked to reply"
    assert dan.commits == 0, (
        "the deployed route was handed a commit, which answers with an empty "
        "response"
    )


def test_a_member_in_an_empty_auto_fired_reply_is_still_given_the_floor():
    """THE ONE THIS FILE EXISTS FOR.

    give_floor's first disjunct -- "is the gateway already producing this
    member's reply?" -- returned without sending anything, on every family. On a
    text-grant family that is exactly backwards: the reply the gateway started
    for itself is the one it DROPS, and this route emits many empty responses
    (bare response.created, no delta, so the runner's pump never even begins a
    hold). Driven live before the fix: the grant took 11.00 s, logged
    `fresh_reply_requested`, and put ZERO frames on the wire. The character was
    silent for the whole turn, and only a reply older than the 15 s autofire
    window escaped it.

    The code's own comment four lines below the branch already said this should
    not happen; only the second disjunct carried the exclusion."""
    async def body(room):
        await a_participant_speaks(room)
        # Exactly the state a bare response.created leaves behind: the gateway
        # says it is answering, moments ago, and nothing will ever arrive.
        rt = room.sessions["dan"]
        rt.autofire_active = True
        rt._last_output_at = time.time()
        await room.give_floor("dan")

    room = run_room(NATIVE, body)
    dan = room.sessions["dan"]
    assert dan.injected, (
        "a member sitting in an empty auto-fired reply was given the floor and "
        "sent nothing: silent for the whole turn"
    )
    assert dan.responses_requested >= 1

    # And the plain-flash behaviour is untouched, because there the auto-fired
    # reply is the one that arrives: touching it is what produced two replies
    # spoken onto the end of each other inside one recorded turn.
    before = None

    async def flash_body(room):
        nonlocal before
        await a_participant_speaks(room)
        rt = room.sessions["dan"]
        rt.autofire_active = True
        rt._last_output_at = time.time()
        before = (rt.commits, rt.responses_requested)
        await room.give_floor("dan")

    room = run_room(GEMINI, flash_body)
    dan = room.sessions["dan"]
    assert (dan.commits, dan.responses_requested) == before, (
        "plain flash was sent something on top of a reply already in flight"
    )
    assert room.autofire_grants == 1


# --------------------------------------------------------------------------
# 3. Casting, and the language the transcript is taken in.
# --------------------------------------------------------------------------

def test_gemini_voice_names_still_cast_on_the_gpt_fallback():
    """210fbfc. The scenario bank names Gemini voices; on the fallback route a
    bank entry is not a typo and a rejected voice takes the character brief down
    with it. Her map is the gpt row's voice_aliases now.

    Her "alloy" catch-all is deliberately NOT kept: an unrecognised name comes
    back untranslated so resolve_voice still refuses it BY NAME, rather than
    running a whole wave in a voice nobody chose."""
    assert voice_for_model("Puck", GPT) in capabilities_for(GPT).voices
    assert voice_for_model("Kore", GPT) in capabilities_for(GPT).voices
    # A leftover ElevenLabs id is the shape that has actually reached this code.
    leftover = "21m00Tcm4TlvDq8ikWAM"
    assert voice_for_model(leftover, GPT) == leftover, (
        "an unknown voice was silently translated to a real one; the wave then "
        "runs in a voice nobody chose"
    )
    # On its own family a Gemini name passes straight through.
    assert voice_for_model("Puck", GEMINI) == "Puck"


@pytest.mark.parametrize("model", [GEMINI, NATIVE, GPT])
def test_the_transcription_language_hint_rides_on_every_session(model, monkeypatch):
    """cabc1dd. Two live runs with an English-speaking participant produced a
    Russian word and a run of Japanese syllables before this existed. She set it
    on connect AND on update_instructions, in two copies; here it is built once,
    in _session_payload, which is what makes it true of every session including
    the mid-session re-briefs her copy did not cover.

    Blank still sends no hint at all, which is the pre-cabc1dd behaviour."""
    monkeypatch.setenv("TRANSCRIPTION_LANG", "en")
    sess = rt_mod.RealtimeVoiceSession(instructions="x", model=model)
    payload = sess._session_payload()
    tx = payload.get("input_audio_transcription")
    assert tx and tx.get("language") == "en", (
        f"{model}: no language hint on the session payload"
    )
    if is_openai_realtime(model):
        assert tx.get("model") == "whisper-1", (
            "the gpt route transcribes the participant with whisper-1 (210fbfc)"
        )
    monkeypatch.setenv("TRANSCRIPTION_LANG", "")
    sess = rt_mod.RealtimeVoiceSession(instructions="x", model=model)
    tx = (sess._session_payload() or {}).get("input_audio_transcription") or {}
    assert not tx.get("language"), "a blank TRANSCRIPTION_LANG still sent a hint"


def test_barge_in_ms_is_still_the_operators_name_for_the_barge_bar(monkeypatch):
    """Her knob. The bar is a PAIR here (a loudness gate beside the duration)
    and the shipped duration is 300 ms rather than her 600, but an operator who
    had exported BARGE_IN_MS must be changing something rather than setting an
    inert variable."""
    monkeypatch.setenv("BARGE_IN_MS", "600")
    monkeypatch.delenv("VAD_BARGE_MS", raising=False)
    reloaded = importlib.reload(rt_mod)
    try:
        assert reloaded.VAD_BARGE_MS == 600
    finally:
        monkeypatch.delenv("BARGE_IN_MS", raising=False)
        importlib.reload(rt_mod)


# --------------------------------------------------------------------------
# 4. What a reply may contain, and what counts as a participant turn.
# --------------------------------------------------------------------------

@pytest.mark.parametrize("reply,expected", [
    # The shape GroupRoom.tell actually injects, parroted back verbatim. Both
    # markers are in one string, so the alternation stops at the inner
    # sentence's full stop: the closing quote and bracket used to be left
    # behind and the turn was recorded starting `") Right, ...`.
    ('(Context, not for you to repeat: Priya just said out loud to the group: '
     '"We slipped.") Right, so where does that leave the launch?',
     'Right, so where does that leave the launch?'),
    ('Priya just said: "We slipped." Right, so where does that leave us?',
     'Right, so where does that leave us?'),
    ('(Context, not for you to repeat: Priya said out loud to the group: We '
     'slipped.) Right, so where next?',
     'Right, so where next?'),
    ('Alex said out loud to the group: we are behind. I think we should cut '
     'scope.', 'I think we should cut scope.'),
    ('Just a normal reply with no marker at all.',
     'Just a normal reply with no marker at all.'),
    # Seen live 2026-09-17 (docs/rooms-verification.md): the note paraphrased,
    # or opened and never closed. Nothing spoken survives it.
    ('(Context, not meant for you to repeat: Jordan just said out loud to the '
     'group: "I stopped the second pass.") What do you need from me?',
     'What do you need from me?'),
    ('(Alex speaks again: "You can say that again. Two people quit and '
     'leadership will not even discuss pay.', ''),
    ('(Casey just said "Sorry, but I think it\'s probably all of it.', ''),
])
def test_a_parroted_context_note_is_stripped_whole(reply, expected):
    """df1ab83. The native-audio route opens by reading the room's own note out
    loud. The room knows exactly what it told him, so the note comes off -- all
    of it, including the punctuation that closed it."""
    assert rvs._strip_context_echo(reply, []) == expected


@pytest.mark.parametrize("reply,expected", [
    ("(Casey pauses.) I agree, for now.", "I agree, for now."),
    ("(The meeting has ended. Alex and Casey have left. It is just the two of "
     "you in the room now.) I stopped doing the second pass three weeks ago.",
     "I stopped doing the second pass three weeks ago."),
    ("(The user hasn't spoken yet, I should wait for their response.)", ""),
    ("(Context, not meant for you to repeat:", ""),
    ("(Context, not", ""),
    ("I agree (for now) with the plan.", "I agree (for now) with the plan."),
])
def test_a_narrated_lead_in_comes_off_balanced_or_not(reply, expected):
    """2026-09-17, live S3A rooms and one-on-ones: a note the model opened and
    never closed used to survive as the caption, and the 1:1 path applied no
    strip at all."""
    assert rvs._strip_narration(reply) == expected


@pytest.mark.parametrize("text,is_direction", [
    ("[Priya remains quiet.]", True),
    ("[Silence]", True),
    ("(He says nothing.)", True),
    ("Priya remains quiet.", False),
    ("[Look] we need to ship this thing before the quarter ends, honestly.", False),
])
def test_a_stage_direction_is_not_a_reply(text, is_direction):
    """df1ab83. The model narrating instead of speaking. Treated as no reply, so
    the turn is not recorded as a line the participant heard."""
    assert rvs._is_stage_direction(text) is is_direction


class _Recorder:
    """Just enough session to drive _record_user_turn and read what it filed."""

    def __init__(self, *, room=None, model=GEMINI):
        self.events: list = []
        self.appended: list = []
        self.sent: list = []
        self.room = room
        self.rt = types.SimpleNamespace(model=model)
        self.segment = 0
        self._last_user_norm = ""
        self._last_user_at = 0.0
        self._last_user_text = ""
        self._user_utterances = 0
        self._recent_agent_texts: list = []
        store = types.SimpleNamespace(
            event=lambda name, **kw: self.events.append((name, kw)))
        self.session = types.SimpleNamespace(
            store=store,
            append_user=self.appended.append,
            broadcast=self._noop,
        )

    async def _noop(self, *a, **kw):
        return None

    async def _send(self, msg):
        self.sent.append(msg)

    def _second_transcript_source(self):
        return rvs.RealtimeVoiceSessionRunner._second_transcript_source(self)

    def record(self, text):
        asyncio.run(rvs.RealtimeVoiceSessionRunner._record_user_turn(self, text))

    @property
    def names(self):
        return [n for n, _ in self.events]


def test_the_near_duplicate_filter_runs_where_a_second_transcriber_exists():
    """Her rule: several sessions can transcribe one utterance with slightly
    different wording, so within 5 s a 60% word overlap is the same turn. It is
    real -- on a relay route a member hears only the participant and its
    transcription is a clean second source, which _pump_member forwards."""
    rec = _Recorder(room=object(), model=NATIVE)
    rec.record("I think the deadline slipped")
    rec.record("I think the deadline slipped a lot")
    assert "user_transcript_duplicate_dropped" in rec.names
    assert rec.appended == ["I think the deadline slipped"]


def test_the_near_duplicate_filter_does_not_run_where_it_can_only_delete_speech():
    """And nowhere else. On plain flash _pump_member throws its members' user
    transcripts away, so the scribe is the only channel and a duplicate cannot
    arise; all the rule could do there was delete a participant who builds on
    their own sentence. Probed live on both routes before this test existed."""
    for room, model in ((object(), GEMINI), (None, NATIVE), (None, GEMINI)):
        rec = _Recorder(room=room, model=model)
        rec.record("yes")
        rec.record("yes exactly")
        assert "user_transcript_duplicate_dropped" not in rec.names, (
            f"room={room is not None} model={model}: a real second utterance "
            f"was dropped as a duplicate on a channel that has no second source"
        )
        assert rec.appended == ["yes", "yes exactly"]


def test_an_echo_is_recorded_as_an_echo_and_not_as_a_duplicate():
    """Ordering. A line that is both an echo of a character and a near-match of
    the last participant turn is an ECHO: echo_dropped names the character it
    matched and is what a retranscribe pass adjudicates, while
    user_transcript_duplicate_dropped says only "we have seen this". The
    near-duplicate test ran first until 2026-09-15 and relabelled the more
    specific finding as the vaguer one."""
    # Eight words at least: under that, _is_echo leaves a line alone,
    # because echo and paraphrase are indistinguishable at that length.
    spoken = "we should probably cut the scope here before the quarter ends"
    rec = _Recorder(room=object(), model=NATIVE)
    # The participant said very nearly this a moment ago...
    rec.record(spoken + " now")
    # ...and a character has just finished saying it, over the speakers.
    rec._recent_agent_texts = [(time.time(), "dan", spoken)]
    rec.record(spoken)
    assert "echo_dropped" in rec.names, rec.names
    assert "user_transcript_duplicate_dropped" not in rec.names
    assert rec.appended == [spoken + " now"], (
        "the echo was recorded as a participant turn"
    )


# --------------------------------------------------------------------------
# 6. The hold-and-adopt turn machine, and the playback clock beside it.
#
# These are the rest of the answer to "nothing in the suite would notice if a
# later edit took one away". They are her mechanisms that live in the RUNNER
# rather than in the room: the per-member hold state, the grant path that reads
# it, the cancel that clears it when the participant starts a new sentence, the
# server-side playback clock the whole "how much did they HEAR" record is built
# on, the scribe repair, and the anti-dominance rotation.
#
# Every one of them was driven by hand during the merge and every one of them
# worked. None of them had a test until this section.
# --------------------------------------------------------------------------

class _Turn:
    """A runner, reduced to the fields the hold machine touches."""

    def __init__(self, *, speaking=None):
        self.events: list = []
        self.flushed: list = []
        self.finished: list = []
        self.cancelled: list = []
        self.sent: list = []
        self.segment = 0
        self._member_states: dict = {}
        self._speech_started_at = 0.0
        self._play_cursor = 0.0
        self._last_played = None
        self._sessions: dict = {}
        self.room = types.SimpleNamespace(
            speaking=speaking, session_for=self._session_for)
        store = types.SimpleNamespace(
            event=lambda name, **kw: self.events.append((name, kw)),
            append_assistant_audio=lambda *a, **kw: None,
        )
        self.session = types.SimpleNamespace(store=store)

    # -- what adopt_member / _cancel_stale_holds / _finish_interrupted call
    def _session_for(self, agent_id):
        return self._sessions.get(agent_id)

    def _resolve_agents(self):
        return [types.SimpleNamespace(id=a, name=a.title())
                for a in ("dan", "priya", "mel")]

    async def _flush_held(self, agent, st):
        self.flushed.append((agent.id, st.take_held()))
        st.mode = "live"

    async def _finish_live(self, agent, st):
        self.finished.append(agent.id)

    async def _finalize_member(self, agent, text, interrupted=False):
        self.finished.append((agent.id, text, interrupted))

    async def _send(self, msg):
        self.sent.append(msg)

    # -- the methods under test, unbound
    def adopt(self, agent_id):
        return asyncio.run(
            rvs.RealtimeVoiceSessionRunner.adopt_member(self, agent_id))

    def cancel_stale(self):
        asyncio.run(rvs.RealtimeVoiceSessionRunner._cancel_stale_holds(self))

    # The real one: _finish_interrupted calls it on self, so a wrapper under a
    # different name would leave that path measuring nothing.
    def _heard_seconds(self, st):
        return rvs.RealtimeVoiceSessionRunner._heard_seconds(self, st)

    heard = _heard_seconds

    def advance(self, agent_id, st, pcm):
        rvs.RealtimeVoiceSessionRunner._advance_play_cursor(
            self, agent_id, st, pcm)

    def finish_interrupted(self, agent, st):
        asyncio.run(
            rvs.RealtimeVoiceSessionRunner._finish_interrupted(self, agent, st))

    @property
    def names(self):
        return [n for n, _ in self.events]


class _Cancellable:
    def __init__(self, turn, agent_id):
        self._turn, self._id = turn, agent_id

    async def cancel_response(self):
        self._turn.cancelled.append(self._id)


def _holding(turn, agent_id, *, started_at=None, chunks=2):
    """Put a member in the state a suppressed reply leaves it in."""
    st = rvs._MemberState()
    st.begin_hold("resp_" + agent_id)
    st.hold_started_at = time.time() if started_at is None else started_at
    for _ in range(chunks):
        st.hold({"type": "agent_audio", "pcm": b"\x00" * 16000})
    turn._member_states[agent_id] = st
    turn._sessions[agent_id] = _Cancellable(turn, agent_id)
    return st


def test_a_held_reply_that_answers_the_current_turn_is_adopted():
    """Her half of turn-taking, and the half that SAVES a turn.

    Every member answers the participant at once; only the one with the floor
    may be heard, so the others are suppressed. Hers keeps the suppressed reply
    instead of throwing it away, and when the routing decision finally names
    that member the reply it already produced is played -- rather than the
    gateway being paid for a second generation of the same answer, several
    seconds later, into a conversation that has moved on.
    """
    turn = _Turn()
    turn._speech_started_at = time.time() - 5     # the participant spoke first
    st = _holding(turn, "priya")
    assert turn.adopt("priya") is True
    assert turn.flushed and turn.flushed[0][0] == "priya"
    assert len(turn.flushed[0][1]) == 2, "the held audio was not what was played"
    assert st.mode == "live"


def test_a_held_reply_that_predates_the_participants_turn_is_dropped_as_stale():
    """And the half that protects the record. A reply that BEGAN before the
    participant's current utterance is a reaction to a colleague, not an answer
    to the question just asked; adopting it puts a character's answer to the
    previous question into this one. hold_started_at against _speech_started_at
    is the whole test, and it is why the pump writes that timestamp.
    """
    turn = _Turn()
    now = time.time()
    turn._speech_started_at = now              # ...and the hold is older
    _holding(turn, "priya", started_at=now - 3)
    assert turn.adopt("priya") is False
    assert turn.flushed == []
    assert "stale_held_reply_dropped" in turn.names


def test_a_completed_held_reply_goes_stale_on_its_own_clock(monkeypatch):
    """held_done: the suppressed reply FINISHED before the floor reached it.
    Kept for HELD_REPLY_TTL so a slow routing decision can still play it, and
    dropped after, because a half-minute-old answer replayed into a live room
    is a character talking about something nobody is discussing any more.
    """
    monkeypatch.setenv("HELD_REPLY_TTL", "12")
    turn = _Turn()
    turn._speech_started_at = time.time() - 5
    st = _holding(turn, "dan")
    st.mode, st.done_at = "held_done", time.time()
    assert turn.adopt("dan") is True
    assert turn.finished == ["dan"], "a completed reply was not closed off"

    turn = _Turn()
    turn._speech_started_at = time.time() - 5
    st = _holding(turn, "dan")
    st.mode, st.done_at = "held_done", time.time() - 30
    assert turn.adopt("dan") is False
    assert "unsolicited_response_suppressed" in turn.names


def test_a_new_utterance_cancels_every_non_floor_members_reply():
    """The one that keeps the DEPLOYED route answering at all.

    On native-audio a member whose response is still active when the
    participant speaks never replies to the new turn: the speech is absorbed
    into the running response and the fallback request comes back empty. So the
    stale hold is cancelled AT THE BRIDGE, not just dropped locally -- that is
    the difference between a character answering the next question and never
    answering again. The floor holder is left alone: it is the one the
    participant is actually interrupting, and the barge-in path owns it.
    """
    turn = _Turn(speaking="dan")
    _holding(turn, "dan")
    _holding(turn, "priya")
    _holding(turn, "mel")
    turn.cancel_stale()
    assert sorted(turn.cancelled) == ["mel", "priya"], (
        "the floor holder was cancelled, or a bystander was not"
    )
    assert turn._member_states["dan"].mode == "holding"
    for aid in ("priya", "mel"):
        assert turn._member_states[aid].mode == "discarding"
        assert turn._member_states[aid].held == []
    dropped = [kw["agent_id"] for n, kw in turn.events
               if n == "hold_cancelled_participant_speaking"]
    assert sorted(dropped) == ["mel", "priya"]


def test_the_playback_clock_measures_what_was_heard_not_what_was_sent():
    """THE SERVER-SIDE PLAYBACK CLOCK (169310c), which has no counterpart in
    ours and answers a question ours could not.

    The gateway delivers a reply many times faster than real time, so "sent"
    and "heard" are different quantities and the gap is most of a turn. Without
    this, a participant talking over a line that finished on the server long
    ago in stream time is invisible: no event, no truncation, and a rater reads
    a participant's reply to a line they were still hearing. 16 kHz mono PCM16
    is 32000 bytes a second, and that arithmetic is what the whole
    heard_seconds record rests on.
    """
    turn = _Turn()
    st = rvs._MemberState()
    before = time.time()
    turn.advance("dan", st, b"\x00" * 64000)     # exactly two seconds of audio
    assert turn._play_cursor - before == pytest.approx(2.0, abs=0.3)
    assert turn._play_cursor > time.time(), (
        "the page is still playing and the clock says it is not: this is the "
        "condition the playback_cut branch reads"
    )
    # A second chunk queues BEHIND the first rather than restarting the clock.
    turn.advance("dan", st, b"\x00" * 64000)
    assert turn._play_cursor - before == pytest.approx(4.0, abs=0.3)
    assert turn._last_played["agent_id"] == "dan"
    # Two seconds into a four-second line, two of them have been heard.
    st.play_start = time.time() - 2.0
    st.play_end = time.time() + 2.0
    assert turn.heard(st) == pytest.approx(2.0, abs=0.3)


def test_an_interrupted_line_is_recorded_as_far_as_it_was_heard():
    """heard_seconds, spent. Text streams well ahead of audio, so the buffer
    holds the whole sentence while only its first seconds were played. The
    record keeps roughly the words that fit in the audio (~2.5/s) so a
    character is not credited with lines nobody heard -- and the event carries
    heard_seconds BESIDE the turn, which is what lets a rater tell a truncated
    DELIVERY from a bad reply.
    """
    turn = _Turn()
    st = rvs._MemberState()
    st.play_start = time.time() - 2.0            # two seconds heard...
    st.play_end = time.time() + 8.0              # ...of a ten-second line
    st.text = ["We slipped the date and I think we have to say so, ",
               "because the alternative is telling them in December."]
    agent = types.SimpleNamespace(id="priya", name="Priya")
    turn.finish_interrupted(agent, st)
    kw, = [k for n, k in turn.events if n == "assistant_interrupted"]
    assert kw["agent_id"] == "priya"
    assert kw["heard_seconds"] == pytest.approx(2.0, abs=0.4)
    (aid, text, interrupted), = turn.finished
    assert aid == "priya" and interrupted is True
    assert text.endswith("…"), "the unheard tail was credited to the character"
    assert len(text.split()) <= 7, text


def test_a_scribe_that_went_quiet_is_replaced_in_kind():
    """The SECOND way a scribe dies -- alive, healthy on every surface, and
    simply no longer emitting transcripts (seen after about six turns on
    native-audio). The scribe is the only participant channel in a room, so
    this is the difference between an encounter with a transcript and
    three quarters of an hour of perfect audio with none.

    Two things about the replacement, both of which cost a run before they were
    fixed: it is built through the room's own factory, so it is the same KIND
    of session on the same model (the scribe hardcoded to "Puck" is exactly the
    one that lost its whole brief on gpt-realtime-2.1 and answered the
    participant out loud), and the fan-out bookkeeping is reset with it, or
    close_participant_turn commits a buffer holding nothing.
    """
    for model in (GEMINI, GPT, NATIVE):
        holder = {}

        async def body(room):
            await a_participant_speaks(room)
            old = room.scribe
            holder["pair"] = (old, await room.reopen_scribe())

        room = run_room(model, body)
        old, new = holder["pair"]
        assert new is not None and new is not old
        assert room.scribe is new
        assert old.closed, f"{model}: the dead scribe's socket was left open"
        assert new.model == model, f"{model}: the replacement is not in kind"
        assert capabilities_for(model).accepts_voice(new.voice), (
            f"{model}: the replacement took a voice this family refuses, and a "
            f"refused voice takes the whole session.update -- 'Never speak' "
            f"included -- down with it"
        )
        assert "Never speak" in new.instructions
        assert room._fanned_to_scribe == 0 and room._scribe_heard_speech is False, (
            f"{model}: the new scribe inherited the old one's fan-out count, so "
            f"it is described as having heard speech it was never sent"
        )
        assert room.scribe_reopens == 1


def _complete_group_replies(runner, room, monkeypatch):
    """Deliver a reply through production finalization after each floor grant.

    The imported direction-delivery harness only signals response_done; it
    never emits speech. Speaker bookkeeping belongs to finalization, so tests
    of who actually spoke must supply that missing gateway response.
    """
    monkeypatch.setenv("ROUTE_TRANSCRIPT_WAIT", "0.01")
    monkeypatch.setenv("AUTOFIRE_WAIT", "0.01")
    agents = {agent.id: agent for agent in runner._resolve_agents()}
    grants = []

    async def give_floor(agent_id):
        # Bypass the harness's completion signal: the real finalizer must be
        # what releases the turn, just as it is when gateway speech arrives.
        rt = await type(room).give_floor(room, agent_id)
        grants.append(agent_id)
        if rt is not None:
            await runner._finalize_member(
                agents[agent_id], "We should confirm the date.", audio_bytes=96000,
            )
        return rt

    monkeypatch.setattr(room, "give_floor", give_floor)
    return grants


def test_the_director_may_not_hand_one_character_every_unnamed_turn(monkeypatch):
    """Anti-dominance (bb3fa13). Absent a direct address the room prefers a
    candidate who did not just speak, and when the director's ONLY candidate is
    the character who just spoke it rotates to the next member instead.

    Not a style preference: in production the director handed Dan four of five
    unnamed turns, and dominance is a measured quantity in this study. The
    rotation is recorded as its own event so the analysis can tell a rotated
    turn from a routed one.
    """
    runner, session, room = _runner_with_room(
        GPT, [{"agent_id": "dan", "intent": "Push back on the date."}],
    )
    grants = _complete_group_replies(runner, room, monkeypatch)
    runner._last_group_speaker = "dan"           # ...and Dan is the only pick
    asyncio.run(asyncio.wait_for(runner._run_group_turn(), timeout=10))

    rotated = session.store.of("dominance_rotated")
    assert rotated, (
        "the director's single candidate had just spoken and was handed the "
        "floor again: this is the four-turns-in-five shape"
    )
    assert rotated[0]["from_agent"] == "dan"
    assert rotated[0]["to_agent"] != "dan"
    spoken = [row["agent_id"] for row in session.store.of("assistant_turn")]
    assert spoken == grants
    assert spoken[0] == rotated[0]["to_agent"]
    # Rotation chooses the first response. The director's ordered follow-up
    # may still be Dan; the last speaker must reflect who actually finished.
    assert spoken == ["priya", "dan"]
    assert runner._last_group_speaker == spoken[-1]


def test_a_character_who_did_not_just_speak_is_left_alone(monkeypatch):
    """The other direction, and the reason the rotation is a preference rather
    than a rule: a genuine routing decision to someone else must not be
    disturbed, or the director stops deciding anything at all.
    """
    runner, session, room = _runner_with_room(
        GPT, [{"agent_id": "priya", "intent": "Ask what the deadline was."}],
    )
    grants = _complete_group_replies(runner, room, monkeypatch)
    runner._last_group_speaker = "dan"
    asyncio.run(asyncio.wait_for(runner._run_group_turn(), timeout=10))
    assert session.store.of("dominance_rotated") == []
    assert grants == ["priya"]
    assert [row["agent_id"] for row in session.store.of("assistant_turn")] == grants
    assert runner._last_group_speaker == "priya"


@pytest.mark.parametrize("grant_fails", [False, True])
def test_a_grant_without_a_finalized_reply_does_not_change_the_last_speaker(
        monkeypatch, grant_fails):
    runner, session, room = _runner_with_room(
        GPT, [{"agent_id": "priya", "intent": "Ask what the deadline was."}],
    )
    monkeypatch.setenv("ROUTE_TRANSCRIPT_WAIT", "0.01")
    monkeypatch.setenv("AUTOFIRE_WAIT", "0.01")
    if grant_fails:
        async def fail_grant(agent_id):
            return None
        monkeypatch.setattr(room, "give_floor", fail_grant)
    # Otherwise use the original harness, which signals completion without
    # finalizing any speech. Neither case justifies recording a new speaker.
    runner._last_group_speaker = "dan"
    asyncio.run(asyncio.wait_for(runner._run_group_turn(), timeout=10))
    assert session.store.of("assistant_turn") == []
    assert runner._last_group_speaker == "dan"
    assert bool(session.store.of("floor_grant_failed")) is grant_fails


@pytest.mark.parametrize("reply,expected", [
    ("(Casey responds with concerns.) If the roadmap is the priority, say so.",
     "If the roadmap is the priority, say so."),
    ("[Priya hesitates.] We need to confirm the date.",
     "We need to confirm the date."),
    ("(Casey pauses.) I agree (for now)", "I agree (for now)"),
    ("(Priya hesitates.) We need time. (She looks down.)",
     "We need time. (She looks down.)"),
    ('(Context, not for you to repeat: Dan just said out loud to the group: '
     '"We slipped.") (Priya hesitates.) What is the revised date?',
     "What is the revised date?"),
    ("We need to confirm the date.", "We need to confirm the date."),
])
def test_a_narrated_lead_in_is_removed_from_the_recorded_group_reply(reply, expected):
    runner, session, room = _runner_with_room(GPT, [])
    agent = next(a for a in runner._resolve_agents() if a.id == "priya")
    room.speaking = agent.id
    asyncio.run(runner._finalize_member(agent, reply, audio_bytes=128000))
    (turn,) = session.store.of("assistant_turn")
    assert turn["text"] == expected
    assert session.shared_history[-1] == {"speaker": "priya", "text": expected}
    assert runner._response_done.is_set()


def test_a_standalone_stage_direction_keeps_its_diagnostic_when_finalized():
    runner, session, room = _runner_with_room(GPT, [])
    agent = next(a for a in runner._resolve_agents() if a.id == "priya")
    room.speaking = agent.id
    asyncio.run(runner._finalize_member(agent, "[Priya remains quiet.]"))
    (diagnostic,) = session.store.of("stage_direction_output")
    assert diagnostic["text"] == "[Priya remains quiet.]"
    (turn,) = session.store.of("assistant_turn")
    assert turn["text"] == ""
    assert turn["transcript_missing"] is True
    assert all(row["speaker"] != "priya" for row in session.shared_history)
