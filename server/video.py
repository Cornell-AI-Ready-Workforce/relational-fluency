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

import boto3
from botocore.config import Config

from .llm import setting

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


def presign_upload(session_id: str, *, expires: int = 3600) -> dict:
    key = video_key(session_id)
    url = _client().generate_presigned_url(
        "put_object",
        Params={"Bucket": BUCKET, "Key": key, "ContentType": "video/webm"},
        ExpiresIn=expires,
    )
    return {"url": url, "key": key, "bucket": BUCKET, "content_type": "video/webm"}


def uploaded_size(session_id: str) -> int:
    """Bytes S3 actually holds for this session's video; 0 if absent."""
    try:
        head = _client().head_object(Bucket=BUCKET, Key=video_key(session_id))
        return int(head.get("ContentLength", 0))
    except Exception:  # noqa: BLE001
        return 0
