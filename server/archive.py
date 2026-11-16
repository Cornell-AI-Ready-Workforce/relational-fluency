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

# One upload pass per session at a time. The close archive and the record
# refresh below both copy record.json; unserialised, the close pass could read
# the old record, lose the race, and land it over the refreshed one.
_locks: dict = {}
_locks_guard = threading.Lock()


def _session_lock(session_dir: Path) -> threading.Lock:
    with _locks_guard:
        return _locks.setdefault(Path(session_dir).name, threading.Lock())


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
    with _session_lock(session_dir):
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


def refresh_record(session_dir: Path, *, client=None, bucket: Optional[str] = None,
                   prefix: Optional[str] = None) -> dict:
    """Re-upload the small record files after they changed. Never raises.

    The close archive runs before the page confirms its webcam upload, so the
    archived record.json and events.jsonl said "no video" for every encounter
    whose recording the page uploaded itself, while the object sat beside them
    in the bucket. The confirm routes rebuild record.json on disk and then call
    this, so the archived copy says what the disk copy says.

    Only ARCHIVE_FILES, not the WAVs: those do not change after close. The
    outcome is added to archive.json under "refreshed"; the close archive's own
    result is left as it was.
    """
    from . import video

    session_dir = Path(session_dir)
    bucket = bucket or video.BUCKET
    prefix = prefix if prefix is not None else f"encounters/{session_dir.name}/"
    result = {"at": time.time(), "uploaded": [], "failed": [], "ok": False}
    with _session_lock(session_dir):
        try:
            client = client or video._client()
            for name in ARCHIVE_FILES:
                p = session_dir / name
                if not p.exists():
                    continue
                try:
                    client.upload_file(str(p), bucket, prefix + name)
                    result["uploaded"].append(name)
                except Exception as exc:  # noqa: BLE001, one file must not stop the rest
                    result["failed"].append({"file": name, "error": f"{type(exc).__name__}: {str(exc)[:160]}"})
            result["ok"] = bool(result["uploaded"]) and not result["failed"]
        except Exception as exc:  # noqa: BLE001, no credentials, no network, no bucket
            result["error"] = f"{type(exc).__name__}: {str(exc)[:160]}"
        try:
            path = session_dir / "archive.json"
            try:
                current = json.loads(path.read_text(encoding="utf-8"))
                if not isinstance(current, dict):
                    current = {}
            except (OSError, ValueError):
                current = {}
            current["refreshed"] = result
            path.write_text(json.dumps(current, indent=2), encoding="utf-8")
        except OSError:
            log.exception("could not write archive.json for %s", session_dir.name)
    if not result["ok"]:
        log.warning("record refresh of %s incomplete: %s", session_dir.name,
                    result.get("error") or result["failed"])
    return result


def refresh_record_later(session_dir: Path, *, runner: Callable = None) -> Optional[threading.Thread]:
    """refresh_record in a daemon thread; None when archiving is off."""
    if not enabled():
        return None
    fn = runner or refresh_record
    t = threading.Thread(target=fn, args=(Path(session_dir),),
                         name=f"archive-refresh-{Path(session_dir).name}", daemon=True)
    t.start()
    return t
