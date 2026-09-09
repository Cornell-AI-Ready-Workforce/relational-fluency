"""One place where model clients are built.

Clients are constructed with an explicit base URL and key rather than letting
the SDK read ambient environment variables. A stray ANTHROPIC_BASE_URL in the
shell (the desktop app exports one) would otherwise silently redirect study
traffic to a different provider, encounters would fail, or worse, quietly run
somewhere other than the configured gateway. For a measurement instrument the
endpoint has to be deliberate and recorded.
"""

from __future__ import annotations

import os

from anthropic import Anthropic, AsyncAnthropic
from dotenv import dotenv_values

# .env wins over ambient environment for these, deliberately.
_FILE = dotenv_values()


def _cfg(name: str, default: str = "") -> str:
    value = _FILE.get(name) or os.getenv(name) or default
    return value.strip()


def setting(name: str, default: str = "") -> str:
    """Public accessor with the same file-wins-over-ambient precedence."""
    return _cfg(name, default)


def gateway_base_url() -> str:
    # LLM_BASE_URL is this project's own explicit var (fine to read from .env or
    # ambient env). ANTHROPIC_BASE_URL, however, is resolved from .env / the
    # hardcoded default ONLY: a stray value in the ambient shell (the desktop
    # app exports one) must never silently redirect study traffic and leak the
    # gateway key to an unintended host. preflight() flags such an ambient value
    # instead of honoring it.
    explicit = _cfg("LLM_BASE_URL")
    if explicit:
        return explicit
    env_val = _FILE.get("ANTHROPIC_BASE_URL")
    if env_val and env_val.strip():
        return env_val.strip()
    return "https://api.ai.it.cornell.edu"


def gateway_api_key() -> str:
    return _cfg("LITELLM_API_KEY") or _cfg("ANTHROPIC_API_KEY")


_REDACTED = "<redacted>"


def redact_key(text: str) -> str:
    """Strip anything key-shaped out of a string that is about to leave here.

    preflight()'s `detail` is the one string in this module that has to be
    assumed public: app.py prints it at boot (so it lands in CloudWatch, 90-day
    retention) and returns it from GET /health, which is unauthenticated and
    exempt from the host guard. Two things put key material into that string.
    A gateway can echo the credential it was sent back in its error body, which
    goes straight into `detail` below. And a key pasted wrapped across two lines
    survives _cfg()'s strip(), so the newline reaches the Authorization header,
    where h11 rejects it by quoting the ENTIRE header value into its exception
    message - the "Illegal header value" line in this repo's own logs. Neither
    is hypothetical enough to gamble a live gateway key on, so every candidate
    detail goes through here first and the key never appears verbatim.

    Substitution is by bare substring, which is only safe for a needle long
    enough that it cannot plausibly occur in ordinary prose - hence the length
    floor below, which now covers the whole key and not only its lines.
    """
    key = gateway_api_key()
    if not key:
        return text
    # h11 quotes the offending header as a bytes repr, in which a newline inside
    # a wrapped key shows up as the two characters \ and n, so the raw value
    # never matches. Redact the escaped form and each line of the key as well,
    # longest needle first so redacting a fragment cannot strand the remainder
    # of a longer one.
    candidates = {key, key.encode("unicode_escape").decode("ascii", "replace")}
    candidates.update(key.split())
    # The floor applies to EVERY needle, including the whole key. It used to
    # apply only to the whitespace-split parts, so a degenerate dev value like
    # LITELLM_API_KEY=x substituted on every letter x in the message: "max
    # retries exceeded" came back as "ma<redacted> retries e<redacted>ceeded".
    # That is the one line an operator reads off /health to find out why the
    # gateway is down, and a placeholder key is exactly when they are reading
    # it. A value under six characters is not a LiteLLM credential (those are
    # "sk-" plus a long random tail), so there is nothing there to protect and
    # mangling the diagnostic is pure loss - while a real key has no needle
    # short enough to reach this branch.
    needles = {n for n in candidates if len(n) >= 6}
    if not needles:
        return text
    out = text
    for needle in sorted(needles, key=len, reverse=True):
        out = out.replace(needle, _REDACTED)
    return out


def text_client() -> AsyncAnthropic:
    """Client for the text models, director, steering, judge, debrief."""
    return AsyncAnthropic(base_url=gateway_base_url(), api_key=gateway_api_key())


def sync_text_client() -> Anthropic:
    """Blocking client for the offline scorer and debrief CLIs."""
    return Anthropic(base_url=gateway_base_url(), api_key=gateway_api_key())


def provenance() -> dict:
    """Recorded with each session so the record shows what served it."""
    return {
        "gateway": gateway_base_url(),
        "text_model": _cfg("CLAUDE_MODEL", "nto.gemini-3.1-flash-lite"),
        "realtime_model": _cfg("REALTIME_MODEL", "nto.gemini-live-2.5-flash"),
    }


def preflight() -> dict:
    """Check the configured gateway answers with the configured key.

    Called at startup. A misconfigured endpoint used to surface only as a 401
    buried in a session log, halfway through an encounter, by which point the
    participant's time is already spent. Better to say so before anyone joins.

    Everything this returns is treated as public (see redact_key), so `detail`
    describes the failure without ever repeating the credential.
    """
    import httpx

    result = {"ok": False, **provenance()}
    ambient = os.getenv("ANTHROPIC_BASE_URL", "")
    if ambient and ambient != gateway_base_url():
        result["ambient_override_ignored"] = ambient
    key = gateway_api_key()
    if key and any(c.isspace() for c in key):
        # A key pasted wrapped across two lines is the copy-paste failure that
        # produced this repo's own "Illegal header value" boot warning. Catch it
        # here rather than letting h11 catch it: h11's complaint quotes the whole
        # Authorization header back, key included, into a detail that is printed
        # at boot and served by the unauthenticated /health. Name the fault, and
        # never the value - the operator only needs to know to re-paste it.
        result["detail"] = (
            "the key contains whitespace (a newline, most likely from a paste "
            "that wrapped) - re-paste it as a single line"
        )
        return result
    try:
        r = httpx.get(
            f"{gateway_base_url().rstrip('/')}/v1/models",
            headers={"Authorization": f"Bearer {key}"},
            timeout=10,
        )
        result["ok"] = r.status_code == 200
        result["status"] = r.status_code
        if r.status_code != 200:
            # Redact before truncating: slicing first could cut the key in half
            # and leave a prefix that no longer matches anything to redact.
            result["detail"] = redact_key(r.text)[:200]
    except Exception as exc:  # noqa: BLE001
        # The exception TYPE is the useful half and is always safe; the message
        # is not, so it goes through the redactor on its way out.
        result["detail"] = f"{type(exc).__name__}: {redact_key(str(exc))}"
    return result
