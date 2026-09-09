"""Where the recorded wave lives — resolved once, for the whole suite.

Round one wrote the author's own scratchpad path, session UUID and all, into
four test files as *the* location of the fixture wave. That is not cosmetic.
On anyone else's machine those paths are absent, so the wave-backed tests
either skip silently (coverage disappears without a sound) or, where the guard
was a bare assertion, fail with a path error that looks like a real defect.
A suite that only runs on one laptop on one day is a suite nobody runs, and the
whole point of the 675 tests is that the next person can change this code
safely.

So: one convention, used everywhere, and no absolute path anywhere in tests/.

  1. An environment variable naming a directory that holds a `sessions/`
     subdirectory of recorded encounters. Three spellings grew up in this
     suite and all three are honoured, in this order:

         RF_FIXTURE_DIR   RF_FIXTURE   DATA_DIR

  2. Failing that, a wave checked into the repository at tests/data/wave.
     Nothing is checked in today (see WAVE_CANDIDATES below), but the hook is
     here so that landing one makes the wave-backed guards run unconditionally
     on every machine, which is where this should end up.

  3. Failing that, **skip** — with a message that says exactly how to point the
     suite at a wave. Never a path error, and never a bare assertion failure.

Two rules the resolution deliberately enforces:

*   A directory only counts as a wave if `sessions/` actually holds at least
    one `*/record.json`. `server.storage` creates `DATA_DIR/sessions` the
    moment it is imported, so "DATA_DIR points at a fresh temp directory" —
    a normal first local run — used to leave an empty-but-existing sessions/
    behind and turn five tests red over a missing fixture rather than skipping
    them. Presence of the directory is not evidence of a wave; a record is.

*   DATA_DIR is read as a source spelling but is never *written* by this file.
    DATA_DIR is where the application writes; the resolved wave is only ever
    read, or copied first. Point DATA_DIR at a copy, never at a collection
    wave you care about.

The resolved answer is exported back into RF_FIXTURE_DIR and RF_FIXTURE at
import time — before pytest imports any test module — because several modules
read only one of the spellings at import time and would otherwise disagree
with each other about whether a wave is present. tests/test_runner_blockers.py
is the case that matters: it parametrises the identity-rewrite regression
guard over `_fixture_scenarios()`, which reads RF_FIXTURE_DIR alone, and an
empty parametrisation collects zero cases and reports neither skip nor
failure. Exporting the resolved path is what makes that guard run for someone
who set DATA_DIR, or RF_FIXTURE, rather than the one spelling it happens to
look for.
"""
from __future__ import annotations

import os
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent

#: Environment spellings already in use across the suite, in precedence order.
WAVE_ENV_VARS = ("RF_FIXTURE_DIR", "RF_FIXTURE", "DATA_DIR")

#: Repo-relative fallbacks, tried in order when no variable names a wave.
#: `data/` at the repo root is deliberately NOT here: that is the application's
#: live DATA_DIR and is gitignored as participant PII. A wave the suite adopts
#: on its own must be one somebody deliberately checked in.
WAVE_CANDIDATES = (
    REPO_ROOT / "tests" / "data" / "wave",
    REPO_ROOT / "tests" / "data",
)

NO_WAVE_REASON = (
    "no recorded wave available. Point the suite at one with "
    "RF_FIXTURE_DIR=<dir> (the directory must contain sessions/<id>/record.json); "
    "RF_FIXTURE and DATA_DIR are accepted too. A wave checked in at "
    "tests/data/wave is picked up with no variable set. "
    "Use a COPY — the suite writes under DATA_DIR."
)


def _is_wave(path: Path | None) -> bool:
    """True only if `path` holds at least one recorded encounter.

    Not `sessions/` exists — `server.storage` makes that directory on import,
    so an empty DATA_DIR looks wave-shaped within milliseconds of the first
    import. A wave is a directory with a record in it.
    """
    if path is None:
        return False
    try:
        sessions = path / "sessions"
        if not sessions.is_dir():
            return False
        return next(sessions.glob("*/record.json"), None) is not None
    except OSError:  # unreadable, a dead symlink, a disconnected UNC share
        return False


def _resolve_wave() -> Path | None:
    for var in WAVE_ENV_VARS:
        raw = os.environ.get(var)
        if raw and _is_wave(Path(raw)):
            return Path(raw).resolve()
    for candidate in WAVE_CANDIDATES:
        if _is_wave(candidate):
            return candidate.resolve()
    return None


#: The one answer, resolved before any test module is imported.
WAVE: Path | None = _resolve_wave()

if WAVE is not None:
    # Assigned, not setdefault: one resolved answer, so a module reading only
    # RF_FIXTURE_DIR and a module reading only DATA_DIR cannot disagree about
    # whether this run has a wave. Written as str(Path) so the separators are
    # this platform's, which is what os.getenv consumers rebuild a Path from.
    os.environ["RF_FIXTURE_DIR"] = str(WAVE)
    os.environ["RF_FIXTURE"] = str(WAVE)


# --- fixtures ----------------------------------------------------------------

@pytest.fixture(scope="session")
def optional_wave() -> Path | None:
    """The wave, or None. Never skips — for tests that work either way."""
    return WAVE


@pytest.fixture(scope="session")
def wave_dir() -> Path:
    """The wave, skipping the test when there is not one on this machine."""
    if WAVE is None:
        pytest.skip(NO_WAVE_REASON)
    return WAVE


@pytest.fixture(scope="session")
def wave_sessions(wave_dir: Path) -> Path:
    """`<wave>/sessions`, guaranteed to hold at least one record.json."""
    return wave_dir / "sessions"


@pytest.fixture(scope="session")
def wave_encounters(wave_sessions: Path) -> list[str]:
    """Encounter ids in the wave, oldest-first by id, at least one."""
    return sorted(p.parent.name for p in wave_sessions.glob("*/record.json"))


@pytest.fixture(scope="session")
def wave_index_db(wave_dir: Path) -> Path:
    """`<wave>/index.db`. A wave without one skips rather than half-runs."""
    db = wave_dir / "index.db"
    if not db.is_file():
        pytest.skip(f"the wave at {wave_dir} has no index.db")
    return db
