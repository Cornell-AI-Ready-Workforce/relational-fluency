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

from typing import Optional

import boto3
from botocore.config import Config
from botocore.exceptions import ClientError

from .llm import setting
from .storage import SESSIONS_DIR

BUCKET = setting("S3_BUCKET", "relational-fluency-study-data")
REGION = setting("AWS_REGION", "us-east-1")

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
