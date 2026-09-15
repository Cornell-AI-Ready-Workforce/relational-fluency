"""The record seam: a fault the runner wrote down must reach the person reading.

Three failures of the same shape, all of them a fact the runner recorded and
`encounter_record.build` did not read, so the artefact a rater is handed states
something false with confidence:

  * a participant turn the transcriber garbled into another script, presented as
    ordinary participant speech;
  * an offline re-transcription certified as "repaired" from the existence of a
    file, while every consumer rebuilt from events.jsonl and saw the raw text;
  * the loss of a group room's only participant transcription channel, which
    leaves a run of agent turns nobody answers and reads as a participant who
    went quiet.

The governing rule, which encounter_record's own docstrings already state for
the video channel: an artefact must never convert a recoverable fault into a
permanent silence. A rater must be able to tell "the participant said little"
from "we failed to hear the participant".

Hermetic. Synthetic session directories written in the shapes the real writers
emit (realtime_voice_session's `_record_user_turn` and `_finalize_member_inner`,
`_pump_scribe`'s finally block, retranscribe's output file), with SESSIONS_DIR
pointed at a tmp_path. No fixture wave required.

No network either, and that is now arranged rather than assumed. Every encounter
here is built without a webcam recording, so every rater_packet.build() below
asks video.exists(), which for an encounter with no local file is a HEAD against
the real Cornell study bucket. The `sessions_root` fixture puts tests/
conftest.py's `offline_bucket` stub in front of it.

This module used also to set three AWS credentials with os.environ.setdefault in
its body — for a presigned playback URL that no longer exists. os.environ is the
process, not the module, so from the moment pytest IMPORTED this file those
credentials were live for every later test in the run, and the HEADs above were
signed with them and sent. Credentials belong to the tests that need them.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from server import (  # noqa: E402
    encounter_record, rater_packet, retranscribe, verify_record, video,
)

SESSION_ID = "s_1772546373_31268f"

# Real speech, written out in the wrong script. This is the shape
# _script_mismatch flags: the model understood the audio, only the caption is
# wrong, so the text is not gibberish the participant produced.
GARBLED = "रिवेरा की टीम ने हाइब्रिड रखा क्योंकि उन्होंने केस बनाया"


# --------------------------------------------------------------------------
# synthetic encounters
# --------------------------------------------------------------------------

def _base_events():
    return [
        {"t": 0.0, "type": "session_start", "scenario": None,
         "participant_id": "p_test", "cohort": "study",
         "cast": [{"id": "mel", "name": "Mel"}, {"id": "drew", "name": "Drew"}]},
        {"t": 0.1, "type": "realtime_session_started",
         "gateway": "https://gateway.invalid", "model": "test-model"},
    ]


def _agent_pair(t, agent_id="mel", text="Say more about the handoff.",
                **extra):
    """A steering_pair in the shape _finalize_member_inner writes."""
    ev = {
        "t": t, "type": "steering_pair",
        "actor": {"agent_id": agent_id, "text": text, "voice": "Leda",
                  "transcript_missing": False, "interrupted": False},
        "direction": {"stage_direction": "press on the handoff", "segment": 0,
                      "interaction": "i1"},
    }
    ev.update(extra)
    return ev


def _write_session(root: Path, events, *, session_id: str = SESSION_ID,
                   hq: object = None) -> Path:
    sdir = root / session_id
    sdir.mkdir(parents=True, exist_ok=True)
    with (sdir / "events.jsonl").open("w", encoding="utf-8") as fh:
        for e in events:
            fh.write(json.dumps(e, ensure_ascii=False) + "\n")
    (sdir / "manifest.json").write_text(
        json.dumps({"session_id": session_id, "started_at": 1772546373.0,
                    "ended_at": 1772546373.0 + 300, "status": "closed",
                    "n_turns": 4}),
        encoding="utf-8",
    )
    if hq is not None:
        # retranscribe.py's output file, written verbatim if a string so a
        # corrupt cache can be exercised.
        (sdir / "transcript_participant_hq.json").write_text(
            hq if isinstance(hq, str) else json.dumps(hq, ensure_ascii=False),
            encoding="utf-8",
        )
    return sdir


def _garbled_encounter():
    return _base_events() + [
        {"t": 12.0, "type": "user_turn", "text": "We kept the hybrid setup.",
         "channel": "voice", "script_mismatch": False},
        _agent_pair(20.0),
        {"t": 31.0, "type": "user_turn", "text": GARBLED,
         "channel": "voice", "script_mismatch": True},
        _agent_pair(44.0, agent_id="drew"),
    ]


def _scribe_lost_encounter(*, restored: bool = False):
    """A group encounter whose scribe pump died at t=150.

    Exactly what the runner writes: voice_error(where=scribe), then
    scribe_pump_ended from the finally block, then every later steering_pair
    stamped participant=None / participant_channel='lost'.
    """
    evs = _base_events() + [
        {"t": 20.0, "type": "group_room_opened", "agents": ["mel", "drew"]},
        {"t": 30.0, "type": "user_turn", "text": "I want to hear Drew first.",
         "channel": "voice", "script_mismatch": False},
        _agent_pair(60.0, participant="I want to hear Drew first.",
                    participant_channel="ok"),
        {"t": 149.0, "type": "voice_error", "where": "scribe",
         "message": "connection closed"},
        {"t": 150.0, "type": "scribe_pump_ended", "segment": 0,
         "interaction": "i1"},
        _agent_pair(170.0, agent_id="drew", text="So what would you do?",
                    participant=None, participant_channel="lost"),
        _agent_pair(210.0, text="Nobody is hearing an answer.",
                    participant=None, participant_channel="lost"),
    ]
    if restored:
        evs += [
            {"t": 240.0, "type": "group_room_opened", "agents": ["mel", "drew"]},
            {"t": 250.0, "type": "user_turn", "text": "Sorry, I was saying...",
             "channel": "voice", "script_mismatch": False},
            _agent_pair(260.0, participant="Sorry, I was saying...",
                        participant_channel="ok"),
        ]
    return evs


# RUN_CODE_SECRET is pinned by an autouse fixture in tests/conftest.py, for the
# whole suite, in place of the os.environ.setdefault that used to sit here. Left
# unpinned, runs._run_code_secret mints a real 32-byte key and persists it to
# DATA_DIR/.run_code_secret as a side effect of running the tests.


@pytest.fixture
def sessions_root(tmp_path, monkeypatch, offline_bucket):
    """A private SESSIONS_DIR, patched into every module that resolved it.

    `offline_bucket` (tests/conftest.py) is not optional. None of these
    encounters has a webcam recording, and the packet decides whether there is
    anything to play by asking storage, so without the stub every test in this
    file that builds a packet signs a HEAD to relational-fluency-study-data —
    from a test about transcription markers.
    """
    root = tmp_path / "sessions"
    root.mkdir()
    monkeypatch.setattr(rater_packet, "SESSIONS_DIR", root)
    monkeypatch.setattr(video, "SESSIONS_DIR", root)
    return root


def _check(checks, label):
    """One named check from verify(), as (ok, detail)."""
    for ok, name, detail in checks:
        if name == label:
            return ok, detail
    raise AssertionError(f"no check named {label!r} in {[c[1] for c in checks]}")


# --------------------------------------------------------------------------
# 1. a garbled participant turn
# --------------------------------------------------------------------------

def test_a_garbled_participant_turn_keeps_its_flag_in_the_record(sessions_root):
    """The flag the runner wrote has to survive the one hop to the record.

    build() copied t/role/text and nothing else, so a turn the platform already
    knew was a transcription failure arrived at every consumer indistinguishable
    from speech.
    """
    sdir = _write_session(sessions_root, _garbled_encounter())
    record = encounter_record.build(sdir)

    turns = [t for t in record["transcript"] if t["role"] == "participant"]
    assert [t["script_mismatch"] for t in turns] == [False, True]
    assert record["counts"]["script_mismatch_turns"] == 1


def test_a_garbled_turn_reaches_the_rater_as_a_marked_turn(sessions_root):
    """The rater is the reason the flag exists.

    22 ESCI items are scored against this text. Unmarked, the rater reads a
    person who said something incoherent; marked, they read a transcript the
    platform cannot vouch for and reach for the N/A option the scale note
    already tells them about.
    """
    _write_session(sessions_root, _garbled_encounter())
    packet = rater_packet.build(SESSION_ID)

    garbled = [t for t in packet["transcript"] if t["text"] == GARBLED]
    assert len(garbled) == 1
    turn = garbled[0]
    assert turn["script_mismatch"] is True
    assert turn["note"] == rater_packet.NOTE_SCRIPT_MISMATCH
    assert packet["counts"]["script_mismatch_turns"] == 1

    # And the marker is not sprayed over clean turns, which would make it noise.
    clean = [t for t in packet["transcript"]
             if t["role"] == "participant" and t["text"] != GARBLED]
    assert clean and all(t["note"] is None for t in clean)


def test_an_encounter_with_no_garbled_turn_says_so(sessions_root):
    sdir = _write_session(sessions_root, _base_events() + [
        {"t": 12.0, "type": "user_turn", "text": "That is fair.",
         "channel": "voice", "script_mismatch": False},
        _agent_pair(20.0),
    ])
    record = encounter_record.build(sdir)
    assert record["counts"]["script_mismatch_turns"] == 0
    ok, detail = _check(verify_record.verify(sdir)[1],
                        "participant transcript script")
    assert ok and detail == "all turns"


# --------------------------------------------------------------------------
# 2. the offline repair
# --------------------------------------------------------------------------

HQ = {"source": "user_audio.wav", "model": "test-transcriber",
      "duration_s": 300.0, "text": "Rivera's team kept the hybrid setup."}


def test_the_offline_repair_survives_every_rebuild(sessions_root):
    """retranscribe folded the repair into record.json; the next write erased it.

    build() is the single reader every consumer goes through, so sourcing the
    repair from retranscribe's own file — rather than from a record.json that
    the video-confirm rebuild and any re-close replace wholesale — is what makes
    it survive at all.
    """
    sdir = _write_session(sessions_root, _garbled_encounter(), hq=HQ)

    assert encounter_record.build(sdir)["participant_transcript_hq"] == HQ

    # The rebuild path that used to clobber it: write() replaces record.json.
    encounter_record.write(sdir)
    on_disk = json.loads((sdir / "record.json").read_text(encoding="utf-8"))
    assert on_disk["participant_transcript_hq"] == HQ
    encounter_record.write(sdir)
    on_disk = json.loads((sdir / "record.json").read_text(encoding="utf-8"))
    assert on_disk["participant_transcript_hq"] == HQ, (
        "a second rebuild erased the repair again"
    )


def test_the_script_check_passes_only_once_the_record_carries_the_repair(sessions_root):
    sdir = _write_session(sessions_root, _garbled_encounter())
    ok, detail = _check(verify_record.verify(sdir)[1],
                        "participant transcript script")
    assert not ok and "run retranscribe" in detail

    (sdir / "transcript_participant_hq.json").write_text(
        json.dumps(HQ), encoding="utf-8")
    ok, detail = _check(verify_record.verify(sdir)[1],
                        "participant transcript script")
    assert ok and "carried in the record" in detail


@pytest.mark.parametrize("hq, why, says", [
    ("{not json at all", "a cache killed mid-write is not a repair",
     "not a readable transcript"),
    ({"source": "user_audio.wav", "text": ""},
     "an empty transcript repairs nothing", "produced no text"),
    ({"source": "user_audio.wav", "text": "   "},
     "whitespace repairs nothing", "produced no text"),
])
def test_a_file_that_reaches_no_reader_is_not_a_repair(sessions_root, hq, why, says):
    """The defect, stated as a test: the check used to stat the file.

    A file on disk and a record a rater can read are different claims. This is
    the case that separates them — the file exists, and the built record carries
    nothing — and the old check reported PASS 'repaired by retranscribe' for it.

    `says` is the second half of it. All three of these once printed the same
    line, which named the plumbing between retranscribe and the record; only one
    of the three is actually about that plumbing, and for the other two the line
    pointed the operator at a subsystem that is working. A permanently red check
    whose message names the wrong cause is worse than no check, because the
    operator learns to scroll past it. Each cause now names itself and the way
    out of it.
    """
    sdir = _write_session(sessions_root, _garbled_encounter(), hq=hq)

    assert encounter_record.build(sdir)["participant_transcript_hq"] is None
    ok, detail = _check(verify_record.verify(sdir)[1],
                        "participant transcript script")
    assert not ok, why
    assert says in detail

    # And the rater still sees the raw turn, marked, rather than a clean one.
    packet = rater_packet.build(SESSION_ID)
    assert packet["counts"]["script_mismatch_turns"] == 1


def test_an_empty_hq_cache_names_the_cause_and_the_way_out_of_it(sessions_root):
    """The latched FAIL, which no amount of doing what it says can clear.

    retranscribe() returns an existing cache verbatim unless --force is passed,
    so a cache that is valid JSON with no text in it makes this check fail
    forever: re-running retranscribe re-reads the same empty cache, makes no HTTP
    call, and changes nothing. The message therefore has to say what actually
    happened — the re-transcription produced nothing — and name the one flag that
    gets past the cache, rather than describing a plumbing gap between two
    components that are both doing their job.
    """
    sdir = _write_session(
        sessions_root, _garbled_encounter(),
        hq={"source": "user_audio.wav", "model": "test-transcriber",
            "duration_s": 300.0, "text": "  \n "},
    )
    ok, detail = _check(verify_record.verify(sdir)[1],
                        "participant transcript script")
    assert not ok
    assert "--force" in detail, "the message must name the flag that clears it"
    assert "produced no text" in detail
    assert "reaches no reader" not in detail, (
        "this is not the plumbing gap; the plumbing delivered an empty repair"
    )


def test_retranscribe_does_not_count_an_empty_cache_as_a_transcription(
        sessions_root, monkeypatch, capsys):
    """The same false statement on the operator's other surface.

    The CLI printed `300.0s -> 0 chars` for this and added it to the "N
    transcribed" total, so a wave whose transcriptions all came back empty
    reported itself as done. A cache written by an older build (or by hand) can
    also be missing `duration_s` outright, and indexing it killed the whole
    --all loop at the first such session, taking every later encounter with it.
    """
    _write_session(sessions_root, _garbled_encounter(),
                   hq={"source": "user_audio.wav", "text": ""})
    monkeypatch.setattr(retranscribe, "SESSIONS_DIR", sessions_root)

    rc = retranscribe.main([SESSION_ID])
    out = capsys.readouterr().out

    assert rc == 0
    assert "0 transcribed" in out, "an empty cache is not a transcription"
    assert "--force" in out


# --------------------------------------------------------------------------
# 3. losing the participant transcription channel
# --------------------------------------------------------------------------

def test_a_lost_participant_channel_is_stated_in_the_record(sessions_root):
    """Per-turn and at the top, because two different readers ask.

    A rater walks the transcript; a wave-level check asks the encounter one
    question. Neither could get an answer before: the record held a run of agent
    turns with no participant between them and no field saying why.
    """
    sdir = _write_session(sessions_root, _scribe_lost_encounter())
    record = encounter_record.build(sdir)

    channel = record["participant_channel"]
    assert channel["state"] == "lost"
    assert channel["lost_at"] == 150.0
    assert channel["restored_at"] is None
    assert channel["untranscribed_s"] == 60.0   # 150.0 -> the last turn at 210.0

    after = [t for t in record["transcript"]
             if t["role"] == "agent" and (t["t"] or 0) > 150.0]
    assert after and all(t["participant_channel"] == "lost" for t in after)
    before = [t for t in record["transcript"]
              if t["role"] == "agent" and (t["t"] or 0) < 150.0]
    assert before and all(t["participant_channel"] == "ok" for t in before)
    assert record["counts"]["unheard_turns"] == 2


def test_the_rater_is_told_the_participant_stopped_being_heard(sessions_root):
    """The distinction the whole packet exists to preserve.

    Two agent turns with no participant reply between them is a rateable
    behaviour on several ESCI items. It is also what a dead transcription
    channel looks like, and only one of those is a finding about the
    participant.
    """
    _write_session(sessions_root, _scribe_lost_encounter())
    packet = rater_packet.build(SESSION_ID)

    unheard = [t for t in packet["transcript"] if t["participant_channel_lost"]]
    assert len(unheard) == 2
    assert unheard[0]["note"] == rater_packet.NOTE_CHANNEL_LOST
    # Said once, at the turn where the loss begins: a paragraph repeated down
    # the rest of the transcript is a paragraph nobody reads.
    assert unheard[1]["note"] is None
    assert packet["counts"]["unheard_turns"] == 2
    # The count is what lets a console warn at the top instead of hoping the
    # rater notices a marker two thirds of the way down.
    assert packet["counts"]["participant_turns"] == 1


def test_verify_fails_an_encounter_whose_participant_channel_ended_early(sessions_root):
    sdir = _write_session(sessions_root, _scribe_lost_encounter())
    ok, checks = verify_record.verify(sdir)

    channel_ok, detail = _check(checks, "participant channel")
    assert not channel_ok
    assert "LOST at 150.0s" in detail and "never recovered" in detail
    assert not ok

    # The check that used to pass instead, and why it was not enough: three
    # turns arrived before the channel died, so "are there participant turns?"
    # answers yes for an encounter whose last third has none.
    assert _check(checks, "participant transcript")[0] is True


def test_a_recovered_channel_is_reported_as_recovered_not_as_intact(sessions_root):
    """A fresh room brings a fresh scribe, so the hole is bounded — not absent.

    The turns inside it were still never transcribed, so this fails too; the
    detail is what tells the researcher it is forty seconds rather than four
    minutes and therefore whether the encounter is worth repairing.
    """
    sdir = _write_session(sessions_root, _scribe_lost_encounter(restored=True))
    record = encounter_record.build(sdir)
    assert record["participant_channel"]["state"] == "restored"
    assert record["participant_channel"]["restored_at"] == 240.0
    assert record["participant_channel"]["untranscribed_s"] == 90.0

    ok, detail = _check(verify_record.verify(sdir)[1], "participant channel")
    assert not ok
    assert "recovered at 240.0s" in detail


def test_an_intact_channel_raises_nothing(sessions_root):
    """The other half of a useful check: no false alarm on a healthy encounter.

    1:1 encounters have no separate transcription channel to lose, so their
    steering pairs carry no participant_channel at all — that null must not read
    as a loss.
    """
    sdir = _write_session(sessions_root, _garbled_encounter())
    record = encounter_record.build(sdir)
    assert record["participant_channel"]["state"] == "ok"
    assert record["participant_channel"]["untranscribed_s"] is None
    assert record["counts"]["unheard_turns"] == 0
    assert all(t.get("participant_channel") is None
               for t in record["transcript"] if t["role"] == "agent")

    ok, detail = _check(verify_record.verify(sdir)[1], "participant channel")
    assert ok and detail == "intact"

    packet = rater_packet.build(SESSION_ID)
    assert packet["counts"]["unheard_turns"] == 0
    assert all(t["participant_channel_lost"] is False
               for t in packet["transcript"])


# --------------------------------------------------------------------------
# 3b. the loss that ENDS the encounter
#
# Every fixture above has agent turns after the loss, and every statement the
# record makes about the loss was measured from them. The encounter that
# collapsed at the loss has none — neither does any loss in the final minute —
# and that is the encounter a wave-level report most needs to be loud about.
# --------------------------------------------------------------------------

def _scribe_lost_at_the_end(*, session_end=300.0):
    """A group encounter whose trail ends at the loss.

    Same writers as `_scribe_lost_encounter`, stopped one event earlier: the
    scribe pump's finally block fires and nothing else is ever recorded, because
    the encounter is over. `session_end` is what SessionRegistry.drop writes
    (server/session.py) on the way out of the participant websocket handler, on
    the same elapsed clock as every other `t`. Pass None for the case where it
    is genuinely absent: a hard process kill, where no finally block runs.
    """
    evs = _base_events() + [
        {"t": 20.0, "type": "group_room_opened", "agents": ["mel", "drew"]},
        {"t": 30.0, "type": "user_turn", "text": "I want to hear Drew first.",
         "channel": "voice", "script_mismatch": False},
        _agent_pair(60.0, participant="I want to hear Drew first.",
                    participant_channel="ok"),
        {"t": 149.0, "type": "voice_error", "where": "scribe",
         "message": "connection closed"},
        {"t": 150.0, "type": "scribe_pump_ended", "segment": 0,
         "interaction": "i1"},
    ]
    if session_end is not None:
        evs.append({"t": session_end, "type": "session_end", "n_turns": 1})
    return evs


def test_a_loss_with_no_turn_after_it_is_sized_against_the_end_of_the_session(
        sessions_root):
    """Two and a half minutes of unheard participant, reported as 0.0 seconds.

    The hole used to be measured against the last recorded TURN, which in this
    encounter is 90 seconds before the loss — so max(0, last_turn - lost_at)
    floored to zero and the record stated, with confidence, that nothing was
    missed. session_end dates the end of the encounter on the same clock, and it
    is in events.jsonl for every session that closed at all.
    """
    sdir = _write_session(sessions_root, _scribe_lost_at_the_end())
    channel = encounter_record.build(sdir)["participant_channel"]

    assert channel["state"] == "lost"
    assert channel["lost_at"] == 150.0
    assert channel["untranscribed_s"] == 150.0

    ok, detail = _check(verify_record.verify(sdir)[1], "participant channel")
    assert not ok
    assert "the last 150.0s" in detail
    assert "the last 0.0s" not in detail, (
        "reassurance printed about the worst encounter in the wave"
    )


def test_a_loss_with_no_session_end_says_the_size_is_unknown_not_zero(
        sessions_root):
    """A hard kill runs no finally block, so there is no session_end to measure
    against — and the honest answer is that the size of the hole cannot be
    established, not that it was zero. The check still fails either way; what
    changes is whether the line it prints is true.
    """
    sdir = _write_session(sessions_root,
                          _scribe_lost_at_the_end(session_end=None))
    channel = encounter_record.build(sdir)["participant_channel"]

    assert channel["state"] == "lost"
    assert channel["lost_at"] == 150.0
    assert channel["untranscribed_s"] is None

    ok, detail = _check(verify_record.verify(sdir)[1], "participant channel")
    assert not ok
    # "the last 0.0s", not "0.0s": the loss timestamp itself is 150.0s, and the
    # thing that must not appear is the sentence claiming a duration.
    assert "the last 0.0s" not in detail
    assert "None" not in detail, "an unknown must not be printed as a value"
    assert "cannot be established" in detail


def test_the_rater_is_told_about_a_loss_that_left_no_turn_to_mark(sessions_root):
    """The packet surfaced the loss only through the turns that came after it.

    There are none here, so the rater was handed a transcript that simply stops
    — the exact reading ("the participant disengaged") the channel note exists to
    prevent — while record["participant_channel"]["state"] said "lost" the whole
    time. The note goes on the last turn that was still being heard, which is
    where a rater reading down the transcript arrives at the silence.
    """
    _write_session(sessions_root, _scribe_lost_at_the_end())
    packet = rater_packet.build(SESSION_ID)

    # Unchanged and still honest: no agent turn was spoken after the loss.
    assert packet["counts"]["unheard_turns"] == 0
    assert all(t["participant_channel_lost"] is False
               for t in packet["transcript"])

    notes = [t["note"] for t in packet["transcript"] if t["note"]]
    assert notes == [rater_packet.NOTE_CHANNEL_LOST_AT_END]
    assert packet["transcript"][-1]["note"] == rater_packet.NOTE_CHANNEL_LOST_AT_END


def test_a_loss_with_turns_still_below_it_does_not_say_the_transcript_ends(
        sessions_root):
    """S3A/S3B: a group segment whose scribe died, then a one_to_one_series.

    The 1:1 writer omits participant_channel, so nothing downstream is stamped
    and the record's state stays "lost" — but the turns are there on the page.
    "The transcript ends here" printed above four further turns is the confident
    false statement this whole mechanism exists to avoid.
    """
    evs = _scribe_lost_at_the_end(session_end=None) + [
        {"t": 200.0, "type": "user_turn", "text": "Just the two of us then.",
         "channel": "voice", "script_mismatch": False},
        _agent_pair(220.0, agent_id="drew", text="What would you say to Mel?"),
    ]
    _write_session(sessions_root, evs)
    packet = rater_packet.build(SESSION_ID)

    assert encounter_record.build(sessions_root / SESSION_ID)[
        "participant_channel"]["state"] == "lost"
    notes = [t["note"] for t in packet["transcript"] if t["note"]]
    assert notes == [rater_packet.NOTE_CHANNEL_GAP_SOMEWHERE]
    assert packet["transcript"][-1]["note"] is None


def test_a_bounded_hole_with_no_turn_inside_it_is_still_marked(sessions_root):
    """The same silence, mid-encounter: the channel came back, and no agent
    turn happened to fall inside the hole, so nothing carried the marker. The
    participant's turns in there are missing all the same.
    """
    evs = _scribe_lost_at_the_end(session_end=None) + [
        {"t": 240.0, "type": "group_room_opened", "agents": ["mel", "drew"]},
        {"t": 250.0, "type": "user_turn", "text": "Sorry, I was saying...",
         "channel": "voice", "script_mismatch": False},
        _agent_pair(260.0, participant="Sorry, I was saying...",
                    participant_channel="ok"),
    ]
    _write_session(sessions_root, evs)
    packet = rater_packet.build(SESSION_ID)

    notes = [(t["t"], t["note"]) for t in packet["transcript"] if t["note"]]
    assert notes == [(60.0, rater_packet.NOTE_CHANNEL_GAP)]


def test_a_hole_with_nothing_before_it_is_marked_without_claiming_a_position(
        sessions_root):
    """The channel died before anything was recorded and came back later.

    There is no turn on the near side of the hole, so the two notes above would
    both be claiming a position ("after this turn") that is false here.
    Saying nothing would hide a hole the record knows about; saying either of
    them would be a confident false statement about where it is. The rater gets
    the fact without the position.
    """
    evs = _base_events() + [
        {"t": 5.0, "type": "group_room_opened", "agents": ["mel", "drew"]},
        {"t": 10.0, "type": "scribe_pump_ended", "segment": 0,
         "interaction": "i1"},
        {"t": 60.0, "type": "group_room_opened", "agents": ["mel", "drew"]},
        {"t": 70.0, "type": "user_turn", "text": "Where were we?",
         "channel": "voice", "script_mismatch": False},
        _agent_pair(80.0, participant="Where were we?",
                    participant_channel="ok"),
    ]
    sdir = _write_session(sessions_root, evs)
    assert encounter_record.build(sdir)["participant_channel"]["state"] == "restored"

    packet = rater_packet.build(SESSION_ID)
    notes = [t["note"] for t in packet["transcript"] if t["note"]]
    assert notes == [rater_packet.NOTE_CHANNEL_GAP_SOMEWHERE]
    assert packet["transcript"][0]["note"] == rater_packet.NOTE_CHANNEL_GAP_SOMEWHERE


def test_an_intact_channel_still_gets_no_note(sessions_root):
    """The other half: this branch reads the record's own state field, so an
    encounter that never lost the channel must come back exactly as before."""
    _write_session(sessions_root, _scribe_lost_encounter())
    packet = rater_packet.build(SESSION_ID)
    # The existing shape: the note is on the first turn AFTER the loss, and the
    # terminal-loss branch must not add a second one.
    assert [t["note"] for t in packet["transcript"]].count(
        rater_packet.NOTE_CHANNEL_LOST) == 1
    assert rater_packet.NOTE_CHANNEL_LOST_AT_END not in [
        t["note"] for t in packet["transcript"]]

    _write_session(sessions_root, _garbled_encounter())
    clean = rater_packet.build(SESSION_ID)
    assert [t["note"] for t in clean["transcript"]
            if t["note"] == rater_packet.NOTE_CHANNEL_LOST_AT_END] == []


# --------------------------------------------------------------------------
# blinding: the new fields must not widen what a rater can see
# --------------------------------------------------------------------------

def test_the_new_markers_carry_no_identifying_or_answer_key_content(sessions_root):
    """tests/test_rater_packet.py pins the packet's blinding; this pins that the
    three fields added here did not open a hole in it, since all three describe
    the capture rather than the encounter's content."""
    _write_session(sessions_root, _scribe_lost_encounter(), hq=HQ)
    packet = rater_packet.build(SESSION_ID)
    blob = json.dumps(packet, ensure_ascii=False)

    for forbidden in ("participant_transcript_hq", "scribe_pump_ended",
                      "stage_direction", "press on the handoff",
                      SESSION_ID, "p_test"):
        assert forbidden not in blob, f"the packet leaked {forbidden!r}"
