"""Serving the webcam recording's bytes through the application.

The recording is the artefact Phase 2 rates, and until this round no process in
this repository ever held one: the browser PUT it straight to S3 and the rater's
page GET it straight back, so nobody — not the researcher, not CI, not the
maintainer — had ever watched a recording in the rating console. Without a live
AWS credential there were no bytes on the machine at all.

These tests are the first exercise that surface has ever had. Nothing here opens
a socket: the S3 client is replaced wholesale (server.video._s3) with a stub that
answers in real response shapes and fails with the real botocore exception types,
and the session directory is a tmp_path.

The questions they keep asking:
  - can a machine with NO credentials serve a recording (local must win, and win
    without S3 being consulted),
  - does a byte range mean the same thing on both branches (the route cannot
    tell them apart, so they must not differ),
  - is "no recording" distinguishable from "not those bytes" (404 vs 416), and
  - does anything hold a whole recording in memory (nothing may).
"""
from __future__ import annotations

import inspect
import io
import json
import os
import re
from pathlib import Path

import pytest
from botocore.exceptions import (
    ClientError, EndpointConnectionError, NoCredentialsError,
)

from server import video

SESSION_ID = "s_1772460300_44c9a2"
OTHER_ID = "s_1772460301_991bbb"
# The minted assignment shape (raters._ASSIGNMENT_ID_RE). The packet's playback
# URL is addressed to the ASSIGNMENT, not the encounter, so this is the only id
# a rater's network tab ever sees.
ASSIGNMENT_ID = "as_0123456789ab"


# --- stubs -------------------------------------------------------------------

def aws_error(code: str, op: str = "HeadObject", status: int = 403) -> ClientError:
    """A real botocore ClientError, shaped the way S3 shapes one."""
    return ClientError(
        {"Error": {"Code": code, "Message": f"{code} (stub)"},
         "ResponseMetadata": {"HTTPStatusCode": status}},
        op,
    )


# The failures a credential-less laptop, a first-apply IAM state or a missing
# VPC endpoint actually produce. None of them may reach a caller uncaught.
AWS_FAILURES = [
    aws_error("AccessDenied"),
    aws_error("ExpiredToken"),
    aws_error("NoSuchBucket", status=404),
    aws_error("PermanentRedirect", status=301),
    aws_error("SlowDown", status=503),
    NoCredentialsError(),
    EndpointConnectionError(endpoint_url="https://s3.us-east-1.amazonaws.com"),
]

# Real container magic, because _sniff_content_type reads it and a rater served
# the wrong type gets a black rectangle with no error to report.
WEBM_MAGIC = b"\x1a\x45\xdf\xa3"
MP4_MAGIC = b"\x00\x00\x00\x18ftypiso5"


def webm(n: int = 4096) -> bytes:
    return WEBM_MAGIC + bytes((i * 7 + 3) % 251 for i in range(n - 4))


def mp4(n: int = 4096) -> bytes:
    return MP4_MAGIC + bytes((i * 11 + 5) % 251 for i in range(n - 12))


class FakeBody:
    """A botocore StreamingBody's read/close surface, and a record of both."""

    def __init__(self, data: bytes):
        self._buf = io.BytesIO(data)
        self.reads: list = []
        self.closed = False

    def read(self, n=-1):
        self.reads.append(n)
        return self._buf.read(n)

    def close(self):
        self.closed = True


class FakeS3:
    """Stands in for the boto3 s3 client: holds one object, or refuses."""

    def __init__(self, *, data: bytes = None, content_type=None,
                 head_error: BaseException = None,
                 get_error: BaseException = None):
        self.data = data
        self.content_type = content_type
        self.head_error = head_error
        self.get_error = get_error
        self.head_calls = 0
        self.ranges: list = []          # the Range of every get_object, in order
        self.bodies: list = []

    def head_object(self, **kw):
        self.head_calls += 1
        if self.head_error is not None:
            raise self.head_error
        if self.data is None:
            raise aws_error("404", op="HeadObject", status=404)
        return {"ContentLength": len(self.data)}

    def get_object(self, Bucket=None, Key=None, Range=None):
        self.ranges.append(Range)
        if self.get_error is not None:
            raise self.get_error
        if self.data is None:
            raise aws_error("NoSuchKey", op="GetObject", status=404)
        if Range:
            m = re.fullmatch(r"bytes=(\d+)-(\d+)", Range)
            assert m, f"S3 was sent a Range it cannot parse: {Range!r}"
            lo, hi = int(m.group(1)), int(m.group(2))
            assert lo <= hi < len(self.data), \
                f"S3 was sent an unsatisfiable Range {Range!r} for {len(self.data)} bytes"
            payload = self.data[lo:hi + 1]
        else:
            payload = self.data
        body = FakeBody(payload)
        self.bodies.append(body)
        resp = {"Body": body, "ContentLength": len(payload)}
        if self.content_type is not None:
            resp["ContentType"] = self.content_type
        return resp


# --- fixtures ----------------------------------------------------------------

@pytest.fixture()
def sessions_root(tmp_path, monkeypatch):
    """An empty session directory for one encounter, and no S3 at all."""
    root = tmp_path / "sessions"
    (root / SESSION_ID).mkdir(parents=True)
    monkeypatch.setattr(video, "SESSIONS_DIR", root)
    return root


@pytest.fixture()
def s3(monkeypatch):
    """An empty bucket by default; each test swaps in the object or failure."""
    stub = FakeS3()
    monkeypatch.setattr(video, "_s3", stub)
    return stub


def put_local(sessions_root, data: bytes, session_id: str = SESSION_ID):
    path = sessions_root / session_id / video.LOCAL_VIDEO_NAME
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    return path


def drain(stream) -> bytes:
    return b"".join(stream.chunks)


# --- local_path: arithmetic, and a gate --------------------------------------

def test_local_path_is_the_name_every_other_reader_already_globs(sessions_root):
    """encounter_record.build and app._video_status both glob session_dir for
    'webcam*' to find a dev capture. A local recording stored under any other
    name would be invisible to both of them, and record.json would keep saying
    the encounter has no video while the console played one."""
    p = video.local_path(SESSION_ID)
    assert p == sessions_root / SESSION_ID / "webcam.webm"
    assert p.name.startswith("webcam")


# --- exists(): the question that replaces "did a confirm event arrive" --------

def test_exists_finds_a_local_file_without_asking_s3(sessions_root, s3):
    """The credential-free laptop. If this consulted S3 it would answer False
    on the machine the researcher actually uses."""
    put_local(sessions_root, webm())
    assert video.exists(SESSION_ID) is True
    assert s3.head_calls == 0, "local must win without S3 being consulted"


def test_exists_finds_the_object_when_there_is_no_local_file(sessions_root, s3):
    """The permanently-unrateable encounter: bytes in the bucket, no
    video_uploaded event, and every reader previously said 'no recording'."""
    s3.data = webm()
    assert video.exists(SESSION_ID) is True


def test_exists_is_false_when_s3_says_the_object_is_absent(sessions_root, s3):
    assert video.exists(SESSION_ID) is False


@pytest.mark.parametrize("bad", ["../../etc", "S_1772460300_44C9A2", "", None])
def test_exists_is_false_not_an_exception_for_a_bad_id(sessions_root, s3, bad):
    assert video.exists(bad) is False


# --- store_local: streaming, atomic ------------------------------------------

def test_store_local_writes_a_file_like_source(sessions_root):
    data = webm(9000)
    n = video.store_local(SESSION_ID, io.BytesIO(data))
    assert n == len(data)
    assert video.local_path(SESSION_ID).read_bytes() == data


def test_store_local_writes_a_byte_iterator_source(sessions_root):
    data = webm(9000)
    chunks = [data[i:i + 700] for i in range(0, len(data), 700)]
    n = video.store_local(SESSION_ID, iter(chunks))
    assert n == len(data)
    assert video.local_path(SESSION_ID).read_bytes() == data


def test_store_local_writes_a_plain_bytes_source(sessions_root):
    """`iter(b"...")` yields INTEGERS, so a bytes body falling into the iterator
    branch would reach fh.write as a TypeError megabytes into a working upload."""
    data = webm(3000)
    assert video.store_local(SESSION_ID, data) == len(data)
    assert video.local_path(SESSION_ID).read_bytes() == data


def test_store_local_never_holds_the_whole_recording(sessions_root):
    """A recording is tens of megabytes and this runs in a request handler. The
    proof is that it asks for bounded reads, more than once."""
    data = webm(video.CHUNK_SIZE * 2 + 1024)
    src = FakeBody(data)
    assert video.store_local(SESSION_ID, src) == len(data)
    assert len(src.reads) >= 3, "one read means the whole body was buffered"
    assert max(src.reads) <= video.CHUNK_SIZE


def test_store_local_leaves_no_temp_file_behind(sessions_root):
    video.store_local(SESSION_ID, io.BytesIO(webm()))
    leftovers = list((sessions_root / SESSION_ID).glob("*.tmp"))
    assert leftovers == []


def test_two_concurrent_stores_do_not_share_a_temp_name(sessions_root, monkeypatch):
    """A spliced recording is the failure mode: two PUTs for one session
    interleaving into one shared '.tmp' would land bytes from both."""
    names = []
    real = video.replace_with_retry
    monkeypatch.setattr(video, "replace_with_retry",
                        lambda tmp, dest, *a, **k: (names.append(tmp.name),
                                                    real(tmp, dest, *a, **k))[1])
    video.store_local(SESSION_ID, io.BytesIO(webm()))
    video.store_local(SESSION_ID, io.BytesIO(webm()))
    assert names[0] != names[1]


def test_store_local_refuses_an_async_source_by_name(sessions_root):
    """Starlette's request.stream() is an async generator. Failing here, with
    the type named, beats raising deep inside the write."""
    async def agen():
        yield b"x"

    with pytest.raises(TypeError) as e:
        video.store_local(SESSION_ID, agen())
    assert "iterator" in str(e.value)


def test_store_local_refuses_a_bad_session_id(sessions_root):
    with pytest.raises(ValueError):
        video.store_local("../escape", io.BytesIO(b"x"))


def test_a_stored_recording_is_immediately_visible_to_exists(sessions_root, s3):
    """The whole point of the upload fallback: bytes land, and every reader can
    see them without a confirmation event ever arriving."""
    assert video.exists(SESSION_ID) is False
    # That first call legitimately fell through to the bucket — there was
    # nothing local to find. After the store there must be no further HEAD.
    asked_before = s3.head_calls
    video.store_local(SESSION_ID, io.BytesIO(webm()))
    assert video.exists(SESSION_ID) is True
    assert s3.head_calls == asked_before


# --- open_stream: nothing to serve -------------------------------------------

# --- open_stream: local wins --------------------------------------------------

# --- open_stream: the range contract, on both branches ------------------------

RANGES = [
    (None, None),        # the whole object
    (0, 99),             # a leading window
    (100, 199),          # a mid window
    (4000, None),        # open-ended: bytes=4000-
    (0, None),           # the seekable-probe form
    (0, 10 ** 9),        # an end past the object: clamp, do not refuse
    (None, 256),         # the suffix form: bytes=-256
    (4095, 4095),        # the last single byte
]


# --- open_stream: what actually goes to S3 ------------------------------------

# --- content type: the black rectangle ---------------------------------------

# --- the shape of the thing the route is handed ------------------------------

# --- the sniff, and how often it costs a round trip ---------------------------
#
# The stored Content-Type is the browser's unverified word on an unsigned PUT,
# so on the S3 branch the container is read out of the object's first twelve
# bytes. That read is a THIRD round trip on top of the HEAD and the body GET,
# and it used to happen on every seek: twenty seeks measured sixty round trips,
# and a rater scrubbing a seven-minute encounter makes far more than twenty.
# What the tests below defend is that making it cheap did not make it wrong.

# --- the presigned playback URL, and why there is no longer one ---------------

# --- credentials: asked once per process, not once per encounter --------------


class FakeSigner:
    def __init__(self, credentials):
        self._credentials = credentials


class SignedS3(FakeS3):
    """A FakeS3 that also carries a request signer, the way a real client does.

    A boto3 client resolves its credentials ONCE, when it is constructed, and
    keeps the answer on its request signer. That is the whole reason the answer
    can be cached at all, and the reason a cached one has to expire.
    """

    def __init__(self, credentials=None, **kw):
        super().__init__(**kw)
        self._request_signer = FakeSigner(credentials)


class Clock:
    def __init__(self, now: float = 1000.0):
        self.now = now

    def monotonic(self) -> float:
        return self.now


class CredentialEnv:
    """Every client build counted, because building one IS the chain walk.

    On a machine that is not an EC2 instance the walk ends at 169.254.169.254,
    the instance metadata service, which is unroutable — botocore waits out its
    connect timeout twice before giving up. Measured at 2.30 s. Counting builds
    is counting those 2.30 s waits.
    """

    def __init__(self):
        self.credentials = None
        self.data = None
        self.builds: list = []

    def client(self, *a, **kw):
        made = SignedS3(credentials=self.credentials, data=self.data)
        self.builds.append(made)
        return made


@pytest.fixture()
def creds(monkeypatch):
    env = CredentialEnv()
    clock = Clock()
    env.clock = clock
    monkeypatch.setattr(video, "boto3", env)
    monkeypatch.setattr(video, "time", clock)
    # A clean slate: these are process-global by design (the answer is about the
    # process), so a test that inherited a neighbour's would prove nothing.
    monkeypatch.setattr(video, "_s3", None)
    monkeypatch.setattr(video, "_no_creds_client", None)
    monkeypatch.setattr(video, "_no_creds_until", 0.0)
    return env


def test_a_credential_appearing_later_is_not_invisible_until_a_restart(
        sessions_root, creds):
    """The thing the cache must not break, and the reason it expires.

    A task can come up a moment before its role is reachable and a researcher
    can run `aws sso login` in the next terminal. A boto3 client pins its
    credentials at construction, so noticing either one means throwing the
    client away and walking the chain again — a cache that only remembered would
    leave the process blind until somebody restarted it, which is a worse bug
    than the one it fixed.
    """
    assert video.exists(SESSION_ID) is False
    creds.credentials = object()          # the role turns up one second late
    creds.data = webm()
    # Inside the window, still the remembered answer: that is the point of it.
    assert video.exists(SESSION_ID) is False
    assert len(creds.builds) == 1
    creds.clock.now += video.CREDENTIAL_RECHECK_SECONDS + 1
    assert video.exists(SESSION_ID) is True
    assert len(creds.builds) == 2, "the window expired, so the chain was rewalked"


def test_a_process_that_has_credentials_is_never_short_circuited(
        sessions_root, creds):
    """The failure mode to be afraid of. "I cannot tell" must never be recorded
    as "there are none": on a deployment whose credentials are fine, that would
    switch the bucket off silently and every encounter without a local file
    would read as one that was never filmed."""
    creds.credentials = object()
    creds.data = webm()
    for _ in range(3):
        assert video.exists(SESSION_ID) is True
    assert creds.builds[0].head_calls == 3, "every call reached S3"
    assert len(creds.builds) == 1


def test_a_client_somebody_else_installed_is_always_asked(sessions_root, creds):
    """A stub with no request signer at all — a client this module cannot read
    an answer off. Not being able to tell is not the same as knowing there are
    none, and the module must fall through to the call it would have made before
    the shortcut existed, even while a remembered negative is still inside its
    window. storage_preflight and every test in this file hand video a client of
    their own; an inherited verdict would answer for all of them."""
    assert video.exists(SESSION_ID) is False        # caches "no credentials"
    assert len(creds.builds) == 1
    stub = FakeS3(data=webm())
    video._s3 = stub                                # restored by the fixture
    assert video.exists(SESSION_ID) is True
    assert stub.head_calls == 1


# =============================================================================
# The two HTTP routes (agent A1). server/app.py only.
#
# Everything above this line is about server/video.py — where the bytes come
# from. Everything below is about what the two routes do with them: which
# credential opens the playback route, what a Range header turns into on the
# wire, and what the upload fallback refuses.
#
# server.video is injected here rather than driven for real, and deliberately:
# these tests are about the route layer, and the route layer has to be provable
# on its own. The reference open_stream below reads a REAL file off disk with a
# real seek, so every byte and every offset asserted against is genuine; the
# S3-or-local decision is video.py's contract to keep and the tests above are
# where it is kept. video_key and upload_receipt are NOT faked — they are the
# shipping functions, so the event this route writes is read back by the same
# reader production uses.
# =============================================================================

import json
import sys as _sys
import types as _types
from dataclasses import dataclass
from typing import Iterator

from fastapi.testclient import TestClient

from server import app as appmod

TOKEN_A = "rt_" + "a" * 32
TOKEN_B = "rt_" + "b" * 32
MINE = "as_aaaaaaaaaaaa"
THEIRS = "as_bbbbbbbbbbbb"
BAD_ROW = "as_cccccccccccc"       # an assignment whose session_id is not minted
PARTICIPANT = "p_1"

# Matches video.LOCAL_VIDEO_NAME, spelled out rather than imported so this half
# of the file still runs while the other half's module is being written.
LOCAL_NAME = "webcam.webm"


# --- the route layer's stand-in for server.video ------------------------------

@dataclass
class RefStream:
    """video.VideoStream's fields, exactly. See the settled contract."""
    chunks: Iterator[bytes]
    length: int
    total: int
    content_type: str
    start: int
    end: int


class RefRangeNotSatisfiable(Exception):
    def __init__(self, total: int):
        self.total = total
        super().__init__(total)


class RouteVideo:
    """A reference video.py for the two routes to run against.

    Small enough to read in one screen and faithful where it matters: the bytes
    come off a real file with a real seek, the range arithmetic is the
    contract's (inclusive, clamped at the end, suffix means the LAST n), and
    every call the routes make is recorded, so a test can assert what the route
    did as well as what it answered.
    """

    def __init__(self, root, *, chunk: int = 8):
        self.root = root
        self.chunk = chunk
        self.opened: list = []        # (session_id, start, end) per open_stream
        self.stored: list = []        # (session_id, bytes written)
        self.pulled: list = []        # one entry per chunk actually yielded
        self.exists_answer = None     # None = ask the disk
        self.store_error: BaseException = None
        self.content_type = "video/webm"

    # --- the settled contract ---
    def local_path(self, session_id: str):
        from server.storage import SESSION_ID_RE
        if not SESSION_ID_RE.fullmatch(session_id or ""):
            raise ValueError("bad session_id")
        return self.root / session_id / LOCAL_NAME

    def exists(self, session_id: str) -> bool:
        if self.exists_answer is not None:
            return self.exists_answer
        try:
            p = self.local_path(session_id)
        except ValueError:
            return False
        return p.is_file() and p.stat().st_size > 0

    def store_local(self, session_id: str, source) -> int:
        if self.store_error is not None:
            raise self.store_error
        path = self.local_path(session_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".part")
        written = 0
        try:
            # The builtin rather than Path.open, for the reason spelled out at
            # video._local_content_type: the unpinned-text-I/O scan in
            # tests/test_deploy_portability reads the mode out of the SECOND
            # positional argument, so a binary Path.open("wb") reads to it as an
            # unpinned text open and lands in a list asserted to only shrink.
            with open(tmp, "wb") as fh:
                for chunk in source:
                    fh.write(chunk)
                    written += len(chunk)
            os.replace(tmp, path)
        except BaseException:
            # Temp-then-replace, so a refused upload leaves no half recording.
            tmp.unlink(missing_ok=True)
            raise
        self.stored.append((session_id, written))
        return written

    def open_stream(self, session_id, *, start=None, end=None):
        self.opened.append((session_id, start, end))
        path = self.local_path(session_id)          # ValueError on a crafted id
        if not path.is_file():
            return None
        total = path.stat().st_size
        if total == 0:
            return None
        if start is None and end is None:
            first, last = 0, total - 1
        elif start is None:
            if end <= 0:
                raise RefRangeNotSatisfiable(total)
            first, last = max(0, total - end), total - 1
        else:
            first = start
            last = total - 1 if end is None else min(end, total - 1)
            if first >= total or first > last:
                raise RefRangeNotSatisfiable(total)
        return RefStream(
            chunks=self._read(path, first, last),
            length=last - first + 1, total=total,
            content_type=self.content_type, start=first, end=last,
        )

    def _read(self, path, first, last):
        with open(path, "rb") as fh:      # the builtin: see store_local above
            fh.seek(first)
            remaining = last - first + 1
            while remaining > 0:
                buf = fh.read(min(self.chunk, remaining))
                if not buf:
                    return
                remaining -= len(buf)
                self.pulled.append(len(buf))
                yield buf


class RouteRaters:
    """Two raters, one assignment each, plus a row with an unusable session_id."""

    def __init__(self):
        self.people = {
            "ra_1": {"rater_id": "ra_1", "name": "Rater A", "kind": "trained"},
            "ra_2": {"rater_id": "ra_2", "name": "Rater B", "kind": "crowd"},
        }
        self.tokens = {TOKEN_A: "ra_1", TOKEN_B: "ra_2"}
        self.assignments = {
            MINE: {"assignment_id": MINE, "session_id": SESSION_ID,
                   "rater_id": "ra_1", "status": "pending"},
            THEIRS: {"assignment_id": THEIRS, "session_id": OTHER_ID,
                     "rater_id": "ra_2", "status": "pending"},
            BAD_ROW: {"assignment_id": BAD_ROW, "session_id": "hand-written",
                      "rater_id": "ra_1", "status": "pending"},
        }

    def rater_for_token(self, token):
        rid = self.tokens.get(token)
        return self.people.get(rid) if rid else None

    def get_assignment(self, assignment_id):
        return self.assignments.get(assignment_id)


def _as_module(name, obj):
    mod = _types.ModuleType(f"server.{name}")
    for attr in dir(obj):
        if not attr.startswith("_"):
            setattr(mod, attr, getattr(obj, attr))
    return mod


def _wire(tmp_path, monkeypatch, *, stand_in: bool):
    """The app, a tmp sessions root, two encounters, and no AWS at all.

    video._s3 is a stub that raises NoCredentialsError at every call — the state
    of a researcher's laptop and of CI. Nothing in this section may need it.

    `stand_in` chooses what sits behind the routes: RouteVideo, so the route
    layer can be pinned on its own and can be pushed into states a real module
    would not reach on demand, or the real server.video, for the end-to-end
    check that the two halves actually meet.
    """
    import server

    root = tmp_path / "sessions"
    for sid in (SESSION_ID, OTHER_ID):
        (root / sid).mkdir(parents=True)
        (root / sid / "manifest.json").write_text(
            json.dumps({"session_id": sid, "participant_id": PARTICIPANT}),
            encoding="utf-8")

    fake_video = RouteVideo(root)
    if stand_in:
        # exists / local_path / store_local / open_stream are the route layer's
        # stand-ins. video_key, upload_receipt and SESSIONS_DIR stay REAL, so
        # the event the upload route writes is read back by production's own
        # reader.
        for attr in ("exists", "local_path", "store_local", "open_stream"):
            monkeypatch.setattr(video, attr, getattr(fake_video, attr),
                                raising=False)
        monkeypatch.setattr(video, "RangeNotSatisfiable", RefRangeNotSatisfiable,
                            raising=False)
    monkeypatch.setattr(video, "SESSIONS_DIR", root)
    monkeypatch.setattr(video, "_s3",
                        FakeS3(head_error=NoCredentialsError(),
                               get_error=NoCredentialsError()))

    raters = RouteRaters()
    mod = _as_module("raters", raters)
    monkeypatch.setitem(_sys.modules, "server.raters", mod)
    monkeypatch.setattr(server, "raters", mod, raising=False)

    monkeypatch.setattr(appmod, "SESSIONS_DIR", root)
    monkeypatch.setattr(appmod, "PARTICIPANT_KEY_REQUIRED", False)
    monkeypatch.setattr(appmod, "ALLOWED_HOSTS",
                        ["localhost", "127.0.0.1", "testserver"])

    # No `with`, so the lifespan does not run. Neither route needs it — the
    # session directory is the tmp one above, not the one startup creates — and
    # startup runs both boot preflights, which cost about a second of failing
    # credential resolution per test and would put two minutes on this file
    # alone for nothing.
    return _types.SimpleNamespace(client=TestClient(appmod.app), root=root,
                                  video=fake_video, raters=raters)


@pytest.fixture()
def wired(tmp_path, monkeypatch):
    """The routes over RouteVideo. See _wire."""
    return _wire(tmp_path, monkeypatch, stand_in=True)


@pytest.fixture()
def live_wired(tmp_path, monkeypatch):
    """The routes over the real server.video. See _wire."""
    return _wire(tmp_path, monkeypatch, stand_in=False)


def recording(wired, data: bytes, session_id: str = SESSION_ID) -> bytes:
    """Put real bytes on disk for one encounter, the way an upload would."""
    (wired.root / session_id / LOCAL_NAME).write_bytes(data)
    return data


def events_of(wired, session_id: str = SESSION_ID) -> list:
    path = wired.root / session_id / "events.jsonl"
    if not path.is_file():
        return []
    return [json.loads(line) for line in
            path.read_text(encoding="utf-8").splitlines() if line.strip()]


VIDEO_URL = f"/api/rater/video/{MINE}"
UPLOAD_URL = f"/api/sessions/{SESSION_ID}/video"


# --- GET /api/rater/video: the credential ------------------------------------

# --- GET /api/rater/video: the whole object ----------------------------------

# --- GET /api/rater/video: the ranges ----------------------------------------

# --- PUT /api/sessions/{id}/video: the authorisation --------------------------

def test_the_upload_refuses_a_participant_who_does_not_own_the_session(wired):
    """The presign route's rule, and the reason it exists: without it any known
    or guessed session id lets an outsider write over someone else's
    recording."""
    r = wired.client.put(UPLOAD_URL, params={"participant_id": "p_someone_else"},
                         content=webm(64))
    assert r.status_code == 403
    assert not (wired.root / SESSION_ID / LOCAL_NAME).exists()


def test_the_upload_refuses_a_request_with_no_participant_at_all(wired):
    assert wired.client.put(UPLOAD_URL, content=webm(64)).status_code == 403


def test_the_upload_404s_a_session_that_does_not_exist(wired):
    r = wired.client.put("/api/sessions/s_1772460399_aaaaaa/video",
                         params={"participant_id": PARTICIPANT},
                         content=webm(64))
    assert r.status_code == 404


@pytest.mark.parametrize("bad", ["..", "S_1772460300_44C9A2", "not-a-session"])
def test_the_upload_refuses_a_crafted_session_id(wired, bad):
    """_session_dir is the traversal check as well as the existence check. The
    uppercase spelling is here because Windows and a default macOS volume
    resolve it to the real directory while every key derived from it names
    something else."""
    r = wired.client.put(f"/api/sessions/{bad}/video",
                         params={"participant_id": PARTICIPANT},
                         content=webm(64))
    assert r.status_code in (400, 404)


# --- PUT /api/sessions/{id}/video: the one-shot guard -------------------------

def test_a_second_upload_is_refused_once_the_bytes_are_there(wired):
    """The guard the presign route closes the bucket with. A second door that
    does not hold it is not a fallback, it is that hole reopened: anyone who
    learns a session id could write over a finished IRB recording."""
    first = recording(wired, webm(256))
    r = wired.client.put(UPLOAD_URL, params={"participant_id": PARTICIPANT},
                         content=webm(999))
    assert r.status_code == 409
    assert (wired.root / SESSION_ID / LOCAL_NAME).read_bytes() == first


def test_a_second_upload_is_refused_on_this_servers_own_receipt(wired):
    """The leg that is easy to leave out.

    video.exists() answers False when it cannot reach S3 — a credential failure
    is not evidence of an empty bucket — so on an S3 outage the byte check alone
    would wave a second write through against a recording that is sitting in the
    bucket. The presign route already refuses on exactly this evidence
    (video.UploadUnconfirmed); this path has to refuse on it too."""
    wired.video.exists_answer = False
    (wired.root / SESSION_ID / "events.jsonl").write_text(
        json.dumps({"t": None, "wall": 1.0, "type": "video_uploaded",
                    "key": video.video_key(SESSION_ID), "bytes": 4096,
                    "status": "ok"}) + "\n", encoding="utf-8")
    r = wired.client.put(UPLOAD_URL, params={"participant_id": PARTICIPANT},
                         content=webm(999))
    assert r.status_code == 409
    assert wired.video.stored == []


def test_a_failed_earlier_upload_does_not_lock_the_encounter_out(wired):
    """The permanently-unrateable defect, from the other side. An encounter
    whose confirm wrote a zero-byte 'failed' event has no receipt and no bytes,
    so the retry has to be allowed through — that is the whole point of putting
    the application back in the byte path."""
    (wired.root / SESSION_ID / "events.jsonl").write_text(
        json.dumps({"t": None, "wall": 1.0, "type": "video_uploaded",
                    "key": video.video_key(SESSION_ID), "bytes": 0,
                    "status": "failed", "error": "AccessDenied"}) + "\n",
        encoding="utf-8")
    r = wired.client.put(UPLOAD_URL, params={"participant_id": PARTICIPANT},
                         content=webm(256))
    assert r.status_code == 200


# --- PUT /api/sessions/{id}/video: storing the bytes --------------------------

def put_in_parts(path: str, parts, participant: str = PARTICIPANT) -> int:
    """PUT `parts` as separate ASGI body messages; returns the status.

    TestClient hands the application one bytes object however the body was
    given to it, so through TestClient a three-chunk upload and one 3 KB buffer
    are indistinguishable — which is the exact difference these tests exist to
    check. Driving the ASGI callable directly is the only way to put a genuinely
    multi-message body through the route, and the only way to send NO
    Content-Length at all, which is what a chunked upload looks like on arrival.
    """
    import asyncio

    messages = [{"type": "http.request", "body": p, "more_body": True}
                for p in parts]
    messages.append({"type": "http.request", "body": b"", "more_body": False})
    sent = []

    async def receive():
        return messages.pop(0) if messages else {"type": "http.disconnect"}

    async def send(message):
        sent.append(message)

    scope = {
        "type": "http", "asgi": {"version": "3.0", "spec_version": "2.3"},
        "http_version": "1.1", "method": "PUT", "scheme": "http",
        "path": path, "raw_path": path.encode(), "root_path": "",
        "query_string": f"participant_id={participant}".encode(),
        "headers": [(b"host", b"testserver"), (b"content-type", b"video/webm")],
        "client": ("127.0.0.1", 5000), "server": ("testserver", 80),
    }
    asyncio.run(appmod.app(scope, receive, send))
    return next(m["status"] for m in sent if m["type"] == "http.response.start")


def test_the_writer_is_handed_a_stream_and_never_a_buffer(wired, monkeypatch):
    """A webcam recording is tens of megabytes, and this process shares its
    memory with every live encounter's audio buffers. Reading the body with
    request.body() would be one line shorter and would hold the whole recording
    in RAM for as long as the write takes."""
    handed = []

    def watching_store(session_id, source):
        handed.append(source)
        return sum(len(c) for c in source)

    monkeypatch.setattr(video, "store_local", watching_store, raising=False)
    wired.client.put(UPLOAD_URL, params={"participant_id": PARTICIPANT},
                     content=webm(1024))
    assert len(handed) == 1
    assert not isinstance(handed[0], (bytes, bytearray))
    # An iterator, not a re-iterable container: nothing on the way collected the
    # chunks into a list it could hand over twice.
    assert iter(handed[0]) is handed[0]


def test_the_body_reaches_the_writer_one_chunk_at_a_time(wired, monkeypatch):
    """The chunks reach the writer as the network delivers them, not after the
    last one has arrived."""
    seen = []

    def watching_store(session_id, source):
        for chunk in source:
            seen.append(len(chunk))
        return sum(seen)

    monkeypatch.setattr(video, "store_local", watching_store, raising=False)
    assert put_in_parts(UPLOAD_URL, [webm(1024), webm(2048), webm(512)]) == 200
    assert seen == [1024, 2048, 512]


# --- PUT /api/sessions/{id}/video: what it refuses ----------------------------

# DEFAULT rather than the constant itself: a parametrize list is evaluated at
# COLLECTION time, so naming appmod._DEFAULT_VIDEO_UPLOAD_BYTES here would make
# every test in this file fail to collect on a tree where that constant is
# missing, instead of failing the one test that is about it.
DEFAULT = object()


@pytest.mark.parametrize("raw,expected", [
    ("1048576", 1048576),
    (None, DEFAULT),
    ("", DEFAULT),
    ("  ", DEFAULT),
    ("512MB", DEFAULT),      # the obvious spelling, and not a byte count
    ("0", DEFAULT),          # a ceiling that would refuse every recording
    ("-1", DEFAULT),
])
def test_an_unusable_ceiling_falls_back_instead_of_breaking_the_import(
        raw, expected):
    """This is read at module level, and verify_record, scoring, retranscribe
    and every pytest process import this module. A stray '512MB' must not stop
    all of them with a ValueError naming nothing anyone would connect to a
    webcam upload — and a ceiling of 0 must not refuse every recording."""
    if expected is DEFAULT:
        expected = appmod._DEFAULT_VIDEO_UPLOAD_BYTES
    assert appmod._upload_ceiling(raw) == expected


def test_an_oversized_declared_body_is_refused_before_a_byte_is_read(
        wired, monkeypatch):
    monkeypatch.setattr(appmod, "MAX_VIDEO_UPLOAD_BYTES", 1024)
    r = wired.client.put(UPLOAD_URL, params={"participant_id": PARTICIPANT},
                         content=webm(4096))
    assert r.status_code == 413
    assert wired.video.stored == []
    assert not (wired.root / SESSION_ID / LOCAL_NAME).exists()


def test_an_oversized_body_that_declares_no_length_is_refused_mid_stream(
        wired, monkeypatch):
    """A chunked upload declares no length at all, so the only number that
    bounds what reaches the disk is the one counted as it lands. Without this
    the fallback is a way for anyone holding a session id to fill the disk out
    from under a conversation that is still being recorded."""
    monkeypatch.setattr(appmod, "MAX_VIDEO_UPLOAD_BYTES", 2048)
    # put_in_parts sends no Content-Length at all, so the up-front check above
    # cannot be what refuses this one — only the count kept while the body lands.
    assert put_in_parts(UPLOAD_URL, [webm(1024)] * 3) == 413
    # And nothing half-written left behind: a truncated file would satisfy
    # video.exists() and lock the encounter out of every retry.
    assert not (wired.root / SESSION_ID / LOCAL_NAME).exists()
    assert events_of(wired) == []


def test_an_empty_body_leaves_nothing_behind_and_no_event(wired):
    """A zero-byte file would answer video.exists() from then on, the one-shot
    guard would refuse every retry, and the encounter would be unrateable with
    nothing in the bucket and nothing on disk — this route's own version of the
    defect it exists to end."""
    r = wired.client.put(UPLOAD_URL, params={"participant_id": PARTICIPANT},
                         content=b"")
    assert r.status_code == 400
    assert not (wired.root / SESSION_ID / LOCAL_NAME).exists()
    assert events_of(wired) == []
    # And the retry is still open.
    again = wired.client.put(UPLOAD_URL, params={"participant_id": PARTICIPANT},
                             content=webm(256))
    assert again.status_code == 200


def test_a_storage_failure_is_503_and_not_500(wired):
    """A full disk or a read-only mount is storage failing, not a bug in this
    process, and a client told 503 retries where a 500 makes it give up."""
    wired.video.store_error = OSError(28, "No space left on device")
    r = wired.client.put(UPLOAD_URL, params={"participant_id": PARTICIPANT},
                         content=webm(256))
    assert r.status_code == 503
    assert events_of(wired) == []


def test_no_receipt_is_written_for_an_upload_that_did_not_land(wired):
    """An event with no bytes behind it is the false receipt that makes an
    encounter look recorded when nothing was stored."""
    wired.video.store_error = OSError("nope")
    wired.client.put(UPLOAD_URL, params={"participant_id": PARTICIPANT},
                     content=webm(256))
    assert video.upload_receipt(SESSION_ID) is None


# --- the fallback has to be reachable from the page that needs it -------------

def test_the_participant_page_may_actually_send_this_put(wired):
    """The participant page is served from APP_HOST and calls the API on
    API_HOST, so every request it makes is cross-origin. A method missing from
    the CORS allowlist is refused at the preflight, and the fallback would be
    unreachable from its only client while every server-side test above
    passed."""
    r = wired.client.options(UPLOAD_URL, headers={
        "Origin": appmod.ALLOWED_ORIGINS[0],
        "Access-Control-Request-Method": "PUT",
    })
    assert r.status_code == 200
    assert "PUT" in r.headers["access-control-allow-methods"]


# --- the two halves, meeting --------------------------------------------------

def test_the_real_modules_one_shot_guard_holds_on_the_fallback(live_wired):
    """video.exists() over a real stored file, refusing a real second write."""
    first = webm(256)
    assert live_wired.client.put(UPLOAD_URL,
                                 params={"participant_id": PARTICIPANT},
                                 content=first).status_code == 200
    second = live_wired.client.put(UPLOAD_URL,
                                   params={"participant_id": PARTICIPANT},
                                   content=webm(999))
    assert second.status_code == 409
    assert video.local_path(SESSION_ID).read_bytes() == first


def put_events(root, *events, session_id: str = SESSION_ID):
    """The confirm endpoint's trail, in the shape it actually writes."""
    sdir = root / session_id
    sdir.mkdir(parents=True, exist_ok=True)
    with (sdir / "events.jsonl").open("w", encoding="utf-8") as fh:
        fh.write(json.dumps({"t": 0.0, "type": "session_start"}) + "\n")
        for ev in events:
            fh.write(json.dumps(ev) + "\n")
    return sdir


# The confirmation a browser posts when the PUT landed, and the one it posts
# when the PUT died. Both are claims; neither is bytes.
RECEIPT_OK = {"t": None, "wall": 1772460800.0, "type": "video_uploaded",
              "key": f"encounters/{SESSION_ID}/webcam.webm",
              "bytes": 8_400_000, "status": "ok"}
RECEIPT_FAILED = {"t": None, "wall": 1772460800.0, "type": "video_uploaded",
                  "key": f"encounters/{SESSION_ID}/webcam.webm",
                  "bytes": 0, "status": "failed", "error": "put 403"}

