"""Tests for the blinded rater packet and the video playback link.

The centre of this file is test_no_forbidden_field_by_name / _by_value. Phase 2
buys its gold labels with rater independence: two or three people who have seen
nothing but the encounter. Every one of the fields those two tests look for —
the participant key, the stage direction, the planted trigger and its ESCI tags,
the actor's system prompt, the offline judge's score — would tell a rater what
the instrument expected before they decided what they saw. There is no way to
notice that leak from the outside; a biased rating looks exactly like an honest
one. So the leak has to be caught here, by name and by value, against a record
that actually contains all of it.

Everything else is hermetic: a synthetic session directory written in the shapes
the real writers use, with SESSIONS_DIR pointed at it. A recorded wave is used
as well when there is one (tests/conftest.py resolves it from RF_FIXTURE_DIR,
RF_FIXTURE or DATA_DIR, or from a wave checked in under tests/data), because a
synthetic encounter cannot demonstrate that the packet survives every shape a
real wave contains. No wave is a skip with instructions, never a path error.

No network. Presigning is HMAC arithmetic over a request that is never sent, so
it is exercised as the local computation it is, with throwaway credentials.
"""

from __future__ import annotations

import json
import os
import re
import sys
from pathlib import Path

import pytest

# The repo root, so `import server` works when pytest is invoked from it.
ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

# Throwaway signing material, set before anything imports boto3-backed code.
# generate_presigned_url needs credentials to sign with; it does not need them
# to be real, and it never contacts anything to find that out.
os.environ.setdefault("AWS_ACCESS_KEY_ID", "AKIATESTONLYTESTONLY")
os.environ.setdefault("AWS_SECRET_ACCESS_KEY", "test-only-secret-never-a-real-key")
os.environ.setdefault("AWS_DEFAULT_REGION", "us-east-1")
# Pin the HMAC key so rating codes are deterministic across runs, and so the
# test never causes a .run_code_secret to be minted inside the repo's data dir.
os.environ.setdefault("RUN_CODE_SECRET", "test-only-rating-code-secret")

from server import rater_packet as rp  # noqa: E402
from server import video  # noqa: E402


# --------------------------------------------------------------------------
# A synthetic encounter, written in the shapes the real writers use.
# --------------------------------------------------------------------------

SESSION_ID = "s_1772460300_44c9a2"

# Distinctive sentinels, so a value-level leak is unambiguous rather than a
# coincidental substring.
PARTICIPANT_KEY = "PKEYDONOTSHOW"
PARTICIPANT_ID = "p_1772460300_donotshow"
RUN_ID = "rundonotshow"
TRIGGER_ID = "t1_trigger_do_not_show"
ESCI_TAG = "esci_tag_do_not_show"
STAGE_DIRECTION = "STAGE DIRECTION: needle them about the reply-all, do not show"
INSTRUCTIONS_SHA = "deadbeefdonotshow"
JUDGE_RATIONALE = "OFFLINE JUDGE VERDICT: do not show"

STARTED_AT = 1772460300.0
ENDED_AT = STARTED_AT + 412.5


def _events(*, with_video: bool = True):
    """The event trail, in the field sets the real writers emit.

    Copied from encounter_record.build's reader and the call sites it reads:
    session_start (session.py), steering_pair (realtime_voice_session's
    _finalize_turn), user_turn, stage_direction, video_uploaded (the confirm
    endpoint in app.py).
    """
    evs = [
        {"t": 0.0, "wall": STARTED_AT, "type": "session_start",
         "scenario": "S1B",
         "participant_id": PARTICIPANT_ID,
         "run_id": RUN_ID,
         "cohort": "study",
         "participant_key": PARTICIPANT_KEY,
         "encounter_index": 1,
         "spec_fingerprint": {"trigger_ids": [TRIGGER_ID], "sha256": "0" * 64},
         "cast": [{"id": "mel", "name": "Mel"}, {"id": "drew", "name": "Drew"}]},
        {"t": 0.1, "type": "realtime_session_started",
         "gateway": "https://api.ai.it.cornell.edu", "model": "nto.gemini-3.1-flash-lite"},
        {"t": 13.1, "type": "user_turn", "text": "No reply-all. I'll talk to Drew this morning."},
        {"t": 42.1, "type": "stage_direction", "agent_id": "mel",
         "stage_direction": STAGE_DIRECTION, "trigger_id": TRIGGER_ID,
         "esci": [ESCI_TAG], "instructions_sha256": INSTRUCTIONS_SHA},
        # An interrupted agent turn: the participant spoke over it.
        {"t": 42.2, "type": "steering_pair",
         "actor": {"agent_id": "mel", "voice": "Leda",
                   "text": "Reply-all, receipts attached, right now",
                   "interrupted": True, "transcript_missing": False,
                   "latency_total_s": 1.8},
         "direction": {"stage_direction": STAGE_DIRECTION, "trigger_id": TRIGGER_ID,
                       "esci": [ESCI_TAG], "probing": False,
                       "instructions_sha256": INSTRUCTIONS_SHA,
                       "segment": 0, "interaction": "i1"}},
        {"t": 60.5, "type": "user_turn", "text": "That is fair. Here is where I am."},
        # An agent turn whose audio played but whose transcript never arrived.
        {"t": 101.8, "type": "steering_pair",
         "actor": {"agent_id": "drew", "voice": "Fenrir", "text": "",
                   "interrupted": False, "transcript_missing": True},
         "direction": {"stage_direction": STAGE_DIRECTION, "trigger_id": TRIGGER_ID,
                       "esci": [ESCI_TAG], "probing": False,
                       "instructions_sha256": INSTRUCTIONS_SHA,
                       "segment": 1, "interaction": "i2"}},
        # A text-channel turn, which takes the other branch of the record builder.
        {"t": 140.0, "type": "assistant_turn", "channel": "text",
         "agent_id": "drew", "text": "The handoff problem is real.", "latency_s": 0.9},
    ]
    if with_video:
        evs.append({"t": None, "wall": ENDED_AT + 2, "type": "video_uploaded",
                    "key": f"encounters/{SESSION_ID}/webcam.webm", "bytes": 8_400_000})
    return evs


def _write_session(root: Path, session_id: str, *, with_video: bool = True,
                   ended: bool = True) -> Path:
    sdir = root / session_id
    sdir.mkdir(parents=True, exist_ok=True)
    with (sdir / "events.jsonl").open("w", encoding="utf-8") as fh:
        for e in _events(with_video=with_video):
            fh.write(json.dumps(e) + "\n")
    manifest = {
        "session_id": session_id, "scenario": "S1B",
        "model": "nto.gemini-3.1-flash-lite",
        "participant_id": PARTICIPANT_ID, "run_id": RUN_ID, "cohort": "study",
        "participant_key": PARTICIPANT_KEY, "encounter_index": 1,
        "started_at": STARTED_AT, "status": "closed", "n_turns": 4,
    }
    if ended:
        manifest["ended_at"] = ENDED_AT
    (sdir / "manifest.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    # The offline judge's cached verdict, sitting where scoring.py leaves it.
    # Present precisely so the blindness tests are testing against a session
    # that really does have a model score next to it on disk.
    (sdir / "score.json").write_text(
        json.dumps({"overall": 4.5, "rationale": JUDGE_RATIONALE}), encoding="utf-8")
    return sdir


@pytest.fixture
def sessions_root(tmp_path, monkeypatch):
    """A private SESSIONS_DIR, patched into every module that resolved it."""
    root = tmp_path / "sessions"
    root.mkdir()
    monkeypatch.setattr(rp, "SESSIONS_DIR", root)
    monkeypatch.setattr(video, "SESSIONS_DIR", root)
    return root


@pytest.fixture
def packet(sessions_root):
    _write_session(sessions_root, SESSION_ID)
    return rp.build(SESSION_ID)


# --------------------------------------------------------------------------
# Blindness. The reason this file exists.
# --------------------------------------------------------------------------

# Field names that must never appear anywhere in a packet, at any depth.
#
# Three groups. Identifiers that would let a rater link packets to a
# participant, a run or each other. The answer key: what the director told the
# actor to do, which planted beat it was, which ESCI items that beat was written
# to make observable, and the actor briefs behind all of it. And the offline
# judge's verdict, which would turn an independent human rating into agreement
# with a model.
FORBIDDEN_KEYS = [
    # identifiers
    "participant_key", "participant_id", "participant_record_id",
    "session_id", "encounter_id", "run_id", "cohort", "encounter_index",
    "qualtrics_id", "identity",
    # the answer key
    "stage_direction", "steering_log", "trigger_id", "triggers", "esci",
    "esci_items", "probing", "instructions_sha256", "spec_fingerprint",
    "system_prompt", "director_prompt", "scene", "analysis_scene", "intro",
    "observe", "cue", "on_silence", "behavioral_markers", "skill_measured",
    "focal_items",
    # condition
    "scenario", "scenario_id", "variant", "parallel_form",
    # the offline judge, and the model path generally
    "score", "scores", "judge", "verdict", "rubric", "provenance",
    "gateway", "realtime_model", "text_model", "model",
    # internals with no rater use
    "agent_id", "voice", "latency_s", "latency_total_s", "bucket", "dir",
]


def _walk(node):
    """Every (key, value) pair anywhere in a nested structure."""
    if isinstance(node, dict):
        for k, v in node.items():
            yield k, v
            yield from _walk(v)
    elif isinstance(node, list):
        for v in node:
            yield from _walk(v)


def _keys(packet):
    return {k for k, _ in _walk(packet)}


def _values(packet):
    """Every scalar value in the packet, as strings.

    Used instead of a substring scan wherever the forbidden token is an ordinary
    word that also occurs legitimately inside the item texts.
    """
    return {str(v) for _, v in _walk(packet) if isinstance(v, (str, int, float))}


def _blob(packet):
    """The packet as text, minus the presigned URL.

    The URL is excluded from the value scan because the S3 object key is
    encounters/{session_id}/webcam.webm and therefore contains the session id.
    That single, known exception is pinned by
    test_session_id_leaks_only_through_the_presigned_object_key, so it cannot
    widen unnoticed; excluding it here keeps the scan from being weakened to
    accommodate it.
    """
    trimmed = dict(packet)
    media = dict(trimmed.get("media") or {})
    media.pop("video_url", None)
    trimmed["media"] = media
    return json.dumps(trimmed, ensure_ascii=False)


def _walk_keys(obj, path="$"):
    if isinstance(obj, dict):
        for k, v in obj.items():
            yield k, f"{path}.{k}"
            yield from _walk_keys(v, f"{path}.{k}")
    elif isinstance(obj, list):
        for i, v in enumerate(obj):
            yield from _walk_keys(v, f"{path}[{i}]")


def test_no_forbidden_field_by_name(packet):
    seen = {}
    for key, where in _walk_keys(packet):
        seen.setdefault(key, where)
    leaked = {k: seen[k] for k in FORBIDDEN_KEYS if k in seen}
    assert not leaked, f"blinded packet leaked forbidden field(s): {leaked}"


def test_no_forbidden_value_by_value(packet):
    """The stronger half: a renamed field is still a leak.

    Names can be changed; the content cannot. So the same check is run over the
    serialised packet, looking for the actual strings that were sitting in the
    record.
    """
    blob = _blob(packet)
    for secret in (PARTICIPANT_KEY, PARTICIPANT_ID, RUN_ID, TRIGGER_ID,
                   ESCI_TAG, STAGE_DIRECTION, INSTRUCTIONS_SHA,
                   JUDGE_RATIONALE, SESSION_ID):
        assert secret not in blob, f"blinded packet leaked the value {secret!r}"


def test_session_id_leaks_only_through_the_presigned_object_key(packet):
    """The one place the blinding is not total, pinned so it cannot spread.

    The webcam object lives at encounters/{session_id}/webcam.webm, a layout
    that predates Phase 2 and under which the wave is already written, so the
    signed playback URL necessarily contains the session id. A rater with the
    network inspector open could read it, and session ids carry a start time to
    the second — enough to tell that two packets recorded twelve minutes apart
    belong to one participant.

    This test says: that is the only leak. Everything else the rater is handed,
    including the rating code, is free of it. If a session id ever turns up in a
    second field, this fails.
    """
    assert SESSION_ID in packet["media"]["video_url"]
    assert SESSION_ID not in _blob(packet)
    assert SESSION_ID not in packet["rating_code"]


def test_actor_brief_never_reaches_the_packet(packet):
    """The situation is the participant's brief, not the characters' briefs.

    S1B's actor prompts tell Drew his internal state — that the handoff problem
    is real and that he half-knows the delivery was poor — which is the answer
    to the thing the participant is being scored on handling.
    """
    blob = _blob(packet).lower()
    for phrase in ("internal state", "how you behave", "system_prompt",
                   "never resolve it for them", "if they attack, harden",
                   "planted", "trigger"):
        assert phrase not in blob, f"actor/director material reached the packet: {phrase!r}"


def test_situation_is_the_participant_brief(packet):
    situation = packet["situation"]
    # The setup the participant read, second person, as they read it.
    assert "You are a senior analyst" in situation["text"]
    assert "Drew" in situation["text"]
    # The people they were told they would meet, by display role, not by the
    # role the actor prompt gives them.
    assert {p["name"] for p in situation["people"]} == {"Mel", "Drew"}
    assert [p["role"] for p in situation["people"]] == ["teammate", "your colleague"]
    # Scene labels, no `observe:` measurement hint.
    assert [p["label"] for p in situation["parts"]] == [
        "Mel pings you first thing", "Coffee-machine run-in with Drew"]
    assert all("with" in p and "mode" in p for p in situation["parts"])


def test_situation_does_not_vary_with_the_participant(sessions_root):
    """Two participants, the same scenario, the same situation text.

    The in-scene identity is derived from the participant key and is stable
    across all four of a participant's encounters, so a situation rendered with
    the real key would let a rater group one participant's packets and carry an
    impression from the first into the rest. compile_scenario is called with an
    empty key for exactly that reason; this pins it.
    """
    _write_session(sessions_root, SESSION_ID)
    other = "s_1772653715_f54a85"
    sdir = _write_session(sessions_root, other)
    manifest = json.loads((sdir / "manifest.json").read_text(encoding="utf-8"))
    manifest["participant_key"] = "SOMEONEELSE"
    (sdir / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    # Rewrite the event trail with a different participant key too.
    lines = (sdir / "events.jsonl").read_text(encoding="utf-8").replace(
        PARTICIPANT_KEY, "SOMEONEELSE").replace(PARTICIPANT_ID, "p_other_x")
    (sdir / "events.jsonl").write_text(lines, encoding="utf-8")

    assert rp.build(SESSION_ID)["situation"] == rp.build(other)["situation"]


# --------------------------------------------------------------------------
# The two per-turn delivery flags.
# --------------------------------------------------------------------------

def test_interrupted_turn_carries_a_neutral_marker(packet):
    turn = next(t for t in packet["transcript"] if t["interrupted"])
    assert turn["speaker"] == "Mel"
    assert turn["note"] == rp.NOTE_INTERRUPTED
    assert not turn["transcript_missing"]
    # Neutral: it describes what happened to the delivery, and says nothing
    # about how well anybody did.
    low = turn["note"].lower()
    assert "weak" not in low and "poor" not in low and "fail" not in low


def test_untranscribed_turn_carries_a_neutral_marker(packet):
    turn = next(t for t in packet["transcript"] if t["transcript_missing"])
    assert turn["speaker"] == "Drew"
    assert turn["text"] == ""
    assert turn["note"] == rp.NOTE_NO_TRANSCRIPT
    # An empty line with no explanation is the rating error the flag exists to
    # prevent, so the marker must say the text is missing rather than silent.
    assert "silence" in turn["note"].lower()


def test_flags_are_also_counted_for_the_top_of_the_console(packet):
    assert packet["counts"]["interrupted_turns"] == 1
    assert packet["counts"]["untranscribed_turns"] == 1


def test_clean_turns_carry_no_marker(packet):
    clean = [t for t in packet["transcript"]
             if not t["interrupted"] and not t["transcript_missing"]]
    assert clean, "expected some ordinary turns"
    assert all(t["note"] is None for t in clean)


# --------------------------------------------------------------------------
# Transcript shape.
# --------------------------------------------------------------------------

def test_transcript_is_speaker_labelled_and_ordered(packet):
    turns = packet["transcript"]
    assert [t["speaker"] for t in turns] == [
        "Participant", "Mel", "Participant", "Drew", "Drew"]
    assert [t["role"] for t in turns] == [
        "participant", "agent", "participant", "agent", "agent"]
    assert [t["t"] for t in turns] == sorted(t["t"] for t in turns)
    assert packet["counts"] == {
        "participant_turns": 2, "agent_turns": 3,
        "interrupted_turns": 1, "untranscribed_turns": 1}


def test_unknown_agent_becomes_a_generic_label(sessions_root):
    """A turn from a character not in the cast must not print its internal id."""
    sdir = _write_session(sessions_root, SESSION_ID)
    lines = (sdir / "events.jsonl").read_text(encoding="utf-8").splitlines()
    lines.append(json.dumps({
        "t": 200.0, "type": "steering_pair",
        "actor": {"agent_id": "ghost_agent_id", "text": "Hello."},
        "direction": {}}))
    (sdir / "events.jsonl").write_text("\n".join(lines) + "\n", encoding="utf-8")
    turns = rp.build(SESSION_ID)["transcript"]
    assert turns[-1]["speaker"] == "The other speaker"
    assert "ghost_agent_id" not in json.dumps(turns)


# --------------------------------------------------------------------------
# Duration.
# --------------------------------------------------------------------------

def test_duration_comes_from_the_manifest(packet):
    assert packet["duration_s"] == pytest.approx(412.5)
    assert packet["duration_display"] == "6:52"


def test_duration_falls_back_to_the_last_turn_when_the_session_never_closed(sessions_root):
    _write_session(sessions_root, SESSION_ID, ended=False)
    p = rp.build(SESSION_ID)
    # The last event's elapsed time: a floor, not a guess, and better than
    # telling a rater the encounter has no length at all.
    assert p["duration_s"] == pytest.approx(140.0)
    assert p["duration_display"] == "2:20"


# --------------------------------------------------------------------------
# The instrument notice.
# --------------------------------------------------------------------------

def test_licensing_notice_travels_with_every_packet(packet):
    notice = packet["instrument_notice"]
    assert "Proprietary" in notice
    assert "confirm licensing/permission before fielding" in notice
    assert "Korn Ferry" in notice


def test_scale_note_offers_the_na_option(packet):
    assert "Not enough information to judge" in packet["scale_note"]


# --------------------------------------------------------------------------
# The rating code.
# --------------------------------------------------------------------------

def test_rating_code_shape_and_stability():
    code = rp.rating_code(SESSION_ID)
    assert re.fullmatch(r"RC-[0-9A-F]{10}", code)
    assert code == rp.rating_code(SESSION_ID)


def test_rating_code_differs_per_encounter():
    ids = [f"s_177246030{i}_44c9a{i}" for i in range(6)]
    codes = {rp.rating_code(i) for i in ids}
    assert len(codes) == len(ids)


def test_rating_code_is_keyed_not_merely_hashed(monkeypatch):
    """Change the secret, get a different code.

    An unkeyed digest of `s_{epoch}_{6 hex}` is brute-forceable — the epoch is
    known to within a day and only 24 bits follow it — so a rater holding a code
    could recover the session id and tell which encounters share a wave. This
    fails if anyone swaps the HMAC for a plain hash.
    """
    first = rp.rating_code(SESSION_ID)
    monkeypatch.setenv("RUN_CODE_SECRET", "a-different-secret-entirely")
    assert rp.rating_code(SESSION_ID) != first


def test_rating_code_rejects_a_crafted_session_id():
    for bad in ("", "../../etc/passwd", "s_1/../x", "s_1\\..\\x", "x" * 200):
        with pytest.raises(ValueError):
            rp.rating_code(bad)


def test_packet_is_the_only_place_the_code_appears(packet):
    assert packet["rating_code"] == rp.rating_code(SESSION_ID)


# --------------------------------------------------------------------------
# Missing / hostile input.
# --------------------------------------------------------------------------

def test_unknown_encounter_builds_nothing(sessions_root):
    assert rp.build("s_1772460300_absent") == {}


def test_crafted_session_id_builds_nothing_rather_than_touching_the_disk(sessions_root):
    for bad in ("", "../../server", "..", "a/b", "a\\b"):
        assert rp.build(bad) == {}


def test_session_directory_without_events_builds_nothing(sessions_root):
    (sessions_root / "s_1772460300_empty").mkdir()
    assert rp.build("s_1772460300_empty") == {}


# --------------------------------------------------------------------------
# Media: the presigned playback link, exercised as the local computation it is.
# --------------------------------------------------------------------------

def test_playback_url_is_signed_locally_and_points_at_the_encounter(sessions_root):
    _write_session(sessions_root, SESSION_ID)
    url = video.playback_url(SESSION_ID)
    assert url is not None
    assert f"encounters/{SESSION_ID}/webcam.webm" in url
    assert "X-Amz-Signature=" in url
    assert "X-Amz-Expires=3600" in url
    # A GET, not a PUT: raters play the recording, they do not replace it.
    assert "X-Amz-Algorithm=AWS4-HMAC-SHA256" in url


def test_playback_url_makes_no_network_call(sessions_root, monkeypatch):
    """Existence is answered from the local upload receipt, never from S3.

    A rater's assignment list is tens of packets; a HEAD per packet turns
    opening the console into a burst of S3 calls that fail as a unit. Any call
    into head_object here is the bug this asserts against.
    """
    _write_session(sessions_root, SESSION_ID)

    def _boom(*a, **k):  # pragma: no cover - only runs if the guard fails
        raise AssertionError("playback_url reached the network")

    monkeypatch.setattr(video, "uploaded_size", _boom)
    real = video._client()
    monkeypatch.setattr(real, "head_object", _boom)
    assert video.playback_url(SESSION_ID) is not None


def test_playback_url_is_none_when_no_video_was_captured(sessions_root):
    _write_session(sessions_root, SESSION_ID, with_video=False)
    assert video.playback_url(SESSION_ID) is None


def test_zero_byte_upload_receipt_is_not_a_video(sessions_root):
    """The confirm endpoint writes the event whether or not the PUT landed.

    `{"bytes": 0}` is what a failed upload looks like, and signing a URL for an
    object that is not there would hand a rater a link that 404s mid-rating.
    """
    sdir = _write_session(sessions_root, SESSION_ID, with_video=False)
    with (sdir / "events.jsonl").open("a", encoding="utf-8") as fh:
        fh.write(json.dumps({"t": None, "type": "video_uploaded",
                             "key": f"encounters/{SESSION_ID}/webcam.webm",
                             "bytes": 0}) + "\n")
    assert video.upload_receipt(SESSION_ID) is None
    assert video.playback_url(SESSION_ID) is None


def test_playback_expiry_is_clamped(sessions_root):
    _write_session(sessions_root, SESSION_ID)
    assert f"X-Amz-Expires={video.MAX_PLAYBACK_SECONDS}" in video.playback_url(
        SESSION_ID, seconds=10 ** 7)
    assert "X-Amz-Expires=60" in video.playback_url(SESSION_ID, seconds=1)
    assert "X-Amz-Expires=60" in video.playback_url(SESSION_ID, seconds=-5)


def test_playback_url_rejects_a_crafted_session_id(sessions_root):
    for bad in ("", "../../secrets", "a/b", "a\\b", "x" * 200):
        assert video.playback_url(bad) is None
        assert video.upload_receipt(bad) is None


def test_playback_url_ignores_a_key_written_into_the_event_trail(sessions_root):
    """The signed key is derived, never read back out of a file on disk."""
    sdir = _write_session(sessions_root, SESSION_ID, with_video=False)
    with (sdir / "events.jsonl").open("a", encoding="utf-8") as fh:
        fh.write(json.dumps({"t": None, "type": "video_uploaded",
                             "key": "some/other/prefix/secret.mp4",
                             "bytes": 1234}) + "\n")
    url = video.playback_url(SESSION_ID)
    assert "some/other/prefix" not in url
    assert f"encounters/{SESSION_ID}/webcam.webm" in url


def test_packet_media_offers_video_and_no_audio(packet):
    media = packet["media"]
    assert media["video_available"] is True
    assert media["expires_in"] == rp.PLAYBACK_SECONDS
    assert media["video_url"].startswith("https://")
    assert media["note"] is None
    # The per-channel WAVs cannot be played back as a conversation, so they are
    # not offered at all.
    assert "audio" not in _blob(packet)


def test_packet_says_so_when_there_is_no_video(sessions_root):
    _write_session(sessions_root, SESSION_ID, with_video=False)
    media = rp.build(SESSION_ID)["media"]
    assert media["video_url"] is None
    assert media["video_available"] is False
    assert "Not enough information to judge" in media["note"]


def test_a_video_that_cannot_be_signed_is_not_reported_as_absent(sessions_root, monkeypatch):
    """The third state, and the reason it is kept separate.

    Signing is local arithmetic, so a failure here is a deployment fault, not a
    blip. Reporting it as "no video recorded" would have a rater score from the
    transcript alone with nothing anywhere saying the video had been withheld.
    """
    _write_session(sessions_root, SESSION_ID)

    def _explode(*a, **k):
        raise RuntimeError("no credentials")

    monkeypatch.setattr(video, "playback_url", _explode)
    media = rp.build(SESSION_ID)["media"]
    assert media["video_url"] is None
    assert media["video_available"] is True
    assert "report it" in media["note"]


# --------------------------------------------------------------------------
# The recorded wave, when there is one on this machine.
#
# Resolution lives in tests/conftest.py (RF_FIXTURE_DIR / RF_FIXTURE /
# DATA_DIR, or a wave checked in under tests/data), so this module no longer
# rebuilds one machine's scratchpad path out of %TEMP% and a session UUID, and
# no longer disagrees with the four other modules that each did that
# differently. `wave_sessions` skips with instructions when nothing names a
# wave.
#
# Several tests below name individual encounters from the REFERENCE wave - the
# 27-encounter synthetic one this module was written against - because they
# assert on defects deliberately seeded into specific encounters. Those go
# through `_reference(...)`: another wave is a wave, not a failure, so a wave
# that does not carry them skips saying which are missing.
# --------------------------------------------------------------------------

REFERENCE_WAVE_SIZE = 27


@pytest.fixture
def wave(monkeypatch, wave_sessions):
    monkeypatch.setattr(rp, "SESSIONS_DIR", wave_sessions)
    monkeypatch.setattr(video, "SESSIONS_DIR", wave_sessions)
    return sorted(p.parent.name for p in wave_sessions.glob("*/record.json"))


def _reference(wave, *encounter_ids):
    """Skip unless this wave carries the named reference encounters."""
    missing = [sid for sid in encounter_ids if sid not in wave]
    if missing:
        pytest.skip("this wave does not carry the reference encounters these "
                    "assertions describe: %s" % (missing,))


def test_every_encounter_in_the_wave_builds_a_blind_packet(wave, wave_sessions):
    """The whole wave, every encounter, both checks.

    A synthetic encounter is one shape. The reference wave has twenty-seven,
    including a one-turn encounter, a group scene, an interrupted delivery, two
    lost transcripts, two encounters with no webcam upload, and an internal
    test run. Every one of them has a real participant key, real trigger ids
    and real stage directions sitting in its event trail, so this is the leak
    test run against the material it is meant to hold back.

    The size is deliberately not asserted. Whatever wave is present, every
    encounter in it must build a packet that leaks nothing; `== 27` was a
    statement about which directory the runner was pointed at, and it turned a
    colleague's wave into a red suite without saying anything about the code.
    """
    assert wave, "the wave resolved but holds no encounters"
    for sid in wave:
        packet = rp.build(sid)
        assert packet, f"{sid} produced no packet"

        seen = {k for k, _ in _walk_keys(packet)}
        leaked = sorted(set(FORBIDDEN_KEYS) & seen)
        assert not leaked, f"{sid} leaked {leaked}"

        record = json.loads(
            (wave_sessions / sid / "record.json").read_text(encoding="utf-8"))
        blob = _blob(packet)
        for value in (sid, record.get("participant_key"), record.get("participant_id"),
                      record.get("run_id")):
            if value:
                assert value not in blob, f"{sid} leaked {value!r}"
        for direction in record.get("steering_log") or []:
            if direction.get("stage_direction"):
                assert direction["stage_direction"] not in blob, f"{sid} leaked a direction"
            if direction.get("trigger_id"):
                assert direction["trigger_id"] not in blob, f"{sid} leaked a trigger id"
            # Structurally, not as a substring. The packet legitimately carries
            # the 22 item texts, and several ESCI slugs are ordinary words that
            # appear inside them — "respectful" is a slug AND a word in "Works
            # well in teams by being respectful of others". A substring search
            # therefore fails on a correct packet. What must not appear is the
            # tagging itself: an `esci` key, or a slug standing alone as a value
            # anywhere in the structure.
            for tag in direction.get("esci") or []:
                assert tag not in _values(packet), (
                    f"{sid} leaked the ESCI tag {tag!r} as a value"
                )
        assert "esci" not in _keys(packet), f"{sid} leaked the ESCI tagging structure"


def test_wave_rating_codes_are_unique_and_stable(wave):
    codes = {sid: rp.rating_code(sid) for sid in wave}
    assert len(set(codes.values())) == len(wave)
    assert codes == {sid: rp.rating_code(sid) for sid in wave}


def test_wave_packets_are_rateable(wave):
    """Every packet carries the four things a rater needs to score it."""
    constructs = set()
    for sid in wave:
        packet = rp.build(sid)
        assert packet["construct"] in (
            "conflict_management", "influence", "inspirational_leadership", "teamwork")
        constructs.add(packet["construct"])
        assert packet["situation"]["text"], f"{sid} has no situation"
        assert packet["situation"]["people"], f"{sid} names nobody"
        assert packet["duration_s"] and packet["duration_s"] > 0
        assert packet["duration_display"]
        assert packet["instrument_notice"]
        assert isinstance(packet["transcript"], list)
        for turn in packet["transcript"]:
            assert set(turn) == {"t", "role", "speaker", "text",
                                 "interrupted", "transcript_missing", "note"}
    # A wave that never touches all four constructs leaves a whole block of the
    # instrument unexercised, which is a study problem worth saying out loud.
    assert len(constructs) == 4, (
        "this wave only covers %s; a block of the instrument would never be "
        "rated" % (sorted(constructs),))


def test_wave_flags_reach_the_packets(wave):
    """The specific encounters the wave seeded these defects into.

    s_1772460300_44c9a2 is the barge-in; s_1772548516_02952d lost an agent
    transcript on the 1:1 path and s_1772958864_451bf5 on the group path. If the
    record builder stops carrying the flags, or this module stops reading them,
    these three go quiet and a rater scores a truncated or empty line as a weak
    reply with nothing on screen saying otherwise.
    """
    _reference(wave, "s_1772460300_44c9a2",
               "s_1772548516_02952d", "s_1772958864_451bf5")
    interrupted = rp.build("s_1772460300_44c9a2")
    assert interrupted["counts"]["interrupted_turns"] >= 1
    assert any(t["note"] == rp.NOTE_INTERRUPTED for t in interrupted["transcript"])

    for sid in ("s_1772548516_02952d", "s_1772958864_451bf5"):
        packet = rp.build(sid)
        assert packet["counts"]["untranscribed_turns"] >= 1, sid
        missing = [t for t in packet["transcript"] if t["transcript_missing"]]
        assert all(t["note"] == rp.NOTE_NO_TRANSCRIPT for t in missing)


def test_wave_video_presence_matches_the_upload_receipts(wave):
    """Two encounters in the reference wave deliberately have no webcam upload.

    Note that record.json on disk says `"video": []` for every encounter — the
    stored copy is written before the browser confirms the upload — which is why
    the packet rebuilds the record from events.jsonl instead of reading it.
    """
    _reference(wave, "s_1772764657_717245", "s_1773142745_384dad")
    with_video = [sid for sid in wave if rp.build(sid)["media"]["video_available"]]
    # Derived, not the reference wave's literal 25: everything except the two
    # seeded no-upload encounters must present a playable video.
    assert len(with_video) == len(wave) - 2
    for sid in ("s_1772764657_717245", "s_1773142745_384dad"):
        media = rp.build(sid)["media"]
        assert media["video_available"] is False
        assert media["video_url"] is None
        assert "Not enough information to judge" in media["note"]
    for sid in with_video[:3]:
        media = rp.build(sid)["media"]
        assert f"encounters/{sid}/webcam.webm" in media["video_url"]
        assert "X-Amz-Signature=" in media["video_url"]


def test_wave_one_turn_encounter_still_builds(wave):
    """P5's second encounter: the participant said one sentence.

    verify_record calls it complete. A rater has to be able to see that there is
    almost nothing here rather than be handed a blank screen, so the packet is
    built and its emptiness is visible in the counts.
    """
    _reference(wave, "s_1772868851_e93ad5")
    packet = rp.build("s_1772868851_e93ad5")
    assert packet["counts"]["participant_turns"] == 1
    assert packet["counts"]["agent_turns"] >= 1
    assert packet["duration_s"] > 0


def test_wave_packet_build_is_deterministic(wave):
    """Same encounter, same packet — except the signature, which is time-based.

    Ratings are joined back by rating_code, so a packet that changed shape
    between two raters' sittings would mean they scored different material.
    """
    sid = wave[0]
    a, b = rp.build(sid), rp.build(sid)
    a["media"] = b["media"] = None
    assert a == b


def test_internal_test_traffic_is_not_marked_in_the_packet(wave):
    """The internal-cohort encounter looks like any other to a rater.

    Cohort is a researcher's concern — it decides what enters the dataset — and
    a rater who could see it would know which encounters do not count.
    """
    _reference(wave, "s_1773142745_384dad")
    packet = rp.build("s_1773142745_384dad")
    assert packet
    # Quoted, so a scenario that happens to use the word ("a new internal
    # process") is not mistaken for the cohort tag leaking as a value.
    assert '"internal"' not in _blob(packet)


def test_wave_transcript_speakers_are_names_not_ids(wave, wave_sessions):
    for sid in wave:
        packet = rp.build(sid)
        record = json.loads(
            (wave_sessions / sid / "record.json").read_text(encoding="utf-8"))
        names = {a["name"] for a in record.get("cast") or []}
        for turn in packet["transcript"]:
            assert turn["speaker"] == "Participant" or turn["speaker"] in names \
                or turn["speaker"] == "The other speaker", turn["speaker"]
