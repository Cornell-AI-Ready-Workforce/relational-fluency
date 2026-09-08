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
import re
from typing import Optional

import boto3
from botocore.config import Config
from botocore.exceptions import ClientError

from .llm import setting
from .storage import SESSIONS_DIR

BUCKET = setting("S3_BUCKET", "relational-fluency-study-data")
REGION = setting("AWS_REGION", "us-east-1")

# Session ids are minted as s_{epoch}_{6 hex} (session.py:34). Anything that is
# not plain identifier characters is refused before it reaches the filesystem or
# an object key: Phase 2 puts a rater-supplied assignment id in front of this
# module, and a crafted session id must be unable to walk out of SESSIONS_DIR on
# a Windows host or aim a signed GET at some other prefix of the study bucket.
_SESSION_ID_RE = re.compile(r"[A-Za-z0-9_-]{1,64}")

# Ceiling on a playback link's life, whatever the caller asks for. A rater opens
# a packet and rates it in one sitting, so an hour covers the work; beyond that
# the link is an IRB video recording reachable by anyone holding the URL, long
# after the rater who was issued it has finished. Twelve hours is the outer
# bound, for a rater who leaves a packet open across a working day.
MAX_PLAYBACK_SECONDS = 12 * 3600

_s3 = None


def _client():
    global _s3
    if _s3 is None:
        # The study bucket is KMS-encrypted, and S3 rejects presigned PUTs to
        # KMS objects unless the URL is SigV4-signed.
        _s3 = boto3.client("s3", region_name=REGION,
                           config=Config(signature_version="s3v4"))
    return _s3


def video_key(session_id: str) -> str:
    return f"encounters/{session_id}/webcam.webm"


def presign_upload(session_id: str, *, expires: int = 3600) -> Optional[dict]:
    # One-shot semantics: only issue a write URL for a session that actually
    # exists and has no object yet. This stops a participant re-requesting a URL
    # later to overwrite (tamper with) their already-captured IRB recording, and
    # stops arbitrary session_ids being used to seed/abuse the study bucket.
    session_dir = SESSIONS_DIR / session_id
    if not session_dir.is_dir():
        return None
    if uploaded_size(session_id) > 0:
        return None
    key = video_key(session_id)
    # Do NOT pin ContentType in the signed params: Safari/iOS MediaRecorder only
    # produces MP4 (video/mp4) while other browsers produce WebM, and a presigned
    # PUT whose signature fixes Content-Type rejects the other type. Leaving it
    # unsigned lets the browser send whichever container it recorded; the object
    # key stays stable (webcam.webm) so uploaded_size()/one-shot checks still work.
    url = _client().generate_presigned_url(
        "put_object",
        Params={"Bucket": BUCKET, "Key": key},
        ExpiresIn=expires,
    )
    return {"url": url, "key": key, "bucket": BUCKET}


def uploaded_size(session_id: str) -> int:
    """Bytes S3 actually holds for this session's video; 0 if absent."""
    try:
        head = _client().head_object(Bucket=BUCKET, Key=video_key(session_id))
        return int(head.get("ContentLength", 0))
    except ClientError as e:
        # Only a genuinely absent object counts as 0 bytes. Any other AWS error
        # (AccessDenied, wrong region, throttling, clock skew) must not be
        # silently reported as "video missing" — surface it so a real upload is
        # not misclassified as absent with no diagnostic trail.
        if e.response.get("Error", {}).get("Code") in ("404", "NoSuchKey", "NotFound"):
            return 0
        raise


def upload_receipt(session_id: str) -> Optional[dict]:
    """The `video_uploaded` event S3 acknowledged, or None.

    POST /api/sessions/{id}/video-uploaded calls uploaded_size() — a real HEAD
    against the bucket — and only then appends this event with the byte count S3
    reported. So the event is not the browser's word for it; it is the server's
    own receipt for an object that existed at that moment, written down.

    Reading the receipt is why playback_url can answer "is there a video?"
    without a network round trip. That matters at Phase 2 scale: a rater's
    assignment list is tens of packets, each of which wants to know whether it
    has something to play, and a HEAD per packet turns opening the console into
    a burst of S3 calls that fail as a unit whenever credentials or the network
    do. A zero-byte receipt is treated as no video, exactly as the record
    builder treats it: the confirm endpoint writes the event whether or not the
    PUT actually landed, and `"bytes": 0` is how a failed upload looks.
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
