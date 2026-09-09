"""Webcam video capture: presigned browser-direct upload to S3.

IRB "What Participants See and Hear" 6a: video is captured by the participant's
browser, stored in Cornell AWS, used solely so human raters can score
non-verbal conduct, and never transmitted to any model provider. Uploading
straight from the browser to S3 satisfies that by construction: the video never
touches the model path, and on Fargate it also survives deploys, which the
container filesystem does not.

Key layout matches the encounter record: encounters/{session_id}/webcam.webm.
"""

from __future__ import annotations

import json
from typing import Optional

import boto3
from botocore.config import Config
from botocore.exceptions import BotoCoreError, ClientError

from .llm import setting
from .storage import SESSION_ID_RE, SESSIONS_DIR

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

# Ceiling on a playback link's life, whatever the caller asks for. A rater opens
# a packet and rates it in one sitting, so an hour covers the work; beyond that
# the link is an IRB video recording reachable by anyone holding the URL, long
# after the rater who was issued it has finished. Twelve hours is the outer
# bound, for a rater who leaves a packet open across a working day.
MAX_PLAYBACK_SECONDS = 12 * 3600

# What uploaded_size() answers when S3 could not tell us anything: not "no
# video", which is a claim about the bucket we are in no position to make.
UNKNOWN_SIZE = -1

_s3 = None


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
    try:
        head = _client().head_object(Bucket=BUCKET, Key=video_key(session_id))
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

    Reading the receipt is why playback_url can answer "is there a video?"
    without a network round trip. That matters at Phase 2 scale: a rater's
    assignment list is tens of packets, each of which wants to know whether it
    has something to play, and a HEAD per packet turns opening the console into
    a burst of S3 calls that fail as a unit whenever credentials or the network
    do. A zero-byte receipt is treated as no video, exactly as the record
    builder treats it: the confirm endpoint writes the event whether or not the
    PUT actually landed, and `"bytes": 0` is how a failed upload looks.

    Since the confirm endpoint also writes the event when S3 could not answer at
    all, an event can carry `"status": "failed"` with `"bytes": null` — captured,
    upload unverified. That is not a receipt: nothing here has been acknowledged,
    so it is filtered out with the zero-byte ones and playback_url stays None.
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


def playback_url(session_id: str, seconds: int = 3600) -> Optional[str]:
    """A short-lived presigned GET for the encounter's webcam recording.

    Phase 2 rates the video, not the WAVs: the webcam capture carries the mixed
    conversation audio (the participant page records mic and agent audio into
    it), while the per-channel WAVs cannot be played back as a conversation at
    all. So this is the one media URL a rater's packet needs.

    Signing is arithmetic — an HMAC over the request the caller intends to make
    — so nothing here touches the network, and a URL is produced for a bucket
    this process could not reach. The existence check is therefore done against
    the local upload receipt (see upload_receipt), and None means "this
    encounter has no video", never "S3 was slow just now".

    ContentType is not overridden. The upload deliberately leaves Content-Type
    unsigned so Safari's MP4 and everyone else's WebM both go under the same
    webcam.webm key; S3 stored whatever the browser declared, and forcing a
    response type here would hand half the wave a container the <video> element
    refuses to play.
    """
    receipt = upload_receipt(session_id)
    if receipt is None:
        return None
    # Clamp rather than reject: a caller asking for a longer link gets a working
    # short one, because a packet that silently loses its video would be scored
    # from the transcript alone and nobody downstream could tell.
    expires = max(60, min(int(seconds), MAX_PLAYBACK_SECONDS))
    # Derive the key rather than reading receipt["key"]. The receipt is a line in
    # a file on disk, and the whole point of validating the session id was to
    # keep a caller from choosing which object gets signed; taking the key back
    # out of the file would hand that choice to whatever wrote the file.
    return _client().generate_presigned_url(
        "get_object",
        Params={"Bucket": BUCKET, "Key": video_key(session_id)},
        ExpiresIn=expires,
    )
