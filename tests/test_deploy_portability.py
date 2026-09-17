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
infra/*.md would be right, and is listed as follow-up
work rather than done here, because those files are owned elsewhere.
"""
from __future__ import annotations

import os
import re
import shutil
import struct
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parent.parent

# The documents rewritten in the cross-platform pass.
PORTABLE_DOCS = [
    "README.md",
    "docs/DEPLOY-AWS.md",
    "docs/OPERATIONS.md",
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


@pytest.mark.parametrize("doc", ["docs/OPERATIONS.md"])
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


# --------------------------------------------------------------------------
# Line endings — asked of git, not inferred from the pattern list
#
# .gitattributes is a set of globs with a last-match-wins rule and a content
# heuristic behind `text=auto`. Reading it tells you the intent; only `git
# check-attr` tells you the decision, and only a checkout tells you the bytes.
# These helpers ask git directly so the tests below assert outcomes.
#
# If this is not a git checkout (an unpacked tarball, say) the property is not
# merely untested, it does not exist — there is nothing for attributes to act
# on. That is a genuine "not applicable", so it skips with a reason. A missing
# `git` binary inside a real checkout is a broken environment, not a
# not-applicable, so it fails.
# --------------------------------------------------------------------------

_IN_GIT_CHECKOUT = (REPO_ROOT / ".git").exists()

requires_git = pytest.mark.skipif(
    not _IN_GIT_CHECKOUT,
    reason=(
        f"{REPO_ROOT} is not a git checkout, so .gitattributes governs nothing "
        "here and there are no committed bytes to compare against"
    ),
)


def _git(*args: str) -> bytes:
    exe = shutil.which("git")
    assert exe, (
        "git is not on PATH, but this IS a git checkout — the line-ending "
        "guarantees cannot be verified. Fix the environment rather than "
        "skipping: a silently unverified checkout is how the PDFs broke."
    )
    return subprocess.run(
        [exe, *args], cwd=REPO_ROOT, check=True, capture_output=True
    ).stdout


def _tracked_files() -> list[str]:
    """Every path in the index, slash-separated (git's own form, on all OSes)."""
    return [p for p in _git("ls-files", "-z").decode("utf-8").split("\0") if p]


def _attributes(paths: list[str]) -> dict[str, dict[str, str]]:
    """{path: {"text": ..., "eol": ..., "binary": ...}} straight from git.

    Values are git's own words: "set", "unset", "unspecified", or the literal
    value ("auto", "lf", "crlf"). Paths need not exist — check-attr answers for
    a hypothetical path too, which is what lets the tests below probe for files
    the repository does not have yet but will.
    """
    payload = "\0".join(paths).encode("utf-8")
    exe = shutil.which("git")
    assert exe, "git is not on PATH"
    out = subprocess.run(
        [exe, "check-attr", "--stdin", "-z", "text", "eol", "binary"],
        cwd=REPO_ROOT,
        input=payload,
        check=True,
        capture_output=True,
    ).stdout.decode("utf-8")
    fields = out.split("\0")
    result: dict[str, dict[str, str]] = {p: {} for p in paths}
    # -z output is a flat NUL-separated stream of (path, attribute, value).
    for i in range(0, len(fields) - 2, 3):
        result.setdefault(fields[i], {})[fields[i + 1]] = fields[i + 2]
    return result


_BLOBS: dict[str, bytes] | None = None


def _blob_bytes(path: str) -> bytes:
    """The bytes git holds for this path — not the working-tree file, which in
    a clone made before .gitattributes landed can differ from them without git
    saying so.

    Addressed as `:<path>`, the index entry, because the index is what a
    checkout materialises. HEAD:<path> would be the same object in a clean tree
    and *missing* for a file added but not yet committed, which is a confusing
    way to fail.

    Read through one `git cat-file --batch` for the whole tree rather than a
    subprocess per file: at ~180 tracked paths the per-process cost dominates
    on Windows, and this job runs on nine CI cells.
    """
    global _BLOBS
    if _BLOBS is None:
        paths = _tracked_files()
        exe = shutil.which("git")
        assert exe, "git is not on PATH"
        stdout = subprocess.run(
            [exe, "cat-file", "--batch"],
            cwd=REPO_ROOT,
            input=("\n".join(f":{p}" for p in paths) + "\n").encode("utf-8"),
            check=True,
            capture_output=True,
        ).stdout
        # Each record is "<sha> <type> <size>\n" then exactly <size> bytes then
        # "\n". Walk by the declared size — the contents are binary and may
        # contain anything, newlines included, so nothing here may split lines.
        blobs, pos = {}, 0
        for p in paths:
            nl = stdout.index(b"\n", pos)
            size = int(stdout[pos:nl].split()[-1])
            start = nl + 1
            blobs[p] = stdout[start : start + size]
            pos = start + size + 1
        _BLOBS = blobs
    return _BLOBS[path]


def _blob_is_utf8_text(path: str) -> bool:
    """Whether the committed bytes are text at all.

    Deliberately NOT git's own heuristic, which is "a NUL byte in the first
    8000". That heuristic is what mangled the PDFs: a PDF starts with an ASCII
    header and a comment line, its first NUL can be well past 8000 bytes, so
    `text=auto` classified all three as text and normalised their line endings
    on the way in. Asking "does this decode as UTF-8" gets the PDFs right, and
    is the property that actually matters — if it is not text, git must be told
    so explicitly rather than left to guess.
    """
    try:
        _blob_bytes(path).decode("utf-8")
    except UnicodeDecodeError:
        return False
    return True


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


@requires_git
def test_every_tracked_extension_is_accounted_for():
    """`* text=auto eol=lf` is a catch-all, so "is everything covered?" cannot be
    answered by reading the pattern list — it is answered by asking git, per
    file, what it decided. The failure this guards is a tracked binary that no
    `binary` line names: git's content heuristic then decides from the first
    8000 bytes, and a file it guesses wrong about is newline-converted on
    checkout. That is not hypothetical here — see the PDFs in
    test_binary_files_survive_a_checkout_byte_for_byte."""
    files = _tracked_files()
    assert files, "no tracked files: is this a git checkout?"
    attrs = _attributes(files)
    # git reports `text` as one of: "auto" (the `*` catch-all), "set" (an
    # explicit `text eol=...` pin), "unset" (what the `binary` macro expands
    # to), or "unspecified" — and only the last means no rule matched.
    unresolved = [p for p in files if attrs[p]["text"] == "unspecified"]
    assert not unresolved, (
        f"these tracked files match no rule in .gitattributes, so their line "
        f"endings are whatever git guesses: {sorted(unresolved)}"
    )
    # And the converse, which is the half that actually bit. `text=auto` is a
    # guess, and the guess is "is there a NUL in the first 8000 bytes" — a test
    # a PDF passes, because its header and first comment are ASCII. So do not
    # ask git whether it thinks a file is binary; ask whether it is text, and
    # require anything that is not to be named.
    unnamed = sorted(
        path
        for path in files
        if attrs[path]["binary"] != "set" and not _blob_is_utf8_text(path)
    )
    assert not unnamed, (
        f"these tracked files are not text, but no `binary` line in "
        f".gitattributes names them: {unnamed}. Until one does, `text=auto` "
        f"decides from the first 8000 bytes and a wrong guess silently "
        f"rewrites their line endings — which is how the PDFs here broke. "
        f"Add the extension to the binary block."
    )


@requires_git
def test_things_a_shell_or_a_container_executes_are_pinned_to_lf():
    """A CRLF shell script or Dockerfile fails as `bad interpreter` or as a
    package name with a trailing CR — never as "your line endings are wrong".
    These inherit LF from the catch-all, so this test is really guarding the
    inheritance: it fails if someone narrows `*`, or extends the .bat/.cmd/.ps1
    CRLF block by symmetry to .sh."""
    for probe in (
        "deploy.sh",
        "scripts/entrypoint.sh",
        "Dockerfile",
        "Dockerfile.dev",
        ".env",
        ".env.example",
    ):
        got = _attributes([probe])[probe]["eol"]
        assert got == "lf", (
            f"{probe} would be checked out with eol={got!r}. Anything /bin/sh, "
            f"docker build or `--env-file` reads must be LF on every platform."
        )


@requires_git
def test_windows_shells_still_get_crlf():
    """The other half of the same promise: cmd.exe reads past the end of an
    LF-only .bat and can execute a truncated command, and docs/OPERATIONS.md
    names cmd.exe as an operator shell."""
    for probe in ("run.bat", "run.cmd", "ops/collect.ps1"):
        got = _attributes([probe])[probe]["eol"]
        assert got == "crlf", f"{probe} would be checked out with eol={got!r}"


@requires_git
def test_a_fresh_checkout_matches_what_the_attributes_promise():
    """Materialise the repository into an empty directory and check the bytes.

    Reading .gitattributes tells you what was asked for; only a checkout tells
    you what arrives, and the two came apart in this repository once already.
    `git checkout-index --prefix` is the checkout without the clone: it writes
    only into the temporary directory, and GIT_INDEX_FILE points at a copy so
    nothing can touch the repository's own index.

    What is asserted, per file, is the promise itself:
      * binary   — bytes identical to the blob, no conversion at all;
      * eol=lf   — no CRLF anywhere;
      * eol=crlf — every LF preceded by a CR.
    """
    tmp = tempfile.mkdtemp(prefix="rf-fresh-checkout-")
    try:
        index_copy = Path(tmp) / "index-copy"
        shutil.copyfile(REPO_ROOT / ".git" / "index", index_copy)
        out = Path(tmp) / "tree"
        out.mkdir()
        env = dict(os.environ, GIT_INDEX_FILE=str(index_copy))
        subprocess.run(
            ["git", "checkout-index", "--all", "--force", f"--prefix={out.as_posix()}/"],
            cwd=REPO_ROOT,
            env=env,
            check=True,
            capture_output=True,
        )

        files = _tracked_files()
        attrs = _attributes(files)
        problems = []
        for path in files:
            got = (out / path).read_bytes()
            if attrs[path]["binary"] == "set":
                want = _blob_bytes(path)
                if got != want:
                    problems.append(
                        f"{path}: declared binary but a checkout produces "
                        f"{len(got)} bytes where the blob has {len(want)} — "
                        f"git converted a file it was told not to touch"
                    )
            elif attrs[path]["eol"] == "crlf":
                if re.search(rb"(?<!\r)\n", got):
                    problems.append(f"{path}: eol=crlf but a bare LF survived")
            else:
                if b"\r\n" in got:
                    problems.append(
                        f"{path}: eol=lf but a checkout produces CRLF, so a "
                        f"Windows tree is not byte-identical to a Linux one"
                    )
        assert not problems, "\n".join(problems)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


@requires_git
def test_binary_files_survive_a_checkout_byte_for_byte():
    """The specific thing the previous test would have caught earlier.

    The three tracked PDFs were committed before .gitattributes existed, under
    core.autocrlf=true. Every checkout since re-inserted a CR before each of
    their LF bytes, which shifts everything after the first one: `startxref`
    still names offset 9429 while the xref table has moved, so a reader reports
    a damaged file. git said nothing, because with `text` set both directions
    of the conversion are "correct". `*.pdf binary` is the fix, and this asserts
    it holds — including that a PDF materialised from the index still parses,
    which is a property no line-ending rule states directly."""
    files = [p for p in _tracked_files() if p.lower().endswith(".pdf")]
    assert files, "no PDF is tracked any more; drop this test or repoint it"
    for path in files:
        data = _blob_bytes(path)
        assert data.startswith(b"%PDF-"), f"{path}: committed blob is not a PDF"
        assert b"%%EOF" in data[-40:], f"{path}: committed blob has no trailer"
        m = list(re.finditer(rb"startxref\s+(\d+)", data))
        assert m, f"{path}: committed blob has no startxref"
        offset = int(m[-1].group(1))
        assert data[offset : offset + 4] == b"xref", (
            f"{path}: startxref points at offset {offset}, which holds "
            f"{data[offset:offset + 12]!r} rather than the xref table. That is "
            f"what CR injection does to a PDF: the bytes are all still there, "
            f"just moved, so the file looks fine to everything except a reader."
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


# --------------------------------------------------------------------------
# `import server` must not depend on how pytest was started
# --------------------------------------------------------------------------
#
# The failure these two guard was worth nine red cells and zero tests run.
# `server` is a top-level package living at the repository root and is never
# installed. There is no root pytest.ini, pyproject.toml, setup.cfg or
# conftest.py and no tests/__init__.py, so under pytest's default `prepend`
# import mode the root reaches sys.path only if the invocation put it there:
# `python -m pytest` does (cwd), the `pytest` console script does not (its own
# bin directory). CI used the console script. Six modules raised
# "ModuleNotFoundError: No module named 'server'" during collection and the job
# failed having tested nothing, while the adjacent `python -c "import
# server.app"` step passed — `python -c` adds the cwd.
#
# It is not a Linux or a 3.11/3.13 effect. It reproduces identically on
# Windows/3.12, which is why it would have taken every cell at once, and why
# every baseline ever quoted for this repository (all produced with
# `python -m pytest`) missed it.


def _pytest_without_cwd_on_path() -> list:
    """An argv that starts pytest WITHOUT the current directory on sys.path.

    That is the only property of `pytest tests` that matters here: the console
    script sets sys.path[0] to its own bin directory, so the repository root is
    never inserted. The script itself is used when one is findable — beside this
    interpreter first, because a venv's Scripts/bin is often not on the PATH a
    test subprocess inherits, then the PATH, which is where CI's is. Failing
    both, `python -P -m pytest` reproduces the same condition exactly (-P, 3.11+,
    suppresses the cwd prepend) so this guard never silently skips on a machine
    whose layout happens to hide the script.
    """
    exe = "pytest.exe" if os.name == "nt" else "pytest"
    beside = Path(sys.executable).parent / exe
    if beside.is_file():
        return [str(beside)]
    found = shutil.which("pytest")
    if found:
        return [found]
    return [sys.executable, "-P", "-m", "pytest"]


def test_the_suite_collects_under_a_bare_pytest_invocation():
    """The invocation CI actually used, run for real.

    A static assertion about ci.yml would not have caught this and does not
    guard it: the hazard is a property of sys.path, not of a YAML string, and it
    bites an IDE runner and anyone typing `pytest` by hand just as hard. So this
    runs the console script the way CI did and asserts collection completes.

    Deliberately three modules and not the whole tree: these are the ones that
    `from server import ...` at module scope without self-inserting the root,
    and they are the exact casualties. Collecting all of tests/ would add
    seconds to every run to prove the same thing.
    """
    targets = ["tests/test_app.py", "tests/test_api_blockers.py",
               "tests/test_core_blockers.py"]
    env = {k: v for k, v in os.environ.items()
           if k not in {"RF_FIXTURE", "RF_FIXTURE_DIR", "DATA_DIR"}}
    # PYTHONPATH would hand the answer to the thing under test.
    env.pop("PYTHONPATH", None)
    proc = subprocess.run(
        [*_pytest_without_cwd_on_path(), *targets,
         "--collect-only", "-q", "-p", "no:cacheprovider"],
        cwd=REPO_ROOT, env=env, capture_output=True, text=True, timeout=300,
    )
    assert "No module named 'server'" not in (proc.stdout + proc.stderr), (
        "collection under a bare `pytest` cannot import the repository root.\n"
        "tests/conftest.py is where that is fixed, once, for every invocation "
        "form.\n\n" + (proc.stdout or proc.stderr)[-2000:]
    )
    assert proc.returncode == 0, (proc.stdout or proc.stderr)[-2000:]


def test_ci_runs_pytest_through_the_interpreter():
    """The second lock, on the invocation CI itself uses.

    conftest.py makes either form work, so this is not what keeps the build
    green — it keeps the *documented* command the same as the one every quoted
    baseline was produced with, so a future reader comparing counts is
    comparing like with like.
    """
    runs = " ".join(str(s.get("run", "")) for s in _matrix_job()["steps"])
    suite = [line.strip() for line in runs.splitlines()
             if re.search(r"\bpytest\s+tests\b", line)]
    assert suite, "the matrix job must run `pytest tests`"
    for line in suite:
        assert re.search(r"\bpython\s+-m\s+pytest\s+tests\b", line), (
            "run the suite as `python -m pytest tests`: the bare console "
            "script does not put the repository root on sys.path, and "
            f"`server` is imported from there.\n  {line}"
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


# The modules that drive a page through node. Each one skips when node is
# absent, which is correct on a laptop and wrong on a runner.
NODE_HARNESS_MODULES = [
    "tests/test_client_blockers.py",
    "tests/test_browser_compat.py",
]


def test_the_node_harnesses_still_locate_node_the_way_ci_checks_for_it():
    """Pins the premise of the two tests below.

    CI proves node is present by asking shutil.which("node") — the same
    question the harnesses ask. If a harness switched to `npx`, a bundled
    runtime, or a hard-coded path, CI's check would go on passing while
    answering about something else. This is the only place that link is
    written down, so assert it rather than trust it.
    """
    for module in NODE_HARNESS_MODULES:
        text = (REPO_ROOT / module).read_text(encoding="utf-8")
        assert 'shutil.which("node")' in text, (
            f"{module} no longer resolves node with shutil.which(). The CI "
            "precondition step in .github/workflows/ci.yml asks that exact "
            "question; update both together or the check stops meaning "
            "anything."
        )
        assert "pytest.skip" in text, (
            f"{module} no longer skips when node is missing — if it now fails "
            "instead, that is better, and the CI precondition can go."
        )


def test_ci_installs_node_for_the_page_harnesses():
    """Node was never installed by the matrix job, so all the Safari-facing
    client coverage — the -webkit prefix guards, the MediaRecorder WebM/MP4
    negotiation, the rating console harness — rested on the runner image
    happening to ship a node. The day an image stops shipping one, every such
    test skips and the build stays green: coverage lost without a sound, which
    is the failure this suite exists to catch."""
    steps = _matrix_job()["steps"]
    node_steps = [s for s in steps if "setup-node" in str(s.get("uses", ""))]
    assert node_steps, (
        "the platform-tests matrix has no actions/setup-node step, so node is "
        f"an inherited property of the runner image rather than a declared "
        f"dependency — and {len(NODE_HARNESS_MODULES)} test modules need it"
    )
    version = str(node_steps[0].get("with", {}).get("node-version", ""))
    assert re.fullmatch(r"\d+(\.\d+)*", version), (
        f"node-version is {version!r}. Pin it the way the python matrix is "
        "pinned: a harness that starts failing should be a change someone "
        "made, not one that happened to them overnight."
    )
    # Installing node is no use after the tests have already run.
    order = [i for i, s in enumerate(steps) if "setup-node" in str(s.get("uses", ""))]
    tests_at = [
        i for i, s in enumerate(steps) if re.search(r"\bpytest\s+tests\b", str(s.get("run", "")))
    ]
    assert order[0] < tests_at[0], "setup-node must come before the test step"


def test_ci_fails_loudly_when_node_is_absent_instead_of_testing_less():
    """setup-node declaring node is not the same as node reaching the
    interpreter that runs pytest. Without a step that checks, a node installed
    somewhere the test process cannot see it produces a green build over a
    quieter test run — the exact shape of failure the audit found 252 times."""
    steps = _matrix_job()["steps"]
    runs = "\n".join(str(s.get("run", "")) for s in steps)
    assert 'shutil.which("node")' in runs, (
        "no step in the matrix job verifies node is reachable from python. "
        "Add one that calls shutil.which('node') — the same call the harnesses "
        "make — and exits non-zero when it comes back empty."
    )
    assert "sys.exit(" in runs or "exit 1" in runs, (
        "the node check must exit non-zero; printing a warning into a green "
        "log is indistinguishable from not checking"
    )
    # -rs makes every skip and its reason visible in the log, so coverage that
    # goes missing for some other reason is at least readable.
    assert re.search(r"pytest\s+tests\b[^\n]*-rs", runs), (
        "run `pytest tests -q -rs` so the log lists what skipped and why"
    )


# --------------------------------------------------------------------------
# Text output that must not depend on the machine that produced it
#
# Path.write_text()/open() in text mode with no encoding= use the machine's
# locale — cp1252 on Windows, UTF-8 on macOS and Linux — and with no newline=
# they translate "\n" to os.linesep. Both make the same script emit different
# bytes on different machines, which is the same defect as the one
# .gitattributes exists to fix, one layer up.
# --------------------------------------------------------------------------

# An unpinned *read* is a decode of bytes someone else wrote; an unpinned
# *write* mints the divergence. Both matter, so scan both.
_TEXT_IO_FUNCS = {"write_text", "read_text"}


def _unpinned_text_io(path: Path) -> list[str]:
    """['<file>:<line> <call>'] for text I/O with no explicit encoding.

    An AST walk rather than a grep, because "encoding" appears in prose all
    over this repository and a grep would be answering a different question.
    """
    import ast

    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    out = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        name = None
        if isinstance(node.func, ast.Attribute):
            name = node.func.attr
        elif isinstance(node.func, ast.Name):
            name = node.func.id
        if name not in _TEXT_IO_FUNCS | {"open"}:
            continue
        if name == "open":
            # `open` as an attribute is ambiguous: Path.open() is file I/O,
            # GroupRoom.open() and socket.open() are not. Every file open in
            # this repository passes a mode, an encoding or both, so requiring
            # at least one argument separates them cleanly. The cost, stated
            # rather than hidden: a bare `p.open()` — an unpinned text read
            # with no arguments at all — would not be seen. None exists today.
            if isinstance(node.func, ast.Attribute) and not (node.args or node.keywords):
                continue
            # Binary mode has no encoding, so it is not in question.
            mode = next(
                (
                    kw.value.value
                    for kw in node.keywords
                    if kw.arg == "mode" and isinstance(kw.value, ast.Constant)
                ),
                None,
            )
            if mode is None and len(node.args) > 1 and isinstance(node.args[1], ast.Constant):
                mode = node.args[1].value
            if isinstance(mode, str) and "b" in mode:
                continue
        if any(kw.arg == "encoding" for kw in node.keywords):
            continue
        out.append(f"{path.name}:{node.lineno} {name}()")
    return out


def test_the_reddit_analysis_outputs_do_not_depend_on_the_machine():
    """01_descriptive_stats_and_topics.py writes two committed artefacts —
    antiwork_descriptive_stats.json and antiwork_topics.json — that back the
    situation taxonomy cited in server/runs.py's FORM_EXCLUSIONS rationale.

    Unpinned, they were written in the locale encoding and with os.linesep line
    endings. json.dumps defaults to ensure_ascii=True so the bytes were ASCII
    either way, but the newline half was already live: antiwork_topics.json is
    CRLF in a Windows working tree and LF in the committed blob, invisible only
    because .gitattributes normalises text on comparison. The encoding half
    goes live the first time anyone passes ensure_ascii=False, which on a
    Reddit corpus full of emoji and smart quotes means mojibake on one platform
    and a UnicodeEncodeError on another, from one script and one input.
    """
    target = REPO_ROOT / "reddit-analysis" / "notebooks" / "01_descriptive_stats_and_topics.py"
    assert not _unpinned_text_io(target), (
        f"unpinned text I/O in {target.name}: {_unpinned_text_io(target)}. "
        'Pass encoding="utf-8" (and newline="" on a committed artefact).'
    )
    # And newline=, which encoding= does not cover: with the default, Python
    # translates every "\n" to os.linesep on write. Asserted through the AST,
    # not as a substring — the docstring beside the call says `newline=""` too,
    # so a text search would keep passing after the keyword was deleted.
    import ast

    tree = ast.parse(target.read_text(encoding="utf-8"), filename=str(target))
    writes = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "write_text"
    ]
    assert writes, f"{target.name} no longer writes its artefacts with write_text()"
    unpinned = [n.lineno for n in writes if not any(kw.arg == "newline" for kw in n.keywords)]
    assert not unpinned, (
        f"{target.name} lines {unpinned}: encoding alone still lets Windows "
        "write CRLF where macOS and Linux write LF, from the same script and "
        'the same input. Pin newline="" too, the way '
        "tools/gen_scenario_map.py does."
    )


def test_the_remaining_unpinned_text_io_is_a_named_list_that_only_shrinks():
    """A recorded gap, not a silent one.

    reddit-analysis/notebooks/02_situation_taxonomy.py has the same unpinned
    write and is owned elsewhere, so it is named here instead of being fixed or
    quietly tolerated. The assertion is equality, so this fails if the problem
    spreads AND if it is fixed — the second failure is the one that tells
    whoever fixes it to delete this test.
    """
    known = {"02_situation_taxonomy.py:125 write_text()"}
    found = set()
    for path in sorted((REPO_ROOT).rglob("*.py")):
        parts = set(path.parts)
        if parts & {".venv", "venv", "__pycache__", "node_modules", ".git"}:
            continue
        if path.name == "01_descriptive_stats_and_topics.py":
            continue  # asserted clean above
        found.update(_unpinned_text_io(path))
    assert found == known, (
        f"the set of unpinned text reads/writes changed.\n"
        f"  newly unpinned: {sorted(found - known)}\n"
        f"  now pinned (delete them from `known`, or drop this test if the set "
        f"is empty): {sorted(known - found)}"
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
