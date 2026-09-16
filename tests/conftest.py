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

The second half of this file is the network guard. It has nothing to do with the
wave and everything to do with the same principle: a suite whose behaviour
depends on what the machine it runs on can reach is a suite nobody can trust.
See its own section below.
"""
from __future__ import annotations

import ipaddress
import os
import socket
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent

# `import server` has to work no matter how pytest was started.
#
# This repository has no root pytest.ini, pyproject.toml, setup.cfg or
# conftest.py, no tests/__init__.py, and `server` is never installed — it is
# imported as a top-level package off the repository root. Under pytest's
# default `prepend` import mode that root reaches sys.path only by accident of
# the invocation:
#
#   python -m pytest tests   -> sys.path[0] is '' (the cwd), so it works.
#   pytest tests             -> sys.path[0] is the console-script's own
#                               directory (…/Scripts, /usr/local/bin), the
#                               rootdir is never inserted, and pytest adds
#                               tests/ as the basedir instead.
#
# Every baseline this project has ever quoted was produced the first way.
# .github/workflows/ci.yml's "Platform test suite" step used the second, and
# `from server import …` at module scope then raised ModuleNotFoundError during
# collection — six modules on this tree (test_api_blockers, test_app,
# test_cohort_integrity, test_core_blockers, test_demo_door, test_demo_honesty),
# "Interrupted: N errors during collection", zero tests run. Not a Linux or a
# 3.11/3.13 effect: it reproduces identically on Windows/3.12, so it would have
# taken out all nine matrix cells at once while the adjacent `python -c "import
# server.app"` step stayed green, because `python -c` does put the cwd on the
# path.
#
# Eleven test modules already self-insert this exact line (tests/test_esci.py:25
# is the pattern). That worked only for the modules that did it and, worse, for
# any module collected AFTER one of them: the three blocker modules sort before
# test_esci.py alphabetically, which is precisely why they were the casualties.
# Doing it here, once, before any test module is imported, removes both the
# invocation dependency and the collection-order dependency.
#
# conftest.py rather than a root pytest.ini/pyproject.toml `pythonpath`: this
# fixes every invocation form (CI, a bare `pytest`, an IDE runner, a single
# module by path) without adding a file to the repository root or moving
# pytest's rootdir, which several tests here resolve paths against.
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

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


# --- the network guard --------------------------------------------------------
#
# Three rounds of this project have carried the rule "the tests make no network
# calls", and for three rounds the rule was enforced by instruction alone. It was
# not being kept. Measured on this branch, with every request refused locally and
# counted rather than sent: 3,120 attempted connections to non-loopback addresses
# across 152 tests in four modules. Two destinations, both of them real:
#
#   * the study bucket. video.exists() is a HEAD against
#     relational-fluency-study-data whenever an encounter has no local recording,
#     which is every synthetic encounter in the suite. With AWS credentials in
#     the environment those HEADs were SIGNED and SENT, and the only trace was
#     one line of stdout per call — "S3 HEAD failed for session ... 403" — in a
#     log nobody reads. Without credentials they were two connects to
#     169.254.169.254, the EC2 metadata service, which is unroutable off EC2, so
#     botocore waited out its timeout twice: 2.3 seconds per encounter.
#
#   * the model gateway and the study bucket again, at boot. `with
#     TestClient(app)` runs the lifespan, the lifespan runs run_preflights(),
#     and that is an httpx GET to gateway_base_url()/v1/models carrying
#     `Authorization: Bearer <the operator's real key>` — on this branch, to
#     https://api.ai.it.cornell.edu — followed by a head_bucket AND a
#     put_object of encounters/_preflight/startup-check.txt into the study
#     bucket. Every test in tests/test_app.py that opens a client did all
#     three. On a machine with working AWS credentials, running the test suite
#     wrote an object into an IRB bucket.
#
# None of this is a test of anything. All of it is a test suite reaching out of
# the machine it was started on, carrying live credentials, at the mercy of
# whatever the network happens to answer. A 403 is the pleasant version: in an
# egress-restricted CI these become connect timeouts, and today's slow suite is
# tomorrow's stuck build.
#
# So the rule is enforced here instead of asserted in a docstring.
#
# WHY THE REFUSAL AND THE FAILURE ARE TWO SEPARATE THINGS. The connect is refused
# with ConnectionRefusedError — an ordinary OSError, which is exactly what the
# code under test would see on a machine with no route out, so no error handling
# behaves differently under the guard than it does offline. But an OSError is
# also swallowable, and everything on these paths swallows it by design:
# llm.preflight() catches Exception and writes the message into a `detail` field,
# storage_preflight() is documented as never raising, video.exists() is
# contractually total and answers False. A guard that only raised would
# therefore be silent for precisely the callers it exists to catch. So the
# violation is also RECORDED, and pytest_runtest_call below turns the record
# into a test failure once the test body is over, where no `except Exception`
# can reach it.
#
# Loopback is allowed: several tests bind a local server and talk to it, and that
# is not what this is about.

#: Every non-loopback connection attempt, as (nodeid, "what"). Appended to from
#: whichever thread made the call — TestClient runs the app on its own asyncio
#: portal thread, so the offender is frequently not the main one.
_NETWORK_VIOLATIONS: list[tuple[str, str]] = []

#: The test the guard blames. Set by pytest_runtest_setup, which runs before any
#: fixture; anything recorded outside a test keeps this value and is reported at
#: the end of the run instead (see pytest_terminal_summary).
_CURRENT_TEST = "<collection / module import / teardown>"

_LOOPBACK_NAMES = frozenset({"", "localhost", "ip6-localhost", "ip6-loopback"})


def _is_loopback(host: object) -> bool:
    """True when this destination stays inside the machine.

    A bare name is resolved by NAME, not by resolving it — resolving it is
    itself the DNS query this guard is trying to prevent. Anything that is not
    an obvious loopback literal or an obvious loopback name is treated as
    outbound, which is the safe direction to be wrong in: a false positive is a
    loud, fixable test failure, and a false negative is a credential on the wire.
    """
    if host is None or host in _LOOPBACK_NAMES:
        # None is what a bind with AI_PASSIVE passes, and an empty host is the
        # same thing spelled differently. Neither addresses anything remote.
        return True
    text = str(host)
    try:
        return ipaddress.ip_address(text).is_loopback
    except ValueError:
        # Not a literal address. `foo.localhost` is reserved to loopback by
        # RFC 6761 and some fixtures use it for virtual-host routing.
        return text == "localhost" or text.endswith(".localhost")


def _refuse(what: str, host: object, port: object) -> None:
    """Record the attempt, then refuse it the way an unrouted network does."""
    _NETWORK_VIOLATIONS.append(
        (_CURRENT_TEST, f"{what} to {host}:{port}")
    )
    # ASCII only, here and in the failure message below. These strings are
    # printed to a console that is cp1252 on a stock Windows install, where an
    # em dash arrives as a replacement character in the middle of the one line
    # the reader is trying to act on.
    raise ConnectionRefusedError(
        f"tests/conftest.py blocked an outbound {what} to {host}:{port}. "
        f"The test suite must not talk to anything but loopback; this one "
        f"was reaching the network from {_CURRENT_TEST}. Stub the client "
        f"(botocore's own exception types, an httpx transport) rather than "
        f"letting the test discover what today's network answers."
    )


_real_connect = socket.socket.connect
_real_connect_ex = socket.socket.connect_ex
_real_getaddrinfo = socket.getaddrinfo


def _address_parts(address: object) -> tuple[object, object]:
    # AF_INET is (host, port); AF_INET6 is (host, port, flowinfo, scope_id).
    if isinstance(address, tuple) and len(address) >= 2:
        return address[0], address[1]
    return address, None


def _guarded_connect(self, address):
    # The family test is what keeps AF_UNIX and anything exotic out of the
    # guard's way: those cannot leave the machine, so they are none of its
    # business, and refusing them would break a socketpair for no reason.
    if self.family in (socket.AF_INET, socket.AF_INET6):
        host, port = _address_parts(address)
        if not _is_loopback(host):
            _refuse("connect", host, port)
    return _real_connect(self, address)


def _guarded_connect_ex(self, address):
    if self.family in (socket.AF_INET, socket.AF_INET6):
        host, port = _address_parts(address)
        if not _is_loopback(host):
            # connect_ex reports by return code rather than by raising, but this
            # is not really a connect_ex any more: the point is to fail the test,
            # and returning WSAECONNREFUSED would let a polling caller loop.
            _refuse("connect_ex", host, port)
    return _real_connect_ex(self, address)


def _guarded_getaddrinfo(host, port, *args, **kwargs):
    """DNS is a network call too, and it is the one that hangs.

    Blocking only connect() would still let every lookup for
    relational-fluency-study-data.s3.amazonaws.com leave the machine, and a
    resolver that cannot be reached is where an egress-restricted CI actually
    stops — before there is a connect to refuse. Catching it here also makes the
    failure message name the HOST rather than whichever of S3's rotating
    addresses DNS happened to hand back, which is the difference between a
    message an engineer can act on and a message with an IP in it.
    """
    if not _is_loopback(host):
        _NETWORK_VIOLATIONS.append(
            (_CURRENT_TEST, f"DNS lookup of {host}:{port}")
        )
        raise socket.gaierror(
            socket.EAI_NONAME,
            f"tests/conftest.py blocked a DNS lookup of {host!r} "
            f"(port {port}) from {_CURRENT_TEST}. The test suite resolves "
            f"nothing but loopback."
        )
    return _real_getaddrinfo(host, port, *args, **kwargs)


# Installed at import rather than inside the fixture. conftest is imported before
# any test module, so this also covers collection — a module body that opened a
# socket while pytest was merely importing it would otherwise be outside every
# fixture's reach, and module bodies in this suite already do enough at import
# time to be worth covering.
socket.socket.connect = _guarded_connect
socket.socket.connect_ex = _guarded_connect_ex
socket.getaddrinfo = _guarded_getaddrinfo


#: How many violations had been recorded when the current test started. Set
#: before any fixture runs, so a connection made while building a fixture — which
#: is where the study-bucket HEADs actually happened — is charged to the test
#: that asked for the fixture.
_SNAPSHOT = 0


def pytest_runtest_setup(item):
    global _CURRENT_TEST, _SNAPSHOT
    _CURRENT_TEST = item.nodeid
    _SNAPSHOT = len(_NETWORK_VIOLATIONS)


@pytest.fixture(autouse=True)
def no_outbound_network():
    """The guard, re-armed for every test.

    Autouse, so it costs nothing to remember and cannot be opted out of by
    forgetting one. Its job at this point is narrow but real: put the guard back
    if a test replaced socket.socket.connect and did not restore it. A suite that
    silently loses its own guard partway through a run is back where it started,
    and the loss would be invisible — the tests after it would simply pass.
    """
    if socket.socket.connect is not _guarded_connect:
        socket.socket.connect = _guarded_connect
    if socket.getaddrinfo is not _guarded_getaddrinfo:
        socket.getaddrinfo = _guarded_getaddrinfo
    yield


@pytest.hookimpl(wrapper=True)
def pytest_runtest_call(item):
    """Turn a recorded attempt into this test's failure.

    Not in the fixture's teardown, though that is where it started. A failure
    raised at teardown is reported as an ERROR next to a test that pytest still
    says PASSED, and "1 passed, 1 error" about one test is the sort of line a
    reader resolves by ignoring it. Raised here it is a plain failure, once,
    against the test that did it.

    Nor at the moment of the call: llm.preflight() catches Exception and writes
    the message into a `detail` field, video.exists() is contractually total and
    answers False. Both of the paths this guard exists to catch would swallow it.
    By the time this runs the evidence is in a list, out of reach of anybody's
    try block.

    A test that failed on its own account keeps its own failure — the yield
    re-raises before this gets a look in — because its reason is the more useful
    one and the network attempt is usually a symptom of it.
    """
    result = yield
    mine = _NETWORK_VIOLATIONS[_SNAPSHOT:]
    if mine:
        # Deduplicated with the count kept: botocore retries, so one HEAD is
        # three lines, and a list of forty identical entries buries the one fact
        # the reader needs — which host, which port.
        tally: dict[str, int] = {}
        for _, what in mine:
            tally[what] = tally.get(what, 0) + 1
        detail = "\n".join(
            f"  {n:>4} x {what}" for what, n in sorted(tally.items())
        )
        pytest.fail(
            f"{item.nodeid} attempted {len(mine)} outbound network "
            f"call(s):\n{detail}\n"
            f"Nothing in this suite may reach a non-loopback address. Put a "
            f"stub in front of the client - tests/test_phase2_blockers.py's "
            f"_StubS3 is the pattern: botocore's own ClientError / "
            f"BotoCoreError types, so the code under test takes the branch it "
            f"really takes.",
            pytrace=False,
        )
    return result


def pytest_terminal_summary(terminalreporter):
    """Report attempts made outside any test, which no fixture can fail.

    Module import and collection run before the first test's setup, so a socket
    opened there is recorded against a placeholder and would otherwise vanish.
    It is rare — nothing does it on this branch today — which is exactly why it
    needs saying out loud rather than being left to be noticed.
    """
    stray = [(who, what) for who, what in _NETWORK_VIOLATIONS
             if not who.startswith("tests")]
    if not stray:
        return
    terminalreporter.write_sep("=", "outbound network calls outside any test",
                               red=True)
    for who, what in stray:
        terminalreporter.write_line(f"  {who}: {what}")


class OfflineBucket:
    """The study bucket, answered in this process. No socket, ever.

    `objects` maps object key to byte count; anything not in it is absent.
    head_object answers the two ways S3 really answers - a ContentLength, or a
    404 ClientError - because video.head_video branches on botocore's own error
    shape and reads the service's code out of `response`. A stub that raised
    some bespoke exception would exercise a path no deployment ever takes and
    would go on passing after head_video stopped classifying anything correctly.

    `fail_with` puts a BotoCoreError in the same place instead, for the states
    that are about AWS refusing to answer at all rather than about the object.

    The same shape tests/test_phase2_blockers.py builds for itself; it lives
    here as well because two more modules need it and a stub that is copied is a
    stub that drifts. `heads` is the evidence a test needs to assert that the
    bucket was NOT asked - which is the whole claim behind preferring a local
    recording.
    """

    def __init__(self, objects=None, fail_with=None):
        self.objects = dict(objects or {})
        self.fail_with = fail_with
        self.heads: list = []

    def head_object(self, Bucket=None, Key=None, **kwargs):  # noqa: N803 - boto3's own kwarg names
        self.heads.append(Key)
        if self.fail_with is not None:
            raise self.fail_with
        size = self.objects.get(Key)
        if size is None:
            from botocore.exceptions import ClientError
            raise ClientError(
                {"Error": {"Code": "404", "Message": "Not Found"}}, "HeadObject")
        return {"ContentLength": size, "ContentType": "video/webm"}


@pytest.fixture(autouse=True)
def rating_code_secret(monkeypatch):
    """Pin the rating-code HMAC key for every test, and undo it afterwards.

    runs._run_code_secret() prefers $RUN_CODE_SECRET and, failing that, MINTS a
    32-byte key and writes it to DATA_DIR/.run_code_secret. So an unpinned suite
    leaves a persisted production secret behind as a side effect of running the
    tests — in the repository's own data directory when DATA_DIR is unset, and
    in a researcher's collection wave when it is pointed at one. There is
    already a .run_code_secret sitting in this repo's data/ and another in the
    gate's fixture wave, both left by exactly this.

    It also has to be pinned for the tests to mean anything: several modules
    assert that a rating code is stable across calls and unique across
    encounters, and an unpinned key makes that a statement about whichever file
    happened to be on disk.

    This lived as `os.environ.setdefault(...)` in two test modules' bodies, and
    both halves of that were wrong. setdefault writes to the PROCESS, so which
    secret the run used depended on which module pytest imported first and
    nothing said so; and `setdefault` yields to an ambient value, so a developer
    with a real RUN_CODE_SECRET exported silently ran a different suite from
    everyone else. setenv is unconditional, and monkeypatch undoes it.

    A test that needs a different key sets one itself — monkeypatch applies in
    order, so theirs wins (tests/test_rater_packet.py's
    test_rating_code_is_keyed_not_merely_hashed depends on that).
    """
    monkeypatch.setenv("RUN_CODE_SECRET", "test-only-rating-code-secret")


@pytest.fixture(autouse=True)
def offline_boot_preflights(monkeypatch):
    """The two credentialed seams the app probes at boot, answered offline.

    `with TestClient(app)` runs the lifespan, and the lifespan runs
    run_preflights(). That is correct behaviour for a deployment and wrong for a
    test process, because both checks are real network calls made with real
    credentials:

      * llm.preflight() GETs {gateway}/v1/models with
        `Authorization: Bearer <whatever key this machine has configured>`. On
        this branch that is https://api.ai.it.cornell.edu — so every test that
        opened a client sent the operator's live gateway key to Cornell's
        gateway. 111 tests in tests/test_app.py do.
      * video.storage_preflight() head_buckets the study bucket and then PUTS
        `encounters/_preflight/startup-check.txt` INTO IT. Running the test
        suite on a machine with working AWS credentials writes an object into an
        IRB bucket. The marker is harmless; "running the tests wrote to the
        study bucket" is not a sentence this project should be able to say.

    Both are replaced with the answer an offline machine honestly gives, and
    both are produced by the real code rather than hand-written: the storage
    check is the real storage_preflight driven by a client that raises
    botocore's own NoCredentialsError, so its result has the shape and the
    error_code a credential-less deployment really produces, and it keeps
    working if that shape changes. The gateway check has no client seam to
    inject, so it returns the failed-probe shape over llm's own provenance().

    A test that supplies its OWN preflight stand-in still wins: monkeypatch
    applies in order and theirs is applied after this
    (tests/test_api_blockers.py's test_startup_is_what_runs_the_preflights
    depends on that). And a caller that passes an explicit `client` —
    storage_preflight(client=stub), which is how that function's own tests drive
    it — is delegated to the real implementation untouched, because a caller
    that brought its own client was never the one reaching the network.

    Nothing is imported here. Patching only what is ALREADY in sys.modules keeps
    this fixture from being the thing that first imports server.app, which would
    bind server.storage's DATA_DIR to whatever the environment held at the start
    of the session — possibly the repository's own data directory, which is the
    hazard tests/test_ratings.py and tests/test_phase2_blockers.py each work
    around at their own import. A run that never touches the app has nothing
    here to patch and pays nothing.

    THIS IS A WORKAROUND SITED IN THE WRONG FILE. The fix belongs in whatever
    builds the TestClient: tests/test_video_route.py already declines to run the
    lifespan for exactly this reason and says so in a comment. It lives here
    because the alternative was 111 red tests in a module this change does not
    own.
    """
    import sys as _sys

    appmod = _sys.modules.get("server.app")
    video = _sys.modules.get("server.video")
    llm = _sys.modules.get("server.llm")
    if appmod is None or video is None or llm is None:
        return

    from botocore.exceptions import NoCredentialsError

    def _offline_gateway_probe():
        return dict(
            llm.provenance(), ok=False,
            detail="offline: tests/conftest.py makes no network calls",
        )

    class _NoCredentials:
        """A boto3 client with nothing to sign with. Raises where boto3 raises.

        No `_request_signer`, deliberately: storage_preflight reads that through
        its own try/except to decide `credentials`, and the honest answer on a
        machine with no credentials is False.
        """

        def head_bucket(self, **kwargs):
            raise NoCredentialsError()

        def put_object(self, **kwargs):
            raise NoCredentialsError()

    real_storage_preflight = video.storage_preflight

    def _offline_storage_probe(client=None):
        return real_storage_preflight(
            client=client if client is not None else _NoCredentials())

    monkeypatch.setattr(appmod, "_preflight", _offline_gateway_probe,
                        raising=False)
    monkeypatch.setattr(video, "storage_preflight", _offline_storage_probe,
                        raising=False)


@pytest.fixture
def offline_bucket(monkeypatch):
    """An empty study bucket, in this process, for the duration of one test.

    Opt-in rather than autouse. Every media test in the suite needs it — since
    the packet started asking storage rather than reading a browser's receipt,
    video.exists() is a HEAD against relational-fluency-study-data for any
    encounter with no local recording — but installing it on every test by
    default would silently displace the stubs that tests build for themselves,
    and a test whose S3 behaviour is quietly supplied by a conftest is a test
    whose author cannot see what it is asserting.

    `video._s3` rather than `video._client`, deliberately: _client() returns the
    cached global, so patching the global leaves both seams free for a test that
    wants its own — one that replaces `_client` wholesale (tests/
    test_phase2_blockers.py) and one that replaces `_s3` (tests/
    test_video_route.py) both still win, because monkeypatch applies theirs
    after this.
    """
    from server import video
    stub = OfflineBucket()
    monkeypatch.setattr(video, "_s3", stub)
    return stub


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
