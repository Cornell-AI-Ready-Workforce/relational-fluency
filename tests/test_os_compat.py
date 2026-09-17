"""Guards for the places where the OS, not the code, decides what happens.

The study is fielded to the public over CloudResearch, so the server may be run
on Linux (the pinned python:3.12-slim container), on a researcher's Mac, or on a
Windows box during a pilot — and the three disagree about two things that this
platform builds identifiers out of:

  * what a filename MEANS. Windows and a default macOS APFS volume match path
    components case-insensitively, silently strip a trailing "." or " ", and
    treat "nul" (and, depending on the build, con/aux/com1/lpt1) as a character
    device that swallows writes. Linux does none of that.
  * whether a file can be RENAMED over. POSIX rename() succeeds no matter who
    holds the file open; Windows fails it with PermissionError while any handle
    is open on either side, which is a live race here because FastAPI serves the
    sync routes that read manifest.json from a threadpool while the event loop
    closes sessions.

Each test below pins one of those, so the behaviour is the same on all three
platforms rather than the same as whichever platform the author was using.
"""
from __future__ import annotations

import importlib
import json
import sqlite3
from pathlib import Path

import pytest

import server.storage as storage

REPO_ROOT = Path(__file__).resolve().parent.parent

# The exact shape session.new_session_id mints, and the spellings of it that
# Windows/macOS resolve to the same directory while Linux does not.
GOOD_ID = "s_1772460300_44c9a2"
ALIASES = [
    GOOD_ID.upper(),          # case-insensitive lookup on Windows/APFS
    "S_1772460300_44c9a2",    # one character of it is enough
    GOOD_ID + ".",            # Windows strips a trailing dot
    GOOD_ID + " ",            # ...and a trailing space
    GOOD_ID + "\\x",          # backslash is a separator on Windows only
    GOOD_ID + "/x",
    "nul",                    # a Windows character device
    "",
]


# ---------- the shared identifier rules ----------

def test_is_safe_path_component_refuses_what_windows_would_rewrite():
    assert storage.is_safe_path_component("S1A")
    assert storage.is_safe_path_component("missed_deadlines")
    for bad in ("", ".", "..", "a/b", "a\\b", "a:b", "trailing.", "trailing ",
                "nul", "NUL", "con", "Aux", "com1", "lpt9", "nul.yaml"):
        assert not storage.is_safe_path_component(bad), bad


# ---------- the Windows rename window ----------

def test_replace_with_retry_survives_a_transient_permission_error(tmp_path, monkeypatch):
    """The Windows case: a reader (or Defender) holds the file for a moment.

    Simulated rather than reproduced with a real handle, so the guard runs on
    Linux and macOS too — where os.replace never raises and the loop is inert.
    """
    calls = {"n": 0}
    real = storage.os.replace

    def flaky(src, dst):
        calls["n"] += 1
        if calls["n"] < 3:
            raise PermissionError(32, "The process cannot access the file")
        return real(src, dst)

    monkeypatch.setattr(storage.os, "replace", flaky)
    monkeypatch.setattr(storage.time, "sleep", lambda _s: None)
    tmp, dest = tmp_path / "x.tmp", tmp_path / "x.json"
    tmp.write_text("{}", encoding="utf-8")
    storage.replace_with_retry(tmp, dest)
    assert calls["n"] == 3
    assert dest.read_text(encoding="utf-8") == "{}"


def test_replace_with_retry_still_raises_when_the_window_never_closes(tmp_path, monkeypatch):
    """A caller that must know still finds out; it is not swallowed."""
    def always(src, dst):
        raise PermissionError(5, "Access is denied")

    monkeypatch.setattr(storage.os, "replace", always)
    monkeypatch.setattr(storage.time, "sleep", lambda _s: None)
    tmp = tmp_path / "x.tmp"
    tmp.write_text("{}", encoding="utf-8")
    with pytest.raises(PermissionError):
        storage.replace_with_retry(tmp, tmp_path / "x.json", attempts=3)


def test_no_atomic_writer_renames_without_the_retry():
    """The helper exists so every writer uses it; a bare os.replace is the bug.

    Every module under server/ is scanned rather than a hand-kept list. The list
    was how ratings.py — the writer that stores a rater's completed 22-item
    submission, the one write whose loss costs a human being their work — sat
    with a bare os.replace while this test passed, and it was excluded BY NAME
    with a comment saying so. A named exclusion in a guard is a hole with a
    label on it; a scan of the whole package has no holes to label, and it also
    catches the next writer somebody adds.
    """
    offenders = {}
    for path in sorted((REPO_ROOT / "server").rglob("*.py")):
        src = path.read_text(encoding="utf-8")
        bare = [ln for ln in src.splitlines()
                if "os.replace(" in ln and "def replace_with_retry" not in ln]
        if bare:
            offenders[str(path.relative_to(REPO_ROOT)).replace("\\", "/")] = bare
    # The one legitimate call is inside the helper itself, in storage.py.
    assert list(offenders) == ["server/storage.py"], offenders
    assert len(offenders["server/storage.py"]) == 1, offenders


# ---------- SessionStore ----------

@pytest.fixture()
def store_env(tmp_path, monkeypatch):
    """server.storage bound to an empty DATA_DIR (the tests/test_raters.py idiom)."""
    monkeypatch.setenv("DATA_DIR", str(tmp_path / "data"))
    importlib.reload(storage)
    storage.init_storage()
    yield storage
    # Put the ambient DATA_DIR back for the modules that read it at import.
    monkeypatch.undo()
    importlib.reload(storage)


def _store(mod, session_id=GOOD_ID):
    return mod.SessionStore(
        session_id, scenario="test_scenario", model="m",
        participant_id=None, capture_audio=False,
    )


def test_close_finishes_even_when_the_manifest_write_loses_the_rename_race(
        store_env, monkeypatch):
    """A failed manifest write must not take the record and the DB row with it.

    On Windows the manifest rename can fail outright while a console poll holds
    manifest.json open. Unguarded, that skipped BOTH write_record() and the
    UPDATE, leaving an encounter with intact audio that reads as still 'active'
    and carries no record.json — invisible to /api/encounters and to the rater
    packet builder.
    """
    st = _store(store_env)
    st.event("user_turn", text="hello")

    real_write = store_env.SessionStore._write_manifest

    def fail_on_close(self, *, status, ended_at=None, n_turns=0):
        if status == "closed":
            raise PermissionError(5, "Access is denied")
        return real_write(self, status=status, ended_at=ended_at, n_turns=n_turns)

    monkeypatch.setattr(store_env.SessionStore, "_write_manifest", fail_on_close)
    st.close(n_turns=1)

    assert (st.dir / "record.json").is_file(), "the analysis record was skipped"
    with sqlite3.connect(store_env.DB_PATH) as conn:
        row = conn.execute("SELECT status FROM sessions WHERE id = ?",
                           (GOOD_ID,)).fetchone()
    assert row and row[0] == "closed", "the encounter still reads as active"
    # The manifest is the one thing allowed to be stale.
    assert json.loads((st.dir / "manifest.json").read_text(
        encoding="utf-8"))["status"] == "active"


def test_the_record_is_written_through_a_temp_file(store_env):
    """record.json is read by the console and the packet builder while sessions
    close, so it is renamed into place rather than truncated in place."""
    st = _store(store_env)
    st.event("user_turn", text="hello")
    st.close(n_turns=1)
    assert (st.dir / "record.json").is_file()
    assert not (st.dir / "record.json.tmp").exists()
    assert json.loads((st.dir / "record.json").read_text(encoding="utf-8"))


# ---------- the routes and the readers ----------

def test_session_dir_refuses_every_alias_of_a_real_encounter(tmp_path, monkeypatch):
    """The route-level half of the same rule.

    Each alias below opens the real directory on Windows and on a default macOS
    volume, and 404s on Linux. They are all refused now, so the three platforms
    agree — and so one encounter cannot be addressed under several names.
    """
    import server.app as appmod
    from fastapi import HTTPException

    sessions = tmp_path / "sessions"
    (sessions / GOOD_ID).mkdir(parents=True)
    monkeypatch.setattr(appmod, "SESSIONS_DIR", sessions)

    assert appmod._session_dir(GOOD_ID) == (sessions / GOOD_ID).resolve()
    for alias in ALIASES:
        with pytest.raises(HTTPException) as exc:
            appmod._session_dir(alias)
        assert exc.value.status_code == 400, alias


# ---------- scenario ids ----------

# ---------- line endings ----------

def test_the_scenario_map_generator_pins_its_newline():
    """CI regenerates docs/scenario-map.md on ubuntu-latest and diffs it.

    Regenerated on Windows without this, the file comes back CRLF, the diff is
    the whole file, and the freshness step fails on every subsequent PR with a
    message about a stale spec. Asserted against the source because running the
    generator would write into the repo's own docs/.
    """
    src = (REPO_ROOT / "tools" / "gen_scenario_map.py").read_text(encoding="utf-8")
    write = [ln for ln in src.splitlines() if "out_path.write_text(" in ln]
    assert write and all('newline=""' in ln for ln in write), write


def test_write_text_with_a_pinned_newline_really_does_emit_lf(tmp_path):
    """The mechanism the line above depends on, checked rather than assumed."""
    p = tmp_path / "map.md"
    p.write_text("a\nb\n", encoding="utf-8", newline="")
    assert p.read_bytes() == b"a\nb\n"
