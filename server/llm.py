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
    return _cfg("LLM_BASE_URL") or _cfg("ANTHROPIC_BASE_URL", "https://api.ai.it.cornell.edu")


def gateway_api_key() -> str:
    return _cfg("LITELLM_API_KEY") or _cfg("ANTHROPIC_API_KEY")


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
    """
    import httpx

    result = {"ok": False, **provenance()}
    ambient = os.getenv("ANTHROPIC_BASE_URL", "")
    if ambient and ambient != gateway_base_url():
        result["ambient_override_ignored"] = ambient
    try:
        r = httpx.get(
            f"{gateway_base_url().rstrip('/')}/v1/models",
            headers={"Authorization": f"Bearer {gateway_api_key()}"},
            timeout=10,
        )
        result["ok"] = r.status_code == 200
        result["status"] = r.status_code
        if r.status_code != 200:
            result["detail"] = r.text[:200]
    except Exception as exc:  # noqa: BLE001
        result["detail"] = str(exc)
    return result
