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

from server import rater_packet as rp
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


def test_local_path_touches_nothing(tmp_path, monkeypatch):
    """No stat, no mkdir. It is called while DECIDING whether to touch disk, and
    a mkdir here would conjure a session directory for any id a rater typed."""
    root = tmp_path / "never-created"
    monkeypatch.setattr(video, "SESSIONS_DIR", root)
    video.local_path(SESSION_ID)
    assert not root.exists()


@pytest.mark.parametrize("bad", [
    "../../../../etc/passwd",
    "s_1772460300_44c9a2/../../secrets",
    "S_1772460300_44C9A2",          # uppercase: a different S3 key, same Windows dir
    "s_1772460300_44c9a2 ",         # Windows strips the trailing space, S3 keeps it
    "s_1772460300_44c9a2.",
    "..",
    "",
    None,
])
def test_local_path_refuses_anything_not_a_minted_session_id(sessions_root, bad):
    """Phase 2 puts a rater-supplied id in front of this module. A crafted id
    that reached the join would address a file outside the session directory."""
    with pytest.raises(ValueError):
        video.local_path(bad)


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


def test_exists_is_false_for_a_zero_byte_local_file(sessions_root, s3):
    """A crashed write leaves exactly this. Calling it playable puts a packet in
    front of a rater with nothing in it."""
    put_local(sessions_root, b"")
    assert video.exists(SESSION_ID) is False


@pytest.mark.parametrize("failure", AWS_FAILURES, ids=lambda e: type(e).__name__ + getattr(e, "response", {}).get("Error", {}).get("Code", ""))
def test_exists_never_raises_whatever_aws_does(sessions_root, s3, failure):
    """This runs once per packet while a rater's assignment list is built. An
    exception here is not one bad packet, it is every packet: the console fails
    to open and Phase 2 stops. Absent credentials mean False."""
    s3.head_error = failure
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


def test_a_failed_store_leaves_neither_a_partial_nor_a_temp(sessions_root, monkeypatch):
    """A half-written WebM plays as a few seconds of a conversation, which is
    worse than no video: the rater scores it without knowing it is truncated."""
    monkeypatch.setattr(video, "replace_with_retry",
                        lambda *a, **k: (_ for _ in ()).throw(PermissionError("held")))
    with pytest.raises(PermissionError):
        video.store_local(SESSION_ID, io.BytesIO(webm()))
    assert not video.local_path(SESSION_ID).exists()
    assert list((sessions_root / SESSION_ID).glob("*.tmp")) == []


def test_store_local_replaces_atomically_rather_than_truncating_in_place(
        sessions_root, monkeypatch):
    """os.replace via storage.replace_with_retry, the idiom every other writer
    here uses, is what stops a concurrent open_stream reading a half file."""
    seen = {}
    real = video.replace_with_retry

    def spy(tmp, dest, *a, **k):
        seen["tmp"], seen["dest"] = tmp, dest
        return real(tmp, dest, *a, **k)

    monkeypatch.setattr(video, "replace_with_retry", spy)
    video.store_local(SESSION_ID, io.BytesIO(webm()))
    assert seen["dest"] == video.local_path(SESSION_ID)
    assert seen["tmp"] != seen["dest"]


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

def test_open_stream_is_none_when_there_is_nothing_anywhere(sessions_root, s3):
    assert video.open_stream(SESSION_ID) is None


def test_open_stream_is_none_for_a_bad_session_id(sessions_root, s3):
    assert video.open_stream("../../etc/passwd") is None


@pytest.mark.parametrize("failure", AWS_FAILURES, ids=lambda e: type(e).__name__)
def test_open_stream_is_none_not_an_exception_when_s3_will_not_answer(
        sessions_root, s3, failure):
    """A botocore exception out of here is a 500 the rater cannot act on."""
    s3.head_error = failure
    assert video.open_stream(SESSION_ID) is None


def test_open_stream_is_none_when_the_object_vanishes_between_head_and_get(
        sessions_root, s3):
    s3.data = webm()
    s3.get_error = aws_error("NoSuchKey", op="GetObject", status=404)
    assert video.open_stream(SESSION_ID) is None


# --- open_stream: local wins --------------------------------------------------

def test_a_local_file_is_served_without_touching_s3(sessions_root, s3):
    """The measure of success for this whole round: no credentials, no bucket,
    and the recording still plays."""
    data = webm(20_000)
    put_local(sessions_root, data)
    s3.head_error = NoCredentialsError()
    stream = video.open_stream(SESSION_ID)
    assert stream is not None
    assert drain(stream) == data
    assert s3.head_calls == 0 and s3.ranges == []


def test_local_wins_even_when_s3_also_holds_an_object(sessions_root, s3):
    """A developer drops a file in by hand to reproduce a rater's report; the
    hand-placed file is the one that must play."""
    put_local(sessions_root, webm(1000))
    s3.data = mp4(4000)
    stream = video.open_stream(SESSION_ID)
    assert stream.total == 1000
    assert s3.head_calls == 0


def test_open_stream_never_holds_the_whole_local_file(sessions_root):
    data = webm(video.CHUNK_SIZE * 2 + 500)
    put_local(sessions_root, data)
    stream = video.open_stream(SESSION_ID)
    sizes = [len(c) for c in stream.chunks]
    assert len(sizes) >= 3, "one chunk means the whole file was buffered"
    assert max(sizes) <= video.CHUNK_SIZE
    assert sum(sizes) == len(data)


def test_the_local_file_is_not_opened_until_the_body_is_consumed(sessions_root):
    """An abandoned VideoStream must not hold a handle: on Windows an open
    handle on either side is what makes replace_with_retry spin."""
    path = put_local(sessions_root, webm())
    stream = video.open_stream(SESSION_ID)
    assert stream is not None
    # Nothing consumed the body, so nothing holds the file: on Windows an open
    # handle would make this rename fail outright.
    path.rename(path.with_suffix(".moved"))


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


@pytest.mark.parametrize("start,end", RANGES)
def test_local_and_s3_answer_a_range_identically(sessions_root, s3, start, end):
    """The route cannot tell the two branches apart, so they must not differ:
    same offsets, same length, same bytes, for the same request."""
    data = webm(4096)

    s3.data = data
    from_s3 = video.open_stream(SESSION_ID, start=start, end=end)
    s3_fields = (from_s3.start, from_s3.end, from_s3.length, from_s3.total)
    s3_bytes = drain(from_s3)

    put_local(sessions_root, data)
    from_local = video.open_stream(SESSION_ID, start=start, end=end)
    local_fields = (from_local.start, from_local.end,
                    from_local.length, from_local.total)

    assert local_fields == s3_fields
    assert drain(from_local) == s3_bytes


@pytest.mark.parametrize("start,end", RANGES)
def test_a_range_returns_exactly_those_bytes(sessions_root, start, end):
    data = webm(4096)
    put_local(sessions_root, data)
    stream = video.open_stream(SESSION_ID, start=start, end=end)
    assert stream.total == len(data)
    assert stream.length == stream.end - stream.start + 1
    assert stream.length == len(drain(stream))
    assert drain(video.open_stream(SESSION_ID, start=start, end=end)) == \
        data[stream.start:stream.end + 1]


def test_the_whole_object_form_covers_the_whole_object(sessions_root):
    data = webm(4096)
    put_local(sessions_root, data)
    s = video.open_stream(SESSION_ID)
    assert (s.start, s.end, s.length, s.total) == (0, 4095, 4096, 4096)


def test_the_suffix_form_is_the_last_n_bytes(sessions_root):
    """RFC 7233's `bytes=-N`. Getting this backwards serves the FIRST N bytes,
    and the player renders the opening of the encounter when the rater scrubbed
    to the end."""
    data = webm(4096)
    put_local(sessions_root, data)
    s = video.open_stream(SESSION_ID, start=None, end=256)
    assert (s.start, s.end, s.length) == (3840, 4095, 256)
    assert drain(s) == data[-256:]


def test_a_suffix_longer_than_the_object_is_the_whole_object(sessions_root):
    data = webm(1000)
    put_local(sessions_root, data)
    s = video.open_stream(SESSION_ID, start=None, end=99_999)
    assert (s.start, s.end, s.length) == (0, 999, 1000)


def test_an_end_past_the_object_is_clamped_not_refused(sessions_root):
    """Players routinely ask for a window past the end of a stream. Refusing it
    makes the recording unseekable for no reason."""
    data = webm(1000)
    put_local(sessions_root, data)
    s = video.open_stream(SESSION_ID, start=500, end=10 ** 9)
    assert (s.start, s.end, s.length) == (500, 999, 500)


@pytest.mark.parametrize("start,end", [
    (4096, None),        # exactly at the end
    (99_999, None),      # far past it
    (99_999, 100_100),
    (500, 499),          # inverted
    (None, 0),           # a zero-length suffix
])
def test_an_unsatisfiable_range_raises_with_the_true_total(sessions_root, start, end):
    """416 is owed a `Content-Range: bytes */TOTAL`, so the total has to ride on
    the exception. And it must be an exception, not None: None means 'no such
    recording', and a rater told that about an encounter that HAS one gets a
    wrong explanation for a wrong reason."""
    put_local(sessions_root, webm(4096))
    with pytest.raises(video.RangeNotSatisfiable) as e:
        video.open_stream(SESSION_ID, start=start, end=end)
    assert e.value.total == 4096


@pytest.mark.parametrize("start,end", [(4096, None), (99_999, None), (None, 0)])
def test_an_unsatisfiable_range_raises_on_the_s3_branch_too(
        sessions_root, s3, start, end):
    s3.data = webm(4096)
    with pytest.raises(video.RangeNotSatisfiable) as e:
        video.open_stream(SESSION_ID, start=start, end=end)
    assert e.value.total == 4096


def test_no_such_recording_is_none_while_a_bad_range_raises(sessions_root, s3):
    """The two answers HTTP owes different statuses, told apart in one test."""
    assert video.open_stream(SESSION_ID, start=99_999) is None
    put_local(sessions_root, webm(10))
    with pytest.raises(video.RangeNotSatisfiable):
        video.open_stream(SESSION_ID, start=99_999)


# --- open_stream: what actually goes to S3 ------------------------------------

def test_a_whole_object_read_sends_no_range_header(sessions_root, s3):
    s3.data = webm(4096)
    s3.content_type = "video/webm"
    stream = video.open_stream(SESSION_ID)
    assert drain(stream) == s3.data
    assert s3.ranges == [None], "a whole-object read should be a plain GET"


def test_a_ranged_read_sends_the_inclusive_range_s3_expects(sessions_root, s3):
    s3.data = webm(4096)
    s3.content_type = "video/webm"
    video.open_stream(SESSION_ID, start=100, end=199)
    assert s3.ranges[0] == "bytes=100-199"


def test_an_open_ended_range_is_resolved_before_it_reaches_s3(sessions_root, s3):
    s3.data = webm(4096)
    s3.content_type = "video/webm"
    video.open_stream(SESSION_ID, start=4000)
    assert s3.ranges[0] == "bytes=4000-4095"


def test_open_stream_never_holds_the_whole_object(sessions_root, s3):
    s3.data = webm(video.CHUNK_SIZE * 2 + 500)
    s3.content_type = "video/webm"
    stream = video.open_stream(SESSION_ID)
    sizes = [len(c) for c in stream.chunks]
    assert len(sizes) >= 3
    assert max(sizes) <= video.CHUNK_SIZE
    body = s3.bodies[0]
    assert max(n for n in body.reads if n is not None) <= video.CHUNK_SIZE


def test_the_s3_body_is_closed_when_the_stream_is_drained(sessions_root, s3):
    s3.data = webm(2000)
    s3.content_type = "video/webm"
    drain(video.open_stream(SESSION_ID))
    assert s3.bodies[0].closed is True


def test_the_s3_body_is_closed_when_a_rater_scrubs_away_mid_stream(sessions_root, s3):
    """Abandoning the body is what scrubbing IS. A connection never returned to
    botocore's pool exhausts it, after which every S3 call in the process
    blocks — not just video."""
    s3.data = webm(video.CHUNK_SIZE * 3)
    s3.content_type = "video/webm"
    stream = video.open_stream(SESSION_ID)
    gen = stream.chunks
    next(gen)
    gen.close()
    assert s3.bodies[0].closed is True


# --- content type: the black rectangle ---------------------------------------

def test_a_local_webm_is_served_as_webm(sessions_root):
    put_local(sessions_root, webm())
    assert video.open_stream(SESSION_ID).content_type == "video/webm"


def test_a_local_mp4_is_not_served_as_webm(sessions_root):
    """Safari's MediaRecorder produces fragmented MP4 under the same
    webcam.webm name. Served as video/webm the element never fires a frame and
    never fires an error: the rater sees a black rectangle and reports nothing."""
    put_local(sessions_root, mp4())
    assert video.open_stream(SESSION_ID).content_type == "video/mp4"


def test_an_unrecognised_local_container_falls_back_rather_than_failing(sessions_root):
    put_local(sessions_root, b"not a video at all" * 50)
    assert video.open_stream(SESSION_ID).content_type == video.DEFAULT_CONTENT_TYPE


def test_the_objects_own_content_type_is_honoured(sessions_root, s3):
    s3.data = mp4()
    s3.content_type = "video/mp4"
    stream = video.open_stream(SESSION_ID)
    assert stream.content_type == "video/mp4"
    assert s3.ranges == [None], "a declared type needs no sniff round trip"


@pytest.mark.parametrize("declared", [None, "", "binary/octet-stream",
                                      "application/octet-stream"])
def test_a_generic_stored_type_is_sniffed_from_the_bytes(sessions_root, s3, declared):
    """The presigned PUT leaves Content-Type unsigned so both containers land
    under one key; a browser that declared nothing leaves binary/octet-stream on
    the object, which no <video> element will play."""
    s3.data = mp4()
    s3.content_type = declared
    stream = video.open_stream(SESSION_ID)
    assert stream.content_type == "video/mp4"
    assert "bytes=0-11" in s3.ranges, "the sniff must read the object's head"


def test_a_failed_sniff_still_serves_the_recording(sessions_root, s3):
    """The object is there. Being unable to name its container is not a reason
    to refuse to play it."""
    s3.data = webm()
    s3.content_type = "binary/octet-stream"

    real_get = s3.get_object

    def get(Bucket=None, Key=None, Range=None):
        if Range == "bytes=0-11":
            raise aws_error("AccessDenied", op="GetObject")
        return real_get(Bucket=Bucket, Key=Key, Range=Range)

    s3.get_object = get
    stream = video.open_stream(SESSION_ID)
    assert stream is not None
    assert stream.content_type == video.DEFAULT_CONTENT_TYPE
    assert drain(stream) == s3.data


def test_a_charset_parameter_on_the_stored_type_is_stripped(sessions_root, s3):
    s3.data = webm()
    s3.content_type = "video/webm; codecs=vp8,opus"
    assert video.open_stream(SESSION_ID).content_type == "video/webm"


# --- the shape of the thing the route is handed ------------------------------

def test_videostream_carries_exactly_what_content_range_needs(sessions_root):
    """start and end are INCLUSIVE offsets, so the route writes
    `bytes {start}-{end}/{total}` straight out of the fields."""
    put_local(sessions_root, webm(4096))
    s = video.open_stream(SESSION_ID, start=10, end=19)
    assert (s.start, s.end, s.total, s.length) == (10, 19, 4096, 10)
    assert f"bytes {s.start}-{s.end}/{s.total}" == "bytes 10-19/4096"


def test_the_chunk_size_is_sane_for_video_over_http():
    """Small enough that a dozen concurrent raters cost megabytes not gigabytes,
    large enough that a megabyte is a handful of sends rather than hundreds."""
    assert 64 * 1024 <= video.CHUNK_SIZE <= 1024 * 1024


def test_one_encounters_recording_is_not_another_encounters(sessions_root, s3):
    """Blinding depends on a packet showing its own encounter and no other."""
    put_local(sessions_root, webm(1000), session_id=SESSION_ID)
    (sessions_root / OTHER_ID).mkdir(parents=True, exist_ok=True)
    put_local(sessions_root, mp4(2000), session_id=OTHER_ID)
    assert video.open_stream(SESSION_ID).total == 1000
    assert video.open_stream(OTHER_ID).total == 2000


# --- the sniff, and how often it costs a round trip ---------------------------
#
# The stored Content-Type is the browser's unverified word on an unsigned PUT,
# so on the S3 branch the container is read out of the object's first twelve
# bytes. That read is a THIRD round trip on top of the HEAD and the body GET,
# and it used to happen on every seek: twenty seeks measured sixty round trips,
# and a rater scrubbing a seven-minute encounter makes far more than twenty.
# What the tests below defend is that making it cheap did not make it wrong.

def test_the_container_is_sniffed_once_however_many_times_a_rater_seeks(
        sessions_root, s3):
    """The seek cost. Twenty seeks are twenty HEADs and twenty body GETs and ONE
    sniff — not sixty round trips — and every one of them still gets the right
    container, because a cached answer that stopped being served would trade the
    round trip for the black rectangle it was bought to prevent."""
    s3.data = mp4(40_000)
    s3.content_type = "binary/octet-stream"
    for i in range(20):
        stream = video.open_stream(SESSION_ID, start=i * 100, end=i * 100 + 99)
        assert stream.content_type == "video/mp4"
        drain(stream)
    assert s3.ranges.count("bytes=0-11") == 1, s3.ranges
    assert len(s3.ranges) == 21, "20 body GETs and one sniff"


def test_a_re_recorded_encounter_is_sniffed_again(sessions_root, s3):
    """What invalidates an entry. The object's byte count is part of the key, so
    an encounter whose recording is replaced is read afresh; a cache that keyed
    on the object name alone would serve the new recording as the old one's
    container for the life of the process."""
    s3.content_type = ""
    s3.data = webm(4096)
    assert video.open_stream(SESSION_ID).content_type == "video/webm"
    s3.data = mp4(8192)
    assert video.open_stream(SESSION_ID).content_type == "video/mp4"


def test_a_sniff_that_failed_is_not_remembered_as_the_answer(sessions_root, s3):
    """A throttled twelve-byte GET is a fact about that instant, not about the
    object. Cached, it would pin video/webm on a Safari MP4 for every rater who
    opened that packet afterwards — a black rectangle that fires no error and so
    gets reported as nothing at all."""
    s3.data = mp4(4096)
    s3.content_type = ""
    real_get = s3.get_object
    throttled = {"on": True}

    def get(Bucket=None, Key=None, Range=None):
        if Range == "bytes=0-11" and throttled["on"]:
            raise aws_error("SlowDown", op="GetObject", status=503)
        return real_get(Bucket=Bucket, Key=Key, Range=Range)

    s3.get_object = get
    assert video.open_stream(SESSION_ID).content_type == video.DEFAULT_CONTENT_TYPE
    throttled["on"] = False
    assert video.open_stream(SESSION_ID).content_type == "video/mp4"


def test_the_sniff_cache_cannot_hand_one_encounter_another_ones_container(
        sessions_root, s3):
    """Two encounters, the same byte count, different containers. A key that did
    not name the object would answer the second rater with the first rater's
    container — and these two recordings are the two a MediaRecorder actually
    produces, so this is the collision that would happen, not a contrived one."""
    objects = {video.video_key(SESSION_ID): webm(4096),
               video.video_key(OTHER_ID): mp4(4096)}

    def head_object(Bucket=None, Key=None):
        return {"ContentLength": len(objects[Key])}

    def get_object(Bucket=None, Key=None, Range=None):
        s3.ranges.append(Range)
        data = objects[Key]
        if Range:
            lo, hi = (int(x) for x in Range.split("=")[1].split("-"))
            data = data[lo:hi + 1]
        return {"Body": FakeBody(data), "ContentType": ""}

    s3.head_object = head_object
    s3.get_object = get_object
    assert video.open_stream(SESSION_ID).content_type == "video/webm"
    assert video.open_stream(OTHER_ID).content_type == "video/mp4"


def test_the_sniff_cache_does_not_grow_with_every_encounter_ever_served():
    """A long-lived Fargate task serves every encounter in the study. An
    unbounded memo would hold one entry per object it has ever streamed, which
    is a slow leak in a process that is meant to stay up for a wave."""
    class Stub:
        def get_object(self, Bucket=None, Key=None, Range=None):
            return {"Body": FakeBody(WEBM_MAGIC + b"\0" * 8)}

    stub = Stub()
    for n in range(video._SNIFF_CACHE_MAX * 2):
        assert video._s3_content_type(
            stub, f"encounters/s_{n}/webcam.webm", "", 4096) == "video/webm"
    assert len(video._sniff_cache) <= video._SNIFF_CACHE_MAX


# --- the presigned playback URL, and why there is no longer one ---------------

def test_nothing_in_this_module_can_mint_a_playback_url():
    """playback_url signed a GET for the recording and handed the string to the
    rating console. That string is a bearer credential for an IRB video: it
    works for anyone who holds it, it lands in the rater's browser history and
    in every proxy log on the way, and its object key names the encounter's
    session id — which the rater is meant to be blind to. Serving the bytes
    through /api/rater/video closed all of that, and it had no caller left.

    Pinned as ABSENCE rather than as "no caller does this", because dead code
    that still works is what the next packet builder reaches for: the way this
    defect returns is somebody finding a helper that hands them a URL.
    """
    assert not hasattr(video, "playback_url")
    assert not hasattr(video, "MAX_PLAYBACK_SECONDS")
    src = Path(video.__file__).read_text(encoding="utf-8")
    minted = [ln.strip() for ln in src.splitlines()
              if "generate_presigned_url(" in ln]
    assert len(minted) == 1, minted
    # And the one survivor is the participant's UPLOAD, which is a different
    # thing entirely: it is issued to the browser that recorded the encounter,
    # for a PUT, once, and it is what puts the bytes in the bucket in the first
    # place. A signed get_object anywhere in this module is the defect back.
    presign = inspect.getsource(video.presign_upload)
    assert "generate_presigned_url(" in presign
    assert "put_object" in presign and "get_object" not in presign


def test_a_rater_mid_sitting_cannot_lose_playback_to_an_expiry(sessions_root, s3):
    """What the old two-strikes re-mint retry was defending.

    The presigned GET expired, so a rater who left a packet open over lunch came
    back to a video element that 403ed, and the console grew a countdown, a
    refresh call, a rate limit and a retry button to paper over it. The stream
    below carries no deadline at all — the thing that could expire is gone — so
    the guarantee that machinery existed for is now structural: open a stream,
    let an arbitrary amount of time pass, open another, and the second is as
    playable as the first with nothing re-fetched from anywhere.
    """
    put_local(sessions_root, webm(4096))
    first = video.open_stream(SESSION_ID)
    assert drain(first) == video.local_path(SESSION_ID).read_bytes()
    # There is no field on the thing the route is handed that could count down.
    assert not any("expire" in f or "deadline" in f
                   for f in vars(first)), vars(first)
    later = video.open_stream(SESSION_ID)
    assert drain(later) == drain(video.open_stream(SESSION_ID))
    assert s3.head_calls == 0, "and it did not need AWS to still be reachable"


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


def test_a_credential_less_process_walks_the_chain_once_not_once_per_packet(
        sessions_root, creds):
    """The measurement this exists for: 26 packets, one walk.

    exists() runs once per packet while a rater's assignment list is built, and
    resolving credentials is what a client construction DOES — 2.297 s of it on
    a machine that is not an EC2 instance, spent connecting twice to a metadata
    service that is not there. Whether this process can obtain credentials at
    all is not a per-encounter fact, so a queue that rediscovered it per
    encounter would be 26 identical answers and a minute of a researcher
    watching a console that looks broken.
    """
    ids = [f"s_17724603{n:02d}_44c9a2" for n in range(26)]
    assert [video.exists(sid) for sid in ids] == [False] * 26
    assert len(creds.builds) == 1, "one credential-chain walk for the whole queue"
    assert creds.builds[0].head_calls == 0, \
        "and no HEAD, because nothing could have signed one"


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


def test_the_recheck_window_is_short_enough_that_nobody_debugs_it():
    """A minute is the trade: long enough that the 2.30 s walk is paid once a
    minute at worst rather than once a packet, short enough that a role attached
    after boot starts working before anyone starts reading logs about it."""
    assert 10 <= video.CREDENTIAL_RECHECK_SECONDS <= 300


def test_the_shortcut_is_indistinguishable_from_the_exception_it_skips(
        sessions_root, creds, s3):
    """Not skipping the call has to mean exactly what making it meant.

    head_video's two-field answer is read by the confirm endpoint, presign's
    one-shot guard, the packet builder and open_stream, and each of them cares
    about the difference between "S3 says no object" and "S3 would not say". A
    shortcut that answered even slightly differently would put a fifth reading
    into a contract that has four.
    """
    s3.head_error = NoCredentialsError()
    raised = video.head_video(SESSION_ID)

    unsigned = SignedS3(credentials=None)
    video._s3 = unsigned                  # restored by the creds fixture
    video._no_creds_client = None
    video._no_creds_until = 0.0
    short_circuited = video.head_video(SESSION_ID)

    assert short_circuited == raised == {"bytes": None,
                                         "error": "NoCredentialsError"}
    assert unsigned.head_calls == 0


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


def test_the_credential_warning_is_once_per_window_not_once_per_packet(
        sessions_root, creds, capsys):
    """head_video printed its warning on every miss, so opening a 26-packet
    queue on a laptop wrote 26 identical lines and buried whatever else the
    console had to say. The line still has to exist — an operator who cannot see
    why the bucket is being skipped has a mystery, not a diagnosis."""
    for n in range(26):
        video.exists(f"s_17724603{n:02d}_44c9a2")
    out = capsys.readouterr().out
    assert out.count("no AWS credentials") == 1
    assert video.BUCKET in out and video.REGION in out


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

def test_the_playback_route_refuses_a_missing_token(wired):
    assert wired.client.get(VIDEO_URL).status_code == 401


@pytest.mark.parametrize("token", ["rt_" + "0" * 32, "", "not-a-token",
                                   "test-session-key"])
def test_the_playback_route_refuses_a_token_that_is_not_a_raters(wired, token):
    """Including the researcher credential. Independent ratings need one
    identifiable author each, so SESSION_KEY must not open a rater's console."""
    assert wired.client.get(VIDEO_URL,
                            params={"token": token}).status_code == 401


def test_another_raters_recording_is_404_and_not_403(wired):
    """The blinding rule the packet route already keeps. A rater who can tell
    'not yours' from 'not there' can count the wave one id at a time, and the
    recording is the most identifying artefact in it."""
    recording(wired, webm(512), session_id=OTHER_ID)
    theirs = wired.client.get(f"/api/rater/video/{THEIRS}",
                              params={"token": TOKEN_A})
    nowhere = wired.client.get("/api/rater/video/as_zzzzzzzzzzzz",
                               params={"token": TOKEN_A})
    assert theirs.status_code == 404
    assert nowhere.status_code == 404
    # Indistinguishable in the body as well as in the status line.
    assert theirs.json() == nowhere.json()


def test_another_raters_recording_is_never_opened(wired):
    """404 is not enough on its own: the bytes must not be read either."""
    recording(wired, webm(512), session_id=OTHER_ID)
    wired.client.get(f"/api/rater/video/{THEIRS}", params={"token": TOKEN_A})
    assert wired.video.opened == []


# --- GET /api/rater/video: the whole object ----------------------------------

def test_a_rater_gets_the_recording_with_no_aws_credentials_anywhere(wired):
    """The measure of success for this whole round.

    video._s3 raises NoCredentialsError at every call in this fixture, and the
    recording still arrives byte for byte through the application. Before this
    route existed the console played from a presigned S3 URL and there was no
    answer at all to give a machine with no credentials — which is why nobody,
    not the researcher and not CI, had ever seen a webcam recording in the
    rating console."""
    data = recording(wired, webm(4096))
    r = wired.client.get(VIDEO_URL, params={"token": TOKEN_A})
    assert r.status_code == 200
    assert r.content == data
    assert r.headers["content-length"] == str(len(data))
    assert r.headers["content-type"].startswith("video/webm")
    # No Content-Range on a whole-object answer: a 200 carrying one tells a
    # player it got a partial response and it will go looking for the rest.
    assert "content-range" not in r.headers


def test_accept_ranges_is_on_the_whole_object_answer(wired):
    """Without it a browser offers no scrubber at all, and a rater who cannot
    seek has to watch ten minutes in real time to check one moment."""
    recording(wired, webm(4096))
    r = wired.client.get(VIDEO_URL, params={"token": TOKEN_A})
    assert r.headers["accept-ranges"] == "bytes"


def test_the_recording_is_kept_out_of_caches(wired):
    """An IRB webcam recording, addressed by a URL carrying a live rater token."""
    recording(wired, webm(256))
    r = wired.client.get(VIDEO_URL, params={"token": TOKEN_A})
    assert "no-store" in r.headers["cache-control"]
    assert "private" in r.headers["cache-control"]


def test_the_body_arrives_in_chunks_rather_than_one_read(wired):
    """Nothing may hold a whole recording in memory: a dozen raters opening
    packets at once would otherwise cost this process a dozen recordings."""
    recording(wired, webm(4096))
    wired.client.get(VIDEO_URL, params={"token": TOKEN_A})
    assert len(wired.video.pulled) == 4096 // 8
    assert max(wired.video.pulled) == 8


def test_the_content_type_is_the_streams_own(wired):
    """Safari's MediaRecorder produces MP4 where everyone else produces WebM,
    and a player handed the wrong container shows a black rectangle."""
    recording(wired, mp4(512))
    wired.video.content_type = "video/mp4"
    r = wired.client.get(VIDEO_URL, params={"token": TOKEN_A})
    assert r.headers["content-type"].startswith("video/mp4")


def test_an_encounter_with_no_recording_is_404(wired):
    assert wired.client.get(VIDEO_URL,
                            params={"token": TOKEN_A}).status_code == 404


def test_an_assignment_row_with_an_unusable_session_id_is_404_not_500(wired):
    """Rows written by hand, or predating the minted shape, exist. One of them
    must not turn a rater's console into a stack trace."""
    r = wired.client.get(f"/api/rater/video/{BAD_ROW}", params={"token": TOKEN_A})
    assert r.status_code == 404


# --- GET /api/rater/video: the ranges ----------------------------------------

def test_an_explicit_range_is_206_with_a_content_range(wired):
    data = recording(wired, webm(4096))
    r = wired.client.get(VIDEO_URL, params={"token": TOKEN_A},
                         headers={"Range": "bytes=100-199"})
    assert r.status_code == 206
    assert r.content == data[100:200]
    assert r.headers["content-range"] == "bytes 100-199/4096"
    assert r.headers["content-length"] == "100"
    assert r.headers["accept-ranges"] == "bytes"


def test_an_open_ended_range_runs_to_the_end(wired):
    """The resume after a stalled read: 'everything from here on'."""
    data = recording(wired, webm(4096))
    r = wired.client.get(VIDEO_URL, params={"token": TOKEN_A},
                         headers={"Range": "bytes=4000-"})
    assert r.status_code == 206
    assert r.content == data[4000:]
    assert r.headers["content-range"] == "bytes 4000-4095/4096"


def test_a_suffix_range_is_the_last_n_bytes(wired):
    """The form that makes a recording scrubbable.

    MediaRecorder writes WebM with no duration in the header, so a browser reads
    the container's trailer to find one — and it asks for it as 'the last N
    bytes', because it does not yet know how long the file is. Answer that with
    the FIRST n bytes, or with the whole object, and the player never learns the
    duration: the recording plays and the timeline stays empty."""
    data = recording(wired, webm(4096))
    r = wired.client.get(VIDEO_URL, params={"token": TOKEN_A},
                         headers={"Range": "bytes=-64"})
    assert r.status_code == 206
    assert r.content == data[-64:]
    assert r.headers["content-range"] == "bytes 4032-4095/4096"


def test_a_suffix_longer_than_the_recording_is_the_whole_recording(wired):
    data = recording(wired, webm(512))
    r = wired.client.get(VIDEO_URL, params={"token": TOKEN_A},
                         headers={"Range": "bytes=-99999"})
    assert r.status_code == 206
    assert r.content == data
    assert r.headers["content-range"] == "bytes 0-511/512"


def test_content_range_describes_the_bytes_sent_not_the_bytes_asked_for(wired):
    """A range whose end runs past the object is clamped, not refused, and the
    header has to follow the clamp: a Content-Range naming bytes the body does
    not contain leaves the player's buffer permanently out of step with the
    file."""
    data = recording(wired, webm(512))
    r = wired.client.get(VIDEO_URL, params={"token": TOKEN_A},
                         headers={"Range": "bytes=500-99999"})
    assert r.status_code == 206
    assert r.headers["content-range"] == "bytes 500-511/512"
    assert r.headers["content-length"] == "12"
    assert r.content == data[500:]


@pytest.mark.parametrize("header", ["bytes=4096-", "bytes=99999-100000",
                                    "bytes=4096-5000"])
def test_an_unsatisfiable_range_is_416_carrying_the_true_total(wired, header):
    """416 and 404 are different facts and a player acts on them differently.
    'You asked past the end of a recording that exists' is recoverable — the
    total in Content-Range is how it learns what to ask for instead — and 'there
    is no recording' is not. Collapse them and a rater either watches a spinner
    forever or is told a recording that exists is missing."""
    recording(wired, webm(4096))
    r = wired.client.get(VIDEO_URL, params={"token": TOKEN_A},
                         headers={"Range": header})
    assert r.status_code == 416
    assert r.headers["content-range"] == "bytes */4096"
    assert r.headers["accept-ranges"] == "bytes"
    assert r.headers["content-length"] == "0"


def test_a_range_against_no_recording_at_all_is_404_not_416(wired):
    """The other half of the same distinction, from the other side."""
    r = wired.client.get(VIDEO_URL, params={"token": TOKEN_A},
                         headers={"Range": "bytes=0-99"})
    assert r.status_code == 404


@pytest.mark.parametrize("header", [
    "bytes=-",             # names nothing
    "bytes=abc-def",       # not numbers
    "bytes=200-100",       # end before start: invalid, per RFC 9110
    "bytes=0-10,20-30",    # a multi-range set this server does not answer
    "items=0-10",          # a range unit we do not understand
    "bytes 0-10",          # no '='
    "-100",
    "",
])
def test_a_malformed_range_is_ignored_and_the_whole_recording_is_served(
        wired, header):
    """RFC 9110 requires an unparseable Range to be ignored, and players depend
    on it. Refusing one takes the recording away from the rater entirely over a
    header they never composed; ignoring it costs at most a seek."""
    data = recording(wired, webm(1024))
    r = wired.client.get(VIDEO_URL, params={"token": TOKEN_A},
                         headers={"Range": header})
    assert r.status_code == 200
    assert r.content == data
    assert "content-range" not in r.headers
    assert r.headers["accept-ranges"] == "bytes"


@pytest.mark.parametrize("header,expected", [
    ("bytes=0-99", (0, 99)),
    ("bytes=100-", (100, None)),
    ("bytes=-64", (None, 64)),
    ("BYTES=0-1", (0, 1)),
    (" bytes = 5 - 9 ", (5, 9)),
    ("bytes=0-0", (0, 0)),
    ("bytes=-0", None),          # 'the last zero bytes' is not a request
    ("bytes=-", None),
    ("bytes=9-5", None),
    ("bytes=0-1,4-5", None),
    ("items=0-1", None),
    (None, None),
    ("", None),
])
def test_the_range_parser_reads_every_form_a_player_sends(header, expected):
    assert appmod._parse_range(header) == expected


def test_a_range_reaches_open_stream_as_the_contract_spells_it(wired):
    """start=None with an end is the suffix form. Getting that the other way
    round serves the first n bytes for the last n, which is the failure that
    leaves a player unable to find the duration."""
    recording(wired, webm(512))
    wired.client.get(VIDEO_URL, params={"token": TOKEN_A},
                     headers={"Range": "bytes=-64"})
    assert wired.video.opened[-1] == (SESSION_ID, None, 64)
    wired.client.get(VIDEO_URL, params={"token": TOKEN_A},
                     headers={"Range": "bytes=10-"})
    assert wired.video.opened[-1] == (SESSION_ID, 10, None)
    wired.client.get(VIDEO_URL, params={"token": TOKEN_A})
    assert wired.video.opened[-1] == (SESSION_ID, None, None)


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

def test_a_recording_goes_in_the_fallback_and_out_the_rating_console(wired):
    """End to end on one machine with no AWS anywhere: the participant's bytes
    arrive through the upload fallback and leave through the rating console."""
    data = webm(4096)
    r = wired.client.put(UPLOAD_URL, params={"participant_id": PARTICIPANT},
                         content=data)
    assert r.status_code == 200
    assert r.json() == {"ok": True, "bytes": 4096,
                        "key": video.video_key(SESSION_ID), "status": "ok"}
    assert (wired.root / SESSION_ID / LOCAL_NAME).read_bytes() == data

    played = wired.client.get(VIDEO_URL, params={"token": TOKEN_A})
    assert played.status_code == 200
    assert played.content == data


def test_the_stored_recording_is_a_receipt_production_reads(wired):
    """video.upload_receipt is NOT faked in this fixture — it is the shipping
    function, and it is what playback_url, the rater packet and the presign
    guard all ask. An event this route wrote that it did not recognise would be
    a recording on disk that every downstream reader calls absent."""
    wired.client.put(UPLOAD_URL, params={"participant_id": PARTICIPANT},
                     content=webm(2048))
    receipt = video.upload_receipt(SESSION_ID)
    assert receipt is not None
    assert receipt["type"] == "video_uploaded"
    assert receipt["bytes"] == 2048
    assert receipt["status"] == "ok"
    assert receipt["key"] == video.video_key(SESSION_ID)


def test_the_event_is_the_confirm_endpoints_shape(wired):
    """One uniform fact whichever path the bytes took. encounter_record,
    rater_packet, verify_record and _video_state all match on `type` and then on
    `bytes`; a second shape here would need a second branch in every one of
    them, and the ones nobody updated would report the recording as absent."""
    wired.client.put(UPLOAD_URL, params={"participant_id": PARTICIPANT},
                     content=webm(128))
    events = [e for e in events_of(wired) if e.get("type") == "video_uploaded"]
    assert len(events) == 1
    assert set(events[0]) >= {"t", "wall", "type", "key", "bytes", "status"}
    assert events[0]["status"] == "ok"
    # Additive only: a reader that does not know `via` ignores it, and it is the
    # one thing in the trail that says the bytes are on the task's own disk
    # rather than in the bucket.
    assert events[0]["via"] == "local"


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

def test_a_researcher_with_no_credentials_can_watch_and_scrub_a_recording(
        live_wired):
    """The whole round's measure of success, over the REAL server.video.

    No AWS credentials — video._s3 raises NoCredentialsError at every call — and
    no API key. A participant's recording goes in through the upload fallback,
    comes back out through the rating console byte for byte, and the trailer
    probe a player uses to find the duration is answered from the end of the
    file. Every earlier test in this section stands the route on a reference
    module so it can be pinned on its own; this one is here because two halves
    that each pass their own tests can still fail to meet.
    """
    data = webm(4096)
    stored = live_wired.client.put(UPLOAD_URL,
                                   params={"participant_id": PARTICIPANT},
                                   content=data)
    assert stored.status_code == 200
    assert stored.json()["bytes"] == 4096

    # Press play.
    whole = live_wired.client.get(VIDEO_URL, params={"token": TOKEN_A})
    assert whole.status_code == 200
    assert whole.content == data
    assert whole.headers["accept-ranges"] == "bytes"
    assert whole.headers["content-type"].startswith("video/webm")

    # Find the duration: the last 64 bytes, which is how a player reads the
    # trailer of a container whose header does not carry one.
    trailer = live_wired.client.get(VIDEO_URL, params={"token": TOKEN_A},
                                    headers={"Range": "bytes=-64"})
    assert trailer.status_code == 206
    assert trailer.content == data[-64:]
    assert trailer.headers["content-range"] == "bytes 4032-4095/4096"

    # Drag the scrubber.
    middle = live_wired.client.get(VIDEO_URL, params={"token": TOKEN_A},
                                   headers={"Range": "bytes=2048-3071"})
    assert middle.status_code == 206
    assert middle.content == data[2048:3072]
    assert middle.headers["content-range"] == "bytes 2048-3071/4096"

    # And past the end is 416 carrying the true total, not a 404.
    past = live_wired.client.get(VIDEO_URL, params={"token": TOKEN_A},
                                 headers={"Range": "bytes=9999-"})
    assert past.status_code == 416
    assert past.headers["content-range"] == "bytes */4096"


def test_the_real_module_agrees_with_the_route_about_no_recording(live_wired):
    """404 on an encounter that has none, over the real module: open_stream
    answers None and the route must not turn that into a 416 or a 500."""
    assert live_wired.client.get(VIDEO_URL,
                                 params={"token": TOKEN_A}).status_code == 404


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


# --- the packet's media block: what the rating console is told ---------------
#
# The other half of the same change. server/rater_packet._media decides, for one
# encounter, whether the console offers a rater something to play — and it used
# to decide from the `video_uploaded` event a browser's confirmation POST leaves
# behind. A browser event is a claim ABOUT storage; it is not storage, and an
# encounter whose confirm leg failed once was permanently unrateable with its
# bytes sitting in the bucket the whole time. These tests are that substitution
# seen from the packet: every state below is set up by putting bytes somewhere
# (or not) and by writing a trail that disagrees with them.

MEDIA_KEYS = {"video_url", "video_available", "video_status", "upload_error",
              "expires_in", "note"}


@pytest.fixture()
def packet_root(sessions_root, monkeypatch):
    """`sessions_root`, with the rater packet reading the same directory.

    rater_packet resolves its own SESSIONS_DIR for the event trail. Pointing
    only video at the tmp tree would leave _media asking storage about the tmp
    encounter and the trail about the real DATA_DIR — two different encounters
    answering one question.
    """
    monkeypatch.setattr(rp, "SESSIONS_DIR", sessions_root)
    return sessions_root


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


def test_a_recording_whose_confirmation_never_arrived_is_rateable(packet_root, s3):
    """THE defect this change exists for, stated at the packet.

    The bytes are on disk and no video_uploaded event was ever written — the
    confirm POST timed out, or the tab closed between the PUT and the beacon.
    The packet used to read the trail, find nothing, and tell the rater "No
    webcam recording was captured for this encounter": an affirmative false
    statement about an IRB recording that exists, and one that no route and no
    CLI could ever talk it out of. The encounter was unrateable for good.
    """
    put_events(packet_root)               # a trail with no confirmation in it
    put_local(packet_root, webm())
    media = rp._media(SESSION_ID, ASSIGNMENT_ID)
    assert media["video_status"] == "ok"
    assert media["video_available"] is True
    assert media["video_url"] == f"/api/rater/video/{ASSIGNMENT_ID}"
    assert media["note"] is None


def test_a_failed_confirmation_over_bytes_that_are_there_is_still_rateable(
        packet_root, s3):
    """The same defect wearing its other face: the confirmation arrived and said
    the upload broke, while the recording landed anyway. A rater was blocked and
    told to report a fault over an encounter they could have rated."""
    put_events(packet_root, RECEIPT_FAILED)
    put_local(packet_root, webm())
    media = rp._media(SESSION_ID, ASSIGNMENT_ID)
    assert media["video_status"] == "ok"
    assert media["upload_error"] is None
    assert media["video_url"] == f"/api/rater/video/{ASSIGNMENT_ID}"


def test_an_object_in_the_bucket_with_no_trail_at_all_is_rateable(packet_root, s3):
    """The recovery path for a wave already in the bucket. Nothing local,
    nothing written down, and the object is simply there — which is the state a
    lost confirm leaves behind and the one the study needs to recover."""
    put_events(packet_root)
    s3.data = webm()
    media = rp._media(SESSION_ID, ASSIGNMENT_ID)
    assert media["video_status"] == "ok"
    assert media["video_url"] == f"/api/rater/video/{ASSIGNMENT_ID}"


def test_a_receipt_over_bytes_that_are_gone_no_longer_promises_playback(
        packet_root, s3):
    """The converse, and the reason this is a substitution rather than an extra
    check. An event saying "ok, 8.4 MB" over an empty bucket used to produce a
    playable-looking packet whose URL fails in the rater's face mid-session. The
    trail is not consulted for that question any more; storage is."""
    put_events(packet_root, RECEIPT_OK)
    media = rp._media(SESSION_ID, ASSIGNMENT_ID)
    assert media["video_status"] == "failed"
    assert media["video_url"] is None
    assert media["video_available"] is True, \
        "a lost recording is a fault to report, not an encounter to rate blind"


def test_bytes_appearing_later_flip_a_lost_recording_to_a_playable_one(
        packet_root, s3):
    """One encounter, two answers, decided by where the bytes are.

    The whole distinction in one test: with nothing in storage the packet still
    says "recorded, and the recording was lost" — which is what keeps a rater
    from scoring it from the transcript as though no camera was ever on — and
    the moment a recording is put back (a re-upload, a recovery CLI, a
    researcher copying it in) the same encounter becomes rateable with no event
    rewritten anywhere.
    """
    put_events(packet_root, RECEIPT_FAILED)
    lost = rp._media(SESSION_ID, ASSIGNMENT_ID)
    assert lost["video_status"] == "failed"
    assert "WAS recorded" in lost["note"]
    assert lost["upload_error"] == "put 403"

    put_local(packet_root, webm())
    assert rp._media(SESSION_ID, ASSIGNMENT_ID)["video_status"] == "ok"


def test_no_camera_is_still_told_apart_from_a_lost_recording(packet_root, s3):
    """The honest negative, which must survive the substitution: no bytes and no
    confirmation ever posted is an encounter that had no camera, and it is
    rateable from the transcript with the N/A instruction rather than blocked.

    Paired with the assertion that bytes alone change the answer, because the
    two states are now decided by the same call and a check that only pinned
    'absent' would pass over a function that never looked at storage at all.
    """
    put_events(packet_root)
    absent = rp._media(SESSION_ID, ASSIGNMENT_ID)
    assert absent["video_status"] == "absent"
    assert absent["video_available"] is False
    assert "Not enough information to judge" in absent["note"]

    put_local(packet_root, webm())
    assert rp._media(SESSION_ID, ASSIGNMENT_ID)["video_status"] == "ok"


def test_the_playback_url_no_longer_names_the_encounter(packet_root, s3):
    """The blinding leak this closes, pinned by value.

    The URL used to be the presigned S3 GET, whose key is
    encounters/{session_id}/webcam.webm — so the one session id anywhere in a
    blinded packet sat in a field the rater's own network tab shows and their
    browser history keeps. A session id carries the encounter's start time to
    the second, which is enough to tell that two packets recorded twelve minutes
    apart belong to one participant: exactly the cross-linking the rating code
    exists to prevent. The URL names the assignment now, which is the rater's
    own handle and links nothing.
    """
    put_events(packet_root, RECEIPT_OK)
    put_local(packet_root, webm())
    media = rp._media(SESSION_ID, ASSIGNMENT_ID)
    blob = json.dumps(media)
    assert SESSION_ID not in blob
    # Not just the whole id: the timestamp half alone is the cross-linking.
    assert SESSION_ID.split("_")[1] not in blob
    assert "X-Amz-Signature" not in blob
    assert media["video_url"].startswith("/"), \
        "app-relative: an absolute URL is fixed at build time and wrong on the " \
        "next host the study runs on"


def test_no_state_hands_the_console_an_expiry_to_count_down(packet_root, s3):
    """expires_in is null everywhere now, the playable state included.

    There is no signature and no deadline, so the console's re-mint machinery —
    the countdown, the refresh call, its rate limit and its retry button — has
    nothing left to fire on. The key stays because every branch carries the same
    key set; the value is the fact that nothing expires.
    """
    states = []
    put_events(packet_root)
    states.append(rp._media(SESSION_ID, ASSIGNMENT_ID))                # absent
    put_events(packet_root, RECEIPT_FAILED)
    states.append(rp._media(SESSION_ID, ASSIGNMENT_ID))                # failed
    put_local(packet_root, webm())
    states.append(rp._media(SESSION_ID, None))                         # unsigned
    states.append(rp._media(SESSION_ID, ASSIGNMENT_ID))                # ok
    assert [s["video_status"] for s in states] == \
        ["absent", "failed", "unsigned", "ok"]
    for state in states:
        assert state["expires_in"] is None, state["video_status"]
        # The invariant the four branches have always had, re-pinned under the
        # new decision rule: a consumer reads media["upload_error"] on any state
        # without a KeyError deciding, at import time, whether to run at all.
        assert set(state) == MEDIA_KEYS, state["video_status"]


def test_a_packet_built_outside_an_assignment_says_so_rather_than_guessing(
        packet_root, s3):
    """An operator inspecting an encounter has no assignment id, so there is no
    URL to name. The recording is still THERE, and saying "no webcam recording"
    about it would be the same false statement this change removes — so the
    fourth state survives, with its meaning narrowed to this."""
    put_local(packet_root, webm())
    media = rp._media(SESSION_ID)
    assert media["video_status"] == "unsigned"
    assert media["video_available"] is True
    assert media["video_url"] is None
    assert "could not be issued" in media["note"]


@pytest.mark.parametrize("crafted", [
    "as_0123456789ab/../../etc",
    "as_0123456789ab?token=rt_" + "f" * 32,
    "as_0123456789ab#x",
    "../as_0123456789ab",
    "AS_0123456789AB",
    "as_0123456789",
    "",
])
def test_a_crafted_assignment_id_never_reaches_the_video_element(
        packet_root, s3, crafted):
    """This string becomes the src of a <video> element, and the id arrives from
    a rater-facing route path. One carrying "/" or "?" or "#" would move that
    request to another route, or graft a query string onto this one and displace
    the token the console appends."""
    put_local(packet_root, webm())
    media = rp._media(SESSION_ID, crafted)
    assert media["video_url"] is None
    assert media["video_status"] == "unsigned"


def test_build_addresses_the_video_at_the_assignment_the_route_hands_it(
        packet_root, s3, monkeypatch):
    """The wiring, through build().

    app.py calls build(session_id, order_seed=assignment_id) — the docstring has
    always said order_seed IS the assignment id — so the media URL has to come
    out addressed to it without the route having to say the same id twice. The
    record and manifest readers are stubbed because this test is about that
    thread, not about transcript assembly.
    """
    put_events(packet_root, RECEIPT_FAILED)
    put_local(packet_root, webm())
    monkeypatch.setattr(rp, "build_record", lambda sdir: {"transcript": []})
    monkeypatch.setattr(rp, "_manifest", lambda sdir: {"session_id": SESSION_ID})

    packet = rp.build(SESSION_ID, order_seed=ASSIGNMENT_ID)
    assert packet["media"]["video_url"] == f"/api/rater/video/{ASSIGNMENT_ID}"
    assert packet["media"]["video_status"] == "ok"

    # An explicit assignment_id wins over a seed that is not one, so a caller
    # shuffling items by something else does not thereby publish a playback URL
    # that resolves to nothing.
    packet = rp.build(SESSION_ID, order_seed="pilot-order-2",
                      assignment_id=ASSIGNMENT_ID)
    assert packet["media"]["video_url"] == f"/api/rater/video/{ASSIGNMENT_ID}"
    assert rp.build(SESSION_ID, order_seed="pilot-order-2")["media"][
        "video_url"] is None


@pytest.mark.parametrize("failure", AWS_FAILURES,
                         ids=lambda e: type(e).__name__ + getattr(
                             e, "response", {}).get("Error", {}).get("Code", ""))
def test_a_storage_outage_downgrades_one_packet_and_does_not_raise(
        packet_root, s3, failure):
    """_media is on the path that builds a rater's packet, and it now asks
    storage a question that can fail. An exception here is not one bad packet:
    the console 500s and Phase 2 stops. A recording nobody can find reads as the
    lost state — which blocks the rating rather than inviting a transcript-only
    one — and the packet still comes back."""
    put_events(packet_root, RECEIPT_OK)
    s3.head_error = failure
    media = rp._media(SESSION_ID, ASSIGNMENT_ID)
    assert media["video_status"] == "failed"
    assert set(media) == MEDIA_KEYS


def test_a_local_recording_reaches_a_rater_with_no_credentials_at_all(
        packet_root, monkeypatch):
    """The measure of success for this whole round, stated at the packet.

    No stub bucket, no credentials: every S3 call raises NoCredentialsError, the
    way it does on a researcher's laptop. A recording sitting next to the
    transcript still produces a playable packet, which is the first time any
    surface in this repository could say that.
    """
    class NoCreds:
        def head_object(self, **kw):
            raise NoCredentialsError()

        def get_object(self, **kw):
            raise NoCredentialsError()

    monkeypatch.setattr(video, "_s3", NoCreds())
    put_events(packet_root)
    put_local(packet_root, webm())
    media = rp._media(SESSION_ID, ASSIGNMENT_ID)
    assert media["video_status"] == "ok"
    assert media["video_url"] == f"/api/rater/video/{ASSIGNMENT_ID}"
