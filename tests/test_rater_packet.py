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

NO NETWORK, and that is enforced rather than asserted. Two things make it true.

The bucket is answered in this process: `offline_bucket` (tests/conftest.py)
puts a stub in front of video._s3, because the media block asks video.exists()
now rather than reading a browser's upload receipt, and exists() on an encounter
with no local recording is a HEAD against relational-fluency-study-data. And
every encounter here that is supposed to HAVE a recording gets real bytes on
local disk, which is what exists() looks at first and what a rater's packet
resolves against on a laptop with no AWS at all.

Nothing sets AWS credentials any more. This module used to set three of them
with os.environ.setdefault in its body, for a presigned URL that no longer
exists — and os.environ is the process, not the module, so from the moment
pytest IMPORTED this file every later test in the run that reached video.exists()
signed a real request and sent it to the real Cornell study bucket. The suite
made 3,120 outbound connections that way. Credentials that only some tests need
belong to those tests, not to whatever happens to be running afterwards.
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path

import pytest
from botocore.exceptions import NoCredentialsError

# The repo root, so `import server` works when pytest is invoked from it.
ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from server import rater_packet as rp  # noqa: E402
from server import video  # noqa: E402


# --------------------------------------------------------------------------
# A synthetic encounter, written in the shapes the real writers use.
# --------------------------------------------------------------------------

SESSION_ID = "s_1772460300_44c9a2"

# The minted assignment shape (server/raters.py: as_[0-9a-f]{12}). The packet's
# playback URL is addressed to an ASSIGNMENT now rather than to an S3 object, so
# a packet built without one reports its recording as present-but-unaddressable.
# Every fixture below therefore passes one: that is how a rater actually
# receives a packet, and building it the other way would leave the playable
# state — the one raters spend their whole sitting in — untested here.
ASSIGNMENT_ID = "as_5f3c11a90b2d"

# The first four bytes of every WebM file: EBML's magic number, which
# video._sniff_content_type reads to decide what to serve a <video> element.
# Real bytes rather than b"x" * n, so a recording written by these tests is a
# recording the byte path would actually play.
WEBM_MAGIC = b"\x1a\x45\xdf\xa3"

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
    if with_video:
        # The bytes, not just the receipt saying bytes exist. The packet decides
        # whether there is anything to play by asking storage (video.exists),
        # and an encounter whose event trail claims an upload while no recording
        # exists anywhere is a DIFFERENT state — the lost upload — with its own
        # branch and its own note. Writing only the event here would test that
        # state under the name of this one, and the playable state, which is
        # what raters actually spend their sitting in, would never be reached.
        (sdir / "webcam.webm").write_bytes(WEBM_MAGIC + b"\x00" * 2048)
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


# RUN_CODE_SECRET is pinned by an autouse fixture in tests/conftest.py, for the
# whole suite. It used to be an os.environ.setdefault in this module's body,
# which pinned it for the whole PROCESS from the moment pytest imported this
# file — so every other module's rating codes silently depended on whether this
# one had been collected. Scoping it here alone would have been the same mistake
# in reverse: the modules that lost the accidental cover would each start
# minting a real .run_code_secret into DATA_DIR.


@pytest.fixture
def sessions_root(tmp_path, monkeypatch, offline_bucket):
    """A private SESSIONS_DIR, patched into every module that resolved it.

    `offline_bucket` (tests/conftest.py) comes with it and is not optional: an
    encounter with no local recording sends video.exists() to head_object, and
    without the stub that is a signed HEAD to the real study bucket from a test
    about transcript markers.
    """
    root = tmp_path / "sessions"
    root.mkdir()
    monkeypatch.setattr(rp, "SESSIONS_DIR", root)
    monkeypatch.setattr(video, "SESSIONS_DIR", root)
    return root


@pytest.fixture
def bucket(offline_bucket):
    """The stub bucket itself, for tests that seed or interrogate it."""
    return offline_bucket


@pytest.fixture
def packet(sessions_root):
    _write_session(sessions_root, SESSION_ID)
    return rp.build(SESSION_ID, assignment_id=ASSIGNMENT_ID)


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
    """The whole packet as text. Every field, no exceptions.

    There used to be one: video_url was cut out before the scan, because the
    playback link was a presigned S3 URL over the object key
    encounters/{session_id}/webcam.webm, so it necessarily carried the session
    id and a scan that included it could never pass. The URL now names the
    ASSIGNMENT and is resolved to an encounter server-side, so the exception has
    no reason to exist and the scan is total again.

    Keeping the hole open after the reason for it closed is how an exemption
    outlives the thing it was granted for. If a session id ever reappears in a
    playback URL, this is one of the places that has to notice.
    """
    return json.dumps(packet, ensure_ascii=False)


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


def test_the_session_id_appears_nowhere_in_the_packet(packet):
    """Blinding, with the last exception to it closed.

    This test used to be called ...leaks_only_through_the_presigned_object_key,
    and it granted one: the webcam object lives at
    encounters/{session_id}/webcam.webm, so a presigned playback URL necessarily
    carried the session id. That was a real leak with a real consequence — a
    rater with the network inspector open, or a rater's browser history, could
    read it, and a session id carries the encounter's start time to the second,
    which is enough to tell that two packets recorded twelve minutes apart
    belong to one participant. That is exactly the cross-linking the rating code
    exists to prevent, and it defeated it in a field printed next to the code.

    The URL now names the ASSIGNMENT — the rater's own handle, which links back
    to nothing — and the app resolves it to an encounter server-side. So the
    exception is gone and the guarantee is the strong one it should always have
    been: the session id is in NO field of the packet, the playback URL
    included. Nothing here is allowed to reintroduce it.
    """
    assert SESSION_ID not in _blob(packet)
    assert SESSION_ID not in packet["rating_code"]
    # Stated separately as well as inside the blob scan, because this is the
    # field that carried it and a future URL scheme is the way it comes back.
    assert SESSION_ID not in packet["media"]["video_url"]
    assert packet["media"]["video_url"] == f"/api/rater/video/{ASSIGNMENT_ID}"


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
    # Equality, not a subset, because a count the console does not know about is
    # a warning the rater never sees: the console reads this dict key by key, so
    # a key added here without a place to render it is silently dropped. The two
    # zeros are the participant-side pair, which this fixture does not exercise —
    # they are asserted at zero so a builder that starts flagging every turn is
    # caught here rather than in the wave-backed tests only.
    assert packet["counts"] == {
        "participant_turns": 2, "agent_turns": 3,
        "interrupted_turns": 1, "untranscribed_turns": 1,
        "script_mismatch_turns": 0, "unheard_turns": 0}


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
# Media: the playback link, which is now this app's own route.
#
# This whole section used to be about video.playback_url — a presigned S3 URL,
# minted per packet, with an expiry to clamp and a signature to check. That
# function is gone, and every one of these tests was written because somebody
# found a defect, so each one below is the same defect restated against the
# design that replaced it. The mechanism changed; none of the guarantees did,
# and several got stronger, because the URL no longer carries anything a rater
# could use on its own.
# --------------------------------------------------------------------------

def test_the_playback_url_is_this_apps_own_route_addressed_to_the_assignment(packet):
    """Was: the signed URL points at THIS encounter's object, as a GET.

    The guarantee underneath it was that a packet's media link addresses this
    encounter's recording and nothing else — a rater must not be handed a link
    to another participant's video, and must not be handed one that can write.
    Both survive, and the way they are met is better: the URL names the
    assignment, the app resolves the assignment to an encounter server-side
    against the rater's own token, and GET is the only method the route has. So
    what is asserted is that the link is a path on this server rather than
    anything a browser could take elsewhere.
    """
    url = packet["media"]["video_url"]
    assert url == f"/api/rater/video/{ASSIGNMENT_ID}"
    # Relative, so it cannot address another origin, and so it cannot become a
    # bearer credential the moment it is copied out of the page. A scheme here
    # would mean the recording had moved back out from behind the rater's token.
    assert url.startswith("/api/")
    assert "://" not in url
    assert "X-Amz-" not in url


def test_a_locally_stored_recording_costs_no_call_to_the_bucket(packet, bucket):
    """Was: building a packet never reaches the network.

    The reason was throughput, and it still holds: a rater's assignment list is
    tens of packets, and a round trip to S3 per packet turns opening the console
    into a burst of calls that fail as a unit. What changed is that the packet
    now genuinely does ask storage — that is the fix for the encounter whose
    confirmation POST failed and which was then permanently unrateable while its
    bytes sat in the bucket — so "never asks" is no longer true and could not
    honestly be asserted.

    What IS true, and is the thing that keeps the cost down, is that exists()
    prefers a local recording and only falls through to the bucket when there is
    none. This pins that: bytes on disk, and the bucket is never spoken to. It
    is also what lets a researcher with no AWS credentials at all open a packet
    on a laptop and press play, which nobody could do under the old design.
    """
    assert packet["media"]["video_available"] is True
    assert packet["media"]["video_status"] == "ok"
    assert bucket.heads == [], (
        "a recording on local disk was answered by a HEAD against S3")


def test_no_recording_anywhere_means_nothing_is_offered_to_play(sessions_root, bucket):
    """Was: playback_url is None when no video was captured.

    Nothing on disk, nothing in the bucket, and the honest answer is that there
    is nothing to play. The bucket IS asked here — that is the fall-through —
    and the 404 it answers with is what makes this the absent state rather than
    a fault.
    """
    _write_session(sessions_root, SESSION_ID, with_video=False)
    assert video.exists(SESSION_ID) is False
    assert bucket.heads == [f"encounters/{SESSION_ID}/webcam.webm"]
    media = rp.build(SESSION_ID, assignment_id=ASSIGNMENT_ID)["media"]
    assert media["video_url"] is None
    assert media["video_available"] is False


def test_a_zero_byte_recording_is_not_a_recording(sessions_root, bucket):
    """Was: a `{"bytes": 0}` upload receipt is not a video.

    The defect this caught was a rater being handed a link that 404s mid-rating,
    because the confirm endpoint writes its event whether or not the PUT landed.
    Presence is decided by storage now, so the receipt is no longer the thing
    that could lie — but a zero-length object still is, in both places bytes can
    live, and a crashed or interrupted write leaves exactly that file. So the
    guarantee is pinned at both, which is more than the receipt version covered:

      * a zero-byte webcam.webm on local disk, which is what a killed
        store_local leaves behind;
      * a zero-byte object in the bucket, which is what a killed PUT leaves.

    upload_receipt keeps its own rule as well, because the record builder and
    the lost-upload branch still read it.
    """
    sdir = _write_session(sessions_root, SESSION_ID, with_video=False)
    with (sdir / "events.jsonl").open("a", encoding="utf-8") as fh:
        fh.write(json.dumps({"t": None, "type": "video_uploaded",
                             "key": f"encounters/{SESSION_ID}/webcam.webm",
                             "bytes": 0}) + "\n")
    assert video.upload_receipt(SESSION_ID) is None

    (sdir / "webcam.webm").write_bytes(b"")
    assert video.exists(SESSION_ID) is False, "an empty local file is not a recording"

    (sdir / "webcam.webm").unlink()
    bucket.objects[f"encounters/{SESSION_ID}/webcam.webm"] = 0
    assert video.exists(SESSION_ID) is False, "an empty object is not a recording"

    media = rp.build(SESSION_ID, assignment_id=ASSIGNMENT_ID)["media"]
    assert media["video_url"] is None


def test_the_packet_hands_out_no_bearer_credential_for_the_recording(
        sessions_root, bucket):
    """Was: the presigned link's expiry is clamped to MAX_PLAYBACK_SECONDS.

    That clamp existed because the link WAS a credential: anyone holding the URL
    could fetch an IRB video recording, with no further authentication, for as
    long as the signature lasted — which meant it had to last long enough for a
    rater's sitting and no longer, and the clamp was the compromise between
    those. It was never a good position; it was the best available one while the
    bytes lived outside the app.

    The bytes are behind this server's own route now, gated by the rater's token,
    so there is no credential in the URL to time-box and nothing for a console to
    count down. The surviving guarantee is the one the clamp was serving, and it
    is now absolute rather than bounded: the packet hands out no material that
    fetches the recording on its own. That is asserted across EVERY media state,
    not just the playable one, because a fallback branch that quietly re-mints a
    signed URL is exactly how this would come back.
    """
    states = []
    _write_session(sessions_root, SESSION_ID)                    # playable
    states.append(rp.build(SESSION_ID, assignment_id=ASSIGNMENT_ID)["media"])
    states.append(rp.build(SESSION_ID)["media"])                 # unaddressable
    _write_session(sessions_root, SESSION_ID, with_video=False)  # lost upload
    (sessions_root / SESSION_ID / "webcam.webm").unlink(missing_ok=True)
    with (sessions_root / SESSION_ID / "events.jsonl").open(
            "a", encoding="utf-8") as fh:
        fh.write(json.dumps({"t": None, "type": "video_uploaded",
                             "key": f"encounters/{SESSION_ID}/webcam.webm",
                             "bytes": 8_400_000, "status": "failed"}) + "\n")
    states.append(rp.build(SESSION_ID, assignment_id=ASSIGNMENT_ID)["media"])
    _write_session(sessions_root, "s_1772653715_f54a85", with_video=False)
    states.append(rp.build("s_1772653715_f54a85",
                           assignment_id=ASSIGNMENT_ID)["media"])  # absent

    assert {m["video_status"] for m in states} == {
        "ok", "unsigned", "failed", "absent"}, "a media state went untested"
    for media in states:
        # None on every branch. Not "a long expiry" and not "absent from the
        # dict": every branch carries the same key set, and the value is the
        # fact that there is no deadline.
        assert media["expires_in"] is None, media["video_status"]
        blob = json.dumps(media)
        for signing in ("X-Amz-", "Signature", "Credential", "Expires",
                        "amazonaws.com", video.BUCKET):
            assert signing not in blob, (
                f"the {media['video_status']} media state carries {signing!r}")


def test_a_crafted_session_id_reaches_neither_the_disk_nor_the_bucket(
        sessions_root, bucket):
    """Was: playback_url refuses a crafted session id.

    Phase 2 puts a rater-supplied assignment id in front of this module, so a
    session id that survived the join must not be able to walk out of
    SESSIONS_DIR on a Windows host or aim a request at some other prefix of the
    study bucket. The shape check moved from playback_url to exists() and
    local_path; the requirement did not move at all.

    `bucket.heads` is the half that would otherwise go unnoticed. Returning
    False after asking S3 about `encounters/../../secrets/webcam.webm` would
    pass an assertion about the return value while still having put the crafted
    key on the wire — so what is asserted is that nothing was asked.
    """
    for bad in ("", "../../secrets", "a/b", "a\\b", "x" * 200):
        assert video.exists(bad) is False, bad
        assert video.upload_receipt(bad) is None, bad
        with pytest.raises(ValueError):
            video.local_path(bad)
    assert bucket.heads == [], (
        "a crafted session id was turned into an object key and looked up")


def test_the_object_key_is_derived_not_read_out_of_the_event_trail(
        sessions_root, bucket):
    """Was: the signed key is derived, never read back out of a file on disk.

    The event trail is written from a browser's confirmation POST, so its `key`
    field is attacker-influenced input. A reader that trusted it could be
    pointed at any object in the bucket. This is the same test one layer down:
    the bytes are found at the DERIVED key, and an event naming a different one
    changes nothing about which object is looked up.
    """
    sdir = _write_session(sessions_root, SESSION_ID, with_video=False)
    with (sdir / "events.jsonl").open("a", encoding="utf-8") as fh:
        fh.write(json.dumps({"t": None, "type": "video_uploaded",
                             "key": "some/other/prefix/secret.mp4",
                             "bytes": 1234}) + "\n")

    # Bytes at the foreign key only: still nothing to play, and the foreign key
    # is never asked about.
    bucket.objects["some/other/prefix/secret.mp4"] = 1234
    assert video.exists(SESSION_ID) is False
    assert bucket.heads == [f"encounters/{SESSION_ID}/webcam.webm"]

    # Bytes at the derived key: playable, whatever the event says.
    bucket.objects[f"encounters/{SESSION_ID}/webcam.webm"] = 8_400_000
    assert video.exists(SESSION_ID) is True
    assert video.video_key(SESSION_ID) == f"encounters/{SESSION_ID}/webcam.webm"
    assert "some/other/prefix" not in _blob(
        rp.build(SESSION_ID, assignment_id=ASSIGNMENT_ID))


def test_packet_media_offers_video_and_no_audio(packet):
    """The packet offers the video channel and does not offer the audio one.

    Was asserting `expires_in == rp.PLAYBACK_SECONDS` and an https:// URL, both
    of which described the presigned link rather than either half of what this
    test is for. expires_in is None on every branch now (see
    test_the_packet_hands_out_no_bearer_credential_for_the_recording for why
    that is the point rather than an omission) and the URL is this app's route.
    """
    media = packet["media"]
    assert media["video_available"] is True
    assert media["video_status"] == "ok"
    assert media["video_url"] == f"/api/rater/video/{ASSIGNMENT_ID}"
    assert media["expires_in"] is None
    assert media["note"] is None
    # The per-channel WAVs cannot be played back as a conversation, so they are
    # not offered at all.
    assert "audio" not in _blob(packet)


def test_packet_says_so_when_there_is_no_video(sessions_root):
    _write_session(sessions_root, SESSION_ID, with_video=False)
    media = rp.build(SESSION_ID, assignment_id=ASSIGNMENT_ID)["media"]
    assert media["video_url"] is None
    assert media["video_available"] is False
    assert "Not enough information to judge" in media["note"]


def test_a_video_that_cannot_be_addressed_is_not_reported_as_absent(sessions_root):
    """The third state, and the reason it is kept separate.

    Was reached by making video.playback_url raise; the fault it stood for was a
    signing failure, which is a deployment fault rather than a blip. Nothing
    signs anything now, so the state has narrowed to one cause — there are bytes
    and no usable assignment id to address them with — and it is reached the way
    it is actually reached, by building a packet outside a rating assignment.

    The guarantee is untouched and is the reason the state exists at all:
    reporting this as "no video recorded" would have a rater score from the
    transcript alone with nothing anywhere saying the video had been withheld.
    A rating made against a missing video cannot be undone; a blocked one can.
    """
    _write_session(sessions_root, SESSION_ID)
    media = rp.build(SESSION_ID)["media"]
    assert media["video_url"] is None
    assert media["video_available"] is True
    assert media["video_status"] == "unsigned"

    # The note is compared against the absent branch's rather than matched
    # against a phrase. What must be true is that a rater cannot read this as
    # "there was no camera" — that is the whole reason the two states are kept
    # apart — and pinning a sentence would make this test fail every time
    # somebody improves the wording, which teaches the next person to change the
    # assertion rather than think about it.
    _write_session(sessions_root, "s_1772653715_f54a85", with_video=False)
    absent = rp.build("s_1772653715_f54a85", assignment_id=ASSIGNMENT_ID)["media"]
    assert absent["video_status"] == "absent"
    assert media["note"] != absent["note"]
    assert absent["note"].lower()[:30] not in media["note"].lower()
    assert media["note"], "a state a rater must not rate needs to say so"

    # And an assignment id that is not the minted shape takes the same branch
    # rather than being interpolated into a URL that would 404 for the rater.
    for bad in ("as_../../x", "as_zz", "", "as_5f3c11a90b2d?x=1"):
        assert rp.build(SESSION_ID, assignment_id=bad)["media"][
            "video_url"] is None, bad


def test_the_bucket_refusing_to_answer_is_not_read_as_a_crash(
        sessions_root, bucket):
    """Credentials gone: False, and a packet, not an exception.

    exists() is documented as never raising, and this is why that matters here
    rather than in the video module's own tests: it is called once per packet
    while a rater's assignment list is built, so an exception is not one bad
    packet, it is every packet — the console fails to open and Phase 2 stops.
    NoCredentialsError is botocore's own type, raised where boto3 raises it, so
    this exercises head_video's real classification rather than a bespoke error
    it would never see in a deployment.
    """
    _write_session(sessions_root, SESSION_ID, with_video=False)
    bucket.fail_with = NoCredentialsError()
    assert video.exists(SESSION_ID) is False
    packet = rp.build(SESSION_ID, assignment_id=ASSIGNMENT_ID)
    # The rest of the packet is unaffected: one unanswerable question about one
    # field must not cost the rater the transcript.
    assert packet["transcript"]
    # Absent rather than "failed": no receipt was written for this encounter, so
    # nothing claims a recording was ever made.
    assert packet["media"]["video_status"] == "absent"


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
def wave(monkeypatch, wave_sessions, offline_bucket):
    """The wave's encounter ids, with SESSIONS_DIR pointed at it.

    `offline_bucket` is here for the same reason it is on `sessions_root`, and
    more sharply: a collected wave carries no local webcam files — the browser
    PUTs straight to S3 and nothing ever writes one next to the transcript — so
    every one of the 27 encounters below sends video.exists() to head_object.
    Twenty-seven signed HEADs against the real study bucket, per wave test, is
    what this module was doing before the stub went in.

    Nothing here writes to the wave directory. The recordings a test needs are
    seeded into the stub bucket, which is where a collected wave's recordings
    genuinely live.
    """
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
            # Exact, because this is the leak guard: the packet is what a crowd
            # rater's browser receives, and a key that arrives here uninvited is
            # study internals shipped to a stranger. Widening it is therefore a
            # deliberate act — script_mismatch and participant_channel_lost were
            # added so a rater can tell a mistranscribed turn and an unheard one
            # from a weak answer — and it belongs in this list, not around it.
            assert set(turn) == {"t", "role", "speaker", "text",
                                 "interrupted", "transcript_missing",
                                 "script_mismatch", "participant_channel_lost",
                                 "note"}
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


def test_wave_video_presence_follows_the_bytes_not_the_upload_receipts(wave, bucket):
    """Was: presence matches the receipts. It must not, and that is the fix.

    The old test read the wave's `video_uploaded` events, counted 25 of them,
    and asserted the packets agreed. It passed for the whole life of the defect
    it was standing next to: an encounter whose confirmation POST failed left no
    event, every reader therefore said it had no recording, and the bytes sat in
    the bucket unreachable forever, with no route and no CLI that could change
    anyone's mind. A test that pins the packet to the paperwork cannot notice
    that, because the paperwork is what is wrong.

    So presence is asserted against STORAGE, and the two are deliberately put
    out of step to prove which one decides:

      * s_1772764657_717245 has no receipt and is given bytes. It must be
        rateable. This is the encounter the old design lost.
      * one encounter that has a receipt is given no bytes. It must NOT promise
        a rater something to play — and must not be reported as "no camera"
        either, because a recording was made and something ate it.

    (record.json on disk says `"video": []` for every encounter — the stored
    copy is written before the browser confirms — which is why the packet
    rebuilds from events.jsonl rather than reading it. That was true before and
    is still true; it is simply no longer the thing presence rests on.)
    """
    _reference(wave, "s_1772764657_717245", "s_1773142745_384dad")
    recovered = "s_1772764657_717245"        # no receipt
    still_absent = "s_1773142745_384dad"     # no receipt
    lost = next(sid for sid in wave
                if sid not in (recovered, still_absent))  # has a receipt

    for sid in wave:
        if sid not in (lost, still_absent):
            bucket.objects[f"encounters/{sid}/webcam.webm"] = 8_400_000

    have_bytes = [sid for sid in wave if sid not in (lost, still_absent)]
    playable = [sid for sid in wave
                if rp.build(sid, assignment_id=ASSIGNMENT_ID)[
                    "media"]["video_status"] == "ok"]
    assert playable == have_bytes, (
        "the packet's idea of which encounters are rateable disagrees with "
        "which ones have bytes")

    # The encounter with no receipt whose bytes are there: rateable, and the
    # link is the assignment's, not the object's.
    media = rp.build(recovered, assignment_id=ASSIGNMENT_ID)["media"]
    assert media["video_available"] is True
    assert media["video_url"] == f"/api/rater/video/{ASSIGNMENT_ID}"
    assert recovered not in json.dumps(media)

    # The encounter with a receipt and no bytes: a fault to report, and
    # explicitly not the "no camera" wording, which is the confusion the whole
    # separate state exists to prevent.
    media = rp.build(lost, assignment_id=ASSIGNMENT_ID)["media"]
    assert media["video_status"] == "failed"
    assert media["video_url"] is None
    assert "WAS recorded" in media["note"]
    assert "no webcam recording was captured" not in media["note"].lower()

    # And the honest negative, unchanged: no receipt, no bytes, no camera.
    media = rp.build(still_absent, assignment_id=ASSIGNMENT_ID)["media"]
    assert media["video_available"] is False
    assert media["video_url"] is None
    assert "Not enough information to judge" in media["note"]


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
    """Same encounter, same packet. The media block included, now.

    Ratings are joined back by rating_code, so a packet that changed shape
    between two raters' sittings would mean they scored different material.

    This used to blank `media` before comparing, because a presigned URL carries
    a timestamp and a signature over it and therefore differed between two calls
    a second apart. That exemption is gone with the presigning: the playback URL
    is a fixed route over the assignment id, expires_in is None, and every field
    of the media block is now a function of the encounter and the assignment.
    Comparing the whole packet is what makes a future field that is not — a
    minted token, a nonce, a wall-clock deadline — fail here rather than be
    absorbed by a blanked key.
    """
    sid = wave[0]
    a = rp.build(sid, assignment_id=ASSIGNMENT_ID)
    b = rp.build(sid, assignment_id=ASSIGNMENT_ID)
    assert a == b
    assert a["media"]["expires_in"] is None


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
