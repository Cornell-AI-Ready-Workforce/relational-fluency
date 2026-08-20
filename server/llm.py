"""One place where model clients are built.

Clients are constructed with an explicit base URL and key rather than letting
the SDK read ambient environment variables. A stray ANTHROPIC_BASE_URL in the
shell (the desktop app exports one) would otherwise silently redirect study
traffic to a different provider — encounters would fail, or worse, quietly run
somewhere other than the configured gateway. For a measurement instrument the
endpoint has to be deliberate and recorded.
"""

from __future__ import annotations

import os

from anthropic import AsyncAnthropic
from dotenv import dotenv_values

# .env wins over ambient environment for these, deliberately.
_FILE = dotenv_values()


def _cfg(name: str, default: str = "") -> str:
    value = _FILE.get(name) or os.getenv(name) or default
    return value.strip()


def gateway_base_url() -> str:
    return _cfg("LLM_BASE_URL") or _cfg("ANTHROPIC_BASE_URL", "https://api.ai.it.cornell.edu")


def gateway_api_key() -> str:
    return _cfg("LITELLM_API_KEY") or _cfg("ANTHROPIC_API_KEY")


def text_client() -> AsyncAnthropic:
    """Client for the text models — director, steering, judge, debrief."""
    return AsyncAnthropic(base_url=gateway_base_url(), api_key=gateway_api_key())


def provenance() -> dict:
    """Recorded with each session so the record shows what served it."""
    return {
        "gateway": gateway_base_url(),
        "text_model": _cfg("CLAUDE_MODEL", "nto.gemini-2.5-flash"),
        "realtime_model": _cfg("REALTIME_MODEL", "nto.gemini-live-2.5-flash"),
    }
