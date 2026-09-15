"""Regression tests for the four app.py defects closed in the final round.

  * the gateway key reaching the participant's own socket, an HTTP 500 body a
    participant can provoke, and the encounter's permanent events.jsonl
  * the researcher's live channel carrying no failure of any kind, so a
    collapsing encounter and a healthy one were the same picture
  * orphaned encounter fragments drawn into a rating wave as if they were real
    encounters, silently
  * an encounter with no webcam recording reported "complete"

Nothing here opens a real socket or contacts anything. The credential seams are
exercised by raising the exception types the real libraries raise —
websockets.InvalidHeaderValue built by websockets, anthropic.AuthenticationError
built by the Anthropic SDK around a real httpx.Response — because a hand-rolled
stand-in would not carry the key in its message, which is the whole point.

The websocket handlers are called as the coroutines they are, with a recording
stand-in for the socket and for the Session. That is deliberate rather than
lazy: what is under test is this module's failure handling, and driving it
through a TestClient would need a scenario on disk, a consent record and a live
registry to reach the same three lines.
"""
from __future__ import annotations

import asyncio
import json
import sqlite3
import time
import types
from pathlib import Path

import httpx
import pytest
from fastapi import HTTPException, WebSocketDisconnect

import server.app as appmod
from server import llm

# A key shaped like a real LiteLLM one, wrapped across two lines the way a paste
# out of a terminal or a ticket wraps it. _cfg()'s strip() does not touch the
# interior newline, so this is exactly what reaches the Authorization header and
# exactly what websockets quotes back.
WRAPPED_KEY = "sk-LIVEKEY-AAAABBBBCCCC\nDDDDEEEEFFFF-TAIL"
FLAT_KEY = "sk-LIVEKEY-AAAABBBBCCCCDDDDEEEEFFFF-TAIL"


def _leaks(blob: str, key: str) -> bool:
    """Whether any part of `key` long enough to matter survives in `blob`.

    Fragments, not just the whole value: a wrapped key arrives in two pieces and
    redacting only the piece before the newline leaves live credential behind.
    """
    fragments = [part for part in key.split() if len(part) >= 6]
    return key in blob or any(fragment in blob for fragment in fragments)


@pytest.fixture()
def live_key(monkeypatch):
    """The process is configured with a credential, as a deployed task is."""
    monkeypatch.setitem(llm._FILE, "LITELLM_API_KEY", WRAPPED_KEY)
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    return WRAPPED_KEY


# --- stand-ins ---------------------------------------------------------------


class FakeWS:
    """A websocket that records instead of sending."""

    def __init__(self, incoming=()):
        self.accepted = False
        self.sent: list = []
        self.closed = None
        self._incoming = list(incoming)

    async def accept(self):
        self.accepted = True

    async def send_json(self, message):
        self.sent.append(message)

    async def close(self, code=1000):
        self.closed = code

    async def receive_json(self):
        if self._incoming:
            return self._incoming.pop(0)
        # What the participant closing the tab looks like from in here.
        raise WebSocketDisconnect(1000)

    def dump(self) -> str:
        return json.dumps(self.sent, default=str)


class FakeStore:
    def __init__(self):
        self.started_at = time.time() - 42.0
        self.events: list = []

    def event(self, etype, **fields):
        self.events.append({"type": etype, **fields})

    def dump(self) -> str:
        return json.dumps(self.events, default=str)


class FakeSession:
    """Enough Session for the two participant handlers and the researcher one."""

    def __init__(self, sid="s_1772460300_44c9a2"):
        self.id = sid
        self.store = FakeStore()
        # The real Session exposes `log` as a property alias for the store.
        self.log = self.store
        self.researcher_wss = set()
        self.participant_ws = None
        self.broadcasts: list = []
        self.shared_history: list = []
        self.steering_log: list = []
        self.triggered_branches: list = []
        self.name_lookup: dict = {}
        self.lock = asyncio.Lock()
        self.scenario = types.SimpleNamespace(
            id="S1A", title="Scenario", intro="Intro", mode="oneonone", cast=[],
        )
        self.primary_engine = None

    async def broadcast(self, message):
        self.broadcasts.append(message)

    def snapshot(self):
        return {"session_id": self.id, "agents": []}

    def append_user(self, text):
        self.shared_history.append({"speaker": "user", "text": text})

    def dump(self) -> str:
        return json.dumps(self.broadcasts, default=str)


def _anthropic_401_echoing_the_key(key: str):
    """The real SDK exception, whose str() is the gateway's body verbatim."""
    import anthropic

    body = {"error": {"message": f"Invalid API key: {key}",
                      "type": "authentication_error"}}
    response = httpx.Response(
        401, json=body,
        request=httpx.Request("POST", "https://api.ai.it.cornell.edu/v1/messages"),
    )
    exc = anthropic.AuthenticationError(json.dumps(body), response=response, body=body)
    assert key in str(exc), "the SDK stopped quoting the body; this test needs rewriting"
    return exc


def _wrapped_key_header_error(key: str):
    """The real websockets exception for a key pasted across two lines."""
    from websockets.exceptions import InvalidHeaderValue

    exc = InvalidHeaderValue("Authorization", f"Bearer {key}")
    assert key in str(exc), "websockets stopped quoting the header; rewrite this test"
    return exc


@pytest.fixture()
def voice_session(monkeypatch):
    """A voice socket whose registry, consent gate and run join are stood in for."""
    session = FakeSession()
    monkeypatch.setattr(appmod.registry, "create", lambda *a, **k: session)
    monkeypatch.setattr(appmod.registry, "drop", lambda sid: None)
    monkeypatch.setattr(appmod, "_consented_participant",
                        lambda pid: {"participant_id": pid, "consent_given": 1})
    monkeypatch.setattr(appmod, "_run_context", lambda pid, run: None)
    return session


# --- the credential must not leave this process ------------------------------


def test_a_wrapped_key_does_not_reach_the_participant_socket_or_the_record(
        live_key, voice_session, monkeypatch):
    """The CRITICAL, at the sink that carries it.

    RealtimeVoiceSessionRunner.run() has no except of its own, so a failure in
    the first connect() to the realtime gateway lands in this handler whole —
    and that is precisely the exception that carries the credential. Both sinks
    are permanent: events.jsonl is the encounter's IRB record and is shipped
    whole in the per-session download zip, and the frame goes down a socket held
    by a recruited member of the public.
    """
    failure = _wrapped_key_header_error(live_key)

    class Boom:
        def __init__(self, session, ws):
            pass

        async def run(self):
            raise failure

    monkeypatch.setattr(appmod, "RealtimeVoiceSessionRunner", Boom)
    ws = FakeWS()

    asyncio.run(appmod.ws_participant_voice(
        ws, scenario="S1A", participant_id="p_1", model=None, launch=None,
        key=None, run=None,
    ))

    errors = [m for m in ws.sent if m.get("type") == "error"]
    assert errors, "the participant must still be told the encounter failed"
    assert not _leaks(ws.dump(), live_key)
    assert not _leaks(voice_session.store.dump(), live_key)
    assert not _leaks(voice_session.dump(), live_key)
    # Redacted, not suppressed: an operator reading events.jsonl still has to be
    # able to see that this was an Authorization header the transport rejected.
    assert "<redacted>" in errors[0]["message"]
    logged = [e for e in voice_session.store.events
              if e["type"] == "error" and e.get("where") == "voice_ws"]
    assert logged and "<redacted>" in logged[0]["message"]


def test_a_gateway_401_echoing_the_key_does_not_reach_the_text_socket(
        live_key, voice_session, monkeypatch):
    """The other credential-carrying shape, on the other participant socket.

    A gateway that quotes the key it was sent back in its 401 body reaches this
    handler as `e`, and anthropic's str() reproduces that body verbatim.
    """
    failure = _anthropic_401_echoing_the_key(FLAT_KEY)
    monkeypatch.setitem(llm._FILE, "LITELLM_API_KEY", FLAT_KEY)

    async def stream_reply(*a, **k):
        raise failure
        yield ""  # pragma: no cover - unreachable, makes this an async generator

    voice_session.primary_engine = types.SimpleNamespace(
        agent=types.SimpleNamespace(id="alex"),
        model="nto.claude-sonnet-4-5",
        live_notes=[],
        stream_reply=stream_reply,
    )
    ws = FakeWS(incoming=[{"type": "user_text", "text": "hello"}])

    asyncio.run(appmod.ws_participant_text(
        ws, scenario="S1A", participant_id=None, model=None, launch=None,
        key=None, run=None,
    ))

    assert not _leaks(ws.dump(), FLAT_KEY)
    assert not _leaks(voice_session.store.dump(), FLAT_KEY)
    assert not _leaks(voice_session.dump(), FLAT_KEY)
    logged = [e for e in voice_session.store.events
              if e["type"] == "error" and e.get("where") == "participant_ws"]
    assert logged, "the encounter record must still say the encounter failed"
    # The failure that arrived has to be the gateway's, or this test would pass
    # on any stand-in's AttributeError and prove nothing about the credential.
    assert "authentication_error" in logged[0]["message"]
    assert "<redacted>" in logged[0]["message"]


@pytest.mark.parametrize("route,label", [
    ("api_post_score", "scoring failed"),
    ("api_post_debrief", "debrief failed"),
])
def test_the_500_a_participant_can_provoke_does_not_carry_the_key(
        route, label, tmp_path, monkeypatch, live_key):
    """/score and /debrief are invoked by the participant's own feedback overlay.

    Both call the model gateway as their first act, so the credential-carrying
    exception arrives as the `e` this route interpolates into an HTTP 500 body.
    """
    monkeypatch.setitem(llm._FILE, "LITELLM_API_KEY", FLAT_KEY)
    failure = _anthropic_401_echoing_the_key(FLAT_KEY)

    sessions = tmp_path / "sessions"
    sid = "s_1772460300_44c9a2"
    (sessions / sid).mkdir(parents=True)
    monkeypatch.setattr(appmod, "SESSIONS_DIR", sessions)
    monkeypatch.setattr(appmod, "SESSION_KEY", "")

    from server import debrief, scoring

    def boom(*a, **k):
        raise failure

    monkeypatch.setattr(scoring, "score_session", boom)
    monkeypatch.setattr(debrief, "generate_debrief", boom)

    with pytest.raises(HTTPException) as caught:
        asyncio.run(getattr(appmod, route)(
            sid, force=False, model=None, participant_id=None, key=None,
        ))

    assert caught.value.status_code == 500
    assert label in caught.value.detail
    assert not _leaks(caught.value.detail, FLAT_KEY)
    assert "<redacted>" in caught.value.detail


# The AWS documentation's own example secret, which is the shape a real one has:
# 40 characters of base64 alphabet and no vendor prefix to recognise it by.
AWS_SECRET = "wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY"


def test_the_bucket_preflights_detail_is_scrubbed_before_it_is_kept(monkeypatch):
    """The second credentialed seam, which fails the way the first one does.

    `detail` is a raw str(exc) off boto3 client construction and it is kept in
    two places that outlive the failure: the boot line (CloudWatch, 90 days) and
    /health's storage block once an operator authenticates.
    """
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", AWS_SECRET)
    monkeypatch.setattr(appmod, "_STORAGE_PREFLIGHT", {})
    monkeypatch.setattr(appmod._video, "storage_preflight", lambda: {
        "ok": False, "bucket": "rf-study-encounters", "region": "us-east-1",
        "readable": False, "writable": False, "error_code": None,
        "detail": ("Invalid header value b'AWS4-HMAC-SHA256 "
                   f"Credential={AWS_SECRET}/20260909/us-east-1'"),
    })

    appmod._check_storage()

    detail = appmod._STORAGE_PREFLIGHT["detail"]
    assert AWS_SECRET not in detail
    assert "<redacted>" in detail

    monkeypatch.setattr(appmod, "SESSION_KEY", "researcher-key")
    body = asyncio.run(appmod.health(key="researcher-key"))
    assert AWS_SECRET not in json.dumps(body["storage"], default=str)


# --- the researcher's live failure channel -----------------------------------

CONTRACT_FIELDS = {"type", "kind", "t", "agent_id", "detail", "severity"}


def test_an_encounter_that_dies_outright_reaches_the_researchers_channel(
        live_key, voice_session, monkeypatch):
    """A gateway that will not connect at all never reaches the runner's own
    emitters, so this module has to report it or nobody does."""
    failure = _wrapped_key_header_error(live_key)

    class Boom:
        def __init__(self, session, ws):
            pass

        async def run(self):
            raise failure

    monkeypatch.setattr(appmod, "RealtimeVoiceSessionRunner", Boom)

    asyncio.run(appmod.ws_participant_voice(
        FakeWS(), scenario="S1A", participant_id="p_1", model=None, launch=None,
        key=None, run=None,
    ))

    events = [m for m in voice_session.broadcasts
              if m.get("type") == "encounter_event"]
    assert len(events) == 1
    frame = events[0]
    assert set(frame) == CONTRACT_FIELDS, "the frame shape is a three-way contract"
    assert frame["kind"] == "voice_ws_error"
    assert frame["severity"] == "error"
    assert isinstance(frame["t"], float)
    assert not _leaks(frame["detail"], live_key)


def test_a_researcher_who_joins_late_is_shown_the_failures_that_already_happened(
        monkeypatch):
    """The whole point of a monitoring surface.

    A researcher opens this console *because* something looks wrong, which means
    they almost always connect after the first failure rather than before it. A
    channel carrying only future events shows a clean screen for an encounter
    whose director died two minutes ago.
    """
    session = FakeSession()
    monkeypatch.setattr(appmod, "SESSION_KEY", "")
    monkeypatch.setattr(appmod.registry, "get", lambda sid: session)

    appmod._arm_encounter_event_log(session)
    already = [
        {"type": "encounter_event", "kind": "auto_steer_error", "t": 31.5,
         "agent_id": "alex", "detail": "RateLimitError", "severity": "error"},
        {"type": "encounter_event", "kind": "trigger_undelivered", "t": 88.0,
         "agent_id": "sam", "detail": None, "severity": "warn"},
    ]
    for frame in already:
        asyncio.run(session.broadcast(frame))

    ws = FakeWS()
    asyncio.run(appmod.ws_researcher(ws, session_id=session.id, key=None))

    replayed = [m for m in ws.sent if m.get("type") == "encounter_event"]
    assert replayed == already, "both failures, in the order they happened"
    assert ws.sent[0]["type"] == "state", "the snapshot still leads"


def test_the_replay_log_keeps_the_newest_and_is_bounded(monkeypatch):
    """A gateway refusing every call produces one event per turn.

    Unbounded, that is an unbounded send on connect; and what a researcher needs
    off this list is what is happening now, not the first failure of the hour.
    """
    session = FakeSession()
    appmod._arm_encounter_event_log(session)
    limit = appmod.ENCOUNTER_EVENT_REPLAY_LIMIT

    for i in range(limit + 50):
        asyncio.run(session.broadcast(
            {"type": "encounter_event", "kind": "transcript_missing", "t": float(i),
             "agent_id": None, "detail": None, "severity": "warn"}))

    assert len(session.encounter_events) == limit
    assert session.encounter_events[0]["t"] == 50.0
    assert session.encounter_events[-1]["t"] == float(limit + 49)


def test_arming_twice_does_not_double_record_and_other_frames_pass_through():
    """Both participant sockets arm on create and the researcher socket arms on
    connect, so this runs two and three times for one session."""
    session = FakeSession()
    appmod._arm_encounter_event_log(session)
    appmod._arm_encounter_event_log(session)

    asyncio.run(session.broadcast({"type": "state", "session_id": session.id}))
    asyncio.run(session.broadcast(
        {"type": "encounter_event", "kind": "director_fallback", "t": 1.0,
         "agent_id": "alex", "detail": None, "severity": "warn"}))

    assert len(session.encounter_events) == 1
    # Still delivered to whoever is connected, not diverted into the log.
    assert [m["type"] for m in session.broadcasts] == ["state", "encounter_event"]


def test_reporting_a_failure_never_takes_down_the_encounter(voice_session):
    """A monitoring frame that could kill the encounter it reports on would be
    worse than no frame."""
    async def explode(message):
        raise RuntimeError("the researcher socket is gone")

    voice_session.broadcast = explode

    asyncio.run(appmod._report_encounter_failure(
        voice_session, "voice_ws_error", "boom"))


# --- orphaned encounters must not be drawn into a rating wave ----------------


@pytest.fixture()
def index_db(tmp_path, monkeypatch):
    """A real session index, built by storage's own schema, in a temp directory."""
    from server import storage

    monkeypatch.setattr(storage, "DATA_DIR", tmp_path)
    monkeypatch.setattr(storage, "SESSIONS_DIR", tmp_path / "sessions")
    monkeypatch.setattr(storage, "PARTICIPANTS_DIR", tmp_path / "participants")
    monkeypatch.setattr(storage, "DB_PATH", tmp_path / "index.db")
    storage.init_storage()
    return tmp_path / "index.db"


def _insert(db: Path, rows):
    conn = sqlite3.connect(db)
    conn.executemany(
        "INSERT INTO sessions (id, participant_id, scenario, model, started_at,"
        " status, n_turns, dir, cohort) VALUES (?,?,?,?,?,?,?,?,?)",
        rows,
    )
    conn.commit()
    conn.close()


def _runs_dir(tmp_path, monkeypatch, runs_json):
    from server import runs

    d = tmp_path / "runs"
    d.mkdir(exist_ok=True)
    for i, run in enumerate(runs_json):
        (d / f"run_{i}.json").write_text(json.dumps(run), encoding="utf-8")
    monkeypatch.setattr(runs, "RUNS_DIR", d)
    return d


REAL = "s_1772460300_44c9a2"
FRAGMENT = "s_1772460301_bbbbbb"


def _two_encounters(db):
    _insert(db, [
        (REAL, "p_1", "S1A", "m", 1772460300.0, "closed", 14,
         f"data/sessions/{REAL}", "study"),
        (FRAGMENT, "p_1", "S1A", "m", 1772460301.0, "closed", 3,
         f"data/sessions/{FRAGMENT}", "study"),
    ])


def test_a_reconnect_fragment_is_withheld_from_the_rater_draw(
        index_db, tmp_path, monkeypatch):
    """A mid-encounter socket drop followed by Reconnect mints a brand new
    session and leaves the first, truncated half behind — closed, with turns, in
    cohort 'study'. It appears in no run's completed[], and it used to be
    assignable: a rater handed a two-minute fragment to score as a full
    encounter, carried in the reliability denominator.
    """
    _two_encounters(index_db)
    _runs_dir(tmp_path, monkeypatch, [{
        "run_id": "r_1",
        "completed": [{"session_id": REAL, "scenario": "S1A"}],
    }])

    rateable, withheld = appmod._rateable_split("study")

    assert rateable == [REAL]
    assert [w["session_id"] for w in withheld] == [FRAGMENT]
    assert withheld[0]["n_turns"] == 3
    assert "no run recorded this encounter as completed" in withheld[0]["reason"]


def test_a_wave_with_no_run_files_is_not_withheld_wholesale(index_db, tmp_path,
                                                            monkeypatch):
    """The failure this filter must not become.

    An absent or unreadable runs directory is not evidence that every encounter
    is an orphan. Treating it as such would withhold an entire wave from rating
    on the strength of a missing directory, which is far worse than the defect
    being fixed.
    """
    from server import runs

    _two_encounters(index_db)
    monkeypatch.setattr(runs, "RUNS_DIR", tmp_path / "nothing-here")

    rateable, withheld = appmod._rateable_split("study")

    assert sorted(rateable) == sorted([REAL, FRAGMENT])
    assert withheld == []


def test_run_files_that_claim_nothing_are_not_treated_as_a_verdict(
        index_db, tmp_path, monkeypatch):
    """Run files present but no completed encounter in any of them: still not a
    statement that all 26 encounters in the wave are fragments."""
    _two_encounters(index_db)
    _runs_dir(tmp_path, monkeypatch, [{"run_id": "r_1", "completed": []}])

    rateable, withheld = appmod._rateable_split("study")

    assert sorted(rateable) == sorted([REAL, FRAGMENT])
    assert withheld == []


def test_the_draw_reports_what_it_refused_rather_than_filtering_in_silence(
        index_db, tmp_path, monkeypatch):
    """A count a researcher reads on the assignment response is a wave they can
    still investigate; the same number discovered as odd two-minute packets in a
    rater's queue is a wave already spent."""
    import server.raters as raters_mod

    _two_encounters(index_db)
    _runs_dir(tmp_path, monkeypatch, [{
        "run_id": "r_1", "completed": [{"session_id": REAL}],
    }])
    monkeypatch.setattr(appmod, "SESSION_KEY", "")
    monkeypatch.setattr(
        raters_mod, "assign",
        lambda sids, rids, per_encounter=3, seed=None: [
            {"assignment_id": "a_1", "session_id": sids[0], "rater_id": rids[0]}],
    )

    body = asyncio.run(appmod.api_rater_assignments_create(
        {"rater_ids": ["r_a"], "cohort": "study", "per_encounter": 1}, key=None,
    ))

    assert body["n_sessions"] == 1
    assert body["n_withheld"] == 1
    assert body["withheld"][0]["session_id"] == FRAGMENT


def test_a_cohort_that_is_entirely_fragments_says_so(index_db, tmp_path,
                                                     monkeypatch):
    """"No encounters to assign" against a cohort that plainly has encounters in
    it sends a researcher hunting for a bad cohort name."""
    _two_encounters(index_db)
    _runs_dir(tmp_path, monkeypatch, [{
        "run_id": "r_1", "completed": [{"session_id": "s_1772460999_zzzzzz"}],
    }])
    monkeypatch.setattr(appmod, "SESSION_KEY", "")

    with pytest.raises(HTTPException) as caught:
        asyncio.run(appmod.api_rater_assignments_create(
            {"rater_ids": ["r_a"], "cohort": "study"}, key=None,
        ))

    assert caught.value.status_code == 400
    detail = caught.value.detail
    assert "withheld" in detail and "2" in detail
    assert "session_ids" in detail, "and how to rate them anyway"


def test_naming_session_ids_explicitly_is_still_a_way_round_the_filter(
        index_db, tmp_path, monkeypatch):
    """Hand-picking a wave is a researcher's judgement; second-guessing it would
    take away the only escape hatch from the run join."""
    import server.raters as raters_mod

    _two_encounters(index_db)
    _runs_dir(tmp_path, monkeypatch, [{"run_id": "r_1", "completed": []}])
    monkeypatch.setattr(appmod, "SESSION_KEY", "")
    monkeypatch.setattr(
        raters_mod, "assign",
        lambda sids, rids, per_encounter=3, seed=None: [{"session_id": s}
                                                        for s in sids],
    )

    body = asyncio.run(appmod.api_rater_assignments_create(
        {"rater_ids": ["r_a"], "session_ids": [FRAGMENT], "per_encounter": 1},
        key=None,
    ))

    assert body["n_sessions"] == 1 and body["n_withheld"] == 0


# --- the video channel is part of "complete" ---------------------------------


def _manifest(**over):
    m = {
        "status": "closed",
        "n_turns": 14,
        "audio": {"user_audio_duration_s": 190.0,
                  "assistant_audio_duration_s_by_agent": {"alex": 210.0}},
    }
    m.update(over)
    return m


@pytest.mark.parametrize("video,expected", [
    ("ok", "complete"),
    ("absent", "partial"),
    ("failed", "partial"),
    ("unknown", "complete"),
])
def test_the_status_badge_consults_the_video_channel(video, expected):
    """The webcam recording is the artefact Phase 2 rates. An encounter nobody
    filmed used to come back "complete" and render as a green badge, which is
    what a researcher triages a wave on.

    "unknown" never downgrades: a confident wrong answer here routes a whole
    day's decisions.
    """
    assert appmod._encounter_status(_manifest(), video) == expected


def test_a_text_mode_encounter_is_not_penalised_for_having_no_camera():
    """No audio channel expected means no camera either; a missing recording is
    not a missing channel there."""
    text_mode = _manifest(audio={})
    assert appmod._encounter_status(text_mode, "absent") == "complete"


@pytest.mark.parametrize("events,expected", [
    ([], "absent"),
    ([{"type": "video_uploaded", "bytes": 0, "error": "put_http_403"}], "failed"),
    ([{"type": "video_uploaded", "bytes": 8_400_000}], "ok"),
    # Last wins: the page retries the confirm, so a "failed" routinely sits
    # ahead of the "ok" one.
    ([{"type": "video_uploaded", "bytes": 0},
      {"type": "video_uploaded", "bytes": 8_400_000}], "ok"),
])
def test_the_video_probe_speaks_encounter_records_three_words(events, expected,
                                                              tmp_path):
    d = tmp_path / "s_1772460300_44c9a2"
    d.mkdir()
    (d / "events.jsonl").write_text(
        "".join(json.dumps({"t": 1.0, **e}) + "\n" for e in events),
        encoding="utf-8",
    )
    assert appmod._video_state(d) == expected


def test_an_unreadable_encounter_is_unknown_rather_than_absent(tmp_path):
    """"Nobody filmed this" and "I could not tell" are different claims, and
    only one of them should downgrade an encounter."""
    d = tmp_path / "s_1772460300_44c9a2"
    d.mkdir()
    assert appmod._video_state(d) == "unknown"


def test_a_local_dev_capture_counts(tmp_path):
    """encounter_record's own rule: a webcam* file in the session dir is a
    recording even though it never went near S3."""
    d = tmp_path / "s_1772460300_44c9a2"
    d.mkdir()
    (d / "webcam.webm").write_bytes(b"\x00" * 16)
    assert appmod._video_state(d) == "ok"


def test_the_encounter_listing_row_reports_the_camera_it_never_had(
        tmp_path, monkeypatch):
    """End to end on the route the evidence console renders.

    The listing is the wave-level triage surface: a green badge on a camera-less
    encounter routes a whole day's decisions wrong, and `video` is carried in its
    own right so the console can tell "never filmed" from "recorded and lost"
    without re-fetching every record.
    """
    from server import storage

    sessions = tmp_path / "sessions"
    d = sessions / "s_1772460300_44c9a2"
    d.mkdir(parents=True)
    (d / "manifest.json").write_text(json.dumps(_manifest(scenario="S1A")),
                                     encoding="utf-8")
    (d / "events.jsonl").write_text("", encoding="utf-8")
    monkeypatch.setattr(storage, "SESSIONS_DIR", sessions)
    monkeypatch.setattr(appmod, "SESSION_KEY", "")

    rows = asyncio.run(appmod.api_encounters(key=None, limit=10, cohort=None))

    assert len(rows) == 1
    assert rows[0]["video"] == "absent"
    assert rows[0]["status"] == "partial"


# --- the consent guard is called, not merely available -----------------------


def test_the_startup_check_asks_consent_check_about_the_real_config(monkeypatch):
    """Contract (b): the judgement lives in consent_check; this module calls it.

    Asserted on the loaded config rather than on a stub, because a check wired to
    something other than the file the app serves would pass a unit test and
    protect nobody.
    """
    seen = {}

    def blocker(cfg):
        seen["cfg"] = cfg
        return "the form still names no researcher"

    fake = types.ModuleType("server.consent_check")
    fake.consent_fielding_blocker = blocker
    monkeypatch.setitem(__import__("sys").modules, "server.consent_check", fake)

    assert appmod.check_consent_fielding() == "the form still names no researcher"
    assert seen["cfg"] == appmod._load_consent()


def test_a_missing_consent_check_is_said_out_loud_rather_than_passing(monkeypatch):
    """An absent check must not read as a clean bill of health."""
    import sys

    monkeypatch.setitem(sys.modules, "server.consent_check", None)

    reason = appmod.check_consent_fielding()

    assert reason and "could not be loaded" in reason


def test_the_check_runs_at_startup():
    names = [h.__name__ for h in appmod.app.router.on_startup]
    assert "_check_consent_on_startup" in names
