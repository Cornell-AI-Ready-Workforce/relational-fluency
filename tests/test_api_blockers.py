"""What the API does when AWS, the disk, or the participant says no.

These are the seams that cannot be exercised today because there is no S3
credential and no bucket: every one of them is driven here with the real
botocore exception types (ClientError with real codes, NoCredentialsError,
EndpointConnectionError) against a stub client, so the wiring is proven before
the credential exists rather than after a wave is collected.

Nothing here opens a socket to anything. The boto3 client is replaced wholesale
(server.video._s3), the session directory is a tmp_path, and the session index
is a throwaway sqlite file — the fixture wave itself is never written to.

The three questions the suite keeps asking:
  - does a synchronous AWS call still run on the event loop that carries every
    live encounter's audio (it must not),
  - does a failure leave a fact in the encounter's own record (it must), and
  - is a capture gate a real gate or a decorative one.
"""
from __future__ import annotations

import asyncio
import inspect
import json
import os
import sqlite3
import subprocess
import sys
import threading
import time
from pathlib import Path

import httpx
import pytest
from botocore.exceptions import (
    ClientError, EndpointConnectionError, NoCredentialsError,
)
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from server import app as appmod
from server import storage as storagemod
from server import video

SESSION_ID = "s_1772460300_44c9a2"
OWNER = "p_1772460300_4327ae"
KEY = "test-session-key"
# A rating assignment over that encounter, in the shape raters.py mints (as_
# plus twelve hex) and checks before it will interpolate one. rater_packet
# builds the playback URL out of this rather than out of the session id, so a
# _media() call made without one is not the call the rater console makes: it
# takes the branch that cannot address the recording. Every _media() below
# passes it for that reason — a test that omits an argument production never
# omits stops exercising production the moment that argument starts mattering,
# which is exactly how this file came to pin a design that had been deleted.
ASSIGNMENT_ID = "as_4c9a2f10b3d7"


# --- stubs -------------------------------------------------------------------

def aws_error(code: str, op: str = "HeadObject", status: int = 403) -> ClientError:
    """A real botocore ClientError, shaped the way S3 shapes one."""
    return ClientError(
        {"Error": {"Code": code, "Message": f"{code} (stub)"},
         "ResponseMetadata": {"HTTPStatusCode": status}},
        op,
    )


# Every failure mode a first-apply misconfiguration or a bad day actually
# produces. The point of the list is that none of them may reach a route
# uncaught, and none of them may cost the encounter its event.
AWS_FAILURES = [
    aws_error("AccessDenied"),
    aws_error("InvalidAccessKeyId"),
    aws_error("ExpiredToken"),
    aws_error("NoSuchBucket", status=404),
    aws_error("PermanentRedirect", status=301),
    aws_error("SlowDown", status=503),
    aws_error("KMS.DisabledException"),
    NoCredentialsError(),
    EndpointConnectionError(endpoint_url="https://s3.us-east-1.amazonaws.com"),
]


class FakeS3:
    """Stands in for the boto3 s3 client: answers, stalls, or raises."""

    def __init__(self, *, size: int = 0, error: BaseException = None,
                 delay: float = 0.0, missing: bool = False):
        self.size = size
        self.error = error
        self.delay = delay
        self.missing = missing
        self.head_calls = 0
        self.put_calls = 0
        self.signed = []

    def _maybe_fail(self):
        if self.delay:
            time.sleep(self.delay)
        if self.error is not None:
            raise self.error

    def head_object(self, **kw):
        self.head_calls += 1
        self._maybe_fail()
        if self.missing:
            raise aws_error("404", status=404)
        return {"ContentLength": self.size}

    def head_bucket(self, **kw):
        self._maybe_fail()
        return {}

    def put_object(self, **kw):
        self.put_calls += 1
        self._maybe_fail()
        return {}

    def generate_presigned_url(self, op, Params=None, ExpiresIn=None):
        # Signing is arithmetic in the real client too, but resolving the
        # credentials to sign with is not — which is why this can raise.
        self._maybe_fail()
        self.signed.append((op, Params["Key"]))
        return f"https://s3.invalid/{Params['Key']}?sig=x&op={op}"


# --- fixtures ----------------------------------------------------------------

@pytest.fixture()
def sessions_root(tmp_path, monkeypatch):
    """One finished encounter on disk, owned by a known participant."""
    root = tmp_path / "sessions"
    sdir = root / SESSION_ID
    sdir.mkdir(parents=True)
    (sdir / "manifest.json").write_text(
        json.dumps({"session_id": SESSION_ID, "participant_id": OWNER,
                    "scenario": "conflict", "status": "closed"}),
        encoding="utf-8")
    (sdir / "events.jsonl").write_text("", encoding="utf-8")
    monkeypatch.setattr(appmod, "SESSIONS_DIR", root)
    monkeypatch.setattr(video, "SESSIONS_DIR", root)
    return sdir


@pytest.fixture()
def s3(monkeypatch):
    """A healthy bucket by default; individual tests swap in the failure."""
    stub = FakeS3(missing=True)
    monkeypatch.setattr(video, "_s3", stub)
    return stub


@pytest.fixture()
def served(monkeypatch):
    """The researcher key and a host the test harness can address."""
    monkeypatch.setattr(appmod, "SESSION_KEY", KEY)
    if appmod.ALLOWED_HOSTS and "testserver" not in appmod.ALLOWED_HOSTS:
        monkeypatch.setattr(appmod, "ALLOWED_HOSTS",
                            list(appmod.ALLOWED_HOSTS) + ["testserver"])


@pytest.fixture()
def client(served):
    return TestClient(appmod.app, raise_server_exceptions=False)


def on_the_loop(call):
    """Drive one request with the event loop in THIS thread, and say which
    thread that was.

    TestClient runs the whole app in a portal thread of its own, so "not the
    main thread" proves nothing there — a route that blocks the loop passes it.
    The question these tests ask is whether the blocking work ran on the thread
    the loop is on, so the loop has to be somewhere known: here.
    """
    async def run():
        transport = httpx.ASGITransport(app=appmod.app)
        async with httpx.AsyncClient(transport=transport,
                                     base_url="http://testserver") as ac:
            return threading.get_ident(), await call(ac)

    return asyncio.run(run())


def events(sdir: Path) -> list:
    return [json.loads(line) for line in
            (sdir / "events.jsonl").read_text(encoding="utf-8").splitlines() if line.strip()]


def video_events(sdir: Path) -> list:
    return [e for e in events(sdir) if e.get("type") == "video_uploaded"]


def presign_url(session_id: str = SESSION_ID) -> str:
    return f"/api/sessions/{session_id}/video-upload-url"


def confirm_url(session_id: str = SESSION_ID) -> str:
    return f"/api/sessions/{session_id}/video-uploaded"


# --- B1: the AWS call must leave the event loop -------------------------------

def test_the_webcam_routes_are_not_coroutines():
    """A plain `def` route is run in FastAPI's threadpool; `async def` is not.

    This is the whole fix for the stall, so it is asserted directly as well as
    behaviourally below — the behavioural test would still pass if someone put
    the blocking call back behind a run_in_threadpool, but silently reverting
    the route to `async def` is the regression that matters.
    """
    assert not asyncio.iscoroutinefunction(appmod.api_video_upload_url)
    assert not asyncio.iscoroutinefunction(appmod.api_video_uploaded)


def test_health_never_pays_the_required_env_source_scan_on_the_loop():
    """/health is `async def`, so anything slow inside it runs ON the loop.

    One thing inside it is slow exactly once. storage.missing_required_env() ->
    _declared_required_env() ast.parse()s every .py file under server/ and
    memoises the result; measured on this tree, 157 ms cold on CPython 3.12 and
    250-300 ms cold on 3.13 against ~0.2 ms warm. A cold call from the route is
    a quarter-second in which the audio relay, the silence detector and every
    concurrent encounter stop.

    It reached CI as the sibling test below going red about one run in seven on
    windows-latest x 3.13, which is misleading twice over: the stall is not the
    S3 HEAD that test is named for (it reproduces with head_calls == 0), and
    3.12 was never safe either — it simply had 0.15 s of headroom under the
    0.35 s budget where 3.13 has none.

    server.app._prime_required_env_scan() pays it at import instead. Asserted
    directly, not just through the timing test: a timing assertion that fails
    one run in seven on one cell is not a regression guard anybody can act on,
    and the production hazard is real on every interpreter.
    """
    from server import storage

    assert str(storage._SERVER_DIR) in storage._SCANNED_REQUIRED_ENV, (
        "importing server.app must leave the REQUIRED_ENV declaration scan "
        "warm; without it the first /health of the process ast.parse()s 33 "
        "source files on the event loop"
    )

    t0 = time.perf_counter()
    appmod._missing_required_env_for_health()
    assert time.perf_counter() - t0 < 0.05, "the scan is not actually memoised"


def test_priming_the_scan_did_not_cache_the_environment_too(monkeypatch):
    """The half of /health that must NOT be memoised.

    Only the static REQUIRED_ENV *declaration* is cached. The route recomputes
    from os.environ per request on purpose — a task redeployed with the value,
    or a secret that resolves late, has to stop showing as missing without a
    restart — and a warming step that quietly froze the answer would reintroduce
    the silent void the config block exists to close, in a form that looks
    healthy.
    """
    from server import storage

    name = "UPSTREAM_CONSENT_VERSION"
    assert name in storage._declared_required_env(), "test's premise moved"

    monkeypatch.delenv(name, raising=False)
    assert name in appmod._missing_required_env_for_health()

    monkeypatch.setenv(name, "2026-09-12.v4")
    assert name not in appmod._missing_required_env_for_health(), (
        "a value that arrived after boot is still reported missing"
    )


def test_a_slow_head_does_not_freeze_the_loop_and_only_happens_once(
        sessions_root, served, s3):
    """A 600 ms HEAD used to be 1.2 s of total silence: two HEADs, on the loop.

    The ticker stands in for the audio relay and the silence detector that fires
    the planted probes. If any gap in it approaches the length of the HEAD, the
    loop was blocked and every concurrent encounter was blocked with it.
    """
    s3.missing = True
    s3.delay = 0.6

    async def drive():
        gaps = []

        async def ticker():
            last = time.perf_counter()
            while True:
                await asyncio.sleep(0.02)
                now = time.perf_counter()
                gaps.append(now - last)
                last = now

        transport = httpx.ASGITransport(app=appmod.app)
        async with httpx.AsyncClient(transport=transport,
                                     base_url="http://testserver") as ac:
            beat = asyncio.create_task(ticker())
            r = await ac.get(presign_url(), params={"participant_id": OWNER})
            health = await ac.get("/health")
            beat.cancel()
            await asyncio.gather(beat, return_exceptions=True)
        return r, health, gaps

    r, health, gaps = asyncio.run(drive())
    assert r.status_code == 200
    assert health.status_code == 200
    assert s3.head_calls == 1, "the route HEADed twice for one question"
    assert gaps, "the ticker never ran at all"
    assert max(gaps) < 0.35, f"the loop stalled for {max(gaps):.2f}s"


# --- B2 / B17 / B19 / B21 / B50: an S3 failure is recorded, not raised --------

@pytest.mark.parametrize("exc", AWS_FAILURES, ids=lambda e: type(e).__name__ + getattr(
    e, "response", {}).get("Error", {}).get("Code", ""))
def test_a_failed_confirm_still_writes_the_event(sessions_root, client, s3, exc):
    """The confirm endpoint always writes. That is what makes a loss recoverable.

    Before, uploaded_size re-raised anything but a 404 and the append sat after
    the call, so the exact case where a recording might be sitting in the bucket
    left no trace at all — and the rater packet then told a human rater the
    encounter had no video.
    """
    s3.error = exc
    r = client.post(confirm_url(), params={"participant_id": OWNER, "key": KEY})

    assert r.status_code == 503, "an AWS error is not a bug in this process"
    assert r.status_code != 500
    evs = video_events(sessions_root)
    assert len(evs) == 1
    assert evs[0]["status"] == "failed"
    assert evs[0]["bytes"] is None       # not zero: nobody looked and saw nothing
    assert evs[0]["error"]               # a short code, so a person can act on it
    assert evs[0]["key"] == f"encounters/{SESSION_ID}/webcam.webm"
    assert r.json()["status"] == "failed"


def test_the_recorded_error_names_the_aws_code(sessions_root, client, s3):
    s3.error = aws_error("AccessDenied")
    client.post(confirm_url(), params={"participant_id": OWNER, "key": KEY})
    assert video_events(sessions_root)[0]["error"] == "AccessDenied"

    s3.error = NoCredentialsError()
    client.post(confirm_url(), params={"participant_id": OWNER, "key": KEY})
    assert video_events(sessions_root)[1]["error"] == "NoCredentialsError"


def test_a_confirmed_upload_is_status_ok_with_no_error(sessions_root, client, s3):
    s3.missing = False
    s3.size = 148_221
    r = client.post(confirm_url(), params={"participant_id": OWNER, "key": KEY})
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is True and body["bytes"] == 148_221
    assert body["status"] == "ok" and "error" not in body
    ev = video_events(sessions_root)[0]
    assert ev["status"] == "ok" and "error" not in ev and ev["bytes"] == 148_221


def test_an_absent_object_is_a_settled_answer_not_a_failure_to_ask(
        sessions_root, client, s3):
    """S3 answered "nothing there". That is a 200 with ok=false, as before —
    and it is written down as a different thing from "S3 would not answer"."""
    r = client.post(confirm_url(), params={"participant_id": OWNER, "key": KEY})
    assert r.status_code == 200
    assert r.json()["ok"] is False
    ev = video_events(sessions_root)[0]
    assert ev["status"] == "failed" and ev["error"] == "not_found" and ev["bytes"] == 0


def test_the_browsers_own_reason_is_recorded_but_kept_apart(sessions_root, client, s3):
    """static/v2.html knows which of presign, PUT or timeout broke; this side
    does not. It is recorded in its own field — `error` is this server's finding
    — and only as a short bare token, because it reaches a human rater."""
    client.post(confirm_url(), params={"participant_id": OWNER, "key": KEY,
                                       "client_error": "put_http_403"})
    ev = video_events(sessions_root)[-1]
    assert ev["error"] == "not_found" and ev["client_error"] == "put_http_403"

    client.post(confirm_url(), params={"participant_id": OWNER, "key": KEY,
                                       "client_error": "s_1772460300_44c9a2 <b>x</b>"})
    assert "client_error" not in video_events(sessions_root)[-1]


# X9. Every reason static/v2.html composes carries a ':' — "recorder_failed:
# NotSupportedError", "recorder_error:NotAllowedError" — and the filter used to
# admit no ':' at all and cap at 40. So the route answered 200, wrote its event,
# and dropped the browser's whole account of what broke: a silent discard of the
# one field that says whether an encounter was lost to permissions, to the
# network, or to a browser that cannot record.
COMPOSED_REASONS = [
    "recorder_failed:NotSupportedError",   # 33 chars, ':' — was rejected
    "recorder_error:NotAllowedError",
    "put_http_403",                        # the shapes that already worked
    "confirm_timeout",
    "abandoned",
]


@pytest.mark.parametrize("reason", COMPOSED_REASONS)
def test_a_composed_browser_reason_is_recorded_not_silently_dropped(
        sessions_root, client, s3, reason):
    client.post(confirm_url(), params={"participant_id": OWNER, "key": KEY,
                                       "client_error": reason})
    assert video_events(sessions_root)[-1]["client_error"] == reason


@pytest.mark.parametrize("junk", [
    "s_1772460300_44c9a2 <b>x</b>",        # spaces and markup
    "x" * 61,                              # longer than a diagnosis
    "put failed: the bucket said no",      # a sentence, i.e. a paste
    "",
])
def test_the_widened_filter_is_still_a_filter(sessions_root, client, s3, junk):
    """':' was admitted; prose, markup and length were not. The value reaches a
    human rater through the packet, so it stays a token."""
    client.post(confirm_url(), params={"participant_id": OWNER, "key": KEY,
                                       "client_error": junk})
    assert "client_error" not in video_events(sessions_root)[-1]


# --- X8: "no camera" is an absence, not a lost recording ----------------------

def absent_events(sdir: Path) -> list:
    return [e for e in events(sdir) if e.get("type") == "video_absent"]


NO_CAMERA_PARAMS = [
    # What static/v2.html's reportNoCamera sends today, colon and all.
    {"client_error": "no_camera:NotAllowedError"},
    {"client_error": "no_camera:NotReadableError"},
    {"client_error": "no_camera:no_supported_mime"},
    {"client_error": "no_camera:track_ended"},
    {"client_error": "no_camera:recorder_failed:NotSupportedError"},
    # And the dedicated parameter, so a fix on the client side lands too.
    {"no_camera": "NotAllowedError"},
    {"no_camera": ""},
]


@pytest.mark.parametrize("params", NO_CAMERA_PARAMS,
                         ids=lambda p: "&".join(f"{k}={v}" for k, v in p.items()))
def test_an_encounter_that_never_had_a_camera_is_recorded_as_absent(
        sessions_root, client, s3, params):
    """X8. The worst outcome in this chain, and it was the new one.

    An encounter with no camera used to be POSTed down the confirm path, which
    wrote a `video_uploaded` event with status "failed" — the words for "this
    WAS recorded and the recording was lost". rater_packet then told the rater
    not to score it and to report a storage fault, and static/rater.html
    disabled the submit button. Every participant who denied the camera, or
    whose camera was held by Zoom or FaceTime, produced a paid encounter no
    rater was allowed to rate and a false fault report to the study team.

    It must be its own event type, and S3 must not be asked: there is no object
    to ask about, and with no credentials the question itself would have turned
    an absent camera into a 503 and a storage fault.
    """
    r = client.post(confirm_url(), params={"participant_id": OWNER, "key": KEY,
                                           **params})
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "absent" and body["ok"] is False
    assert body["bytes"] is None and body["key"] is None

    assert video_events(sessions_root) == [], \
        "an absent camera was written down as a recording that was lost"
    absent = absent_events(sessions_root)
    assert len(absent) == 1
    assert absent[0]["reason"], "the absence was recorded without a reason"
    assert s3.head_calls == 0, "S3 was asked about an object that never existed"


def test_the_reason_the_camera_never_ran_survives_into_the_trail(
        sessions_root, client, s3):
    """The reason is the whole point: "NotAllowedError" (the participant said
    no) and "NotReadableError" (another application held the camera) are
    different findings about the fielding, and only one of them is fixable."""
    client.post(confirm_url(), params={"participant_id": OWNER, "key": KEY,
                                       "client_error": "no_camera:NotReadableError"})
    assert absent_events(sessions_root)[-1]["reason"] == "NotReadableError"

    client.post(confirm_url(), params={"participant_id": OWNER, "key": KEY,
                                       "no_camera": "no_supported_mime"})
    assert absent_events(sessions_root)[-1]["reason"] == "no_supported_mime"

    # A bare report with nothing to say is still a report, and says so.
    client.post(confirm_url(), params={"participant_id": OWNER, "key": KEY,
                                       "client_error": "no_camera"})
    assert absent_events(sessions_root)[-1]["reason"] == "unspecified"


def test_an_encounter_with_no_camera_stays_rateable(
        sessions_root, client, s3, monkeypatch):
    """End to end, on the two surfaces a human being reads. The record must say
    "absent" and the packet must hand the rater the transcript with the N/A
    instruction — not the blocked "report this fault" state."""
    from server import rater_packet as rp

    monkeypatch.setattr(rp, "SESSIONS_DIR", sessions_root.parent)
    client.post(confirm_url(), params={"participant_id": OWNER, "key": KEY,
                                       "client_error": "no_camera:NotAllowedError"})

    assert _record_of(sessions_root)["video_upload"]["state"] == "absent"
    media = rp._media(SESSION_ID, ASSIGNMENT_ID)
    assert media["video_status"] == "absent"
    assert media["video_available"] is False, \
        "the rating console will block this encounter"
    assert "WAS recorded" not in media["note"]


def test_an_absence_report_cannot_write_off_a_recording_that_landed(
        sessions_root, client, s3, monkeypatch):
    """Order independence. A beacon is fire-and-forget and may arrive late or
    twice; if a stray one could downgrade a confirmed upload, the study would
    lose a recording that is sitting in the bucket.

    "Lose" means lose to a human being, so the packet is asked for the URL and
    not just for the word: a status of "ok" over a null video_url is an
    encounter the rater is told to report rather than rate, which is the same
    loss by a different name.
    """
    from server import rater_packet as rp

    monkeypatch.setattr(rp, "SESSIONS_DIR", sessions_root.parent)
    s3.missing = False
    s3.size = 148_221
    client.post(confirm_url(), params={"participant_id": OWNER, "key": KEY})
    assert video_events(sessions_root)[-1]["status"] == "ok"

    client.post(confirm_url(), params={"participant_id": OWNER, "key": KEY,
                                       "no_camera": "NotAllowedError"})
    assert (video.upload_receipt(SESSION_ID) or {}).get("bytes") == 148_221
    assert _record_of(sessions_root)["video_upload"]["state"] == "ok"
    media = rp._media(SESSION_ID, ASSIGNMENT_ID)
    assert media["video_status"] == "ok"
    assert media["video_available"] is True
    assert media["video_url"] == f"/api/rater/video/{ASSIGNMENT_ID}", \
        "the recording survived the stray beacon and the rater still cannot play it"


def test_a_real_upload_failure_is_still_a_failure(sessions_root, client, s3):
    """The other half of the contract: nothing about the absence branch may
    soften a recording that was made and lost. Only a client that says
    "no camera" gets the absent branch."""
    s3.error = NoCredentialsError()
    r = client.post(confirm_url(), params={"participant_id": OWNER, "key": KEY,
                                           "client_error": "put_http_403"})
    assert r.status_code == 503
    assert absent_events(sessions_root) == []
    assert video_events(sessions_root)[-1]["status"] == "failed"


def test_the_three_states_are_distinguishable_from_the_record(sessions_root, client, s3):
    """No event / failed event / ok event — the distinction rater_packet needs."""
    assert video_events(sessions_root) == []          # never captured

    s3.error = aws_error("SlowDown", status=503)
    client.post(confirm_url(), params={"participant_id": OWNER, "key": KEY})
    failed = video_events(sessions_root)[-1]
    assert failed["status"] == "failed"
    # A failure is not a receipt: nothing was acknowledged, so no rater may be
    # handed something to play off it. That used to be asserted as
    # `video.playback_url(...) is None` — the presigned link that no longer
    # exists — and the question has moved rather than gone away: whether a
    # rater is handed a recording is decided by asking storage, so ask storage.
    # It is the stronger form of the same guarantee, because it also refuses
    # the case a minted link never covered, an "ok" event over bytes that are
    # not there.
    assert video.upload_receipt(SESSION_ID) is None
    assert video.exists(SESSION_ID) is False

    s3.error = None
    s3.missing = False
    s3.size = 99
    client.post(confirm_url(), params={"participant_id": OWNER, "key": KEY})
    assert video_events(sessions_root)[-1]["status"] == "ok"
    assert (video.upload_receipt(SESSION_ID) or {}).get("bytes") == 99


# --- B3: "lost upload" and "no video" must not collapse into one state --------

def _record_of(sdir):
    from server.encounter_record import build

    return build(sdir)


def test_the_record_tells_a_lost_upload_from_an_encounter_with_no_camera(
        sessions_root, client, s3):
    """B3. record.json is the artefact the study ships to analysts and the
    evidence view renders, and it said the same thing — `"video": []` — about an
    encounter that never had a camera and one whose recording was made and lost.

    Those are different judgements. Rating from the transcript because there is
    nothing to watch is the instrument working; rating from the transcript
    because the upload broke is a fault to report, and it can only be reported
    by somebody who is told it happened. The event that says which is written on
    every confirm; the record builder filtered it out on bytes > 0 and kept
    nothing, so the one recoverable fact was dropped at the one place a person
    reads.
    """
    # (a) Nothing ever confirmed: genuinely no video.
    absent = _record_of(sessions_root)
    assert absent["video"] == []
    assert absent["video_upload"] == {"state": "absent", "error": None,
                                      "client_error": None, "attempts": 0}

    # (b) The recording was made and the upload could not be confirmed.
    s3.error = aws_error("SlowDown", status=503)
    client.post(confirm_url(), params={"participant_id": OWNER, "key": KEY,
                                       "client_error": "put_http_403"})
    lost = _record_of(sessions_root)
    assert lost["video"] == [], "there is still nothing to play"
    assert lost["video_upload"]["state"] == "failed", \
        "a lost upload is still recorded as an encounter with no camera"
    assert lost["video_upload"]["error"] == "SlowDown"
    # R14: the browser's own diagnosis reaches the record instead of dying in
    # events.jsonl. It is the only account of which leg actually broke.
    assert lost["video_upload"]["client_error"] == "put_http_403"
    assert lost["video_upload"]["attempts"] == 1

    # (c) A later confirm succeeds. Last-wins, and the earlier failure must not
    # keep a stored recording out of the record.
    s3.error = None
    s3.missing = False
    s3.size = 148_221
    client.post(confirm_url(), params={"participant_id": OWNER, "key": KEY})
    ok = _record_of(sessions_root)
    assert ok["video"] == [{"key": f"encounters/{SESSION_ID}/webcam.webm",
                            "bytes": 148_221}]
    assert ok["video_upload"]["state"] == "ok"
    assert ok["video_upload"]["error"] is None
    assert ok["video_upload"]["attempts"] == 2


def test_the_record_and_the_rater_packet_agree_on_all_four_states(
        sessions_root, client, s3, monkeypatch):
    """The same states, end to end: the event trail, the analyst-facing record
    and the blinded packet a rater actually opens.

    They are derived independently — the record rebuilds from the events, and
    the packet now asks STORAGE and reads the events only for the question
    storage cannot answer — and the whole failure this fixes was two surfaces
    disagreeing about the same encounter. A rater warned that the upload broke
    while record.json says the encounter had no video is only half a fix.

    Three states became four when the playback link stopped being a presigned
    S3 URL. The record still has three, because they are facts about the
    encounter; the packet gained "unsigned", which is a fact about the PACKET —
    the bytes exist and this particular packet has no assignment id to address
    them with. It is included here rather than left to the packet's own tests
    because it is the one state in which the two surfaces can newly disagree,
    and the disagreement has to stay confined to addressing: the record says
    the recording is there, and a packet that cannot reach it must not go on to
    tell a rater the encounter had no camera or that the upload was lost.
    """
    from server import rater_packet as rp

    monkeypatch.setattr(rp, "SESSIONS_DIR", sessions_root.parent)

    assert _record_of(sessions_root)["video_upload"]["state"] == "absent"
    absent = rp._media(SESSION_ID, ASSIGNMENT_ID)
    assert absent["video_status"] == "absent"

    s3.error = aws_error("AccessDenied")
    client.post(confirm_url(), params={"participant_id": OWNER, "key": KEY,
                                       "client_error": "put_http_403"})
    assert _record_of(sessions_root)["video_upload"]["state"] == "failed"
    media = rp._media(SESSION_ID, ASSIGNMENT_ID)
    assert media["video_status"] == "failed"
    # R14: what the browser said reaches the person who has to report it, next
    # to what this server found. "not_found" alone tells a rater the recording
    # is missing, which they can already see; "put_http_403" is the half that
    # says whether this is a permissions problem or a network one.
    assert "AccessDenied" in media["upload_error"]
    assert "put_http_403" in media["upload_error"]
    assert "put_http_403" in media["note"]

    s3.error = None
    s3.missing = False
    s3.size = 148_221
    client.post(confirm_url(), params={"participant_id": OWNER, "key": KEY})
    assert _record_of(sessions_root)["video_upload"]["state"] == "ok"
    ok = rp._media(SESSION_ID, ASSIGNMENT_ID)
    assert ok["video_status"] == "ok"
    assert ok["video_url"] == f"/api/rater/video/{ASSIGNMENT_ID}"

    # The fourth. Same encounter, same storage, same record — a packet built
    # outside a rating assignment, which is what an operator inspecting an
    # encounter gets. It must degrade to "I cannot address this", never to a
    # judgement about the encounter that contradicts the record beside it.
    unsigned = rp._media(SESSION_ID)
    assert unsigned["video_status"] == "unsigned", \
        "the packet's judgement about the ENCOUNTER changed because this caller had " \
        "no assignment id. 'absent' or 'failed' here contradicts the record beside " \
        "it: one tells a rater to rate a recorded encounter from the transcript, " \
        "the other reports a storage fault against a bucket that lost nothing"
    assert unsigned["video_available"] is True, \
        "the rating console would invite a transcript-only rating of an encounter " \
        "whose recording is sitting in storage"
    assert unsigned["video_url"] is None, \
        "a URL nothing can serve is worse than none: the console reports a present " \
        "recording as a missing one"
    assert unsigned["upload_error"] is None, \
        "an addressing problem was written down as an upload fault against the bucket"
    assert _record_of(sessions_root)["video_upload"]["state"] == "ok", \
        "the record moved because a packet could not name a URL"

    # And on every branch there is no deadline for anyone to count down to. The
    # app serves the bytes for as long as the rater's own token is good for; an
    # expiry here is the presigned link coming back, and with it the hour into
    # a sitting where every remaining encounter reads as "no video".
    for state in (absent, media, ok, unsigned):
        assert state["expires_in"] is None


def test_the_packet_still_refuses_to_leak_a_session_id_through_a_client_error(
        sessions_root, client, s3, monkeypatch):
    """The browser's reason is now shown to raters, so it goes through the same
    blinding as the server's own: a session id anywhere in it drops the whole
    string. A rater who can read two packets' start times to the second can tell
    which belong to one participant, which is what the rating code prevents."""
    from server import rater_packet as rp

    monkeypatch.setattr(rp, "SESSIONS_DIR", sessions_root.parent)
    # The route's own token filter would refuse this, so it is written straight
    # into the trail — an older event, or one written before that filter existed.
    with (sessions_root / "events.jsonl").open("a", encoding="utf-8") as fh:
        fh.write(json.dumps({"type": "video_uploaded", "bytes": 0,
                             "status": "failed", "error": None,
                             "client_error": f"put failed for {SESSION_ID}"}) + "\n")
    media = rp._media(SESSION_ID, ASSIGNMENT_ID)
    assert media["video_status"] == "failed"
    assert media["upload_error"] is None
    assert SESSION_ID not in json.dumps(media)


@pytest.mark.parametrize("exc", AWS_FAILURES, ids=lambda e: type(e).__name__ + getattr(
    e, "response", {}).get("Error", {}).get("Code", ""))
def test_the_presign_route_answers_503_not_500(sessions_root, client, s3, exc):
    """503 says "storage, not you", and the log line carries bucket and region.
    A 500 said nothing the page or the operator could act on."""
    s3.error = exc
    r = client.get(presign_url(), params={"participant_id": OWNER, "key": KEY})
    assert r.status_code == 503
    assert "storage" in r.json()["detail"]


def test_an_unanswerable_head_still_issues_the_upload_url(sessions_root, monkeypatch, client):
    """The one-shot guard exists to protect a recording we know is there.

    A HEAD that could not be answered is not that, and refusing on it throws
    away a recording that has not been made yet — the participant's encounter is
    over by the time anyone finds out.
    """
    class HeadFails(FakeS3):
        def head_object(self, **kw):
            self.head_calls += 1
            raise aws_error("SlowDown", status=503)

    stub = HeadFails()
    monkeypatch.setattr(video, "_s3", stub)
    r = client.get(presign_url(), params={"participant_id": OWNER, "key": KEY})
    assert r.status_code == 200
    assert r.json()["key"] == f"encounters/{SESSION_ID}/webcam.webm"
    assert stub.signed and stub.signed[0][0] == "put_object"


def test_an_unanswerable_head_records_that_the_guard_was_weakened(
        sessions_root, monkeypatch, client):
    """A guarantee that quietly evaporates is indistinguishable from one that
    was never made. The trail says which recordings were signed unverified."""
    class HeadFails(FakeS3):
        def head_object(self, **kw):
            self.head_calls += 1
            raise aws_error("AccessDenied")

    monkeypatch.setattr(video, "_s3", HeadFails())
    assert client.get(presign_url(),
                      params={"participant_id": OWNER, "key": KEY}).status_code == 200
    noted = [e for e in events(sessions_root)
             if e.get("type") == "video_presign_unverified"]
    assert len(noted) == 1
    assert noted[0]["error"] == "AccessDenied"
    assert noted[0]["key"] == f"encounters/{SESSION_ID}/webcam.webm"


def test_an_unanswerable_head_does_not_disable_the_guard_on_a_known_recording(
        sessions_root, monkeypatch, client):
    """Issuing a write URL whenever the HEAD is unanswerable disabled the
    one-shot tamper guard for the whole duration of any HEAD-side failure.

    A task role with s3:PutObject but a denied or throttled HeadObject is a
    plausible narrow first-apply IAM state — exactly the state the preflight
    exists to catch — and on it the owning participant could fetch unlimited
    write URLs over an already-captured IRB recording. The local receipt is what
    this server itself confirmed earlier, so the guard can hold without asking
    S3 anything.
    """
    (sessions_root / "events.jsonl").write_text(
        json.dumps({"type": "video_uploaded", "bytes": 4096, "status": "ok",
                    "key": f"encounters/{SESSION_ID}/webcam.webm"}) + "\n",
        encoding="utf-8")

    class HeadFails(FakeS3):
        def head_object(self, **kw):
            self.head_calls += 1
            raise aws_error("AccessDenied")

    stub = HeadFails()
    monkeypatch.setattr(video, "_s3", stub)
    for _ in range(3):
        r = client.get(presign_url(), params={"participant_id": OWNER, "key": KEY})
        # 503, NOT 409. The guard holds either way — no URL is minted — but the
        # two refusals mean different things to the browser, which reads 409 as
        # "the earlier PUT landed" and stops re-sending the recording. Here
        # nothing has confirmed anything: the refusal rests on this server's own
        # earlier receipt while S3 declines to answer, so telling the page the
        # object is safe would be a claim nobody checked.
        assert r.status_code == 503, "an unconfirmed refusal must not read as a confirmation"
        assert "confirm" in r.json()["detail"]
        assert "AccessDenied" in r.json()["detail"]   # and it names what broke
    assert not stub.signed, "no write URL may be minted over a known recording"


def test_a_present_object_is_still_refused_a_second_write(sessions_root, client, s3):
    """The one refusal that IS a confirmation: S3 was asked and said the object
    is there. 409 is what tells the page not to send the bytes again."""
    s3.missing = False
    s3.size = 512
    r = client.get(presign_url(), params={"participant_id": OWNER, "key": KEY})
    assert r.status_code == 409


def test_the_three_presign_refusals_are_three_different_answers(sessions_root,
                                                               client, monkeypatch):
    """P10. presign_upload used to answer None to three questions and the route
    turned all three into 409 "video already uploaded".

    Two of those were false statements — a missing session is not a finished
    recording — and the third is one the browser ACTS on: static/v2.html treats
    409 as "the first PUT landed" and skips re-sending tens of megabytes. So a
    refusal resting on a local receipt that S3 would not confirm could stop a
    retry after a failed PUT. Loud and separate beats quietly agreeable.
    """
    # (a) S3 confirms the object: the only 409.
    confirmed = FakeS3(size=4096)
    monkeypatch.setattr(video, "_s3", confirmed)
    assert client.get(presign_url(),
                      params={"participant_id": OWNER, "key": KEY}).status_code == 409

    # (b) A receipt we wrote, and an S3 that will not confirm it: 503.
    (sessions_root / "events.jsonl").write_text(
        json.dumps({"type": "video_uploaded", "bytes": 4096, "status": "ok",
                    "key": f"encounters/{SESSION_ID}/webcam.webm"}) + "\n",
        encoding="utf-8")
    monkeypatch.setattr(video, "_s3", FakeS3(error=aws_error("SlowDown", status=503)))
    assert client.get(presign_url(),
                      params={"participant_id": OWNER, "key": KEY}).status_code == 503

    # (c) No such session directory at all. Unreachable through the route, which
    # 404s first, so it is asserted on the function: the next caller of
    # presign_upload must not be told a missing session is a finished recording.
    with pytest.raises(video.NoSuchSession):
        video.presign_upload("s_1772460300_000000")


def test_uploaded_size_reports_unknown_rather_than_raising(sessions_root, s3):
    s3.error = aws_error("ExpiredToken")
    assert video.uploaded_size(SESSION_ID) == video.UNKNOWN_SIZE
    assert video.UNKNOWN_SIZE < 0        # so every `> 0` caller reads it as "no"


def test_uploaded_size_has_no_caller_left_in_this_server(sessions_root):
    """P11: a comment the code contradicted, pinned so it cannot drift again.

    The docstring above uploaded_size says nothing in this server calls it —
    both former callers ask head_video() directly, because an integer cannot
    keep "S3 says none" apart from "S3 would not say". If that stops being true,
    either the claim or the call is wrong and someone has to look.
    """
    src = Path(appmod.__file__).read_text(encoding="utf-8")
    assert "video.uploaded_size(" not in src
    # presign_upload was the other caller; it asks head_video for the same reason.
    presign = inspect.getsource(video.presign_upload)
    assert "head_video(" in presign and "uploaded_size(" not in presign
    # And the docstring says so, so a reader is not sent looking for a caller.
    assert "NOTHING IN THIS SERVER CALLS THIS" in video.uploaded_size.__doc__


def test_the_client_is_built_with_bounded_timeouts(monkeypatch, tmp_path):
    """boto3's defaults are 60 s connect, 60 s read, legacy retries. On a route
    that shares a loop with live audio, that is minutes of frozen encounters.

    The credential chain is pinned to "there is nothing here" first, and that
    is not tidiness. Constructing a boto3 client RESOLVES credentials, so this
    test — a question about a Config object — was the one place in this file
    that reached the open internet: on a machine that is not an EC2 instance
    the walk ends at 169.254.169.254, the instance metadata service, which is
    unroutable off EC2, and botocore waits out its connect timeout twice before
    giving up. Measured at 2.30 s, which is most of this file's runtime, and on
    a machine that DOES hold a credential it would build a client able to sign
    a real request against the IRB bucket.

    Every spelling is neutralised rather than just the metadata service,
    because a contributor with ~/.aws/credentials would otherwise run a
    different test from CI's — and no fake credential is put in the
    environment in its place: boto3's default session caches the credentials
    object it resolves for the life of the process, so a fake one planted here
    would still be there for every test that ran afterwards.
    """
    monkeypatch.setenv("AWS_EC2_METADATA_DISABLED", "true")
    monkeypatch.setenv("AWS_SHARED_CREDENTIALS_FILE", str(tmp_path / "no-credentials"))
    monkeypatch.setenv("AWS_CONFIG_FILE", str(tmp_path / "no-config"))
    monkeypatch.delenv("AWS_PROFILE", raising=False)
    monkeypatch.setattr(video, "_s3", None)
    cfg = video._client().meta.config
    assert cfg.connect_timeout == 3
    assert cfg.read_timeout == 5
    assert cfg.retries["mode"] == "standard"
    # botocore reads max_attempts as *retries* and resolves it to a total, so
    # the bound asserted here is the resolved one: three tries, not sixty
    # seconds each with legacy retries on top.
    assert cfg.retries["total_max_attempts"] == 3
    assert cfg.signature_version == "s3v4"   # KMS objects need SigV4 presigns


# --- B5: the storage preflight ------------------------------------------------

def test_the_preflight_reports_a_reachable_writable_bucket():
    stub = FakeS3()
    out = video.storage_preflight(client=stub)
    assert out["ok"] and out["readable"] and out["writable"]
    assert out["bucket"] == video.BUCKET and out["region"] == video.REGION
    assert stub.put_calls == 1
    # Under encounters/, the only prefix the task role may write to; a probe
    # anywhere else would report a failure that says nothing about whether a
    # participant's webcam upload can land.
    assert video.PREFLIGHT_KEY.startswith("encounters/")


def test_the_preflight_does_not_warm_the_shared_client(monkeypatch):
    """A boto3 client pins its credentials at construction. A boot check that
    populated the module client would fix that answer for the life of the
    process, from whatever the environment held at import time."""
    monkeypatch.setattr(video, "_s3", None)
    video.storage_preflight(client=FakeS3())
    assert video._s3 is None


@pytest.mark.parametrize("exc", AWS_FAILURES, ids=lambda e: type(e).__name__ + getattr(
    e, "response", {}).get("Error", {}).get("Code", ""))
def test_the_preflight_never_raises(exc):
    """It reports; it does not gate. A transient S3 blip must not stop a process
    that can still run encounters and write every local artefact."""
    out = video.storage_preflight(client=FakeS3(error=exc))
    assert out["ok"] is False
    assert out["error_code"]


def test_a_readable_but_unwritable_bucket_is_not_ok(monkeypatch):
    """The presigned PUT the browser executes is signed by this process, so a
    task role that can list but not put loses every recording. A read-only
    preflight would have passed."""
    class NoWrite(FakeS3):
        def put_object(self, **kw):
            raise aws_error("AccessDenied", op="PutObject")

    out = video.storage_preflight(client=NoWrite())
    assert out["readable"] is True
    assert out["writable"] is False and out["ok"] is False
    assert out["error_code"] == "AccessDenied"


LEAKY_PREFLIGHT = {"ok": False, "bucket": "relational-fluency-study-data",
                   "region": "us-east-1", "credentials": False,
                   "readable": False, "writable": False,
                   "error_code": "NoCredentialsError",
                   "detail": "Unable to locate credentials for profile x"}


def test_importing_the_app_contacts_nothing_and_writes_nothing(tmp_path):
    """P12. `import server.app` used to run both preflights in the module body:
    an httpx GET to the gateway, a head_bucket, and a put_object into the study
    bucket.

    Three things wrong with that, and the third is the one that matters. It is
    slow — a black-holed S3 path costs seconds per call — on a module the
    offline tools and every pytest process import. It fails for no reason
    wherever there are no credentials, which is every offline tool run. And it
    PUT an object into an IRB bucket as a side effect of reading a record.

    The "writes nothing" half went untested for as long as the name has been
    making the claim, and it was false: the module body also called
    init_storage(), so a bare import created DATA_DIR, sessions/, participants/
    and a schema-only index.db. CI's own `python -c "import server.app"` did it
    in all nine matrix cells, verify_record and scoring did it on the way to
    producing a report, and on a read-only filesystem the import raised outright
    — none of which anything asked for. DATA_DIR is pointed at an empty
    directory here and the directory is the assertion.

    Asserted in a subprocess, because this process imported server.app long ago
    and nothing here could observe what that import did.
    """
    repo_root = Path(appmod.__file__).parents[1]
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    probe = tmp_path / "probe.py"
    probe.write_text(
        "import socket, sys\n"
        f"sys.path.insert(0, {str(repo_root)!r})\n"
        "opened = []\n"
        "socket.socket.connect = lambda self, a: opened.append(a)\n"
        "import server.app as a\n"
        "assert not opened, 'the import opened a socket to %r' % (opened,)\n"
        "assert a._STORAGE_PREFLIGHT['checked'] is False\n"
        "assert a._PREFLIGHT['checked'] is False\n"
        # An unrun check reports unknown, never False: "not tested" and "tested
        # and failed" are different answers and /health publishes this one.
        "assert a._STORAGE_PREFLIGHT['ok'] is None\n"
        "assert a._PREFLIGHT['ok'] is None\n"
        # Asked from inside the process too, so the failure names itself rather
        # than arriving as a bare listing mismatch.
        "import server.storage as s\n"
        "left = sorted(p.name for p in s.DATA_DIR.iterdir())\n"
        "assert not left, 'the import created %r in DATA_DIR' % (left,)\n"
        "print('CLEAN')\n",
        encoding="utf-8")
    env = dict(os.environ, DATA_DIR=str(data_dir))
    out = subprocess.run([sys.executable, str(probe)], capture_output=True,
                         text=True, cwd=str(repo_root), timeout=120, env=env)
    assert out.returncode == 0, out.stdout + out.stderr
    assert "CLEAN" in out.stdout
    assert sorted(p.name for p in data_dir.iterdir()) == [], \
        "importing server.app minted storage"


def test_startup_is_what_creates_the_data_directory(tmp_path, monkeypatch):
    """The other half: lazy must not mean never.

    A process that is going to serve initialises its store up front, so a
    DATA_DIR that cannot be written (a mis-mounted volume, wrong ownership in
    the container, a read-only filesystem) is a startup failure rather than
    something a paid participant discovers halfway through a conversation. The
    hook is driven directly rather than through TestClient because standing the
    whole app up would also run the preflights, which reach the network.
    """
    from server import storage as storage_mod

    data = tmp_path / "data"
    monkeypatch.setattr(storage_mod, "DATA_DIR", data)
    monkeypatch.setattr(storage_mod, "SESSIONS_DIR", data / "sessions")
    monkeypatch.setattr(storage_mod, "PARTICIPANTS_DIR", data / "participants")
    monkeypatch.setattr(storage_mod, "DB_PATH", data / "index.db")
    assert not data.exists()

    asyncio.run(appmod._init_storage_on_startup())

    assert (data / "sessions").is_dir() and (data / "participants").is_dir()
    assert (data / "index.db").is_file()
    with sqlite3.connect(data / "index.db") as conn:
        tables = {r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'")}
    assert {"sessions", "participants"} <= tables


def test_the_storage_hook_runs_only_after_the_refusal(tmp_path, monkeypatch):
    """A deployment that must not serve does not get to mint a data directory on
    its way to exiting — the same ordering rule the preflights are held to."""
    hooks = appmod.app.router.on_startup
    assert appmod._init_storage_on_startup in hooks
    assert hooks.index(appmod._refuse_to_serve_unprotected) \
        < hooks.index(appmod._init_storage_on_startup)

    from server import storage as storage_mod

    data = tmp_path / "data"
    monkeypatch.setattr(storage_mod, "DATA_DIR", data)
    monkeypatch.setattr(storage_mod, "SESSIONS_DIR", data / "sessions")
    monkeypatch.setattr(storage_mod, "PARTICIPANTS_DIR", data / "participants")
    monkeypatch.setattr(storage_mod, "DB_PATH", data / "index.db")
    monkeypatch.setattr(appmod, "SESSION_KEY", "")
    monkeypatch.setattr(appmod, "ALLOWED_HOSTS", [appmod.APP_HOST])
    monkeypatch.setenv("HOST", "0.0.0.0")

    with pytest.raises(RuntimeError):
        asyncio.run(appmod._refuse_to_serve_unprotected())
    assert not data.exists(), "a process that refused to serve still made a store"


def test_the_first_write_initialises_a_data_directory_that_appeared_late(
        tmp_path, monkeypatch):
    """And lazy must not mean broken. Nothing initialises at import any more, so
    a DATA_DIR that only exists at write time — a mounted volume, a test
    repointing it, an offline tool that decides to write after all — has to be
    minted by the writer itself, or a participant's consent record dies on a
    missing directory at the moment they consent."""
    from server import storage as storage_mod

    data = tmp_path / "late"
    monkeypatch.setattr(storage_mod, "DATA_DIR", data)
    monkeypatch.setattr(storage_mod, "SESSIONS_DIR", data / "sessions")
    monkeypatch.setattr(storage_mod, "PARTICIPANTS_DIR", data / "participants")
    monkeypatch.setattr(storage_mod, "DB_PATH", data / "index.db")

    pid = storage_mod.create_participant("code-1", False, "v1")
    assert storage_mod.get_participant(pid)["consent_given"] is False
    assert storage_mod.record_consent(pid, "v1")["consent_given"] is True
    with sqlite3.connect(data / "index.db") as conn:
        row = conn.execute("SELECT consent_given FROM participants WHERE id = ?",
                           (pid,)).fetchone()
    assert row == (1,)


def test_startup_is_what_runs_the_preflights(monkeypatch):
    """The other half: lazy must not mean never. A served process still checks
    both seams before anyone can join, and /health still answers from it."""
    calls = []
    monkeypatch.setattr(appmod, "SESSION_KEY", KEY)
    if appmod.ALLOWED_HOSTS and "testserver" not in appmod.ALLOWED_HOSTS:
        monkeypatch.setattr(appmod, "ALLOWED_HOSTS",
                            list(appmod.ALLOWED_HOSTS) + ["testserver"])
    monkeypatch.setattr(appmod, "_preflight",
                        lambda: calls.append("gateway") or {"ok": True, "gateway": "g"})
    monkeypatch.setattr(video, "storage_preflight",
                        lambda: calls.append("storage") or {
                            "ok": True, "bucket": "b", "region": "r",
                            "readable": True, "writable": True})
    monkeypatch.setattr(appmod, "_PREFLIGHT", dict(appmod._UNCHECKED_GATEWAY))
    monkeypatch.setattr(appmod, "_STORAGE_PREFLIGHT", dict(appmod._UNCHECKED_STORAGE))

    with TestClient(appmod.app) as started:
        assert calls == ["gateway", "storage"]
        storage = started.get("/health").json()["storage"]
    assert storage["checked"] is True and storage["ok"] is True


def test_a_preflight_that_blows_up_neither_stops_the_port_nor_skips_the_other(
        monkeypatch, capsys):
    """It reports, it does not gate — including when it fails in a way its own
    never-raises contract did not anticipate. A diagnostic that can stop a
    process which could still run encounters is worse than no diagnostic, and a
    check that quietly did not run because an unrelated one threw is the exact
    state these checks exist to abolish."""
    monkeypatch.setattr(appmod, "SESSION_KEY", KEY)
    if appmod.ALLOWED_HOSTS and "testserver" not in appmod.ALLOWED_HOSTS:
        monkeypatch.setattr(appmod, "ALLOWED_HOSTS",
                            list(appmod.ALLOWED_HOSTS) + ["testserver"])

    def boom():
        raise RuntimeError("botocore said no")

    monkeypatch.setattr(appmod, "_preflight", boom)
    monkeypatch.setattr(video, "storage_preflight",
                        lambda: {"ok": True, "bucket": "b", "region": "r",
                                 "readable": True, "writable": True})
    monkeypatch.setattr(appmod, "_STORAGE_PREFLIGHT", dict(appmod._UNCHECKED_STORAGE))
    with TestClient(appmod.app) as started:
        assert started.get("/health").status_code == 200
        # The gateway blew up; the bucket was still checked.
        assert started.get("/health").json()["storage"]["checked"] is True
    assert "model gateway preflight did not complete" in capsys.readouterr().out


def test_health_carries_storage_and_stays_200(client, monkeypatch):
    monkeypatch.setattr(appmod, "_STORAGE_PREFLIGHT",
                        {"ok": False, "bucket": "b", "region": "r",
                         "readable": False, "writable": False,
                         "error_code": "NoSuchBucket"})
    r = client.get("/health")
    assert r.status_code == 200            # a storage blip must not kill the task
    assert r.json()["storage"]["error_code"] == "NoSuchBucket"


def test_health_does_not_name_the_bucket_to_the_open_internet(client, monkeypatch):
    """/health carries no check_key and is the one path exempted from the Host
    allowlist, and the ALB forwards every path to the target group by default —
    so it answers strangers. Publishing the study bucket's exact name, its
    region and whether the task has credentials hands an attacker the target for
    the very "seed/abuse the study bucket" attack the presign guard exists to
    stop, and names the bucket holding IRB-recorded encounters."""
    monkeypatch.setattr(appmod, "_STORAGE_PREFLIGHT", LEAKY_PREFLIGHT)
    # The required environment supplied, so the only fault in play is the
    # bucket. /health's top-level word now answers to the config block too (see
    # app._health_status), and this test's claim — a storage fault does not move
    # the word — can only be read when nothing else is moving it.
    monkeypatch.setenv(storagemod.UPSTREAM_CONSENT_VERSION_ENV,
                       "cornell-irb-2026-09-v3")
    body = client.get("/health").json()
    assert body["status"] == "ok", "a storage fault must not move the word"
    storage = body["storage"]
    # The shape a probe needs, and nothing more.
    assert storage == {"ok": False, "readable": False, "writable": False,
                       "error_code": "NoCredentialsError"}
    flat = json.dumps(body)
    for secret in ("relational-fluency-study-data", "us-east-1",
                   "Unable to locate credentials"):
        assert secret not in flat, f"/health published {secret!r} unauthenticated"


def test_health_is_a_projection_so_a_new_field_is_private_by_default(client,
                                                                    monkeypatch):
    """Blacklists rot. A field added to the preflight later must not be
    published on the day it appears just because nobody remembered /health."""
    monkeypatch.setattr(appmod, "_STORAGE_PREFLIGHT",
                        dict(LEAKY_PREFLIGHT, kms_key_arn="arn:aws:kms:secret"))
    assert "kms_key_arn" not in json.dumps(client.get("/health").json())


def test_a_valid_key_widens_health_to_the_operators_full_diagnosis(client,
                                                                   monkeypatch):
    monkeypatch.setattr(appmod, "_STORAGE_PREFLIGHT", LEAKY_PREFLIGHT)
    storage = client.get("/health", params={"key": KEY}).json()["storage"]
    assert storage == LEAKY_PREFLIGHT
    # A wrong key is not an error here — refusing the probe would take the task
    # out of service — it just gets the narrow answer.
    narrow = client.get("/health", params={"key": "wrong"})
    assert narrow.status_code == 200
    assert "bucket" not in narrow.json()["storage"]


def test_health_never_widens_when_no_key_is_configured(monkeypatch):
    """check_key waves everyone through when SESSION_KEY is empty, which would
    publish the full block on precisely the deployment least able to afford it,
    so /health compares the key itself instead of calling it."""
    monkeypatch.setattr(appmod, "SESSION_KEY", "")
    monkeypatch.setattr(appmod, "_STORAGE_PREFLIGHT", LEAKY_PREFLIGHT)
    if appmod.ALLOWED_HOSTS and "testserver" not in appmod.ALLOWED_HOSTS:
        monkeypatch.setattr(appmod, "ALLOWED_HOSTS",
                            list(appmod.ALLOWED_HOSTS) + ["testserver"])
    keyless = TestClient(appmod.app, raise_server_exceptions=False)
    for params in ({}, {"key": ""}, {"key": "anything"}):
        body = keyless.get("/health", params=params).json()
        assert "bucket" not in body["storage"]
        assert body["session_key_configured"] is False


def test_health_says_whether_the_researcher_credential_landed(client):
    """The only way to check SESSION_KEY from outside a running task. Not a
    secret: False here says nothing an unauthenticated GET of any researcher
    route would not already prove."""
    assert client.get("/health").json()["session_key_configured"] is True


# --- contract 5: a public host with no researcher key must not start ---------

def test_a_public_deployment_without_a_session_key_refuses_to_start(monkeypatch):
    """The bind address is the first test and the allowlist the second.

    HOST has to be set here: the guard now returns early for a loopback bind
    whatever the allowlist says, because refusing a fresh clone that follows the
    README quick start is worse than the exposure it prevents. A public
    hostname on a public interface is the case that must still refuse.
    """
    monkeypatch.setenv("HOST", "0.0.0.0")
    monkeypatch.setattr(appmod, "SESSION_KEY", "")
    monkeypatch.setattr(appmod, "ALLOWED_HOSTS",
                        ["rf.ai-ready-workforce.ai.cornell.edu", "localhost"])
    msg = appmod._refuse_unprotected_public_start()
    assert msg and "SESSION_KEY" in msg
    assert "rf.ai-ready-workforce.ai.cornell.edu" in msg


def test_an_empty_allowlist_is_the_widest_setting_not_the_narrowest(monkeypatch):
    """ALLOWED_HOSTS="" was the guard's one-environment-variable bypass.

    `public` is computed by filtering ALLOWED_HOSTS, so [] read as "no public
    hostnames, therefore local dev" — while app.py's own comment tells operators
    that emptying ALLOWED_HOSTS disables the host check, which is the first
    thing anyone reaches for when the ALB or a new hostname trips the guard. On
    a public bind that combination served /api/ratings, /researcher, /evidence
    and the download zips to anyone, with the Host allowlist off as well.
    """
    monkeypatch.setenv("HOST", "0.0.0.0")
    monkeypatch.setattr(appmod, "SESSION_KEY", "")
    monkeypatch.setattr(appmod, "ALLOWED_HOSTS", [])
    msg = appmod._refuse_unprotected_public_start()
    assert msg and "SESSION_KEY" in msg
    assert "any Host header" in msg
    # ...and it is still the key, not the allowlist, that unblocks it.
    monkeypatch.setattr(appmod, "SESSION_KEY", KEY)
    assert appmod._refuse_unprotected_public_start() is None


def test_local_development_is_unaffected(monkeypatch):
    monkeypatch.setattr(appmod, "SESSION_KEY", "")
    monkeypatch.setattr(appmod, "ALLOWED_HOSTS", ["localhost", "127.0.0.1"])
    assert appmod._refuse_unprotected_public_start() is None
    monkeypatch.setattr(appmod, "ALLOWED_HOSTS", [])
    assert appmod._refuse_unprotected_public_start() is None
    # The ASGI harness's own host name is not a routable one either, and a
    # suite that stands the app up must not have to invent a key to do it.
    monkeypatch.setattr(appmod, "ALLOWED_HOSTS", ["localhost", "testserver"])
    assert appmod._refuse_unprotected_public_start() is None


def test_a_public_deployment_with_a_key_starts(monkeypatch):
    monkeypatch.setattr(appmod, "SESSION_KEY", KEY)
    monkeypatch.setattr(appmod, "ALLOWED_HOSTS",
                        ["rf.ai-ready-workforce.ai.cornell.edu"])
    assert appmod._refuse_unprotected_public_start() is None


# --- B23 / B19: consent is checked by flag, on every capture path -------------

RECORDS = {
    "p_consented": {"id": "p_consented", "consent_given": True},
    "p_pending": {"id": "p_pending", "consent_given": False},
    "p_declined": {"id": "p_declined", "consent_given": False, "declined": True},
}


@pytest.fixture()
def people(monkeypatch):
    monkeypatch.setattr(appmod, "get_participant", lambda pid: RECORDS.get(pid))


@pytest.mark.parametrize("path", ["/ws/participant", "/ws/participant/voice"])
@pytest.mark.parametrize("pid", ["p_declined", "p_pending", "p_unknown"])
def test_both_sockets_refuse_anyone_who_has_not_consented(client, people, path, pid):
    """The text socket used to check that the record EXISTS. A participant who
    read the form and refused was accepted on it, and their transcript was
    written into cohort "study" and queued to a human rater."""
    url = path + f"?scenario=conflict&participant_id={pid}"
    with pytest.raises(WebSocketDisconnect) as caught:
        with client.websocket_connect(url):
            pass
    assert caught.value.code == 4403


def test_the_voice_socket_refuses_a_connection_with_no_record_at_all(client, people):
    """Capture there is audio and webcam under the IRB. An anonymous one has no
    business opening."""
    with pytest.raises(WebSocketDisconnect) as caught:
        with client.websocket_connect("/ws/participant/voice?scenario=conflict"):
            pass
    assert caught.value.code == 4403


def test_the_text_socket_still_opens_with_no_record_at_all(client, people, monkeypatch):
    """The gate must be `participant_id and not consented`, not `not consented`.

    Refusing an absent participant_id killed the documented single-agent text
    entrance (README: /?scenario=missed_deadlines): static/participant.html
    deliberately skips the consent gate in text mode and opens this socket with
    an empty pidParam, so every such connection was closed 4403 — and app.py's
    own module docstring calls that path the one that is "sufficient to make
    sure the conversation is working well and steer the model".

    Allowing it costs nothing that matters, which the next test proves.
    """
    def no_such_scenario(*a, **k):
        raise FileNotFoundError("unknown scenario: conflict")

    monkeypatch.setattr(appmod.registry, "create", no_such_scenario)
    with client.websocket_connect("/ws/participant?scenario=conflict") as ws:
        # It opened: the reply is the scenario failing, not a 4403 close.
        assert ws.receive_json()["type"] == "error"


def test_an_anonymous_text_encounter_can_never_become_study_data():
    """Why allowing it is safe, stated as the thing that has to stay true.

    With no participant record there is no run to resolve, so the encounter
    carries no run id, no participant key and no cohort — it is unattributable
    rather than misattributed, and a cohort-filtered rating draw cannot reach
    it. That was the actual harm B23 named; the record's mere existence was not.
    """
    assert appmod._run_context(None) is None
    assert appmod._run_context(None, "some_run_id") is None


@pytest.mark.parametrize("path", ["/ws/participant", "/ws/participant/voice"])
def test_both_sockets_open_for_a_consented_participant(client, people, monkeypatch, path):
    """The gate passes and the session is what fails, which is how we know the
    refusal above was the consent check and not a broken socket."""
    def no_such_scenario(*a, **k):
        raise FileNotFoundError("unknown scenario: conflict")

    monkeypatch.setattr(appmod.registry, "create", no_such_scenario)
    with client.websocket_connect(
            path + "?scenario=conflict&participant_id=p_consented") as ws:
        assert ws.receive_json()["type"] == "error"


def test_the_helper_is_the_one_rule_both_gates_use(people):
    assert appmod._consented_participant("p_consented")
    assert appmod._consented_participant("p_declined") is None
    assert appmod._consented_participant("p_pending") is None
    assert appmod._consented_participant(None) is None


# --- B20: a swallowed mint in /start ------------------------------------------

@pytest.fixture()
def runs_root(tmp_path, monkeypatch):
    from server import runs

    monkeypatch.setattr(runs, "DATA_DIR", tmp_path)
    monkeypatch.setattr(runs, "RUNS_DIR", tmp_path / "runs")
    return runs


def test_a_failed_mint_is_logged_with_the_run_it_cost(client, runs_root, monkeypatch,
                                                      capsys):
    """Silence here costs a consented participant's first encounter: with no
    participant_record_id on the run, the manifest records run_id and cohort as
    null, and a null cohort means "not study data"."""
    def full_disk(**kw):
        raise OSError(28, "No space left on device")

    monkeypatch.setattr(appmod, "create_participant", full_disk)
    monkeypatch.setattr(appmod, "get_participant", lambda pid: None)
    r = client.get("/start", params={"pid": "RF_TEST_A1"}, follow_redirects=False)
    assert r.status_code == 307
    out = capsys.readouterr().out
    assert "WARNING" in out and "could not mint a participant record" in out
    assert "OSError" in out
    run_id = r.headers["location"].split("run=")[1].split("&")[0]
    assert run_id in out, "the log line must name the run it cost"


def test_the_mint_is_retried_once_before_giving_up(client, runs_root, monkeypatch,
                                                   capsys):
    """A JSON write plus a sqlite row on a busy EFS mount fails transiently, and
    a second attempt usually takes."""
    calls = []

    def flaky(**kw):
        calls.append(kw)
        if len(calls) == 1:
            raise OSError(11, "Resource temporarily unavailable")
        return "p_minted_second_try"

    monkeypatch.setattr(appmod, "create_participant", flaky)
    monkeypatch.setattr(appmod, "get_participant", lambda pid: None)
    r = client.get("/start", params={"pid": "RF_TEST_A2"}, follow_redirects=False)
    assert len(calls) == 2
    assert "participant_id=p_minted_second_try" in r.headers["location"]
    assert "could not mint" not in capsys.readouterr().out


def test_a_failed_write_back_does_not_mint_a_second_record(client, runs_root,
                                                           monkeypatch, capsys):
    """The mint and the run write-back are not one unit.

    They fail for the same reason (a full or stalled DATA_DIR), so a single try
    around both looked right — but when the mint SUCCEEDED and runs.save then
    raised, the handler set pid_record = None and attempt 2 minted a SECOND
    record. A save failure therefore left an orphaned participant record, and
    two of them if the second save also failed, where the old code left exactly
    one. An identity that exists is still the right one to hand the page.
    """
    calls = []

    def mint(**kw):
        calls.append(kw)
        return f"p_minted_{len(calls)}"

    def stalled_save(run):
        raise OSError(5, "Input/output error")

    monkeypatch.setattr(appmod, "create_participant", mint)
    monkeypatch.setattr(appmod, "get_participant", lambda pid: None)
    monkeypatch.setattr(runs_root, "save", stalled_save)
    r = client.get("/start", params={"pid": "RF_TEST_A3"}, follow_redirects=False)
    assert len(calls) == 1, "a save failure must not re-mint the identity"
    assert "participant_id=p_minted_1" in r.headers["location"]
    # And it is loud: a run whose write-back was lost is a join that has to be
    # repaired at consent, which nothing downstream would otherwise report.
    out = capsys.readouterr().out
    assert "could not write it back to the run" in out
    assert "p_minted_1" in out


def test_a_raw_qualtrics_key_is_never_handed_over_as_a_record_id(
        client, runs_root, monkeypatch):
    """?participant_id= is one of the spellings Qualtrics pipes the raw key
    through. Passing it on as if it were a record id put the page in an
    unbreakable "We couldn't save your consent" loop: /api/consent 404s on it."""
    monkeypatch.setattr(appmod, "create_participant",
                        lambda **kw: (_ for _ in ()).throw(OSError("nope")))
    monkeypatch.setattr(appmod, "get_participant", lambda pid: None)
    r = client.get("/start", params={"participant_id": "RF_TEST_B1"},
                   follow_redirects=False)
    assert "participant_id=" not in r.headers["location"]
    assert "consent=1" not in r.headers["location"]


def test_consent_reattaches_a_record_to_a_run_that_lost_its_mint(
        client, runs_root, monkeypatch):
    """The repair path: the page mints its own record, and the run adopts it, so
    the rest of the run is joinable even though /start's mint failed."""
    run = runs_root.create("RF_TEST_C1", cohort="study")
    assert not run.get("participant_record_id")
    monkeypatch.setattr(appmod, "create_participant", lambda **kw: "p_from_page")
    r = client.post("/api/consent", json={"code": "RF_TEST_C1", "consent_given": True,
                                          "run_id": run["run_id"]})
    assert r.status_code == 200
    assert runs_root.get(run["run_id"])["participant_record_id"] == "p_from_page"


def test_an_existing_record_on_a_run_is_never_overwritten(client, runs_root, monkeypatch):
    """That id is the identity the run's earlier encounters were recorded under."""
    run = runs_root.create("RF_TEST_C2", cohort="study")
    run["participant_record_id"] = "p_original"
    runs_root.save(run)
    monkeypatch.setattr(appmod, "record_consent", lambda pid, v: {"id": pid})
    r = client.post("/api/consent", json={"participant_id": "p_second",
                                          "consent_given": True,
                                          "run_id": run["run_id"]})
    assert r.status_code == 200
    assert runs_root.get(run["run_id"])["participant_record_id"] == "p_original"


def test_the_repair_fires_on_the_body_the_page_actually_sends(client, runs_root,
                                                              monkeypatch):
    """The repair above was unreachable from the only client on this path.

    _adopt_participant_record returned immediately unless the POST carried a
    run_id, and static/v2.html's consent-ACCEPT handler sends
    {code, consent_given, participant_id} and no run_id — only its DECLINE
    handler sends one. So the run kept participant_record_id None, _run_context
    could not resolve the encounter, and the manifest recorded run_id, cohort
    and participant key as null: "not study data" per storage.py. The consented
    participant's first encounter was still silently dropped from the analysis
    set — B20's exact harm — while the code and the test above suggested it was
    repaired, because the test built the payload with run_id by hand.

    The body below is the one v2.html sends, verbatim.
    """
    run = runs_root.create("RF_VERIFY_1", cohort="study")
    assert not run.get("participant_record_id")
    monkeypatch.setattr(appmod, "create_participant", lambda **kw: "p_from_page")
    r = client.post("/api/consent", json={"code": "RF_VERIFY_1",
                                          "consent_given": True,
                                          "participant_id": None})
    assert r.status_code == 200
    assert runs_root.get(run["run_id"])["participant_record_id"] == "p_from_page"


def test_the_repair_without_a_run_id_still_never_overwrites(client, runs_root,
                                                            monkeypatch):
    """The code-based fallback fills a blank and nothing else — it must not be a
    way to repoint a run whose earlier encounters already carry an identity."""
    run = runs_root.create("RF_VERIFY_2", cohort="study")
    run["participant_record_id"] = "p_original"
    runs_root.save(run)
    monkeypatch.setattr(appmod, "create_participant", lambda **kw: "p_from_page")
    client.post("/api/consent", json={"code": "RF_VERIFY_2", "consent_given": True})
    assert runs_root.get(run["run_id"])["participant_record_id"] == "p_original"


def test_the_repair_needs_something_to_go_on(client, runs_root, monkeypatch):
    """No run_id and no code is not a run to adopt onto — it must not guess."""
    run = runs_root.create("RF_VERIFY_3", cohort="study")
    monkeypatch.setattr(appmod, "record_consent", lambda pid, v: {"id": pid})
    client.post("/api/consent", json={"participant_id": "p_x", "consent_given": True})
    assert not runs_root.get(run["run_id"]).get("participant_record_id")


# --- B16 / R24: a decline must not withdraw a run it did not record -----------

def test_a_decline_against_a_consented_record_withdraws_nothing(client, runs_root,
                                                                monkeypatch, capsys):
    """storage.record_decline returns None for an already-consented record: a
    decline cannot retroactively withdraw a consent under which audio and webcam
    were already captured. Withdrawing the run anyway split the contradiction
    across two files — the participant record saying consented and never
    declined, the run saying withdrawn BECAUSE consent was declined — so neither
    one alone shows it, and the participant is locked out of every remaining
    encounter with no clearing path.

    The request that produces this is ordinary, not adversarial: both tabs of a
    duplicated study link get &consent=1, so the one left open still offers
    Decline after the other has consented.
    """
    run = runs_root.create("RF_DECLINE_1", cohort="study")
    monkeypatch.setattr(appmod, "record_decline",
                        lambda pid, v, run_id=None: None)   # the storage guard
    r = client.post("/api/consent/decline",
                    json={"participant_id": "p_consented", "run_id": run["run_id"]})
    assert r.status_code == 200                    # the page has already closed
    assert r.json()["recorded"] is False
    assert r.json()["withdrawn"] is False
    assert runs_root.get(run["run_id"]).get("withdrawn") is None
    assert "was not recorded" in capsys.readouterr().out


def test_a_real_decline_still_stops_the_run(client, runs_root, monkeypatch):
    """The refusal is data and the run must stop handing out encounters, or
    reopening the study link enrols someone who just said no."""
    run = runs_root.create("RF_DECLINE_2", cohort="study")
    monkeypatch.setattr(appmod, "record_decline",
                        lambda pid, v, run_id=None: {"id": pid, "declined": True})
    r = client.post("/api/consent/decline",
                    json={"participant_id": "p_pending", "run_id": run["run_id"]})
    assert r.json() == {"recorded": True, "withdrawn": True, "reason": "recorded",
                        "consent_text_version": r.json()["consent_text_version"]}
    withdrawn = runs_root.get(run["run_id"])["withdrawn"]
    assert withdrawn["reason"] == "declined_consent"


@pytest.fixture()
def participant_store(tmp_path, monkeypatch):
    """An empty participant store under tmp_path, wired through storage's own
    globals — so nothing here writes into the fixture wave."""
    from server import storage

    root = tmp_path / "pdata"
    monkeypatch.setattr(storage, "DATA_DIR", root)
    monkeypatch.setattr(storage, "SESSIONS_DIR", root / "sessions")
    monkeypatch.setattr(storage, "PARTICIPANTS_DIR", root / "participants")
    monkeypatch.setattr(storage, "DB_PATH", root / "index.db")
    storage.init_storage()
    return storage


def test_a_refused_decline_says_which_refusal_it_was(client, participant_store):
    """B16, the half the page acts on.

    `recorded: false` covers two situations a participant must not be told the
    same thing about. If the record already carries consent, an encounter may
    already have been recorded under it — so the "nothing about you was
    recorded" card is a false statement about their own data, and they are still
    enrolled in a study they believe they have left. If there is no record at
    all, nothing was captured and the honest line is that the refusal could not
    be filed. A boolean cannot tell those apart, so the client would have to
    guess, and a guess printed on a consent screen is stated as fact.
    """
    storage = participant_store
    consented = storage.create_participant("RF_DECLINE_CONSENT", False, "2026-09-01")
    assert storage.record_consent(consented, "2026-09-01") is not None

    r = client.post("/api/consent/decline", json={"participant_id": consented})
    assert r.status_code == 200
    assert r.json()["recorded"] is False
    assert r.json()["reason"] == "already_consented"
    # And it really did not un-consent them.
    assert storage.get_participant(consented)["consent_given"] is True

    # The other refusal: no such record. Same booleans, different reason.
    r = client.post("/api/consent/decline",
                    json={"participant_id": "p_0000000000_ffffff"})
    assert r.json()["recorded"] is False
    assert r.json()["reason"] == "no_record"


def test_a_run_with_completed_encounters_is_never_withdrawn_by_a_decline(
        client, runs_root, monkeypatch):
    """A run with finished encounters holds recorded data a late decline did not
    undo. Marking it withdrawn would tell an analyst the participant stopped
    partway through a run they actually finished."""
    run = runs_root.create("RF_DECLINE_3", cohort="study")
    run["completed"] = ["s_1772460300_44c9a2", "s_1772460301_44c9a3"]
    runs_root.save(run)
    monkeypatch.setattr(appmod, "record_decline",
                        lambda pid, v, run_id=None: {"id": pid, "declined": True})
    r = client.post("/api/consent/decline",
                    json={"participant_id": "p_pending", "run_id": run["run_id"]})
    assert r.json()["recorded"] is True          # the refusal is still data
    assert r.json()["withdrawn"] is False
    assert runs_root.get(run["run_id"]).get("withdrawn") is None


# --- B22: the cohort filter belongs in the query ------------------------------

@pytest.fixture()
def index_db(tmp_path, monkeypatch):
    """One internal encounter, newest, in front of five study ones — the shape
    the fixture wave actually has."""
    db = tmp_path / "index.db"
    conn = sqlite3.connect(db)
    conn.execute("CREATE TABLE sessions (id TEXT, scenario TEXT, model TEXT,"
                 " started_at TEXT, n_turns INT, status TEXT, duration_s REAL,"
                 " run_id TEXT, cohort TEXT)")
    rows = [("s_internal", "sc", "m", "2026-03-09", 4, "closed", 60.0, "r0", "internal")]
    rows += [(f"s_study_{i}", "sc", "m", f"2026-03-0{i}", 8, "closed", 300.0,
              f"r{i}", "study") for i in range(1, 6)]
    conn.executemany("INSERT INTO sessions VALUES (?,?,?,?,?,?,?,?,?)", rows)
    conn.commit()
    conn.close()
    from server import storage
    monkeypatch.setattr(storage, "DB_PATH", db)
    return db


def test_a_filtered_page_is_not_emptied_by_rows_it_filtered_out(client, index_db):
    """LIMIT counts rows the query returns, not rows the caller keeps. The
    internal encounter is the newest row, so it used to occupy page one on its
    own and a pager that stops on a short page exported nothing at all."""
    r = client.get("/api/sessions", params={"key": KEY, "cohort": "study",
                                            "limit": 1, "offset": 0})
    assert r.status_code == 200
    assert [row["id"] for row in r.json()] == ["s_study_5"]


def test_paging_a_cohort_walks_the_whole_wave(client, index_db):
    seen = []
    for offset in range(0, 10, 2):
        page = client.get("/api/sessions", params={"key": KEY, "cohort": "study",
                                                   "limit": 2, "offset": offset}).json()
        seen += [row["id"] for row in page]
        if len(page) < 2:
            break                      # the idiom every pager uses
    assert sorted(seen) == [f"s_study_{i}" for i in range(1, 6)]


def test_the_cohort_filter_still_excludes_other_cohorts(client, index_db):
    ids = [row["id"] for row in client.get(
        "/api/sessions", params={"key": KEY, "cohort": "study", "limit": 100}).json()]
    assert "s_internal" not in ids and len(ids) == 5
    internal = client.get("/api/sessions",
                          params={"key": KEY, "cohort": "internal", "limit": 100}).json()
    assert [row["id"] for row in internal] == ["s_internal"]


def test_an_index_without_the_cohort_column_answers_a_cohort_query_with_nothing(
        client, tmp_path, monkeypatch):
    db = tmp_path / "old.db"
    conn = sqlite3.connect(db)
    conn.execute("CREATE TABLE sessions (id TEXT, scenario TEXT, model TEXT,"
                 " started_at TEXT, n_turns INT, status TEXT, duration_s REAL)")
    conn.execute("INSERT INTO sessions VALUES ('s_1','sc','m','2026-01-01',8,'closed',9.0)")
    conn.commit()
    conn.close()
    from server import storage
    monkeypatch.setattr(storage, "DB_PATH", db)
    assert client.get("/api/sessions",
                      params={"key": KEY, "cohort": "study"}).json() == []
    # ...but an unfiltered listing still works on a pre-migration index.
    assert len(client.get("/api/sessions", params={"key": KEY}).json()) == 1


# --- B44: the rating draw must not run on the loop ----------------------------

def test_the_assignment_draw_runs_off_the_event_loop(served, monkeypatch):
    """853 ms on the fixture wave, on the same loop as every live encounter.
    A researcher allocating while the last encounters run is how a wave ends."""
    from server import raters

    seen = {}

    def fake_assign(session_ids, rater_ids, per_encounter=3, seed=None):
        seen["thread"] = threading.get_ident()
        return [{"assignment_id": "as_1"}]

    monkeypatch.setattr(raters, "assign", fake_assign)
    loop_thread, r = on_the_loop(lambda ac: ac.post(
        "/api/rater-assignments", params={"key": KEY},
        json={"session_ids": ["s_1"], "rater_ids": ["ra_1", "ra_2"],
              "per_encounter": 1}))
    assert r.status_code == 200
    assert seen["thread"] != loop_thread


def test_the_assignment_listing_reads_off_the_event_loop(served, monkeypatch):
    from server import raters

    seen = {}

    def fake_list(cohort=None, status=None):
        seen["thread"] = threading.get_ident()
        return [{"assignment_id": "as_1", "rater_id": "ra_1", "session_id": "s_1"}]

    monkeypatch.setattr(raters, "list_assignments", fake_list)
    monkeypatch.setattr(raters, "list_raters", lambda: [{"rater_id": "ra_1",
                                                         "name": "A", "kind": "crowd"}])
    loop_thread, r = on_the_loop(lambda ac: ac.get("/api/rater-assignments",
                                                   params={"key": KEY}))
    assert r.status_code == 200
    assert r.json()[0]["rater_name"] == "A"
    assert seen["thread"] != loop_thread


def test_an_unknown_rater_is_still_a_404(client, monkeypatch):
    from server import raters

    monkeypatch.setattr(raters, "get_rater", lambda rid: None)
    r = client.get("/api/rater-assignments", params={"key": KEY, "rater_id": "ra_nope"})
    assert r.status_code == 404


def test_a_rater_who_owes_nothing_is_still_a_200(client, monkeypatch):
    """The unknown-rater sentinel is None, not []. An empty queue is a real
    answer and must not collapse into the 404."""
    from server import raters

    monkeypatch.setattr(raters, "get_rater",
                        lambda rid: {"rater_id": rid, "name": "A", "kind": "crowd"})
    monkeypatch.setattr(raters, "assignments_for_rater", lambda rid, status=None: [])
    monkeypatch.setattr(raters, "list_raters", lambda: [])
    r = client.get("/api/rater-assignments", params={"key": KEY, "rater_id": "ra_1"})
    assert r.status_code == 200 and r.json() == []


def test_the_rating_code_is_minted_off_the_loop_too(served, monkeypatch):
    """rating_code looks like arithmetic and is not.

    It is an HMAC under runs._run_code_secret, which re-reads
    DATA_DIR/.run_code_secret from disk on EVERY call unless RUN_CODE_SECRET is
    set. So an N-row listing was still doing N disk reads on the event loop
    after the four store reads were moved off it -- while the comment above the
    worker hop said all of it had been moved.
    """
    from server import rater_packet, raters

    seen = {}

    def watched_code(session_id):
        seen.setdefault("threads", set()).add(threading.get_ident())
        return "RC-XXXXXXXXXX"

    monkeypatch.setattr(rater_packet, "rating_code", watched_code)
    monkeypatch.setattr(raters, "list_assignments", lambda cohort=None, status=None: [
        {"assignment_id": "as_%d" % i, "rater_id": "ra_1", "session_id": "s_%d" % i}
        for i in range(5)])
    monkeypatch.setattr(raters, "list_raters",
                        lambda: [{"rater_id": "ra_1", "name": "A", "kind": "crowd"}])
    loop_thread, r = on_the_loop(lambda ac: ac.get("/api/rater-assignments",
                                                   params={"key": KEY}))
    assert r.status_code == 200 and len(r.json()) == 5
    assert seen["threads"] and loop_thread not in seen["threads"]


# --- B44 (remainder): the three rater-console routes the fix skipped ----------

@pytest.fixture()
def rater_store(monkeypatch, served):
    """A rater, a queue, and a packet -- each recording the thread it ran on.

    Every one of these is a JSON scan plus a sqlite open in the real module.
    """
    from server import rater_packet, raters

    threads = {}

    def note(name):
        threads.setdefault(name, threading.get_ident())

    def rater_for_token(tok):
        note("token")
        if tok != "tok":
            return None
        return {"rater_id": "ra_1", "name": "A", "kind": "crowd"}

    def assignments_for_rater(rid, status=None):
        note("queue")
        return [{"assignment_id": "as_1", "rater_id": "ra_1", "session_id": "s_1",
                 "status": "pending", "assigned_at": "2026-03-01"}]

    def get_assignment(aid):
        note("assignment")
        return {"assignment_id": aid, "rater_id": "ra_1", "session_id": "s_1",
                "status": "pending"}

    def build(session_id, order_seed=None):
        note("packet")
        return {"session_id": session_id, "items": []}

    monkeypatch.setattr(raters, "rater_for_token", rater_for_token)
    monkeypatch.setattr(raters, "assignments_for_rater", assignments_for_rater)
    monkeypatch.setattr(raters, "get_assignment", get_assignment)
    monkeypatch.setattr(rater_packet, "build", build)
    monkeypatch.setattr(rater_packet, "rating_code", lambda sid: "RC-XXXXXXXXXX")
    return threads


@pytest.mark.parametrize("url,ran", [
    ("/api/rater/me", ("token", "queue")),
    ("/api/rater/assignments", ("token", "queue")),
    ("/api/rater/packet/as_1", ("token", "assignment", "packet")),
])
def test_the_rater_console_routes_read_off_the_event_loop(rater_store, url, ran):
    """The three routes B44 named by line and the fix did not touch.

    The rater console opens a packet -- manifest, transcript, scenario spec,
    events and a presign -- once per assignment, and fetches /api/rater/me on
    load and after every submission. Every one of those reads shared the loop
    with every live encounter's audio, three lines below a run_in_threadpool
    added for exactly this reason.
    """
    loop_thread, r = on_the_loop(lambda ac: ac.get(url, params={"token": "tok"}))
    assert r.status_code == 200
    for name in ran:
        assert name in rater_store, "%s never ran for %s" % (name, url)
        assert rater_store[name] != loop_thread, "%s ran on the loop for %s" % (name, url)


@pytest.mark.parametrize("url", ["/api/rater/me", "/api/rater/assignments",
                                 "/api/rater/packet/as_1"])
def test_a_bad_rater_token_is_still_a_401_from_inside_the_worker(rater_store, client,
                                                                 url):
    """HTTPException raised inside the hop has to propagate unchanged, or moving
    the read off the loop would turn every refusal into a 500."""
    assert client.get(url, params={"token": "nope"}).status_code == 401


def test_a_packet_whose_encounter_is_gone_is_still_a_404(rater_store, client,
                                                         monkeypatch):
    from server import rater_packet

    monkeypatch.setattr(rater_packet, "build", lambda sid, order_seed=None: None)
    assert client.get("/api/rater/packet/as_1",
                      params={"token": "tok"}).status_code == 404


# --- B45 (remainder): one bad float must not take down the whole export -------

def test_a_non_finite_rating_does_not_500_the_whole_export(client, monkeypatch):
    """/api/reliability went through _json_safe and /api/ratings did not.

    Starlette serialises with allow_nan=False, so one NaN anywhere in the corpus
    -- a record written before the coercion existed, a hand-edited file, or any
    future float field the coercion does not police -- 500s the entire export
    and hides every other rating behind the one bad row.
    """
    from server import ratings

    rows = [{"assignment_id": "as_1", "seconds": float("nan"), "scores": {"i1": 4}},
            {"assignment_id": "as_2", "seconds": 61.0, "scores": {"i1": 5}}]
    monkeypatch.setattr(ratings, "all_ratings", lambda cohort=None: rows)
    r = client.get("/api/ratings", params={"key": KEY})
    assert r.status_code == 200
    body = r.json()
    assert body["n"] == 2
    assert body["ratings"][0]["seconds"] is None     # undefined, not fatal
    assert body["ratings"][1]["seconds"] == 61.0     # the good row survives


@pytest.mark.parametrize("bad", [float("nan"), float("inf"), float("-inf")])
def test_every_non_finite_shape_degrades_to_null(client, monkeypatch, bad):
    from server import ratings

    monkeypatch.setattr(ratings, "all_ratings",
                        lambda cohort=None: [{"scores": {"i1": bad}}])
    r = client.get("/api/ratings", params={"key": KEY})
    assert r.status_code == 200
    assert r.json()["ratings"][0]["scores"]["i1"] is None


# --- R14 / R23: comments the code must not contradict -------------------------

def test_the_receipt_docstring_names_the_call_the_route_actually_makes():
    """The confirm route calls head_video, not uploaded_size -- the whole point
    of that change was to keep "S3 says none" apart from "S3 would not say", and
    uploaded_size collapses them. A docstring naming the old call sends the next
    reader looking for a contract that is not there."""
    import inspect

    assert "head_video()" in video.upload_receipt.__doc__
    src = inspect.getsource(appmod.api_video_uploaded)
    assert "video.head_video(" in src and "video.uploaded_size(" not in src


def test_the_storage_preflight_rationale_is_not_written_in_the_present_tense():
    """The rationale described the browser silently discarding the failure as a
    live defect. static/v2.html now resolves {ok:false, reason} and reports it,
    so a reader auditing the client from that comment would go hunting for a bug
    that is gone.

    Read with the comment's own wrapping normalised away: the sentence is a
    prose comment and rewrapping it is not a regression, so pinning the exact
    line break would make this test fail for a reason it does not care about
    and teach the next person to edit the test instead of the claim.
    """
    src = " ".join(
        Path(appmod.__file__).read_text(encoding="utf-8").replace("#", " ").split()
    )
    assert "the failure is a silent resolve(false)" not in src
    assert "used to be a silent resolve(false)" in src
