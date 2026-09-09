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

def test_the_minted_session_id_is_accepted_and_every_alias_of_it_is_not():
    """One encounter, one spelling — decided by the id's shape, not by the disk.

    rater_packet.rating_code HMACs the session id STRING. If two spellings of
    one encounter can both be resolved, that encounter is issued two different
    RC- codes, and the blinded handle a rater quotes and a researcher joins on
    stops being one-per-encounter.
    """
    assert storage.valid_session_id(GOOD_ID)
    for alias in ALIASES:
        assert not storage.valid_session_id(alias), alias


def test_is_safe_path_component_refuses_what_windows_would_rewrite():
    assert storage.is_safe_path_component("S1A")
    assert storage.is_safe_path_component("missed_deadlines")
    for bad in ("", ".", "..", "a/b", "a\\b", "a:b", "trailing.", "trailing ",
                "nul", "NUL", "con", "Aux", "com1", "lpt9", "nul.yaml"):
        assert not storage.is_safe_path_component(bad), bad


def test_every_module_that_turns_a_session_id_into_a_path_uses_the_one_rule():
    """A second, looser transcription of the rule is how this drifted before.

    app.py, rater_packet.py and video.py each carried [A-Za-z0-9_-]{1,64}, which
    accepts uppercase; raters.py carried the strict one. Compared by pattern
    rather than by identity so a module reloaded by another test still counts.
    """
    import server.rater_packet as rater_packet
    import server.raters as raters
    import server.video as video

    for mod in (rater_packet, raters, video):
        assert mod._SESSION_ID_RE.pattern == storage.SESSION_ID_RE.pattern, mod.__name__


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

    ratings.py is deliberately not in this list — it belongs to another agent's
    file set and still carries a bare os.replace at the time of writing.
    """
    for rel in ("server/storage.py", "server/runs.py", "server/raters.py",
                "server/encounter_record.py"):
        src = (REPO_ROOT / rel).read_text(encoding="utf-8")
        bare = [ln for ln in src.splitlines()
                if "os.replace(" in ln and "def replace_with_retry" not in ln]
        if rel == "server/storage.py":
            # The one legitimate call is inside the helper itself.
            assert len(bare) == 1, bare
        else:
            assert not bare, (rel, bare)


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


def test_a_session_store_refuses_an_id_it_could_not_be_found_by(store_env):
    """Validated where the directory is minted, not only where it is read.

    Everything downstream — app._session_dir, the packet builder, the rater
    assignment — now insists on the minted shape, so a store created under any
    other one would record a paid participant's encounter where nothing could
    ever address it.
    """
    for bad in ("s_bad", GOOD_ID.upper(), GOOD_ID + ".", "nul"):
        with pytest.raises(ValueError):
            _store(store_env, bad)


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


def test_one_encounter_cannot_be_issued_two_rating_codes():
    import server.rater_packet as rater_packet

    code = rater_packet.rating_code(GOOD_ID)
    assert code.startswith("RC-")
    for alias in ALIASES:
        with pytest.raises(ValueError):
            rater_packet.rating_code(alias)


def test_a_video_key_cannot_be_minted_from_a_spelling_nothing_else_derives(
        tmp_path, monkeypatch):
    """S3 keys are case-sensitive on every platform; directory lookups are not.

    So on a Windows or macOS host, presign_upload's "does this session exist?"
    check passed for a wrong-cased id and then signed a PUT for
    encounters/S_.../webcam.webm — an object key no reader ever derives. The
    participant's webcam recording, which is the artefact raters score, would
    upload successfully and be unfindable.
    """
    import server.video as video

    assert video.video_key(GOOD_ID) == f"encounters/{GOOD_ID}/webcam.webm"

    sessions = tmp_path / "sessions"
    (sessions / GOOD_ID).mkdir(parents=True)
    monkeypatch.setattr(video, "SESSIONS_DIR", sessions)
    for alias in ALIASES:
        with pytest.raises(ValueError):
            video.video_key(alias)
        with pytest.raises(video.NoSuchSession):
            video.presign_upload(alias)
        assert video.upload_receipt(alias) is None
        assert video.playback_url(alias) is None


# ---------- scenario ids ----------

def test_a_scenario_is_found_only_under_its_own_spelling(tmp_path, monkeypatch):
    """A scenario id is stamped onto the recording and into the manifest.

    A wrong-cased participant link that loads the scenario on the researcher's
    Mac, 404s on the Linux container and labels the encounter with whichever
    spelling the URL carried is a data problem, not a cosmetic one.
    """
    import server.scenarios as scenarios

    (tmp_path / "S1A.yaml").write_text("id: S1A\n", encoding="utf-8")
    monkeypatch.setattr(scenarios, "SCENARIOS_DIR", tmp_path)
    monkeypatch.setattr(scenarios, "_legacy_by_id", dict)

    assert scenarios._find_scenario_file("S1A") == tmp_path / "S1A.yaml"
    for bad in ("s1a", "S1a", "S1A.", "S1A "):
        with pytest.raises(FileNotFoundError):
            scenarios._find_scenario_file(bad)


def test_a_scenario_id_that_names_a_windows_device_is_refused(tmp_path, monkeypatch):
    """"nul" matches [A-Za-z0-9_-] but is a character device on Windows: opening
    it succeeds and reads back nothing, so the scenario would load as empty
    rather than report itself missing."""
    import server.scenarios as scenarios

    monkeypatch.setattr(scenarios, "SCENARIOS_DIR", tmp_path)
    monkeypatch.setattr(scenarios, "_legacy_by_id", dict)
    for bad in ("nul", "CON", "aux", "com1", "lpt1"):
        with pytest.raises(FileNotFoundError):
            scenarios._find_scenario_file(bad)


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
