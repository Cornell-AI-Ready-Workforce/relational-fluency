"""Webcam video capture: presigned browser-direct upload to S3, and the bytes.

IRB "What Participants See and Hear" 6a: video is captured by the participant's
browser, stored in Cornell AWS, used solely so human raters can score
non-verbal conduct, and never transmitted to any model provider. Uploading
straight from the browser to S3 satisfies that by construction: the video never
touches the model path, and on Fargate it also survives deploys, which the
container filesystem does not.

Key layout matches the encounter record: encounters/{session_id}/webcam.webm.

The second half of this module — exists, local_path, store_local, open_stream —
puts the application back in the byte path, which it was deliberately kept out
of and which cost more than it saved. Browser-PUTs-to-S3 and rater-GETs-from-S3
means no process here ever holds a recording, and the consequences compounded:
a playback link that expires mid-rating, a presigned URL living in the rater's
browser history as a bearer credential, an encounter whose confirmation event
failed being permanently unrateable while its bytes sat in the bucket, and —
the one that matters most — no way to see a recording at all without live AWS
credentials. Nobody had ever watched one in the rating console.

Serving the bytes through the app fixes all four with one mechanism. A local
file is preferred over S3 wherever one exists, so a researcher with no
credentials can open a packet on a laptop and press play, and so a developer
can drop a recording into a session directory by hand to reproduce a rater's
report. That local name is `webcam.webm`, which is what encounter_record.build
and app._video_status already glob for — one name, whatever container is inside
it, so no reader can disagree with another about whether a recording exists.

There is deliberately NO function here that mints a presigned GET any more.
Playback is /api/rater/video/{assignment_id}, which streams the bytes below; a
signed playback URL is a bearer credential for an IRB video recording that
outlives the page it was issued to and sits in the rater's browser history and
in every proxy log on the way. Serving the bytes closed that, and the only way
to keep it closed is for there to be nothing in this module that can open it
again — dead code that still works is how a removed defect comes back.
"""

from __future__ import annotations

import json
import os
import secrets
import time
from collections import OrderedDict
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator, Optional

import boto3
from botocore.config import Config
from botocore.exceptions import BotoCoreError, ClientError

from .llm import setting
from .storage import SESSION_ID_RE, SESSIONS_DIR, replace_with_retry

# WHICH bucket, and which region — and these two names are read differently
# from the credentials that reach them, which is the part that surprises people.
#
# setting() is .env-file-wins-over-ambient (server.llm._cfg). The credentials
# are the other way round: boto3 reads AWS_ACCESS_KEY_ID and friends out of the
# process environment itself, and the only thing that puts .env there is
# server/app.py's load_dotenv(), which does NOT override a value the shell has
# already set. So a deployment can perfectly well end up pointed at the bucket
# .env names while signing with a key the shell supplied — and, worse, a tool
# that imports this module WITHOUT importing server.app never runs that
# load_dotenv() at all, resolves no credentials, and reports every encounter as
# unfilmed while the objects sit in the bucket. .env.example and
# docs/DEPLOY-AWS.md spell the whole table out; this comment exists so the
# asymmetry is visible at the line that causes it.
#
# AWS_DEFAULT_REGION is deliberately NOT consulted. Every client below is
# constructed with region_name=REGION explicitly, so the AWS CLI's preferred
# spelling moves nothing here: setting it alone leaves this us-east-1, which is
# right until the bucket is somewhere else and then wrong in a way that comes
# back as a PermanentRedirect naming neither variable. One name, read in one
# place, is the only version of this that an operator can check.
BUCKET = setting("S3_BUCKET", "relational-fluency-study-data")
REGION = setting("AWS_REGION", "us-east-1")

# Session ids are minted as s_{epoch}_{6 hex} (session.new_session_id). Anything
# else is refused before it reaches the filesystem or an object key: Phase 2 puts
# a rater-supplied assignment id in front of this module, and a crafted session
# id must be unable to walk out of SESSIONS_DIR on a Windows host or aim a
# signed GET at some other prefix of the study bucket.
#
# Shared with storage rather than transcribed, and narrowed from the old
# [A-Za-z0-9_-]{1,64}, which accepted uppercase. S3 keys are case-sensitive on
# every platform while Windows and default-APFS macOS directory lookups are not,
# so on a researcher's own machine a wrong-cased id passed the "does this
# session exist?" check in presign_upload and then signed a PUT for
# encounters/S_.../webcam.webm — an object key nothing else in the system ever
# looks at. The participant's webcam recording, which is the artefact raters
# score, would upload successfully and be unfindable.
_SESSION_ID_RE = SESSION_ID_RE

# What uploaded_size() answers when S3 could not tell us anything: not "no
# video", which is a claim about the bucket we are in no position to make.
UNKNOWN_SIZE = -1

# How long a "this process cannot obtain AWS credentials" answer is trusted
# before the credential chain is walked again.
#
# The answer is not per-encounter and never was: either this process can sign a
# request or it cannot. Discovering it is the expensive part — constructing a
# boto3 client RESOLVES credentials, and on a machine that is not an EC2
# instance that resolution ends at the instance metadata service,
# 169.254.169.254, which is unroutable off EC2, so botocore waits out its
# connect timeout twice before raising NoCredentialsError. Measured here at
# 2.297 s for the first miss in a credential-less process; the gate measured
# 2.30 s.
#
# The old code paid that once per client and then never again, which sounds
# like a cache and is really a trap. A boto3 client resolves its credentials
# when it is CONSTRUCTED and holds the answer in its request signer for life, so
# a client built a second before its task role was attached went on raising
# NoCredentialsError for the whole life of the process and nothing short of a
# restart could change its mind — while every miss in between still walked into
# the call to be told so and printed a warning saying it, 26 identical lines for
# a 26-packet rater queue. Making the answer explicit is what lets it be both
# reused AND retired.
#
# Sixty seconds, and the number is chosen from the other side: how long may a
# process that boots before its credentials exist stay blind? An ECS task can
# come up a moment before its task role is reachable, a researcher can run
# `aws sso login` in the next terminal, and a role can be attached to a running
# instance. All three must start working WITHOUT a restart, so the answer has to
# expire; a minute is short enough that nobody debugs it and long enough that
# the 2.30 s walk is paid once a minute at worst rather than once a packet.
# Credentials that merely ROTATE need nothing from this: botocore refreshes them
# behind a credentials object that stays non-None the whole time.
CREDENTIAL_RECHECK_SECONDS = 60.0

_s3 = None

# The client a "no credentials" answer was read from, and the moment that answer
# stops being trusted. The client is remembered, not just a flag, because
# storage_preflight and the tests install clients of their own into _s3: an
# answer read from one client must never be applied to a different one, or a
# caller that supplied a working client would be told the bucket is unreachable
# without it ever being asked.
_no_creds_client = None
_no_creds_until = 0.0


def _client():
    global _s3
    if _s3 is None:
        # The study bucket is KMS-encrypted, and S3 rejects presigned PUTs to
        # KMS objects unless the URL is SigV4-signed.
        #
        # The timeouts and the retry mode are not tuning, they are a safety
        # bound. boto3's defaults are 60 s connect, 60 s read and 'legacy'
        # retries, and this client is reached from HTTP routes that share one
        # asyncio loop with every live encounter: a black-holed S3 path (a
        # missing VPC endpoint, a wrong egress rule) would otherwise park a
        # request for minutes while no participant's audio moves and the ALB
        # health check — 5 s timeout, three strikes — retires the task
        # mid-sentence. Two standard-mode attempts against a 3 s connect and a
        # 5 s read cap the worst case at seconds, and a capped failure is one
        # the confirm endpoint can record as a failure.
        _s3 = boto3.client(
            "s3", region_name=REGION,
            config=Config(signature_version="s3v4", connect_timeout=3,
                          read_timeout=5,
                          retries={"mode": "standard", "max_attempts": 2}),
        )
    return _s3


def _aws_code(exc: Exception) -> str:
    """A short, loggable name for whatever AWS just refused to do.

    ClientError carries the service's own code (AccessDenied, SlowDown,
    PermanentRedirect); the BotoCoreError family — NoCredentialsError,
    EndpointConnectionError, ReadTimeoutError — carries none, so the class name
    is the most specific thing there is to write down.
    """
    if isinstance(exc, ClientError):
        code = (exc.response or {}).get("Error", {}).get("Code")
        if code:
            return str(code)
    return type(exc).__name__


def _credentials_missing() -> bool:
    """True only when this process is KNOWN to be unable to sign an S3 request.

    Answers "should we even try?" before every call that would otherwise cost a
    credential-chain walk. See CREDENTIAL_RECHECK_SECONDS for why the walk is
    expensive and why the answer expires.

    TRUE IS THE ONLY CONFIDENT ANSWER THIS RETURNS. Anything it cannot read —
    a client somebody else installed, a botocore that moved the attribute — is
    False, meaning "no opinion, make the call", which is exactly what happened
    before this shortcut existed. Not knowing must never be allowed to look like
    knowing there are none: that would silently switch S3 off for a deployment
    whose credentials are fine, and every encounter without a local file would
    read as never filmed.
    """
    global _s3, _no_creds_client, _no_creds_until

    now = time.monotonic()
    if _no_creds_client is not None and (_s3 is _no_creds_client or _s3 is None):
        # `_s3 is None` counts as the same client: nothing but a rebuild puts it
        # back, and rebuilding is the very walk this window exists to skip.
        if now < _no_creds_until:
            return True
        # The window is up, so throw the client away. A boto3 client resolves
        # its credentials ONCE, when it is constructed, and holds that answer in
        # its request signer for life — so re-reading this one can only repeat
        # itself, and a role attached after boot would be invisible until the
        # process restarted. Dropping it is what makes the recheck a recheck.
        _s3 = None
        _no_creds_client = None

    try:
        client = _client()
        creds = client._request_signer._credentials
    except Exception:  # noqa: BLE001, botocore internals are not a contract
        return False
    if creds is not None:
        _no_creds_client = None
        return False

    if client is not _no_creds_client:
        # Once per window, not once per packet. head_video used to print this
        # for every miss, so opening a 26-packet rater queue on a laptop with no
        # credentials wrote 26 identical warnings and buried whatever else the
        # console had to say.
        print(
            f"  WARNING: no AWS credentials resolved (bucket {BUCKET}, region "
            f"{REGION}); S3 will not be asked again for "
            f"{int(CREDENTIAL_RECHECK_SECONDS)}s"
        )
    _no_creds_client = client
    _no_creds_until = now + CREDENTIAL_RECHECK_SECONDS
    return True


class PresignRefused(Exception):
    """A write URL was refused for a reason that is NOT "the object is there".

    presign_upload used to answer None to three different questions — no such
    session, S3 confirms an object, and our own trail says there is one while S3
    will not answer — and the route turned all three into 409 "video already
    uploaded". Two of those are false statements, and the third is one the
    browser ACTS on: a 409 tells static/v2.html the earlier PUT landed, so it
    stops re-sending the recording. On the unconfirmable branch that is a
    recording the page declines to retry on the strength of a receipt nobody has
    just checked. A refusal this module cannot stand behind must not reach a
    client looking like a confirmation.
    """

    def __init__(self, code: Optional[str] = None):
        self.code = code or "unknown"
        super().__init__(self.code)


class NoSuchSession(PresignRefused):
    """No session directory, so there is nothing to attach a recording to."""


class UploadUnconfirmed(PresignRefused):
    """This server's own receipt says an object exists; S3 would not confirm it.

    The one-shot tamper guard still holds — no URL is issued — but the caller is
    told "cannot confirm" rather than "already uploaded", so nothing downstream
    can read it as a landed upload. A loud failure that costs a retry is the
    right trade against a browser quietly deciding a recording is safe when
    nothing checked.
    """


def video_key(session_id: str) -> str:
    """The one object key for this encounter's webcam recording.

    Validated here, at the point the key is minted, and not only in the readers
    further down: every other function in this module derives its key from this
    one, so this is the single place that decides which object a participant's
    recording is written to and which object a rater's packet plays back. A
    session id that differs only in case (or by a trailing "." or " ", which
    Windows strips from a path but S3 keeps in a key) names the same directory
    on a Windows/macOS host and a DIFFERENT S3 object — an upload that succeeds
    and can never be found.
    """
    if not _SESSION_ID_RE.fullmatch(session_id or ""):
        raise ValueError("bad session_id")
    return f"encounters/{session_id}/webcam.webm"


# Where the boot check writes its marker. Under encounters/ deliberately: the
# task role is granted s3:PutObject on encounters/* and steering-logs/* only
# (infra/terraform/ecs.tf), so a probe anywhere else would report a failure that
# says nothing about whether a participant's webcam upload can land. One fixed
# key, overwritten each boot, so the check cannot accumulate objects.
PREFLIGHT_KEY = "encounters/_preflight/startup-check.txt"


def storage_preflight(client=None) -> dict:
    """Check the study bucket before anyone records into it.

    The model gateway has had a boot preflight since a wrong endpoint turned up
    as a 401 halfway through an encounter (llm.preflight). S3 is the other
    credentialed seam and had none, so a wrong S3_BUCKET, a wrong AWS_REGION or
    a missing kms:GenerateDataKey grant was exercised for the first time in the
    last second of the first encounter — by the participant's browser, which
    discards the failure silently. This says so at boot instead.

    Reachable and writable are asked separately because they fail separately and
    are fixed differently: head_bucket needs s3:ListBucket, while the presigned
    webcam PUT the browser executes is signed by this process's credentials and
    so needs s3:PutObject plus the KMS grant. A read-only preflight would have
    passed happily on a task role that cannot store a single recording.

    Never raises. This is diagnosis, not a gate: a transient S3 blip must not
    stop a process that can still run encounters and still write every local
    artefact.

    Builds its own client rather than warming the module one, and takes a
    `client` for tests. A boto3 client resolves its credentials when it is
    constructed, so a boot check that populated the shared client would pin
    whatever the environment happened to hold at import — the wrong moment to
    fix that answer for the life of the process, and the wrong client to hand
    every later request.
    """
    result = {"ok": False, "bucket": BUCKET, "region": REGION,
              "credentials": False, "readable": False, "writable": False}
    try:
        if client is None:
            # Tighter than the request path's: this one runs before the port is
            # open, and an unreachable bucket must delay the boot by seconds,
            # not by a retry ladder.
            client = boto3.client(
                "s3", region_name=REGION,
                config=Config(signature_version="s3v4", connect_timeout=3,
                              read_timeout=3,
                              retries={"mode": "standard", "max_attempts": 1}),
            )
    except Exception as exc:  # noqa: BLE001, a client that cannot be built is a config error
        result["error_code"] = type(exc).__name__
        result["detail"] = str(exc)[:200]
        return result
    # Resolving credentials is local (env, shared file, or the container
    # metadata hop), and having none is the failure worth naming on its own:
    # every call below would fail with NoCredentialsError and the operator would
    # be left guessing whether the bucket name was also wrong.
    try:
        creds = client._request_signer._credentials
        result["credentials"] = creds is not None
    except Exception:  # noqa: BLE001, botocore internals are not a contract
        result["credentials"] = False
    try:
        client.head_bucket(Bucket=BUCKET)
        result["readable"] = True
    except (ClientError, BotoCoreError) as e:
        result["error_code"] = _aws_code(e)
        return result
    try:
        client.put_object(Bucket=BUCKET, Key=PREFLIGHT_KEY,
                          Body=b"relational-fluency storage preflight\n")
        result["writable"] = True
    except (ClientError, BotoCoreError) as e:
        result["error_code"] = _aws_code(e)
        return result
    result["ok"] = True
    return result


def _note_unverified_presign(session_id: str, code: Optional[str]) -> None:
    """Write down that a write URL was issued without a confirmed HEAD.

    The one-shot guard is weakened, not held, on this path (see presign_upload),
    and a weakening nobody can see afterwards is indistinguishable from one that
    never happened. This is the trail's record that for this session, at this
    moment, S3 could not say whether an object was already there.
    """
    import json as _json
    import time as _time

    try:
        with (SESSIONS_DIR / session_id / "events.jsonl").open(
                "a", encoding="utf-8") as fh:
            fh.write(_json.dumps({
                "t": None, "wall": _time.time(),
                "type": "video_presign_unverified",
                "key": video_key(session_id),
                "error": code or "unknown",
            }) + "\n")
    except OSError:
        # The URL still goes out: a missing note is worse than a lost recording
        # only if you have both, and we would rather have the recording.
        pass


def presign_upload(session_id: str, *, expires: int = 3600) -> Optional[dict]:
    # One-shot semantics: only issue a write URL for a session that actually
    # exists and has no object yet. This stops a participant re-requesting a URL
    # later to overwrite (tamper with) their already-captured IRB recording, and
    # stops arbitrary session_ids being used to seed/abuse the study bucket.
    #
    # The guarantee is weaker than it reads when S3 will not answer the HEAD,
    # and the honest statement of it is: an object S3 CONFIRMS is refused; an
    # object only our OWN trail knows about is refused; a recording nobody can
    # find any evidence of is allowed through, and the fall-through is written
    # into the trail as video_presign_unverified.
    #
    # Refusing outright on an unanswerable HEAD would throw away a recording
    # that has not been made yet — the participant's encounter is over by the
    # time anyone finds out — but issuing unconditionally, which is what this
    # did, disabled the tamper guard for the whole duration of any HEAD-side
    # failure. A task role with s3:PutObject and a denied or throttled
    # HeadObject is a plausible narrow first-apply IAM state, and precisely the
    # one the preflight exists to catch; on it, the owning participant could
    # fetch unlimited write URLs over an already-captured IRB recording. The
    # local receipt closes that: when we have our own acknowledged
    # video_uploaded event, we know an object is there without asking S3, so the
    # guard holds on exactly the recordings it was written to protect.
    #
    # Only ONE of those refusals returns None, and that is deliberate: None means
    # "S3 confirmed an object is there", which is the single case a client may
    # safely read as "the earlier PUT landed, do not send the bytes again". The
    # other two raise (see PresignRefused) so neither can reach the browser
    # wearing a confirmation's clothes.
    # Shape first, filesystem second. On Windows and on a default macOS volume
    # SESSIONS_DIR/"S_1772460300_44C9A2" IS the real session directory, so
    # is_dir() below would say yes and every key signed from here would carry
    # the caller's spelling into an S3 key that no reader ever derives.
    if not _SESSION_ID_RE.fullmatch(session_id or ""):
        raise NoSuchSession(session_id)
    session_dir = SESSIONS_DIR / session_id
    if not session_dir.is_dir():
        raise NoSuchSession(session_id)
    probe = head_video(session_id)
    if probe["bytes"] is None:
        # S3 would not answer. Fall back to what this server itself confirmed
        # earlier, then let the URL out and say so.
        if upload_receipt(session_id) is not None:
            raise UploadUnconfirmed(probe["error"])
        _note_unverified_presign(session_id, probe["error"])
    elif probe["bytes"] > 0:
        return None
    key = video_key(session_id)
    # Do NOT pin ContentType in the signed params: Safari/iOS MediaRecorder only
    # produces MP4 (video/mp4) while other browsers produce WebM, and a presigned
    # PUT whose signature fixes Content-Type rejects the other type. Leaving it
    # unsigned lets the browser send whichever container it recorded; the object
    # key stays stable (webcam.webm) so head_video() and the one-shot check above
    # still find it whatever the browser actually sent.
    url = _client().generate_presigned_url(
        "put_object",
        Params={"Bucket": BUCKET, "Key": key},
        ExpiresIn=expires,
    )
    return {"url": url, "key": key, "bucket": BUCKET}


def head_video(session_id: str) -> dict:
    """One HEAD against the bucket, with the answer kept apart from the failure.

    Returns {"bytes": int, "error": None} when S3 answered — including the
    genuinely-absent object, which is honestly 0 bytes — and
    {"bytes": None, "error": "<code>"} when it could not answer at all.

    The distinction is the whole point. "S3 says there is no object" and "S3
    would not tell us" look identical to a caller that only has an integer, and
    the confirm endpoint has to write them down differently: the first is a
    recording that never landed, the second is a recording that may well be
    sitting in the bucket while the record says the encounter has none. Raising
    instead — which is what this used to do for every code but 404 — put the
    caller in the worst of the three positions: no number, and no event either.
    """
    # Derived before the credential check so a session id that is not a minted
    # one still raises ValueError here, whatever the credential state — the
    # caller's mistake must not turn into an S3 answer on one machine and an
    # exception on another.
    key = video_key(session_id)
    if _credentials_missing():
        # Nothing to sign with, so this HEAD could not leave the process anyway:
        # botocore would raise NoCredentialsError out of the signer. Saying so
        # here skips the credential-chain walk that discovers it, which is 2.30 s
        # on a machine that is not an EC2 instance (CREDENTIAL_RECHECK_SECONDS),
        # and returns the same {"bytes": None, "error": "NoCredentialsError"} the
        # caught exception would have produced — the same string _aws_code gives
        # it, so nothing downstream can tell the shortcut from the real thing.
        return {"bytes": None, "error": "NoCredentialsError"}
    try:
        head = _client().head_object(Bucket=BUCKET, Key=key)
        return {"bytes": int(head.get("ContentLength", 0)), "error": None}
    except (ClientError, BotoCoreError) as e:
        code = _aws_code(e)
        # Only a genuinely absent object counts as 0 bytes.
        if code in ("404", "NoSuchKey", "NotFound"):
            return {"bytes": 0, "error": None}
        # Everything else — AccessDenied, an expired token, a wrong region's
        # PermanentRedirect, a SlowDown, no credentials at all — is logged with
        # the coordinates a person needs to act on it. Without the bucket, the
        # region and the session id, the only trace was an ASGI traceback that
        # named none of them.
        print(
            f"  WARNING: S3 HEAD failed for session {session_id} "
            f"(bucket {BUCKET}, region {REGION}): {code}"
        )
        return {"bytes": None, "error": code}


def uploaded_size(session_id: str) -> int:
    """Bytes S3 actually holds for this session's video; 0 if absent.

    UNKNOWN_SIZE (-1) when S3 could not be asked. Callers test `> 0`, so an
    unknown state reads as "not known to be uploaded" rather than as an
    exception escaping into a route that has no handler for it.

    NOTHING IN THIS SERVER CALLS THIS ANY MORE, and that is the first thing to
    know about it. Both former callers — the presign route and the confirm route
    — now ask head_video() directly, because an integer cannot express the
    difference between "S3 says there is no object" and "S3 would not say", and
    that difference is what the confirm endpoint has to write into the record.
    It survives as a compatibility shim for anything outside this repository
    that still imports it, and the only exercise it gets is its own test plus a
    monkeypatch in tests/test_rater_packet.py — which is a weak place to be: a
    helper whose sole exercise is its own test is one nobody will notice
    breaking, and its UNKNOWN_SIZE contract is now asserted by no production
    code path at all. Prefer head_video() in anything new; this should be
    deleted outright once that monkeypatch is retargeted.
    """
    probe = head_video(session_id)
    return UNKNOWN_SIZE if probe["bytes"] is None else probe["bytes"]


def upload_receipt(session_id: str) -> Optional[dict]:
    """The `video_uploaded` event S3 acknowledged, or None.

    POST /api/sessions/{id}/video-uploaded calls head_video() — a real HEAD
    against the bucket — and only then appends this event with the byte count S3
    reported. So the event is not the browser's word for it; it is the server's
    own receipt for an object that existed at that moment, written down. (It
    called uploaded_size() when this was written; that collapsed "S3 says none"
    into "S3 would not say", which is the distinction the route now has to
    record, so it asks head_video directly. presign_upload does the same, for
    the same reason, so uploaded_size has no caller left in this server — see
    its own docstring.)

    Reading the receipt is a local file read, so it costs no round trip. It is
    NOT how anything decides whether a rater has something to play any more —
    exists() asks where the bytes are, because an upload whose confirm POST
    failed left no event at all while its object sat in the bucket, and every
    reader then called that encounter unfilmed forever. What survives here is
    the one-shot upload guard (see presign_upload), where "we already
    acknowledged an object for this session" is a fact about our own history and
    is exactly the right thing to read out of our own history. A zero-byte
    receipt is treated as no video, exactly as the record builder treats it: the
    confirm endpoint writes the event whether or not the PUT actually landed,
    and `"bytes": 0` is how a failed upload looks.

    Since the confirm endpoint also writes the event when S3 could not answer at
    all, an event can carry `"status": "failed"` with `"bytes": null` — captured,
    upload unverified. That is not a receipt: nothing here has been acknowledged,
    so it is filtered out with the zero-byte ones and this answers None.
    The failed event is still the record that the encounter HAD a recording
    attempt, which is what tells "never captured" apart from "captured, and we
    could not confirm it" — a distinction the rater packet has to make and
    cannot make from a receipt alone.
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
                if '"video_uploaded"' not in line:
                    # Cheap prefilter: events.jsonl is hundreds of lines per
                    # encounter and only a couple are ever this type.
                    continue
                try:
                    ev = json.loads(line)
                except ValueError:
                    continue
                if ev.get("type") == "video_uploaded" and (ev.get("bytes") or 0) > 0:
                    # Last one wins, matching encounter_record.build: a repeated
                    # confirmation is the later, better-informed one.
                    latest = ev
    except OSError:
        return None
    return latest


# playback_url() and MAX_PLAYBACK_SECONDS stood here, and are gone deliberately.
#
# They minted a presigned GET for the recording and handed it to the rating
# console. That URL is a bearer credential for an IRB video: anyone holding the
# string can fetch the recording, it survives in the rater's browser history and
# in every intermediary's logs, and it carried the encounter's SESSION ID in the
# object key — which the rater is supposed to be blind to. It also expired,
# which is how a rater mid-sitting lost playback, and it could not produce a
# single frame on a machine without live AWS credentials, which is why nobody
# had ever watched a recording in the console.
#
# open_stream() below replaced all of that, and the two functions had no
# production caller left. They are deleted rather than left as dead code because
# dead code that still WORKS is an invitation: the next packet builder that
# wants a URL finds one, and the defect that this round removed comes straight
# back. If a caller reappears it should reach for /api/rater/video, which is
# addressed by assignment id and streams these bytes.


# --- the byte path ------------------------------------------------------------

# What a locally-stored recording is called inside the session directory. The
# same basename the S3 key ends in, and the same basename encounter_record.build
# and app._video_status already glob for ("webcam*"), so landing a file here
# makes those two readers agree with this module for free.
#
# The extension is a lie for half the wave and that is deliberate: Safari's
# MediaRecorder produces fragmented MP4 while Chrome and Firefox produce WebM,
# the presigned PUT leaves Content-Type unsigned so both land under the one key,
# and a second local name would give exists() and local_path() two answers to
# disagree about. The container is read out of the bytes instead — see
# _sniff_content_type.
LOCAL_VIDEO_NAME = "webcam.webm"

# What we serve when neither the object's own metadata nor the first bytes say.
# WebM because that is what every browser but Safari records; a wrong guess here
# costs the rater a black rectangle, which is why sniffing comes first.
DEFAULT_CONTENT_TYPE = "video/webm"

# Content types that carry no information. S3 stores whatever the browser
# declared on the unsigned PUT, and a browser that declared nothing leaves
# binary/octet-stream on the object — which a <video> element will not play at
# all, so it must never be passed through as if it were an answer.
_GENERIC_CONTENT_TYPES = frozenset({
    "", "binary/octet-stream", "application/octet-stream",
})

# What a sniff cost before this, and why it is remembered.
#
# On the S3 branch every rater seek was three round trips: HEAD for the size,
# GET for the bytes, and a twelve-byte GET to read the container out of the
# object's head because the stored Content-Type is the browser's unverified word
# on an unsigned PUT. Twenty seeks measured sixty round trips, and a rater
# scrubbing a seven-minute encounter makes far more than twenty. The container
# is a property of the OBJECT, not of the request, so it is worth reading once.
#
# WHAT INVALIDATES AN ENTRY: the object's byte count is part of the key, so an
# encounter re-recorded to a different length re-sniffs on its next read; the
# whole memo is dropped when the S3 client is replaced, because an entry read
# through one client says nothing about what a different credential, bucket or
# region would return; and the oldest entry is evicted past _SNIFF_CACHE_MAX. An
# object overwritten with a DIFFERENT container at EXACTLY the same byte count
# would keep the stale answer until one of those three happens — accepted
# knowingly, because presign_upload's one-shot guard means an encounter's object
# is written once, and two MediaRecorder containers agreeing to the byte is not
# a thing that occurs.
#
# 256 entries: a wave is tens of encounters and a rater's queue is tens of
# packets, so the working set fits many times over, while the bound is what
# stops a long-lived process accumulating one entry per encounter it has ever
# served. Each entry is two short strings and an int.
_SNIFF_CACHE_MAX = 256
_sniff_cache: "OrderedDict[tuple, str]" = OrderedDict()
_sniff_cache_client = None

# How much of a recording is in memory at once, on both the read and the write
# side. A recording is tens of megabytes, so the whole object never fits in a
# request handler's working set at Phase 2 concurrency: a dozen raters scrubbing
# a 40 MB packet would be half a gigabyte of buffers.
#
# 256 KiB rather than something smaller or larger. Smaller (say 8 or 64 KiB)
# multiplies the per-chunk ASGI send and, on the S3 side, the per-read overhead
# by hundreds for every megabyte, and a seek in a <video> element is latency the
# rater watches. Larger (1 MiB and up) buys nothing measurable on a media stream
# and both delays the first bytes of a scrub and multiplies the per-connection
# footprint. 256 KiB is four reads to the megabyte and a few megabytes of
# buffers across every concurrent rater.
CHUNK_SIZE = 256 * 1024


class RangeNotSatisfiable(Exception):
    """The caller asked for bytes this recording does not have.

    Carries the true total, because the only correct answer to an unsatisfiable
    range is 416 with `Content-Range: bytes */TOTAL` — the client needs the size
    to ask again. Distinct from open_stream returning None, which means there is
    no recording here at all: HTTP owes those two different statuses (416 and
    404), and a rater shown one when the other is true gets a wrong explanation
    for why the video will not play, which is the difference between "reload
    this" and "this encounter was never filmed".
    """

    def __init__(self, total: int):
        self.total = int(total)
        super().__init__(f"range not satisfiable, object is {self.total} bytes")


@dataclass
class VideoStream:
    """One response's worth of a recording, still on the wire.

    `chunks` is an iterator and is never a materialised body — see CHUNK_SIZE.
    `length` is the bytes in THIS response and `total` the bytes in the whole
    object; they are equal only for a whole-object request. `start` and `end`
    are inclusive offsets, the form Content-Range wants, so the route can write
    the header straight out of these fields without arithmetic of its own.

    Local files and S3 objects both produce this, with identical values for the
    same request, because the route cannot tell the two apart and neither can
    the test that pins them.
    """

    chunks: Iterator[bytes]
    length: int
    total: int
    content_type: str
    start: int
    end: int


def local_path(session_id: str) -> Path:
    """Where a locally-stored recording for this encounter lives.

    Pure arithmetic: no stat, no mkdir, nothing that touches the disk, so it is
    safe to call while deciding whether to touch the disk at all.

    Validated with the same shape check the object key uses, and for the same
    reason: Phase 2 puts a rater-supplied assignment id in front of this module,
    and a crafted session id that reached the join would walk straight out of
    SESSIONS_DIR. Raising rather than returning a sentinel is deliberate — a
    caller that forgot to check a None would silently address the parent
    directory instead.
    """
    if not _SESSION_ID_RE.fullmatch(session_id or ""):
        raise ValueError("bad session_id")
    return SESSIONS_DIR / session_id / LOCAL_VIDEO_NAME


def _local_size(session_id: str) -> int:
    """Bytes on local disk for this encounter; 0 for absent or unreadable.

    Zero-length counts as absent, matching upload_receipt's rule that a
    zero-byte upload is not a recording: a crashed or interrupted write leaves
    exactly that file, and reporting it as playable puts a packet in front of a
    rater with nothing in it.
    """
    try:
        st = local_path(session_id).stat()
    except (OSError, ValueError):
        return 0
    return st.st_size if st.st_size > 0 else 0


def _sniff_content_type(head: bytes) -> Optional[str]:
    """The container these first bytes are, or None if they are neither.

    Both formats a browser MediaRecorder produces land under the one webcam.webm
    key, so the name cannot be trusted and the bytes are the only evidence.
    Serving Safari's fragmented MP4 as video/webm gives the rater a black
    rectangle and NO error — the element simply never fires a frame — so this is
    worth twelve bytes of the object.

    WebM is Matroska, whose EBML magic is the first four bytes. ISO-BMFF (MP4)
    puts a box length first and the 'ftyp' type at offset 4.
    """
    if head[:4] == b"\x1a\x45\xdf\xa3":
        return "video/webm"
    if len(head) >= 8 and head[4:8] == b"ftyp":
        return "video/mp4"
    return None


def _local_content_type(path: Path) -> str:
    try:
        # The builtin rather than Path.open, here and in the two opens below:
        # tests/test_deploy_portability's unpinned-text-I/O scan reads the mode
        # out of the second positional argument, so a binary Path.open("rb")
        # reads to it as an unpinned TEXT open and lands in a list that is
        # asserted to only shrink. Same call, and the whole repository already
        # spells a binary open this way.
        with open(path, "rb") as fh:
            head = fh.read(12)
    except OSError:
        return DEFAULT_CONTENT_TYPE
    return _sniff_content_type(head) or DEFAULT_CONTENT_TYPE


def _resolve_range(total: int, start: Optional[int],
                   end: Optional[int]) -> tuple:
    """Turn a requested range into inclusive absolute offsets, or refuse it.

    The single place both the local and the S3 branch resolve a range, so the
    two cannot drift into answering the same request with different offsets —
    which the route, having no idea which branch served it, would have no way to
    notice.

    Three forms, all of them RFC 7233's:
      both None            the whole object
      start, end None      open-ended, `bytes=N-`: N to the end
      start None, end set  the suffix form, `bytes=-N`: the LAST N bytes
    An end past the object is clamped rather than refused, because that is what
    a player sends when it guesses a window past the end of a stream, and
    refusing it makes the recording unseekable for no reason.
    """
    if start is None and end is None:
        return 0, total - 1
    if start is None:
        # Suffix form. A zero-length suffix is unsatisfiable by the RFC, and a
        # suffix longer than the object is the whole object, not an error.
        suffix = int(end)
        if suffix <= 0:
            raise RangeNotSatisfiable(total)
        return max(0, total - suffix), total - 1
    start = int(start)
    # A start at or past the end is the one case that is genuinely a 416: there
    # are no bytes there to send, and answering 200 with the whole object would
    # have the player render from an offset it did not ask for.
    if start < 0 or start >= total:
        raise RangeNotSatisfiable(total)
    if end is None or int(end) >= total:
        end = total - 1
    else:
        end = int(end)
    if end < start:
        raise RangeNotSatisfiable(total)
    return start, end


def exists(session_id: str) -> bool:
    """Are there playable bytes for this encounter, anywhere?

    Local file first, then the bucket. This is what replaces "did a browser
    confirmation event arrive" as the question the rater packet asks, and that
    substitution is the whole fix for the permanently-unrateable encounter: an
    upload whose confirm POST failed left no video_uploaded event, so every
    reader said the encounter had no recording while the object sat in the
    bucket, with no route and no CLI that could ever change its mind. Asking
    where the bytes are, rather than what we wrote down about them, cannot get
    stuck in that state.

    NEVER RAISES, and that is not defensive habit. This is called once per
    packet while a rater's assignment list is built, so an exception here is not
    one bad packet, it is every packet — the console fails to open and Phase 2
    stops. Absent credentials, an unreachable endpoint and a denied HeadObject
    are all False: "we cannot find bytes" is the honest reading, and it
    downgrades one packet's video instead of taking the console down. The
    temptation to let the exception through "for visibility" is exactly the bug
    that would do that; the visibility lives in head_video's logged warning
    instead, which names the bucket, the region and the session.

    NEVER SLOW EITHER, which is the same requirement wearing different clothes.
    A rater's assignment list asks this once per packet, so anything costing
    seconds per miss costs a minute of a researcher staring at a console that
    looks broken. The one thing that could — resolving AWS credentials on a
    machine that has none — is asked once per CREDENTIAL_RECHECK_SECONDS rather
    than once per encounter, because it is a fact about the process and not
    about the encounter. See _credentials_missing.
    """
    if not _SESSION_ID_RE.fullmatch(session_id or ""):
        return False
    if _local_size(session_id) > 0:
        return True
    # head_video already splits "S3 says no object" (0) from "S3 would not say"
    # (None) and never raises for either; both are False here, but they are not
    # the same fact, and a caller that needs them apart should ask it directly.
    return (head_video(session_id)["bytes"] or 0) > 0


def store_local(session_id: str, source) -> int:
    """Write a recording to local disk, streaming, and return bytes written.

    `source` is a file-like with .read(n) or an iterable of bytes — Starlette
    hands a SpooledTemporaryFile for the first and a chunk generator for the
    second, and neither is ever drained into a single buffer here: a recording
    is tens of megabytes and this runs inside a request handler.

    Temp file plus os.replace via storage.replace_with_retry, the atomic-write
    idiom every other writer in this codebase uses, because exists() and
    open_stream() read this path concurrently and a reader must never see a
    half-written file — a truncated WebM plays as a few seconds of a
    conversation, which is worse than no video at all, because the rater scores
    it without knowing anything is missing.

    The temp name carries a random suffix rather than the plain ".tmp" the
    manifest writer uses. A manifest write is owned by one session loop and
    cannot race itself; two PUTs for the same session arriving together would
    interleave into one shared temp file and land a spliced recording.
    """
    dest = local_path(session_id)
    # exist_ok, and parents: the route has already authorised this against a
    # real session, and creating the directory here is what lets a recording
    # still be stored for an encounter whose directory was cleaned up under it.
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_name(f"{dest.name}.{secrets.token_hex(4)}.tmp")

    if isinstance(source, (bytes, bytearray, memoryview)):
        # A whole body already in hand. Handled before the iterator branch
        # because `iter(b"...")` yields INTEGERS, which would reach fh.write as
        # a TypeError several megabytes into an otherwise-working upload.
        chunks = iter((bytes(source),))
    elif hasattr(source, "read"):
        chunks = iter(lambda: source.read(CHUNK_SIZE), b"")
    else:
        try:
            chunks = iter(source)
        except TypeError:
            # An async generator lands here. Saying so beats the alternative:
            # `for` over it raises deep inside the write, after the temp file
            # exists, with a message about __iter__ that names nothing useful.
            raise TypeError(
                "store_local needs a file-like or a synchronous byte iterator; "
                f"got {type(source).__name__}"
            ) from None

    written = 0
    try:
        with open(tmp, "wb") as fh:
            for chunk in chunks:
                if not chunk:
                    continue
                fh.write(chunk)
                written += len(chunk)
            fh.flush()
            # The rename is atomic against a concurrent reader but not against a
            # power loss: without this, a crash can leave the directory entry
            # pointing at a file whose blocks were never written, and exists()
            # would then report a recording that plays as nothing.
            os.fsync(fh.fileno())
        replace_with_retry(tmp, dest)
    except BaseException:
        # Leave no partial file behind under a name a later run might mistake
        # for a real recording, and no wasted tens of megabytes on the disk.
        try:
            tmp.unlink()
        except OSError:
            pass
        raise
    return written


def _local_stream(session_id: str, start: Optional[int],
                  end: Optional[int]) -> Optional[VideoStream]:
    path = local_path(session_id)
    total = _local_size(session_id)
    if total <= 0:
        return None
    start, end = _resolve_range(total, start, end)
    length = end - start + 1

    def chunks() -> Iterator[bytes]:
        # Opened inside the generator, not beside the stat above: a VideoStream
        # the route builds and then abandons (a client that hangs up before the
        # first send) would otherwise hold an open handle until the garbage
        # collector got to it, and on Windows an open handle on either side is
        # exactly what makes replace_with_retry spin.
        with open(path, "rb") as fh:
            fh.seek(start)
            left = length
            while left > 0:
                buf = fh.read(min(CHUNK_SIZE, left))
                if not buf:
                    # The file shrank under us — a re-record during playback.
                    # Stop rather than spin; the short body is the signal.
                    return
                left -= len(buf)
                yield buf

    return VideoStream(chunks=chunks(), length=length, total=total,
                       content_type=_local_content_type(path),
                       start=start, end=end)


def _s3_content_type(client, key: str, declared: Optional[str],
                     total: int) -> str:
    """What the object says it is, or what its first bytes say it is.

    The upload deliberately leaves Content-Type unsigned so Safari's MP4 and
    everyone else's WebM both land under webcam.webm, which means the object's
    own metadata is whatever the browser chose to declare — sometimes right,
    sometimes binary/octet-stream, which no <video> element will play. One
    twelve-byte ranged GET settles it, on that path only; it is a round trip a
    rater never notices, against an unrateable packet they certainly would.

    That round trip is made ONCE per object rather than once per seek — see
    _SNIFF_CACHE_MAX for what the repeat cost and what drops an entry. `total`
    is the object's size, already in hand from the HEAD the caller just did, and
    is part of the cache key rather than a second thing to check.
    """
    global _sniff_cache_client
    # Base type only: a stored `video/webm; codecs="vp8,opus"` is normalised to
    # video/webm. The parameter is dropped rather than passed through because it
    # is the browser's unverified word on the unsigned PUT — a wrong or
    # malformed codecs list makes an element refuse a file it could have played,
    # while every element sniffs the container itself and needs only the type.
    declared = (declared or "").split(";")[0].strip().lower()
    if declared and declared not in _GENERIC_CONTENT_TYPES:
        # No round trip on this branch, so nothing to remember: the answer came
        # out of the response the caller already had.
        return declared

    if client is not _sniff_cache_client:
        # A different client is a different view of the bucket — other
        # credentials, or a module re-pointed at another bucket or region — and
        # an answer read through the old one is not evidence about the new one.
        _sniff_cache.clear()
        _sniff_cache_client = client
    cache_key = (BUCKET, key, int(total))
    cached = _sniff_cache.get(cache_key)
    if cached is not None:
        _sniff_cache.move_to_end(cache_key)
        return cached

    try:
        obj = client.get_object(Bucket=BUCKET, Key=key, Range="bytes=0-11")
        head = obj["Body"].read(12)
        obj["Body"].close()
    except (ClientError, BotoCoreError, KeyError, OSError):
        # Not worth failing playback over: the object is there, we simply could
        # not sniff it, and WebM is right for every browser but one.
        #
        # NOT cached, and that is the point of putting the return here rather
        # than below: a throttled or momentarily-denied twelve-byte GET is a
        # fact about this instant, not about the object, and remembering it
        # would pin the fallback type on a recording for the life of the process
        # — a Safari MP4 served as WebM plays as a black rectangle with no error
        # for every rater who opens it afterwards.
        return DEFAULT_CONTENT_TYPE
    resolved = _sniff_content_type(head or b"") or DEFAULT_CONTENT_TYPE
    # Cached including the fall-back-to-default case: the bytes were read and
    # they were neither container, which is a settled answer about this object
    # and not worth re-reading on every seek.
    _sniff_cache[cache_key] = resolved
    _sniff_cache.move_to_end(cache_key)
    while len(_sniff_cache) > _SNIFF_CACHE_MAX:
        _sniff_cache.popitem(last=False)
    return resolved


def _s3_stream(session_id: str, start: Optional[int],
               end: Optional[int]) -> Optional[VideoStream]:
    # head_video, not a bare head_object: it already classifies every AWS
    # failure the way the rest of this module does and already keeps "no object"
    # apart from "no answer", and a second way to fail here would be a second
    # thing to keep in step with it.
    probe = head_video(session_id)
    total = probe["bytes"] or 0
    if total <= 0:
        # Includes probe["bytes"] is None — S3 would not answer at all. There is
        # nothing to serve either way, and head_video has already logged the
        # code with the bucket, the region and the session id.
        return None
    start, end = _resolve_range(total, start, end)
    length = end - start + 1
    key = video_key(session_id)
    client = _client()
    params = {"Bucket": BUCKET, "Key": key}
    # Only send Range when one was asked for, so a whole-object read stays a
    # plain GET: S3 answers a Range covering the whole object with a 206 and a
    # Content-Range, and there is no reason to make the common case the odd one.
    if not (start == 0 and end == total - 1):
        params["Range"] = f"bytes={start}-{end}"
    try:
        obj = client.get_object(**params)
        body = obj["Body"]
    except (ClientError, BotoCoreError) as e:
        # The object was there a moment ago at HEAD time. Log with the same
        # coordinates head_video logs, and answer "nothing to serve" rather than
        # letting a botocore exception out of a route that has no handler for
        # it — a 500 tells the rater nothing they can act on.
        print(
            f"  WARNING: S3 GET failed for session {session_id} "
            f"(bucket {BUCKET}, region {REGION}): {_aws_code(e)}"
        )
        return None
    ctype = _s3_content_type(client, key, obj.get("ContentType"), total)

    def chunks() -> Iterator[bytes]:
        try:
            while True:
                buf = body.read(CHUNK_SIZE)
                if not buf:
                    return
                yield buf
        finally:
            # A rater who scrubs abandons the stream mid-body, every time.
            # Without this the underlying HTTPS connection is never returned to
            # botocore's pool, and a session of ordinary seeking exhausts it —
            # after which every S3 call in the process, not just video, blocks.
            try:
                body.close()
            except Exception:  # noqa: BLE001, closing a dead socket is not news
                pass

    return VideoStream(chunks=chunks(), length=length, total=total,
                       content_type=ctype, start=start, end=end)


def open_stream(session_id: str, *, start: Optional[int] = None,
                end: Optional[int] = None) -> Optional[VideoStream]:
    """The encounter's recording as a byte stream, from local disk or from S3.

    None when there is nothing to serve — no such encounter, no recording, or an
    S3 that would not answer. RangeNotSatisfiable when the recording exists and
    the requested bytes are not in it; see that class for why the two cannot be
    collapsed into one answer.

    LOCAL WINS, and it wins without S3 being consulted at all. That is what
    makes a laptop with no AWS credentials able to open the rating console and
    play a recording, which nobody has ever been able to do; it is also what
    lets a developer drop a file into a session directory and reproduce a
    rater's report against the real playback path rather than a mock of it.
    """
    if not _SESSION_ID_RE.fullmatch(session_id or ""):
        return None
    if _local_size(session_id) > 0:
        return _local_stream(session_id, start, end)
    return _s3_stream(session_id, start, end)
