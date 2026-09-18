"""Copy a closed encounter's record to the study bucket.

Study 1 plan 5.2: the task's disk is ephemeral until the EFS volume is applied,
and even with it the analysis copy belongs in S3 beside the webcam video. So
when a session store closes, everything it wrote — record.json, events.jsonl,
manifest.json, the WAV channels — is uploaded under the same prefix the video
already uses, `encounters/<session_id>/`, and the outcome is written to
`archive.json` in the session directory so a wave check can see which
encounters have a second copy.

Best effort, off the request path: the upload runs in a daemon thread, never
raises into the close, and a machine with no credentials (every laptop) logs
one line per session and moves on. The S3 client, bucket and region are the
video module's, so there is exactly one place that says where study data goes.
"""
from __future__ import annotations

import json
import logging
import os
import threading
import time
from pathlib import Path
from typing import Callable, Iterable, Optional

log = logging.getLogger(__name__)

#: What is archived, in upload order. Small files first, so a partial archive
#: still has the record a rater reads.
ARCHIVE_FILES = ("manifest.json", "record.json", "events.jsonl")
ARCHIVE_GLOBS = ("*.wav",)

ENABLED_ENV = "ARCHIVE_SESSIONS_TO_S3"


def enabled() -> bool:
    """On unless ARCHIVE_SESSIONS_TO_S3 says 0/false/no."""
    return os.getenv(ENABLED_ENV, "1").strip().lower() not in ("0", "false", "no", "off")


def files_to_archive(session_dir: Path) -> list[Path]:
    out = [session_dir / n for n in ARCHIVE_FILES if (session_dir / n).exists()]
    for pat in ARCHIVE_GLOBS:
        out.extend(sorted(p for p in session_dir.glob(pat) if p.is_file()))
    return out


def archive_session(session_dir: Path, *, client=None, bucket: Optional[str] = None,
                    prefix: Optional[str] = None) -> dict:
    """Upload the session's files; write and return archive.json. Never raises."""
    from . import video  # one home for bucket, region and client

    session_dir = Path(session_dir)
    bucket = bucket or video.BUCKET
    prefix = prefix if prefix is not None else f"encounters/{session_dir.name}/"
    result = {"at": time.time(), "bucket": bucket, "prefix": prefix,
              "uploaded": [], "failed": [], "ok": False}
    try:
        client = client or video._client()
        for p in files_to_archive(session_dir):
            key = prefix + p.name
            try:
                client.upload_file(str(p), bucket, key)
                result["uploaded"].append(p.name)
            except Exception as exc:  # noqa: BLE001, one file must not stop the rest
                result["failed"].append({"file": p.name, "error": f"{type(exc).__name__}: {str(exc)[:160]}"})
        result["ok"] = bool(result["uploaded"]) and not result["failed"]
    except Exception as exc:  # noqa: BLE001, no credentials, no network, no bucket
        result["error"] = f"{type(exc).__name__}: {str(exc)[:160]}"
    try:
        (session_dir / "archive.json").write_text(json.dumps(result, indent=2), encoding="utf-8")
    except OSError:
        log.exception("could not write archive.json for %s", session_dir.name)
    if result["ok"]:
        log.info("archived %s: %d files -> s3://%s/%s", session_dir.name,
                 len(result["uploaded"]), bucket, prefix)
    else:
        log.warning("archive of %s incomplete: %s", session_dir.name,
                    result.get("error") or result["failed"])
    return result


def archive_session_later(session_dir: Path, *, runner: Callable = None) -> Optional[threading.Thread]:
    """Archive in a daemon thread so a session close returns at once.

    Returns the thread (tests join it), or None when archiving is off.
    """
    if not enabled():
        return None
    fn = runner or archive_session
    t = threading.Thread(target=fn, args=(Path(session_dir),), name=f"archive-{Path(session_dir).name}", daemon=True)
    t.start()
    return t
