"""Portability guards for the documented commands, the build context, and CI.

Every finding these tests cover has the same shape: something worked on the
machine it was written on and failed silently, or fatally, somewhere else. The
study recruits participants from the public on Chrome, Firefox and Safari across
macOS, Windows and Linux, and researchers set up second machines to watch a
wave; "it works here" is not a property this repository can rely on.

The doc assertions deliberately look **inside fenced code blocks only**. The
prose in those same files now explains, at length, why `sed -i ''` and
`${REPO%%/*}` are wrong — so a naive grep of the whole file would fail on the
warnings written to prevent the bug. What matters is what a reader copies and
runs, and that is exactly the fenced content.

Scope note: only the five documents rewritten in this pass are checked
(README.md and the four runbooks under docs/). Widening the set to
infra/*.md and agents/README.md would be right, and is listed as follow-up
work rather than done here, because those files are owned elsewhere.
"""
from __future__ import annotations

import re
import struct
from pathlib import Path

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parent.parent

# The documents rewritten in the cross-platform pass.
PORTABLE_DOCS = [
    "README.md",
    "docs/DEPLOY.md",
    "docs/DEPLOY-AWS.md",
    "docs/OPERATIONS.md",
    "docs/RATING.md",
]

_FENCE = re.compile(r"^```(?P<lang>[A-Za-z0-9_+-]*)\s*$")


def _code_blocks(rel_path: str):
    """Yield (language, first_line_number, [lines]) for each fenced block.

    A fence with no info string closes the block it opened, so this is a simple
    two-state scan rather than anything that needs a markdown parser.
    """
    text = (REPO_ROOT / rel_path).read_text(encoding="utf-8")
    lang, start, buf = None, 0, []
    for lineno, line in enumerate(text.splitlines(), start=1):
        m = _FENCE.match(line)
        if m is None:
            if lang is not None:
                buf.append(line)
            continue
        if lang is None:
            lang, start, buf = m.group("lang").lower(), lineno, []
        else:
            yield lang, start, buf
            lang, buf = None, []


def _all_code_lines():
    """(doc, lineno, language, line) for every line inside a fenced block."""
    for doc in PORTABLE_DOCS:
        for lang, start, lines in _code_blocks(doc):
            for offset, line in enumerate(lines, start=1):
                yield doc, start + offset, lang, line


# --------------------------------------------------------------------------
# Documented commands
# --------------------------------------------------------------------------

# Each entry: (substring, why it cannot stand in a copy-pasteable block).
WINDOWS_FATAL = [
    (
        "&& source ",
        "`python -m venv .venv && source .venv/bin/activate` is a PARSE error in "
        "Windows PowerShell 5.1 — '&&' is not a valid statement separator there, "
        "so the whole line is refused before anything runs. `source` is also not "
        "a PowerShell command and there is no .venv/bin/ on Windows to source. "
        "Give Windows its own block instead.",
    ),
    (
        "sed -i ''",
        "macOS/BSD-only. GNU sed (every Linux box, and Git Bash on Windows) reads "
        "-i with its suffix attached, takes '' as the script and the s|...| "
        "expression as a filename, and exits 2 leaving the file untouched. In the "
        "release procedure the next line was `tofu apply`, which then re-applied "
        "the tag already pinned: a deploy that reports success and serves the old "
        "build to participants.",
    ),
    (
        "%%/*}",
        "`${REPO%%/*}` is bash parameter expansion. PowerShell parses it as a "
        "braced variable NAME ('REPO%%/*'), finds nothing, and expands it to the "
        "empty string with no error at all. cmd.exe has no such construct. Build "
        "the registry host from $ACCOUNT and $REGION instead.",
    ),
    (
        "$(openssl rand",
        "cmd.exe has no command substitution, so this stores the literal text "
        "'$(openssl rand -hex 32)' as SESSION_KEY — the only credential in the "
        "system, gating every participant recording and recruitment record — and "
        "nothing errors, because the system works. Generate with `python -c "
        "\"import secrets; print(secrets.token_urlsafe(32))\"` as a visible step "
        "and paste the result.",
    ),
    (
        "cd ~/relational",
        "`~` is not a path in cmd.exe, and this named a directory nothing in this "
        "repository creates (spelled three different ways across the docs). Say "
        "'from the repository root' instead.",
    ),
]


@pytest.mark.parametrize("needle,why", WINDOWS_FATAL, ids=[n for n, _ in WINDOWS_FATAL])
def test_no_windows_fatal_construct_in_any_documented_command(needle, why):
    hits = [
        f"{doc}:{lineno}: {line.strip()}"
        for doc, lineno, _lang, line in _all_code_lines()
        if needle in line
    ]
    assert not hits, f"{why}\n\nFound in:\n" + "\n".join(hits)


def test_python3_appears_only_as_the_pre_venv_bootstrap():
    """`python3` is not a reliable command name on Windows.

    The python.org installer creates python.exe and the `py` launcher, not
    python3.exe; Windows ships an App Execution Alias of that name that opens
    the Microsoft Store, and where it does resolve it resolves to the system
    interpreter rather than the activated virtualenv. Inside an activated venv
    `python` is the project interpreter on all three platforms, so `python3` is
    only correct in the one place a venv does not exist yet: creating it.
    """
    offenders = []
    for doc, lineno, _lang, line in _all_code_lines():
        if "python3" not in line:
            continue
        if "python3 -m venv" in line:
            continue  # the sanctioned pre-venv bootstrap, macOS/Linux only
        offenders.append(f"{doc}:{lineno}: {line.strip()}")
    assert not offenders, (
        "Use `python` (an activated venv provides it on all three platforms). "
        "Reserve `python3` for creating the virtualenv on macOS/Linux, and pair "
        "that with `py -3.12` for Windows.\n" + "\n".join(offenders)
    )


def test_quick_start_gives_each_platform_its_own_block():
    """The README quick start must run verbatim on all three, not with caveats."""
    langs = {lang for lang, _start, _lines in _code_blocks("README.md")}
    for expected in ("bash", "powershell", "bat"):
        assert expected in langs, (
            f"README.md has no ```{expected} block. The quick start needs one "
            "block per platform: translating a POSIX block line-by-line is what "
            "sends a Windows researcher to `pip install` against the system "
            "Python."
        )

    blocks: dict[str, str] = {}
    for lang, _s, lines in _code_blocks("README.md"):
        # Concatenate, don't overwrite: a language can have several blocks and
        # the last one is not the quick start.
        blocks[lang] = blocks.get(lang, "") + "\n" + "\n".join(lines)
    # Windows venvs live in Scripts/, not bin/, and each shell activates its own way.
    assert "Activate.ps1" in blocks["powershell"]
    assert "Copy-Item" in blocks["powershell"]
    assert "activate.bat" in blocks["bat"]
    assert "copy .env.example" in blocks["bat"]
    assert "source .venv/bin/activate" in blocks["bash"]


@pytest.mark.parametrize("doc", ["docs/OPERATIONS.md", "docs/RATING.md"])
def test_operator_runbooks_carry_a_powershell_form(doc):
    """Every operational command used to be bash-only.

    `export` exists in neither Windows shell, and in Windows PowerShell 5.1
    `curl` is an alias for Invoke-WebRequest that rejects `-s` with an error
    naming a missing Uri — which reads like a broken URL, not a shell mismatch,
    so the researcher debugs the wrong thing.
    """
    langs = {lang for lang, _s, _l in _code_blocks(doc)}
    assert "powershell" in langs, f"{doc} has no ```powershell block"
    text = (REPO_ROOT / doc).read_text(encoding="utf-8")
    assert "curl.exe" in text, (
        f"{doc} must say to write `curl.exe` rather than `curl` on Windows "
        "PowerShell, or every curl line on the page fails confusingly."
    )


def test_unattributed_check_is_written_out_for_every_shell():
    """OPERATIONS.md flags this as the check to run on the first arrivals of
    every wave: an `unattributed` run is a paid participant whose recording you
    hold and whose recruitment record you cannot join to it. It has to work on
    whatever machine the person watching the wave is sitting at."""
    text = (REPO_ROOT / "docs" / "OPERATIONS.md").read_text(encoding="utf-8")
    section = text.split("Check this after the first arrivals of every wave", 1)
    assert len(section) == 2, "the unattributed-cohort check has moved or gone"
    body = section[1][:3000]
    assert "```bash" in body
    assert "```powershell" in body
    assert "```bat" in body
    assert "Invoke-RestMethod" in body


# --------------------------------------------------------------------------
# Browsers
# --------------------------------------------------------------------------


def _headings(rel_path: str) -> set[str]:
    """GitHub-style anchor slugs for every ATX heading in a file."""
    slugs = set()
    for line in (REPO_ROOT / rel_path).read_text(encoding="utf-8").splitlines():
        if not line.startswith("#"):
            continue
        title = line.lstrip("#").strip().lower()
        slug = re.sub(r"[^\w\s-]", "", title).strip().replace(" ", "-")
        slugs.add(slug)
    return slugs


def test_readme_names_all_three_required_browsers():
    """The single browser sentence in 2,300 lines of docs said "Chrome or
    Safari" — omitting Firefox, which is a required target, in the one place
    anybody would look."""
    text = (REPO_ROOT / "README.md").read_text(encoding="utf-8")
    browsers = text.split("## Browsers", 1)
    assert len(browsers) == 2, "README.md has no ## Browsers section"
    section = browsers[1].split("\n## ", 1)[0]
    for name in ("Chrome", "Firefox", "Safari"):
        assert name in section, f"{name} is a required target and is unnamed"
    # Safari is macOS-only: saying so stops someone hunting for a Windows build.
    assert "does not exist on this OS" in section


def test_container_split_is_documented_where_raters_are_supported():
    """Chrome and Firefox record WebM; Safari records MP4. `static/v2.html`
    has implemented that two-container strategy all along and no document
    mentioned it, so a rater who cannot play a file had no documented cause to
    look up and the person triaging it had no matrix to check."""
    client = (REPO_ROOT / "static" / "v2.html").read_text(encoding="utf-8")
    # Guard against the docs drifting from what the client actually negotiates.
    for mime in ("video/webm;codecs=vp8,opus", "video/mp4;codecs=h264,aac"):
        assert mime in client, (
            f"{mime} is no longer in static/v2.html — the container table in "
            "README.md and docs/RATING.md describes a negotiation that has "
            "changed and must be updated with it."
        )
    for doc in ("README.md", "docs/RATING.md"):
        text = (REPO_ROOT / doc).read_text(encoding="utf-8")
        assert "WebM" in text and "MP4" in text, (
            f"{doc} must say that recordings arrive in two containers"
        )


def test_cross_document_browser_links_resolve():
    """README -> RATING and RATING -> README both link by anchor. A renamed
    heading silently breaks them, and these are the links someone follows while
    triaging a failed recording."""
    readme = (REPO_ROOT / "README.md").read_text(encoding="utf-8")
    rating = (REPO_ROOT / "docs" / "RATING.md").read_text(encoding="utf-8")
    assert "docs/RATING.md#what-a-rater-plays" in readme
    assert "what-a-rater-plays" in _headings("docs/RATING.md")
    assert "../README.md#browsers" in rating
    assert "browsers" in _headings("README.md")


# --------------------------------------------------------------------------
# Build context
# --------------------------------------------------------------------------


def _dockerfile_copy_sources() -> list[str]:
    sources = []
    for line in (REPO_ROOT / "Dockerfile").read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line.upper().startswith("COPY "):
            continue
        parts = line.split()[1:]
        sources.extend(parts[:-1])  # last token is the destination
    return sources


def _dockerignore_lines() -> list[str]:
    return [
        ln.strip()
        for ln in (REPO_ROOT / ".dockerignore").read_text(encoding="utf-8").splitlines()
        if ln.strip() and not ln.strip().startswith("#")
    ]


def test_dockerignore_is_an_allowlist():
    """A denylist drifts. The one it replaces missed `.terraform/` — which the
    documented deploy order creates in step 1 and then ships to the daemon in
    step 3 — plus `venv/`, `.pytest_cache/`, `node_modules/` and the raw
    research data trees. An allowlist is exact by construction."""
    lines = _dockerignore_lines()
    assert lines and lines[0] == "*", (
        ".dockerignore must open with `*` and re-include only what the "
        "Dockerfile COPYs; found: " + repr(lines[:3])
    )
    re_included = {ln[1:] for ln in lines if ln.startswith("!")}
    for heavy in (
        ".terraform",
        "venv",
        "node_modules",
        ".pytest_cache",
        "infra",
        "tests",
        "data",
        "logs",
        "reddit-analysis",
        "finetuning",
        "agents",
    ):
        assert heavy not in re_included, f"{heavy} must not enter the build context"


def test_dockerignore_re_includes_everything_the_dockerfile_copies():
    """The two files have to agree, or a COPY fails at build time (or, worse,
    copies an empty directory) for a reason that is not visible in either file
    alone."""
    re_included = {ln[1:] for ln in _dockerignore_lines() if ln.startswith("!")}
    missing = [src for src in _dockerfile_copy_sources() if src not in re_included]
    assert not missing, (
        "Dockerfile COPYs these but .dockerignore does not re-include them, so "
        f"they are excluded from the build context: {missing}"
    )


def test_dockerfile_ships_the_esci_item_bank():
    """server/esci.py resolves the item bank relative to the repo root and
    _load() raises RuntimeError when it is absent — deliberately, because a
    half-loaded instrument is worse than none. The import is lazy, inside the
    rating routes, so the image built cleanly and `import server.app` passed
    while every /rate, /api/raters, /api/ratings and /api/reliability request in
    the deployed service would have 500'd on the missing file."""
    from server import esci

    needed = esci.ITEMS_CSV.resolve().relative_to(REPO_ROOT)
    copied = _dockerfile_copy_sources()
    assert any(
        needed == Path(src) or Path(src) in needed.parents for src in map(Path, copied)
    ), (
        f"{needed.as_posix()} is read at import of the rating modules but no "
        f"Dockerfile COPY covers it. COPY sources are: {copied}"
    )


def test_line_endings_have_one_answer():
    """Without a .gitattributes, the committed bytes of a generated file depend
    on each contributor's core.autocrlf. docs/scenario-map.md is regenerated and
    compared byte-for-byte by CI, so a CRLF commit turns into ~98 changed lines
    and a failure message telling the contributor to do what they just did."""
    path = REPO_ROOT / ".gitattributes"
    assert path.exists(), "no .gitattributes: line endings are per-contributor"
    lines = [
        ln.split("#", 1)[0].strip()
        for ln in path.read_text(encoding="utf-8").splitlines()
    ]
    lines = [ln for ln in lines if ln]
    catch_all = [ln for ln in lines if ln.split()[0] == "*"]
    assert catch_all, "no `*` rule: line endings are still per-contributor"
    assert "text=auto" in catch_all[0] and "eol=lf" in catch_all[0], (
        "`* text=auto eol=lf` is what makes a checkout byte-identical on macOS, "
        "Windows and Linux. It is safe only because tools/gen_scenario_map.py "
        "pins newline= on its write; the two go together."
    )
    # Binaries must never be newline-converted on a Windows checkout.
    for pattern in ("*.png", "*.pdf", "*.zip", "*.wav"):
        assert any(
            ln.startswith(pattern) and "binary" in ln for ln in lines
        ), f"{pattern} is not marked binary"
    # cmd.exe mis-parses an LF-only .bat, and cmd is a documented operator shell.
    bat = [i for i, ln in enumerate(lines) if ln.startswith("*.bat")]
    assert bat and "eol=crlf" in lines[bat[0]]
    star = [i for i, ln in enumerate(lines) if ln.split()[0] == "*"]
    assert bat[0] > star[0], (
        "the *.bat rule must come after the `*` rule — for a given attribute "
        "the LAST matching pattern wins, so ordering is the override"
    )


# --------------------------------------------------------------------------
# CI
# --------------------------------------------------------------------------


def _ci() -> dict:
    return yaml.safe_load(
        (REPO_ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")
    )


def _matrix_job() -> dict:
    for job in _ci()["jobs"].values():
        if "strategy" in job and "matrix" in job["strategy"]:
            return job
    raise AssertionError("no job in ci.yml has a matrix")


def test_ci_covers_all_three_operating_systems():
    """CI was ubuntu-latest x 3.12 only, so nothing platform-specific was ever
    caught on any of the nine combinations the study meets."""
    matrix = _matrix_job()["strategy"]["matrix"]
    assert set(matrix["os"]) == {"ubuntu-latest", "macos-latest", "windows-latest"}
    assert _matrix_job()["strategy"].get("fail-fast") is False, (
        "fail-fast must be off, or one red cell hides the state of the others"
    )


def test_ci_actually_runs_the_platform_test_suite():
    """The suite guarding participant-facing behaviour — the API, the voice
    runner, the rating console, reliability, the client blockers — was run by
    nothing in CI. `tests` appeared nowhere as a pytest target."""
    steps = _matrix_job()["steps"]
    runs = " ".join(str(s.get("run", "")) for s in steps)
    assert re.search(r"\bpytest\s+tests\b", runs), (
        "the matrix job must run `pytest tests`"
    )
    assert "pip install pytest" in runs, (
        "pytest is not in requirements.txt (that file is the frozen production "
        "set), so the job has to install it or the step fails on a missing dep"
    )


def test_ci_python_versions_match_the_declared_support_range():
    """requirements.txt is where the supported range is stated. If CI and that
    statement disagree, one of them is lying to a researcher choosing an
    interpreter."""
    declared = re.search(
        r"Supported Python: ([0-9., or]+)\.",
        (REPO_ROOT / "requirements.txt").read_text(encoding="utf-8"),
    )
    assert declared, "requirements.txt no longer declares a supported Python range"
    stated = set(re.findall(r"3\.\d+", declared.group(1)))
    tested = set(str(v) for v in _matrix_job()["strategy"]["matrix"]["python-version"])
    assert stated == tested, (
        f"requirements.txt says {sorted(stated)}, CI tests {sorted(tested)}. "
        "An untested floor is not a floor, and an untested ceiling is a guess."
    )


# --------------------------------------------------------------------------
# The audio path on Python 3.13+
# --------------------------------------------------------------------------
#
# PEP 594 removed `audioop` in 3.13. server/voice/realtime.py falls back to a
# pure-Python `_ratecv`/`_rms`, and that fallback carries every microphone chunk
# a participant speaks — the audio that reaches Gemini Live and the audio that is
# recorded. It was marked `# pragma: no cover` and no test in the suite touched
# it, so the first execution of that code would have been a real encounter on a
# researcher's fresh python.org install. These tests run it on every interpreter.


@pytest.fixture()
def pure_python_audio(monkeypatch):
    """Force the 3.13+ path regardless of the interpreter running the suite."""
    from server.voice import realtime

    monkeypatch.setattr(realtime, "audioop", None)
    return realtime


def _tone(n: int, amplitude: int = 8000) -> bytes:
    """Deterministic signed-16-bit mono PCM; a triangle, not silence."""
    vals = [((i * 977) % (2 * amplitude)) - amplitude for i in range(n)]
    return struct.pack("<%dh" % n, *vals)


def test_rms_fallback_matches_the_c_implementation(pure_python_audio):
    import server.voice.realtime as rt

    pcm = _tone(400)
    fallback = pure_python_audio._rms(pcm)
    try:
        import audioop  # noqa: PLC0415
    except ModuleNotFoundError:
        # 3.13+: no reference to compare against, so assert the contract instead.
        assert 0 < fallback < 32768
        return
    assert abs(fallback - audioop.rms(pcm, 2)) <= 1, (
        "the pure-Python RMS drives SilenceDetector, i.e. end-of-turn detection "
        "and barge-in. Drifting from the C path changes when a participant is "
        "judged to have stopped speaking."
    )
    assert rt._rms(b"") == 0


def test_rms_fallback_handles_empty_and_silent_input(pure_python_audio):
    assert pure_python_audio._rms(b"") == 0
    assert pure_python_audio._rms(struct.pack("<200h", *([0] * 200))) == 0


def test_ratecv_fallback_preserves_length_when_rates_match(pure_python_audio):
    pcm = _tone(160)
    out, state = pure_python_audio._ratecv(pcm, 16000, 16000, None)
    assert len(out) == len(pcm)
    assert state is not None


def test_ratecv_fallback_resamples_at_the_right_ratio(pure_python_audio):
    """24 kHz from the gateway down to the 16 kHz the browser plays: the one
    conversion this code exists to do."""
    pcm = _tone(2400)  # 100 ms at 24 kHz
    out, _state = pure_python_audio._ratecv(pcm, 24000, 16000, None)
    samples = len(out) // 2
    assert abs(samples - 1600) <= 1, f"expected ~1600 samples at 16 kHz, got {samples}"


def test_ratecv_fallback_is_streaming_not_per_chunk(pure_python_audio):
    """State has to carry across chunks. Restarting per chunk drops or repeats
    a fraction of a sample every time, which over a 7-12 minute encounter is
    audible drift and a transcript that slides against the video."""
    whole = _tone(2400)
    half = len(whole) // 2
    one, state = pure_python_audio._ratecv(whole[:half], 24000, 16000, None)
    two, _ = pure_python_audio._ratecv(whole[half:], 24000, 16000, state)
    streamed = (len(one) + len(two)) // 2
    at_once = len(pure_python_audio._ratecv(whole, 24000, 16000, None)[0]) // 2
    assert abs(streamed - at_once) <= 1, (
        f"streaming produced {streamed} samples, one-shot {at_once}"
    )
    assert state != (0, 0.0)


def test_ratecv_fallback_stays_in_range_and_survives_empty_input(pure_python_audio):
    out, state = pure_python_audio._ratecv(b"", 24000, 16000, None)
    assert out == b"" and state is None

    loud = struct.pack("<8h", *([32767, -32768] * 4))
    out, _ = pure_python_audio._ratecv(loud, 16000, 24000, None)
    for value in struct.unpack("<%dh" % (len(out) // 2), out):
        assert -32768 <= value <= 32767


def test_ratecv_fallback_holds_a_constant_signal_constant(pure_python_audio):
    """Linear interpolation between equal samples must not invent a ramp: a
    constant input is the simplest thing that shows an off-by-one in the
    interpolator, and it is what silence looks like."""
    pcm = struct.pack("<600h", *([1234] * 600))
    out, _ = pure_python_audio._ratecv(pcm, 24000, 16000, None)
    values = struct.unpack("<%dh" % (len(out) // 2), out)
    # The first sample interpolates from the (absent) previous chunk's value,
    # which the implementation seeds with samples[0]; everything after is flat.
    assert set(values[1:]) == {1234}, f"constant input produced {sorted(set(values))}"
