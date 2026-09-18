"""FastAPI server, participant + researcher endpoints.

  - /ws/participant/voice , the participant's encounter: audio both ways,
                            live captions, webcam upload alongside
  - /ws/researcher        , live transcript + steering controls
"""
from __future__ import annotations

import functools
import json
import os
import re
import secrets
import time
from pathlib import Path
from typing import Optional

import zipfile

import anyio
import yaml
from dotenv import load_dotenv
from fastapi import (
    Depends, FastAPI, HTTPException, Query, Request, WebSocket, WebSocketDisconnect,
)
from fastapi.responses import (
    FileResponse, HTMLResponse, JSONResponse, RedirectResponse, Response,
    StreamingResponse,
)
from fastapi.staticfiles import StaticFiles
from fastapi.middleware.cors import CORSMiddleware
from uvicorn.middleware.proxy_headers import ProxyHeadersMiddleware

from .engine import DEFAULT_MODEL
from .scenarios import list_scenarios, load_scenario
from .session import registry
from .storage import (
    create_participant, get_participant, init_storage, missing_required_env,
    participant_withdrawal, record_withdrawal,
    valid_session_id,
)
from .realtime_voice_session import RealtimeVoiceSessionRunner

load_dotenv()
# Deliberately NOT init_storage() — see _init_storage_on_startup below. This
# module's own rule (stated at _refuse_unprotected_public_start) is that an
# import decides nothing, contacts nothing and writes nothing; calling it here
# created DATA_DIR, sessions/, participants/ and index.db every time an offline
# tool, a test process or CI's `python -c "import server.app"` merely imported
# the module. A process that is going to serve initialises its data directory;
# an import does not.

SESSION_KEY = os.getenv("SESSION_KEY", "").strip()
ROOT_DIR = Path(__file__).parent.parent
STATIC_DIR = ROOT_DIR / "static"
CONFIG_DIR = ROOT_DIR / "config"
# Mirror storage.py, DATA_DIR may be a mounted volume in production.
from .storage import SESSIONS_DIR as SESSIONS_DIR  # re-export

# Public hostnames. Cornell IT delegated ai-ready-workforce.ai.cornell.edu to
# Route 53; rf.* is the participant entrance (app + broker WSS) and api.rf.* is
# the backend API. Comma-separated overrides let staging and local dev differ.
APP_HOST = os.getenv("APP_HOST", "rf.ai-ready-workforce.ai.cornell.edu").strip()
API_HOST = os.getenv("API_HOST", "api.rf.ai-ready-workforce.ai.cornell.edu").strip()

# Host header allowlist. Empty ALLOWED_HOSTS disables the check (local dev).
_DEFAULT_ALLOWED = f"{APP_HOST},{API_HOST},localhost,127.0.0.1"
ALLOWED_HOSTS = [h.strip() for h in os.getenv("ALLOWED_HOSTS", _DEFAULT_ALLOWED).split(",") if h.strip()]

# Browser origins permitted to call the API. The app and the API are separate
# hostnames, so calls from the participant page are cross-origin.
_DEFAULT_ORIGINS = f"https://{APP_HOST},http://localhost:{os.getenv('PORT', '8765')},http://127.0.0.1:{os.getenv('PORT', '8765')}"
ALLOWED_ORIGINS = [o.strip() for o in os.getenv("ALLOWED_ORIGINS", _DEFAULT_ORIGINS).split(",") if o.strip()]

app = FastAPI(title="Relational Fluency Platform")

# Behind the ALB, honour X-Forwarded-Proto so generated URLs are https/wss.
app.add_middleware(ProxyHeadersMiddleware, trusted_hosts="*")

# Host-header allowlist. Implemented directly rather than with
# TrustedHostMiddleware because the ALB health check addresses the task by its
# private IP, which can never be in the allowlist, TrustedHostMiddleware would
# answer 400 and the target would be marked unhealthy forever. /health is
# therefore exempt, which — with no check_key on it either — makes it the one
# route reachable from the open internet. Whatever it returns is public: see
# _PUBLIC_STORAGE_FIELDS, which is what keeps that true as fields are added.
if ALLOWED_HOSTS:

    @app.middleware("http")
    async def _guard_host(request, call_next):
        if request.url.path != "/health":
            host = (request.headers.get("host") or "").split(":")[0]
            if host and host not in ALLOWED_HOSTS:
                return JSONResponse({"detail": "Invalid host header"}, status_code=400)
        return await call_next(request)

app.add_middleware(
    CORSMiddleware,
    allow_origins=ALLOWED_ORIGINS,
    allow_credentials=True,
    # PUT is here for exactly one route: the webcam upload fallback at
    # PUT /api/sessions/{id}/video. The participant page is served from APP_HOST
    # and calls the API on API_HOST, so every one of its requests is
    # cross-origin and a method missing from this list is refused at the
    # preflight — which would make the fallback unreachable from the only client
    # that has any reason to use it, while every server-side test passed.
    allow_methods=["GET", "POST", "PUT", "OPTIONS"],
    allow_headers=["*"],
)

from .llm import preflight as _preflight
# Every string in this file that came from an exception raised at the model
# gateway goes through this before it is written down or sent anywhere. Two
# live-gateway shapes carry the credential verbatim — a key pasted wrapped
# across two lines, which websockets quotes back inside "invalid Authorization
# header", and a gateway that echoes the key in a 401 body, which the Anthropic
# SDK's str() reproduces whole. The sinks in this file are the worst kind: the
# participant's own socket, an HTTP 500 body on routes a participant can invoke,
# events.jsonl (archived per encounter and shipped whole in the download zip),
# and the boot log. redact_key lives in llm.py because that is where the key is
# read; do not write a second one here.
from .llm import redact_key

from botocore.exceptions import BotoCoreError, ClientError

from . import video as _video

# The two credentialed seams — the model gateway and the study bucket — are
# checked at STARTUP, by run_preflights() below. Neither is checked here, in the
# module body, and that is the whole point of this comment.
#
# Both used to run on import. That made `import server.app` do an httpx GET to
# the gateway, two S3 calls and a put_object into the study bucket, which is
# wrong three times over. It is slow (a black-holed S3 path costs ~6 s per call
# and the gateway probe another 10 s) on a module that verify_record and
# retranscribe and every pytest process import. It fails, noisily and for no
# reason, wherever there are no credentials — which is every offline tool run.
# And it PUT an object into an IRB bucket as a side effect of running a report:
# the preflight's marker key is harmless in itself, but "reading a record wrote
# to the study bucket" is not a sentence this project should be able to say.
#
# The answer before the check has run is "unknown", written down as such rather
# than defaulted to False: ok=None with checked=False is a state /health can
# publish honestly, where ok=False would tell an operator the bucket had been
# tested and failed. Same rule as everywhere else here — an explicit unknown
# beats a confident wrong answer.
_UNCHECKED_GATEWAY = {"ok": None, "checked": False}
_UNCHECKED_STORAGE = {"ok": None, "checked": False,
                      "bucket": _video.BUCKET, "region": _video.REGION}
_PREFLIGHT = dict(_UNCHECKED_GATEWAY)
_STORAGE_PREFLIGHT = dict(_UNCHECKED_STORAGE)


def _check_gateway() -> None:
    global _PREFLIGHT

    # Verify the model gateway before anyone can join. A wrong endpoint used to
    # show up only as a 401 mid-encounter; now it is visible at boot and on
    # /health.
    _PREFLIGHT = dict(_preflight(), checked=True)
    if _PREFLIGHT.get("ambient_override_ignored"):
        print(
            f"  note: ignoring ambient ANTHROPIC_BASE_URL="
            f"{_PREFLIGHT['ambient_override_ignored']}, using {_PREFLIGHT['gateway']}"
        )
    if not _PREFLIGHT.get("ok"):
        print(
            f"  WARNING: model gateway {_PREFLIGHT['gateway']} did not answer "
            f"({_PREFLIGHT.get('status') or _PREFLIGHT.get('detail')}). "
            f"Encounters will fail until this is fixed."
        )

def _check_storage() -> None:
    global _STORAGE_PREFLIGHT

    # The other credentialed seam. The webcam recording is the artefact Phase 2
    # rates, it is uploaded browser-direct with a URL this process signs, and
    # until this check existed nothing touched the bucket until the last second
    # of the first encounter — where the failure used to be a silent
    # resolve(false) in the participant's browser. The client half of that is
    # fixed: static/v2.html's uploadRecording now resolves {ok:false, reason}
    # and reports it, so the browser no longer discards the failure. This check
    # is what makes a wrong bucket visible BEFORE a wave is collected rather
    # than during it.
    _STORAGE_PREFLIGHT = dict(_video.storage_preflight(), checked=True)
    # `detail` is a raw str(exc) off boto3 client construction, and it is kept
    # in two places: the boot line below, which lands in CloudWatch with 90-day
    # retention, and /health's storage block once an operator authenticates
    # (_PUBLIC_STORAGE_FIELDS withholds it from a stranger, not from them). The
    # bucket is the second credentialed seam and it fails the way the gateway
    # does — a secret with a stray newline in it comes back with the signing
    # header quoted whole — so it is scrubbed here, at the one point both sinks
    # read from, rather than at each of them.
    if _STORAGE_PREFLIGHT.get("detail"):
        _STORAGE_PREFLIGHT["detail"] = redact_key(_STORAGE_PREFLIGHT["detail"])
    if not _STORAGE_PREFLIGHT.get("ok"):
        print(
            f"  WARNING: study bucket {_STORAGE_PREFLIGHT['bucket']} "
            f"({_STORAGE_PREFLIGHT['region']}) is not "
            f"{'readable' if not _STORAGE_PREFLIGHT.get('readable') else 'writable'}"
            f" ({_STORAGE_PREFLIGHT.get('error_code') or _STORAGE_PREFLIGHT.get('detail')})."
            f" Webcam recordings will be lost until this is fixed."
        )


def _check_required_env() -> None:
    """Name the environment variables this deployment has no usable value for.

    The third preflight. storage declares REQUIRED_ENV and
    storage.missing_required_env() reads it, and its own docstring says this is
    "what a boot preflight and /health should publish" — for a long time nothing
    called it, so a variable a wave depended on could be unset (or set to a
    placeholder like "xxx", which `tofu plan` accepts and this side treats as
    unset) while /health answered 200 and runs kept accumulating. REQUIRED_ENV
    is empty today — the consent step that once needed a variable here is taken
    in Qualtrics, outside this app — and the wiring stays so that the next
    variable a wave depends on is declared in one place and published in both.

    A warning, not a refusal: a laptop and every CI run would be taken out by a
    variable that only a recruiting deployment actually needs, and the thing
    that must not happen is the wave, not the boot.

    Routed through the same guarded helper /health uses, for both of its
    reasons: this runs inside a startup hook, where an unexpected exception is a
    task that never serves, and it warms the answer /health falls back to when
    its own re-check fails.
    """
    missing = _missing_required_env_for_health()
    if not missing:
        return
    from .storage import REQUIRED_ENV

    print(f"  WARNING: {len(missing)} required environment variable(s) are "
          f"unset or set to a placeholder. This process will serve, answer "
          f"/health 200 and record NOTHING:")
    for name in missing:
        print(f"    {name}: {REQUIRED_ENV.get(name, 'required by this server')}")


def run_preflights() -> None:
    """Check the gateway and the study bucket, and say so on stdout.

    Explicit and idempotent: a launcher, a startup hook or an operator may call
    it, and importing this module may not. Neither check gates anything — both
    report — because a transient blip must not stop a process that can still run
    encounters and still write every local artefact.

    The three are run independently, and a failure in one does not skip the
    others. All are documented as never raising, so this catches nothing that is
    supposed to happen; what it prevents is the shape where an unexpected
    gateway error leaves the bucket silently unchecked and /health saying
    "unknown" with nobody having asked for that answer. A check that quietly did
    not run is exactly the state these checks exist to abolish.
    """
    for name, check in (("model gateway", _check_gateway),
                        ("study bucket", _check_storage),
                        ("required environment", _check_required_env)):
        try:
            check()
        except Exception as e:  # noqa: BLE001, a diagnostic must not stop the process
            # Redacted because this is the one line here that a gateway
            # exception reaches unfiltered. preflight() puts its own `detail`
            # through redact_key, but an exception RAISED out of preflight
            # bypasses that entirely — and the wrapped-key case raises rather
            # than returning, quoting the whole Authorization header into the
            # message. This print lands in CloudWatch with 90-day retention.
            print(f"  WARNING: the {name} preflight did not complete: "
                  f"{redact_key(f'{type(e).__name__}: {e}')}")


def _refuse_unprotected_public_start() -> Optional[str]:
    """Why this process must not serve, or None if it may.

    SESSION_KEY is the only thing standing between the open internet and the
    researcher surface: /api/sessions, /api/encounters, the per-session download
    zips, the director view, the whole dataset (see check_key). Empty, check_key
    waves everyone through — which is right for a laptop and catastrophic for a
    task behind a public hostname, where the first symptom is a stranger reading
    IRB-recorded encounters and there is no symptom at all until then.

    The test is the bind address first and the host allowlist second: the
    interface decides whether a stranger can open a socket at all, and the
    allowlist then says which names this process will answer to once they can.
    A non-loopback bind plus either a public hostname or an empty (= anything)
    allowlist is a deployment somebody else can reach. Returning a string rather
    than raising keeps a plain import side-effect-free, which the offline tools
    (verify_record, retranscribe) and the test suite depend on; the
    startup hook below is what actually refuses. The preflights are held to the
    same rule and for the same reason — see run_preflights: an import decides
    nothing, contacts nothing and writes nothing.
    """
    if SESSION_KEY:
        return None
    # Bind address first, allowlist second. ALLOWED_HOSTS DEFAULTS to the two
    # production hostnames, so testing the allowlist alone refused to start on a
    # fresh clone following the README quick start — which is worse than the
    # exposure it prevents, because the first thing a new contributor meets is a
    # server that will not run. What actually decides whether a stranger can
    # reach this process is the interface it binds: loopback is reachable only
    # from this machine, whatever hostnames it would answer to.
    host = os.getenv("HOST", "127.0.0.1").strip()
    if host in ("127.0.0.1", "localhost", "::1", ""):
        return None
    # "testserver" is the ASGI test harness's own host name, not a routable one:
    # a suite that stands the app up on a local-only allowlist is a development
    # server by any reading, and refusing it would only teach everybody to set
    # SESSION_KEY=x — which is how the production value ends up being "x".
    local = ("localhost", "127.0.0.1", "::1", "testserver")
    public = [h for h in ALLOWED_HOSTS if h not in local]
    # An EMPTY allowlist is the widest setting there is, not the narrowest.
    # `public` is computed by filtering ALLOWED_HOSTS, so [] used to read as
    # "no public hostnames, therefore local dev" — while line 57's own comment
    # tells operators that emptying ALLOWED_HOSTS disables the host check, which
    # is the first thing anyone reaches for when the ALB or a new hostname trips
    # the guard. On a non-loopback bind that combination is the worst of both:
    # the researcher surface unauthenticated AND the Host allowlist off, so the
    # process answers to any name pointed at it. Describe it the way it behaves.
    answers_to_anything = not ALLOWED_HOSTS
    if not public and not answers_to_anything:
        return None
    return (
        "SESSION_KEY is empty but this deployment binds "
        + host
        + " and answers to "
        + (", ".join(public) if public else "any Host header (ALLOWED_HOSTS is empty)")
        + ". Without it every researcher route — recorded encounters, the "
        "download zips, the whole dataset — is open to anyone who finds the "
        "hostname. Set SESSION_KEY, or bind HOST=127.0.0.1, or set "
        "ALLOWED_HOSTS=localhost,127.0.0.1 for local development."
    )


async def _line_buffer_stdout_on_startup() -> None:
    """Startup hook, and the first one: make this process's own output arrive.

    Every diagnostic in this file is a `print()`, which is stdout, and Python
    block-buffers stdout whenever it is not a terminal — which is every
    deployment, every `> server.log`, every `| tee`, every container that
    captures its own output, and every launcher a researcher is likely to be
    handed. uvicorn's INFO lines are stderr and are not buffered, so the two
    streams separate completely and the boot warnings sit in a 8 KB buffer
    while the INFO lines stream past them.

    MEASURED, `python -m uvicorn server.app:app > server.log`, with a
    required variable unset (at the time, the one a wave depended on):

        INFO:     Started server process [51700]
        INFO:     Waiting for application startup.
        INFO:     Application startup complete.
        INFO:     Uvicorn running on http://127.0.0.1:8792

    — and that is the WHOLE file for as long as the server runs. /health says
    "degraded", ready false, and names the variable the entire time. The
    WARNING lines that name it appeared only when the process was killed and
    the buffer was flushed on exit. So the one line telling an operator that
    nothing will be recorded is invisible exactly when it is needed, and
    legible only once the server is stopped.

    Line buffering, not `flush=True` at forty call sites: it covers the
    request-time notices too — a refused capture, a withdrawal, an entry link
    that lost its key — which have the same problem and are the lines somebody
    tails a log for.

    In a startup hook rather than the module body because this module's rule is
    that an import decides nothing and changes nothing (see
    _refuse_unprotected_public_start): a process that is going to serve
    configures its own output; an offline tool that merely imports this module
    keeps whatever buffering its own launcher gave it.

    Guarded and silent: a stdout that cannot be reconfigured (a replaced
    stream, a pytest capture object, an embedding host) is not a reason to
    refuse to serve, and there is nothing an operator could do about it.
    """
    import sys

    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(line_buffering=True)
        except Exception:  # noqa: BLE001 — a log setting must not stop a server
            pass


app.router.on_startup.append(_line_buffer_stdout_on_startup)


async def _refuse_to_serve_unprotected() -> None:
    """Startup hook: come up serving, or do not come up.

    On the router's startup list rather than in the module body so that
    importing server.app stays safe, and rather than only in __main__ so that
    any launcher — uvicorn's CLI, gunicorn, a test harness — is held to it too.
    Raising here is what stops the process: uvicorn reports the failure and
    exits non-zero instead of binding a port.
    """
    refusal = _refuse_unprotected_public_start()
    if refusal:
        print(f"  REFUSING TO START: {refusal}")
        raise RuntimeError(refusal)


app.router.on_startup.append(_refuse_to_serve_unprotected)


async def _init_storage_on_startup() -> None:
    """Startup hook: create the data directory and the index, once, up front.

    This is the half of the import fix that stops "lazy" from meaning "never".
    storage.init_storage is idempotent and every writer calls it, so the store
    would come up on the first encounter anyway — but a data directory that is
    unwritable (a mis-mounted volume, a read-only filesystem, wrong ownership in
    the container) must surface when the process starts, not halfway through the
    first paid participant's conversation. Anything init_storage raises stops
    startup here, which is the loud failure: uvicorn exits non-zero rather than
    binding a port and accepting encounters it cannot record.

    Ordered AFTER the refusal above, for the same reason the preflights are: a
    deployment that must not serve does not get to mint a data directory on its
    way to exiting.
    """
    init_storage()


app.router.on_startup.append(_init_storage_on_startup)


async def _run_preflights_on_startup() -> None:
    """Startup hook: the gateway and bucket checks, once the process is serving.

    Ordered AFTER the refusal above so a deployment that must not serve does not
    first spend seconds on network probes, and does not write the preflight
    marker into the study bucket on its way to exiting.

    run_preflights swallows and names anything either check throws, so nothing
    here can take the port down: a diagnostic that can stop a process which
    could still run encounters is worse than no diagnostic.
    """
    run_preflights()


app.router.on_startup.append(_run_preflights_on_startup)




# Participant-facing HTML must never be cached. A stale build is invisible to
# the participant and looks like a broken feature, and during collection it
# would mean people running different versions of the instrument.
@app.middleware("http")
async def _no_store_html(request, call_next):
    response = await call_next(request)
    ctype = response.headers.get("content-type", "")
    if ctype.startswith("text/html"):
        response.headers["Cache-Control"] = "no-store, must-revalidate"
        response.headers["Pragma"] = "no-cache"
    return response


app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


# What an unauthenticated caller may learn about the study bucket. /health is
# reachable from the open internet — it carries no check_key and is the one path
# exempted from the Host allowlist above (the ALB health check addresses the
# task by its private IP), and infra/terraform/alb.tf forwards every path to the
# target group by default — so everything it returns is public by construction.
#
# The preflight dict itself is a diagnostic for an operator and holds three
# things that must not be: the bucket name and region (a free target for exactly
# the "seed/abuse the study bucket" attack video.presign_upload's one-shot guard
# exists to prevent, and it names the bucket holding IRB-recorded encounters),
# whether the task has credentials at all, and `detail` — a raw str(exc) from
# boto3 client construction that nothing redacts. Project instead of blacklist,
# so a field added to the preflight later is private until someone says
# otherwise rather than published the day it appears.
#
# `checked` is published because without it `ok: null` is unreadable: a probe
# cannot tell "the bucket check ran and could not decide" from "the check has
# not run in this process at all", and the second is the ordinary state of every
# import that is not a served app. It says whether a check happened, never what
# it found, so it names nothing a stranger may not know.
_PUBLIC_STORAGE_FIELDS = ("ok", "checked", "readable", "writable", "error_code")


def _health_status(missing: list) -> str:
    """The one word at the top of /health, and whether it may say "ok".

    D3. This route answered `200 {"status": "ok"}` while its own `config` block
    said ok:false — a state in which (on the build of the day) every voice
    socket closed 4403, runs kept accumulating and NOTHING was recorded.
    That combination is what made the silent void silent: an uptime check, a
    status page and an operator's curl all read the top-level word, and the top
    level said the box was fine while the wave recorded zero encounters.

    WHY THE STATUS CODE STAYS 200, WHICH IS THE HALF THAT COULD HAVE CAUSED AN
    OUTAGE. infra/terraform/alb.tf's target group health checks path "/health"
    with matcher "200", interval 30, unhealthy_threshold 3 — and ecs.tf sets
    deployment_minimum_healthy_percent = 100. Answering 503 here when a
    required variable is unset would therefore take every task out of
    service 90 seconds after it booted and wedge the deploy that introduced it,
    turning a wave that records nothing into a site that serves nothing. The
    argument for it is real — a deployment that cannot record must not pretend
    to run — but it is an argument for a DIFFERENT deploy order than the one
    this service actually has: the target group would have to be repointed at a
    liveness path first, by hand, with an `aws elbv2 modify-target-group`,
    because this stack's Terraform state is not in the account (see
    docs/DEPLOY-AWS.md) and every task revision here was registered by CLI. A
    change whose safety depends on somebody having run an unrelated command
    first is a crash loop waiting for the one deploy where they did not.
    So: the code stays 200 for the load balancer, and the WORD stops lying.

    `ready` beside it is the same answer for a caller that wants a boolean
    instead of matching a string — an uptime check can be pointed at
    `.ready == true` and will fire the moment the variable goes missing, which
    is the alarm this deployment did not have.
    """
    return "ok" if not missing else "degraded"


#: The last answer missing_required_env() gave, or None if it has never
#: returned one. See below for why it exists and why None is not [].
_LAST_REQUIRED_ENV: Optional[list] = None


def _prime_required_env_scan() -> None:
    """Pay the REQUIRED_ENV source scan HERE, at import, so no request can.

    storage.missing_required_env() is cheap on its second call and expensive on
    its first: _declared_required_env() ast.parse()s all 33 .py files under
    server/ and memoises the result in storage._SCANNED_REQUIRED_ENV. Measured
    on this tree: 157 ms cold on CPython 3.12 and 250-300 ms cold on 3.13,
    against 0.1-0.5 ms warm.

    /health is `async def` and calls it per request, so a cold cache puts that
    sweep ON the event loop. For a quarter of a second nothing else runs: not
    the audio relay, not the silence detector that fires the planted probes,
    not any other encounter on the task. It showed up as
    tests/test_api_blockers.py::test_a_slow_head_does_not_freeze_the_loop…
    going red about one run in seven on windows-latest x 3.13 — the loop gap it
    measures was entirely this scan and not the S3 HEAD the test is named for
    (reproduced with head_calls == 0 and a 0.15-0.18 s gap on 3.12; 3.13 simply
    ran out of the headroom 3.12 still has).

    Priming at import rather than in a startup hook, and not by making /health a
    plain `def`:

    *   The startup hooks already warm it in production — _check_required_env()
        says so in its own docstring — so a deployed task never paid this inside
        a request. But anything that mounts the ASGI app without running its
        lifespan does: httpx.ASGITransport, which the blocker suite uses, and
        any future harness or embedding that does the same. Import happens
        before either, unconditionally, and cannot be reordered away.
    *   Making the route a threadpool route would move the sweep off the loop
        but would put the LIVENESS PROBE behind anyio's bounded worker pool,
        alongside the deliberately-blocking webcam routes. A /health that can
        queue is the one thing _health_status()'s docstring above exists to
        prevent: this target group replaces every task on three missed probes,
        with no rollback. It stays `async def`, and it stays instant.

    Swallows everything, for _missing_required_env_for_health()'s reason one
    level up: a diagnosis may not be the thing that stops the process from
    importing. If it fails here, the first /health pays the scan exactly as it
    did before — slow, and still correct.
    """
    try:
        from .storage import _declared_required_env

        _declared_required_env()
    except Exception as e:  # noqa: BLE001 — warming a cache may not kill import
        print(f"  NOTE: could not pre-scan REQUIRED_ENV at import "
              f"({type(e).__name__}: {e}); /health will scan on first call.")


_prime_required_env_scan()


def _missing_required_env_for_health() -> list:
    """missing_required_env(), but it may not take the service off the load
    balancer.

    The route above spends a long comment arguing that /health must keep
    answering 200 when the deployment is misconfigured, because the target group
    matches "200" with unhealthy_threshold 3 over a 30 s interval, the service
    sets deployment_minimum_healthy_percent = 100, and nothing here sets
    health_check_grace_period_seconds or a deployment circuit breaker — so a
    non-200 on this path replaces every task about ninety seconds after it boots
    and keeps doing it, with no rollback. An uncaught exception is a 500, and a
    500 is a non-200: the config block added two lines below the argument could
    reintroduce the exact outage the argument exists to prevent, by a different
    door.

    It is unlikely — storage._scan_required_env already catches OSError,
    SyntaxError and ValueError per file — but "unlikely" is the wrong standard
    for a function whose failure mode is a permanent crash loop. The realistic
    triggers are the ones that fall outside a per-file except: the directory
    walk itself failing, a RecursionError or a MemoryError on a pathological
    source file. So the diagnosis is allowed to fail, and when it does the route
    reports the last answer it had rather than the process disappearing.

    The fallback is deliberately NOT the empty list, at either stage. Empty
    means "nothing is missing", which is the one sentence this whole block was
    written to stop /health saying when it is not true. So a failed re-check
    reports the last answer that was actually computed, and a failure with no
    earlier answer at all reports every required variable as unproven — because
    that is what it is: the check that would have proven them did not run.
    """
    global _LAST_REQUIRED_ENV
    try:
        _LAST_REQUIRED_ENV = list(missing_required_env())
    except Exception as e:  # noqa: BLE001 — a diagnosis may not kill the task
        if _LAST_REQUIRED_ENV is None:
            from .storage import REQUIRED_ENV

            _LAST_REQUIRED_ENV = sorted(REQUIRED_ENV)
            known = "no check has ever succeeded, so every one is unproven"
        else:
            known = f"reporting the last known answer {_LAST_REQUIRED_ENV!r}"
        print(f"  WARNING: /health could not check the required environment "
              f"({type(e).__name__}: {e}); {known}.")
    return list(_LAST_REQUIRED_ENV)


@app.get("/health")
async def health(key: Optional[str] = Query(None)) -> dict:
    """Liveness probe for the ALB target group. Deliberately unauthenticated and
    dependency-free: it answers whether this process can serve, not whether the
    model gateway or the study bucket is reachable, so a transient upstream blip
    cannot cause ECS to kill healthy tasks mid-encounter.

    Public. Answers 200 to anyone, so nothing here may say more than a stranger
    may know — see _PUBLIC_STORAGE_FIELDS. A valid researcher key widens the
    storage block to the operator's full diagnosis; an absent or wrong key is
    never an error here, it just gets the narrow answer, because refusing the
    probe would take the task out of service."""
    # Nested so the liveness field cannot be shadowed by preflight keys.
    # active_sessions: a deploy rollout retires the old task within ~2 minutes,
    # which cuts any encounter running on it. Check this is 0 before applying.
    #
    # storage is the startup answer, not a fresh call: this route is hit every
    # 30 s by the health check and a HEAD per probe would be both a cost and a
    # way for a slow bucket to make a healthy task look dead. It is here so a
    # misconfigured bucket is one curl away instead of a discovery made after
    # collection, and the 200 stays unconditional for exactly that reason.
    # `ok: null, checked: false` is what a process that never ran its startup
    # hooks reports — an import, not a deployment — and is a genuine unknown
    # rather than a bucket that failed its check.
    #
    # The gateway block is published whole: llm.preflight is documented as
    # public and puts every message it carries through redact_key. The storage
    # block has no such contract, so it is projected.
    #
    # Compared explicitly rather than through check_key, which waves everyone
    # through when SESSION_KEY is empty — that would publish the full block on
    # precisely the deployment least able to afford it.
    authorised = bool(SESSION_KEY) and bool(key) and secrets.compare_digest(key, SESSION_KEY)
    storage = (dict(_STORAGE_PREFLIGHT) if authorised else
               {k: _STORAGE_PREFLIGHT.get(k) for k in _PUBLIC_STORAGE_FIELDS
                if k in _STORAGE_PREFLIGHT})
    # The third block, and the one that closes the quietest failure this
    # project has had: a deployment missing a variable the wave depended on
    # closed every voice socket 4403, kept minting runs and answered this route
    # 200 — so the first evidence of it was an empty dataset at the end of the
    # wave. storage.missing_required_env was written to be what "a boot
    # preflight and /health should publish" and was wired to neither; it is
    # wired to both now (REQUIRED_ENV is empty today, see _check_required_env).
    #
    # Computed per request rather than cached at startup, unlike the two blocks
    # above, so a variable that appears after boot (a task redeployed with the
    # value, a secret that resolved late) stops showing as missing.
    #
    # What that costs, stated accurately, because the sentence that used to be
    # here — "an os.environ read, not a network round trip" — is what would stop
    # the next reader from spotting the one real hazard on this line. Warm, it
    # is an os.environ read and the claim holds. COLD, the first call also
    # ast.parse()s all 33 source files under server/: 157 ms on 3.12, 250-300 ms
    # on 3.13, on this event loop, blocking every encounter on the task. Only
    # the static declaration scan is memoised, never the environment read, so
    # the late-secret behaviour above is untouched. _prime_required_env_scan()
    # at import is what guarantees the cold path is unreachable from here; if
    # that is ever removed, this line becomes a quarter-second loop stall again.
    #
    # Published unauthenticated, which the storage block deliberately is not.
    # These are variable NAMES, never values: every one of them is required to
    # appear in .env.example, in the ECS task definition and in docs/ by
    # tests/test_required_deployment_env.py, so naming it here tells a stranger
    # nothing the repository does not. Withholding them would make the field
    # unreadable to the one person who can act on it.
    missing = _missing_required_env_for_health()
    return {"status": _health_status(missing),
            "ready": not missing,
            "gateway": _PREFLIGHT, "storage": storage,
            "config": {"ok": not missing, "missing_required_env": missing},
            # Not a secret, and the only way to check the researcher credential
            # from outside a running task: an operator who has just deployed
            # needs to know the key landed in the environment, and finding out
            # by fetching /api/ratings without one is a worse way to learn it.
            # False here says nothing an unauthenticated GET of any researcher
            # route would not already prove.
            "session_key_configured": bool(SESSION_KEY),
            "active_sessions": len(registry.list_ids())}


def check_key(key: Optional[str]) -> None:
    """Guard for researcher and study-data routes.

    SESSION_KEY protects recorded encounters, downloads, and the researcher
    views. It must NOT be required of participants: their link is handed to
    every recruited person, and a key that opens the whole dataset should not
    travel that way.
    """
    if SESSION_KEY and key != SESSION_KEY:
        raise HTTPException(status_code=401, detail="Bad or missing key")


# Participants arrive from Qualtrics with their CloudResearch key in the URL and
# nothing else. Set PARTICIPANT_KEY_REQUIRED=1 to also demand SESSION_KEY on
# their routes (useful while the study is not yet open).
PARTICIPANT_KEY_REQUIRED = os.getenv("PARTICIPANT_KEY_REQUIRED", "").strip() not in ("", "0", "false")


def check_participant(key: Optional[str]) -> None:
    if PARTICIPANT_KEY_REQUIRED:
        check_key(key)


def _operator_key(key: Optional[str]) -> bool:
    """Is this caller the researcher rather than a participant?

    The same rule check_key enforces, as a question instead of a refusal, for
    the places where a participant-open route offers an operator something extra
    (choosing a run's cohort) rather than refusing them outright. Open when no
    SESSION_KEY is configured, which is local dev and is how the rest of the app
    behaves; compared in constant time when one is, since the answer decides
    whether a run counts as study data.
    """
    if not SESSION_KEY:
        return True
    return bool(key) and secrets.compare_digest(key, SESSION_KEY)


# --- HTML routes ---

@app.get("/", response_class=HTMLResponse)
async def landing_page(request: Request, key: Optional[str] = None):
    """Landing page with the scenario picker popup.

    A participant arriving from Qualtrics carries their id in the query. The
    base URL is what gets pasted into the survey, so forward those visitors
    to the study entry point before the researcher-key check (origin/main
    6b504f3).

    This forwards to `/start`: the study run, four constructs, one encounter
    each — the same run the pasted link in docs/OPERATIONS.md starts.

    The forwarded request goes through entry_params and the link-probe filter
    like any other, which is the point: a scanner or an unfurler that fetches
    the base URL is still shown the entry check page and still mints no run.
    `participantId` is in the tuple below AND in entry_params, so the key
    survives the hop -- without the second half, a participantId-only arrival
    reads as "no key parameter at all" at /start and is shown the check page
    instead of a run.

    check_key stays exactly where origin/main put it: AFTER the redirect. The
    researcher-key check is what a participant would otherwise hit, and hitting
    it is the bug this fixes.
    """
    q = request.query_params
    if any(k in q for k in ("pid", "participant_id", "participantId", "PROLIFIC_PID")):
        return RedirectResponse(url=f"/start?{request.url.query}", status_code=307)
    check_key(key)
    return (STATIC_DIR / "landing.html").read_text(encoding="utf-8")


@app.get("/researcher", response_class=HTMLResponse)
async def researcher_page(key: Optional[str] = None):
    check_key(key)
    return (STATIC_DIR / "researcher.html").read_text(encoding="utf-8")


@app.get("/test")
async def start_test_run(
    name: Optional[str] = None,
    variant: Optional[str] = None,
    key: Optional[str] = None,
):
    """Internal testing entry. Tags the run cohort=internal so test traffic can
    never be mistaken for study data, and needs no Qualtrics setup: pass a name
    so bug reports can say whose session it was.

    THIS DOOR STAYS. The researcher demos the platform to their lab through it,
    and the demo entrance of the next phase is built on top of it. What it does
    not stay is open: GET /test and the voice socket reached live audio
    and webcam capture in two requests from anywhere on the internet, on the
    study's gateway budget. Containment held — the run is cohort=internal and
    falls out of ?cohort=study — so what was exposed was spend and recording
    rather than the dataset, but a recording surface with no credential in front
    of it is not one to leave on a public host.

    Gated the way the rest of the app gates things (check_key): open when no
    SESSION_KEY is configured, which is a local checkout and is how /researcher
    and every download route already behave, and the researcher key when one is.
    Anyone holding that key keeps the entrance they had.
    """
    check_key(key)

    from fastapi.responses import RedirectResponse

    from . import runs

    # Same refusal the participant links make, and the same 400: a letter no
    # form carries used to pin every construct to nothing and switch off
    # FORM_EXCLUSIONS, so a tester walking the study saw a run the study can
    # never produce. See runs.normalize_variant.
    try:
        variant = runs.normalize_variant(variant)
    except ValueError as e:
        raise HTTPException(400, str(e))

    tester = (name or "anon").strip().replace(" ", "_")[:24]
    run = runs.create(
        f"test_{tester}_{int(time.time())}",
        variant=variant, cohort="internal",
    )
    # Mint the participant record here, exactly as /start does, and carry it in
    # the redirect. The cohort tag only reaches an encounter's manifest through
    # _run_context, which resolves the run from the participant *record* id on
    # the voice socket. Without a record minted against this run, the page
    # minted a second record that no run pointed at, and the internal encounter
    # recorded cohort=null — so it was excluded from ?cohort=study but
    # invisible to ?cohort=internal too, and the tag was true only at the run
    # level. Minting it here also means the internal path exercises the same
    # identity code the study path does, which is the point of a test entrance.
    q = f"?run={run['run_id']}"
    try:
        pid_record = create_participant(
            code=run["participant_id"],
            # Same binding /start writes, and the reason the demo door needs it
            # too: cohort="internal" is the whole point of this entrance, and a
            # record that does not carry it can have that tag re-derived away by
            # a later run. See _run_context.
            run_id=run["run_id"], cohort=run.get("cohort", "internal"),
        )
        run["participant_record_id"] = pid_record
        runs.save(run)
        q += f"&participant_id={pid_record}"
    except Exception:  # noqa: BLE001, a test entrance must still open
        pass
    return RedirectResponse(url=f"/v2{q}", status_code=307)


@app.get("/api/runs")
async def api_runs_export(key: Optional[str] = None, cohort: Optional[str] = None):
    """The join table for analysis: every run with its participant key,
    Qualtrics response id, cohort, completion code, and the session ids of the
    encounters it produced.

    `cohort` is one of "study", "internal" (the /test entrance) or
    "unattributed" (a participant whose survey key did not pipe). Filtering to
    "study" is the supported way to get the analysis set; the unattributed runs
    are not discarded, because each one is a real consented encounter that a
    hand-join can still rescue."""
    check_key(key)
    from .runs import RUNS_DIR, completion_code

    out = []
    if RUNS_DIR.exists():
        for f in sorted(RUNS_DIR.glob("*.json")):
            try:
                run = json.loads(f.read_text(encoding="utf-8"))
            except ValueError:
                continue
            if cohort and run.get("cohort", "study") != cohort:
                continue
            out.append({
                "run_id": run["run_id"],
                "participant_id": run.get("participant_id"),
                "qualtrics_id": run.get("qualtrics_id"),
                "cohort": run.get("cohort", "study"),
                # Surfaced so a Qualtrics piping failure (a run carrying a
                # synthetic unattributed_* key) or a key presented with two
                # different survey responses is visible in the export itself,
                # not only in a server log line.
                "participant_key_status": run.get("participant_key_status"),
                # What actually arrived when the key was unusable. This is the
                # only thing an unattributed run can be hand-joined on, and the
                # export is where that join gets done, so withholding it here
                # would leave the run permanently unattributable. Null whenever
                # the key was fine.
                "raw_participant_key": run.get("raw_participant_key"),
                "qualtrics_id_conflict": bool(run.get("qualtrics_id_conflict")),
                "created_at": run.get("created_at"),
                "finished": run.get("index", 0) >= len(run.get("scenarios", [])),
                "completion_code": completion_code(run),
                # Present only on a run the participant stopped, or one that
                # ended because they declined the consent form. Without it the
                # export cannot distinguish a withdrawal from a dropout, and
                # "finished: false" reads the same for both — which is the
                # difference between a participant who left and one whose
                # browser died, and an IRB report needs the first number.
                "withdrawn": run.get("withdrawn"),
                # Which forms were steered by the cross-construct exclusion
                # rather than drawn, so an analyst who sees one variant
                # over-represented can tell design from chance.
                "form_exclusions": run.get("form_exclusions", []),
                # How the construct order was chosen (Williams square row), so
                # position effects can be modelled without re-deriving the row.
                "order": run.get("order"),
                # Which entry link this participant came in on, and what that
                # link's arm excluded. Flat `arm` because that is the column an
                # analysis groups by; the whole record beside it because the arm
                # name alone does not say that a restricted run served both
                # parallel forms of its constructs, which is what stops a
                # between-arm comparison being read as a pre/post one.
                "arm": (run.get("construct_pool") or {}).get("arm", "full"),
                "construct_pool": run.get("construct_pool"),
                # Present only when one participant key has runs in more than
                # one arm. Null on every ordinary run.
                "other_arm_runs": run.get("other_arm_runs"),
                "encounters": [
                    {
                        "scenario": c.get("id"),
                        "session_id": c.get("session_id"),
                        # The counterbalancing cell, which is otherwise only
                        # recoverable by re-deriving the order from `assigned`.
                        "position": i + 1,
                        "construct": c.get("construct"),
                        "variant": c.get("variant"),
                        "parallel_form": c.get("parallel_form"),
                        "no_participant_turns": bool(c.get("no_participant_turns")),
                    }
                    for i, c in enumerate(run.get("completed", []))
                ],
                "assigned": [sc.get("id") for sc in run.get("scenarios", [])],
            })
    return out


# --- The participant entry links -------------------------------------------
#
# Three links go into Qualtrics, and two of them are these. They differ in
# exactly one thing — which constructs the run may draw from — and in nothing
# else: same participant-key spellings, same validation, same resume, same four
# encounters, same completion code.
#
# That is why the query parameters are declared once, below, and depended on by
# every entry route rather than repeated in each signature. The failure this
# avoids is specific and has happened to this endpoint before: the spellings
# Qualtrics pipes a key under (pid, participant_id, PROLIFIC_PID) and the
# validation that rejects an unreplaced ${e://Field/...} live in one place, so a
# fourth spelling or a new placeholder is added once and all three links get it.
# Three hand-written handlers would have drifted the first time one was edited,
# and the symptom — one link silently accepting the literal placeholder and
# collapsing every participant into one shared run — is invisible until analysis.

def entry_params(
    request: Request,
    key: Optional[str] = None,
    pid: Optional[str] = None,
    participant_id: Optional[str] = None,
    participantId: Optional[str] = None,
    PROLIFIC_PID: Optional[str] = None,
    variant: Optional[str] = None,
    qid: Optional[str] = None,
    cohort: Optional[str] = None,
) -> dict:
    """The query parameters every participant entry link accepts.

    One declaration, three routes. `qid` is the Qualtrics ResponseID and is the
    join between a survey response and a run, so it is accepted on every arm and
    not only on /start.

    The request itself is carried alongside them, because whether to ENROL
    somebody is a question about who is at the door and not only about what is
    in the URL — see _link_probe_reason below.
    """
    return {
        "key": key, "pid": pid, "participant_id": participant_id,
        # The fourth spelling, from origin/main 6b504f3. The comment above this
        # function says a fourth spelling "is added once and all three links get
        # it"; this is that addition. It is the spelling the base-URL redirect
        # on the landing page forwards, so /start would otherwise be handed a
        # key it does not read.
        "participantId": participantId,
        "PROLIFIC_PID": PROLIFIC_PID, "variant": variant, "qid": qid,
        "cohort": cohort, "request": request,
    }


# WHY A GET OF AN ENTRY LINK IS NOT ALWAYS AN ARRIVAL.
#
# GET /start creates a study-cohort run and mints a participant record. That is
# right for the person the link was handed to and wrong for everything else that
# fetches a URL — and a participant link spends its whole life in places that
# fetch URLs nobody clicked: the Qualtrics survey body, the recruitment email,
# the Slack or Teams channel where the team pastes it to check it, a security
# scanner's crawl, a browser prefetching a link that was only hovered. Two runs
# were created on production in one day by exactly that, one of them by a bare
# GET carrying no participant key at all. Each is a row in the study cohort with
# no human behind it, and the link is about to be pasted into a live survey.
#
# THE FIX IS NOT TO REQUIRE POST. The entry link is a GET in a Qualtrics
# redirect, and a real participant arriving by GET must still get their run:
# turning them away costs the encounter outright, which is the one thing this
# entry surface is built never to do. The two cases are separated instead, on
# the two signals that actually tell them apart:
#
#   * the request says it is not a person — an unfurler's user agent, or a
#     prefetch/preview purpose header. No browser a participant drives sends
#     these, and everything that does is fetching the link rather than
#     following it.
#   * the URL CARRIES NO PARTICIPANT KEY THIS SERVER CAN USE — either no key
#     parameter at all, or one whose value normalize_participant_key cannot
#     accept.
#
# The second half of that used to read "no key PARAMETER at all", on the
# argument that a survey whose piping has broken still sends the parameter, so
# its presence marked a real participant. It does not. THE RAW TEMPLATE LINK IS
# THE STRING WITH THE PARAMETER ON IT — ?pid=${e://Field/ParticipantKey} is
# exactly what the team pastes into Slack, Teams and the recruitment email to
# check it, and exactly what a corporate link scanner, a mail previewer and a
# calendar client then fetch. Measured: with that link, an Outlook user agent,
# a Defender or Proofpoint rewrite, a Zoom preview, and a request with no user
# agent at all each minted a run, because the key parameter was present and the
# user-agent denylist was the only thing left deciding. A denylist is the wrong
# thing to be the only thing deciding, and the two cases were never
# distinguishable anyway: "a participant whose pipe broke" and "the template
# nobody has piped yet" are the same bytes.
#
# So an unusable value is treated as no key, and the participant whose pipe
# broke pays one click for it. That is the whole cost, it is paid by somebody
# whose session already needs a hand-join, and it is the trade this surface
# makes everywhere: never refuse an arrival, and never enrol a fetch.
#
# Neither case is refused. Both get a 200 page carrying one Continue button that
# POSTs back to the same URL, and that POST enrols them exactly as the GET would
# have — into the same `unattributed` run, with the same raw key kept for the
# hand-join, and (see runs.find_unattributed_for_survey_response) into the run
# they already have if they press it twice. A person who has been misjudged pays
# one click; an unfurler, a prefetch and a scanner enrol nobody, because none of
# them presses a button.

#: User agents that fetch a link because somebody pasted it. Matched loosely and
#: case-insensitively on purpose: a false positive costs a participant one
#: click, and a false negative costs the study a run nobody sat behind.
_LINK_PROBE_UA_RE = re.compile(
    r"bot[/\s;)]|bot$|spider|crawler|unfurl|preview|link[-_ ]?check|validator|"
    r"scanner|monitor|facebookexternalhit|slack|discord|telegram|whatsapp|"
    r"skype|embedly|iframely|pinterest|curl/|wget/|python-requests|"
    r"go-http-client|okhttp|java/|libwww|headlesschrome|phantomjs",
    re.I,
)

#: How a browser says it is fetching this URL speculatively rather than because
#: somebody asked for it: Chrome sends Sec-Purpose (and once X-Purpose), Firefox
#: X-Moz. A prefetched entry link minted the run before the participant had
#: decided to click, and if they then did not click, the run stayed.
_PREFETCH_HEADERS = (
    ("sec-purpose", ("prefetch", "prerender")),
    ("purpose", ("prefetch", "preview")),
    ("x-purpose", ("prefetch", "preview")),
    ("x-moz", ("prefetch", "prerender")),
)

#: The reason strings for the other half: nothing at the door this server can
#: enrol. Two of them because they send an operator to different places — one
#: says the link lost its parameter, the other says the parameter is carrying
#: template text — and both mean the same thing to the person at the door.
_NO_KEY_AT_ALL = "no participant key parameter on the link"
_KEY_UNUSABLE = "the participant key on the link is not usable"

#: Reasons that describe the LINK rather than the requester. The page says
#: something different for these: whoever is reading it may well be a
#: participant whose survey piping broke, and telling them a machine fetched
#: the link would be both false and no help.
_LINK_FAULT_REASONS = (_NO_KEY_AT_ALL, _KEY_UNUSABLE)


def _link_probe_reason(request: Optional[Request]) -> Optional[str]:
    """Why this request is something fetching the link rather than somebody
    following it, or None.

    A short token rather than a bare True: when an operator asks why a wave is
    one arrival short, "Slack fetched the link" and "the browser prefetched it"
    send them to different places. The user agent is never echoed whole — the
    matched token is what can be acted on.
    """
    if request is None:
        return None
    headers = request.headers
    for name, values in _PREFETCH_HEADERS:
        raw = (headers.get(name) or "").lower()
        for value in values:
            if value in raw:
                return f"{name}: {value}"
    match = _LINK_PROBE_UA_RE.search(headers.get("user-agent") or "")
    if match:
        return f"user agent contains {match.group(0).strip()!r}"
    return None


# The page both cases get. A whole self-contained document deliberately: it is
# served to whatever fetched the link, so it may not depend on /static, on the
# gateway, or on anything else this process might be failing at.
#
# action="" posts to the current URL, query string and all, which is what
# carries ?variant=, ?qid= and the rest through to the POST — and it means no
# value out of the URL is ever written into this HTML, so there is nothing here
# to escape and nothing to get wrong.
_ENTRY_CHECK_PAGE = """<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="robots" content="noindex, nofollow">
<link rel="icon" type="image/png" href="/static/favicon.png">
<link rel="apple-touch-icon" href="/static/apple-touch-icon.png">
<title>Relational Fluency study</title>
<style>
 body {{ font: 16px/1.5 system-ui, -apple-system, "Segoe UI", Roboto, sans-serif;
        max-width: 34rem; margin: 12vh auto; padding: 0 1.25rem; color: #1a1a1a; }}
 h1 {{ font-size: 1.35rem; margin-bottom: .75rem; }}
 button {{ font: inherit; padding: .7rem 1.4rem; border: 0; border-radius: .4rem;
          background: #b31b1b; color: #fff; cursor: pointer; }}
 .note {{ color: #555; font-size: .9rem; margin-top: 1.75rem; }}
</style></head><body>
<h1>You are about to start the study</h1>
<p>Press Continue to open your session. Nothing is recorded until you do, and
   your microphone and camera stay off until the consent step on the next
   screen is complete.</p>
<form method="post" action=""><button type="submit">Continue</button></form>
<p class="note">{note}</p>
</body></html>
"""

#: Said to the participant whose link lost its key: enough to take part, and
#: enough that they tell somebody, because their session will need a hand-join.
_ENTRY_CHECK_NO_KEY_NOTE = (
    "This link did not carry the identifier the survey normally adds to it. "
    "You can still take part &mdash; press Continue &mdash; but please tell the "
    "research team, because your session will have to be matched to your survey "
    "response by hand."
)

#: Said when the request looked like a link preview, a scanner or a prefetch.
#:
#: Written to be true whoever reads it, which the first draft was not. It said
#: the link "was opened by something other than a participant's browser", and
#: some participants' browsers are matched by the denylist on purpose: Slack's
#: iOS in-app browser carries the token "Slack", and Discord, Telegram, WhatsApp
#: and Skype all ship one too. Costing that person a click is the accepted
#: trade; telling them, on the one screen they read carefully, that they are not
#: a person is not — it reads as a refusal, and a participant who believes they
#: have been refused closes the tab instead of pressing the button.
_ENTRY_CHECK_PROBE_NOTE = (
    "This page appears when a link is opened by something other than a "
    "participant&rsquo;s own browser &mdash; a link preview, a scanner or a "
    "prefetch &mdash; and also when we cannot tell a browser apart from those, "
    "which can happen when the link is opened from inside a chat or email app. "
    "Nothing has been created yet. If you are here to take part, press "
    "Continue; if you are not, you can close this page and nothing will be "
    "recorded."
)


def _entry_check_page(reason: str) -> HTMLResponse:
    """200 and a Continue button. Never a refusal: see the block above."""
    note = (_ENTRY_CHECK_NO_KEY_NOTE if reason in _LINK_FAULT_REASONS
            else _ENTRY_CHECK_PROBE_NOTE)
    return HTMLResponse(_ENTRY_CHECK_PAGE.format(note=note), status_code=200)


async def _enter_study(arm: Optional[str], p: dict):
    """Start or resume this participant's run and redirect them into it.

    The whole of /start's behaviour. `arm` is always None (the full study run)
    since the two-construct arm links were removed for Study 1; the parameter
    and the arm record on the run document stay so a run still says which pool
    it drew from.
    """
    key = p.get("key")
    pid = p.get("pid")
    participant_id = p.get("participant_id")
    participantId = p.get("participantId")
    PROLIFIC_PID = p.get("PROLIFIC_PID")
    variant = p.get("variant")
    qid = p.get("qid")
    cohort = p.get("cohort")
    request = p.get("request")

    check_participant(key)
    from fastapi.responses import RedirectResponse

    from . import runs

    arm_name = (arm or "full")

    # THE ONE PLACE THIS HANDLER DECIDES NOT TO ENROL. See the block above
    # entry_params for why, and note what this is not: it is not a refusal, it
    # is not a 400, and it never applies to the POST the Continue button sends.
    # Asked before anything else because the whole value of it is that nothing
    # happened — no run, no participant record, no directory scan, and no 400
    # or 503 for an unfurl to render as a broken link either.
    confirmed = request is not None and request.method == "POST"
    if not confirmed:
        reason = _link_probe_reason(request)
        if reason is None:
            # Asked of the VALUE, not of the parameter's presence. See the
            # block above entry_params: the raw template link carries the
            # parameter, so presence proved nothing and left the user-agent
            # denylist as the only thing standing between a pasted link and a
            # study row. normalize_participant_key is the same question the
            # enrolment path asks a few lines below, so the two can never drift
            # into disagreeing about what counts as a key.
            _, at_the_door = runs.normalize_participant_key(
                pid or participant_id or participantId or PROLIFIC_PID)
            if not any(v is not None
                       for v in (pid, participant_id, participantId,
                                 PROLIFIC_PID)):
                reason = _NO_KEY_AT_ALL
            elif at_the_door != "ok":
                reason = _KEY_UNUSABLE
        if reason is not None:
            print(f"  NOTE: entry link for arm {arm_name!r} was fetched with "
                  f"{reason}; serving the entry check page and enrolling "
                  f"nobody. A participant reaching this presses Continue.")
            return _entry_check_page(reason)

    # ?variant= pins the form of every construct in the run, which makes it a
    # study parameter and not a participant one. Round two gated ?cohort= right
    # below, in this same handler, and left this one open — and open it was
    # worse than the thing that was closed: with SESSION_KEY set and no key in
    # the URL, /start?variant=A served the S1 A + Teamwork pairing the
    # instrument forbids on 200 runs in 200 (against 0 in 200 with the
    # parameter absent), and stamped every one of them "form was pinned by the
    # caller; exclusion not applied" — the run document explaining away the
    # discriminant-validity pairing FORM_EXCLUSIONS exists to prevent, on behalf
    # of a caller who was a stray query parameter in a redirect. Qualtrics
    # forwards whatever is in the URL, so this arrives on all three links.
    #
    # Ignored rather than refused, exactly as a participant-supplied ?cohort=
    # is: the safe reading of a stray parameter on a recruited person's link is
    # the run they should have had anyway, and a 400 at the door mid-study costs
    # the encounter outright. The operator, who holds the key, still gets the
    # 400 below, because for them it is a link they can fix.
    if variant and not _operator_key(key):
        print(f"  WARNING: entry link for arm {arm_name!r} was given "
              f"?variant={variant!r} without the researcher key; ignoring it "
              f"and drawing this participant's forms as the study intends.")
        variant = None

    # A form letter no scenario carries is refused here rather than recorded.
    # Left alone it did not fail: create() pinned every construct to a letter
    # nothing matched, drew at random anyway, and then declined to apply
    # FORM_EXCLUSIONS "because the caller pinned it" — so one wrong letter in
    # the Qualtrics redirect served the forbidden S1 A + Teamwork pairing to
    # about half a wave and wrote an explanation for it onto every run. 400
    # rather than the 503 an unbuildable arm gets: this is a bad parameter in a
    # link somebody pasted, and the operator fixes it in Qualtrics.
    try:
        variant = runs.normalize_variant(variant)
    except ValueError as e:
        print(f"  WARNING: entry link for arm {arm_name!r} was given an "
              f"unusable ?variant=: {e}. No run was created.")
        raise HTTPException(400, str(e))

    # ?cohort= decides whether this run is study data at all — cohort=internal
    # drops the run out of ?cohort=study and exempts it from the encounter
    # floor.
    # An operator's deliberate choice is legitimate and is how the lab walks the
    # links without contaminating the wave; a participant-supplied one is a
    # recruited person being recorded and silently excluded at the same time,
    # and it arrives on all three participant links because Qualtrics forwards
    # whatever is in the URL. So it is honoured only for someone holding the
    # researcher credential, gated the way the rest of the app gates things:
    # open when no SESSION_KEY is configured (local dev), the key when one is.
    # Ignored rather than refused, because the safe reading of a stray parameter
    # on a real participant's link is the cohort they should have had anyway,
    # and a 400 mid-study costs the encounter outright.
    if cohort and not _operator_key(key):
        print(f"  WARNING: entry link for arm {arm_name!r} was given "
              f"?cohort={cohort!r} without the researcher key; ignoring it and "
              f"recording this arrival in the cohort its participant key earns.")
        cohort = None
    elif cohort:
        # An operator's own link, and their own typo. A cohort name nobody
        # defined is not a differently-grouped run, it is one that drops out of
        # ?cohort=study and ?cohort=internal at once — so every run the link
        # creates is silently invisible, and nobody finds out until an analyst
        # counts the wave and it is short. They hold the key, so they get told.
        try:
            cohort = runs.normalize_cohort(cohort)
        except ValueError as e:
            print(f"  WARNING: entry link for arm {arm_name!r} was given an "
                  f"unusable ?cohort=: {e}. No run was created.")
            raise HTTPException(400, str(e))

    raw_key = pid or participant_id or participantId or PROLIFIC_PID
    pkey, key_status = runs.normalize_participant_key(raw_key)
    if pkey is None:
        # Never turn a real participant away over the survey's broken link: they
        # are mid-study, and a 400 page costs the encounter data outright. They
        # proceed, but under a unique, clearly-marked identifier so the run
        # cannot masquerade as attributable study data and, crucially, so two
        # bad arrivals cannot collide into one shared run (which is exactly what
        # an unreplaced placeholder used to do: participant 2 landed inside
        # participant 1's half-finished run). The raw value is kept on the run
        # for a later hand-join, and the reject is logged so a piping failure
        # surfaces on the first arrival rather than at analysis time.
        pkey = f"unattributed_{secrets.token_hex(6)}"
        print(
            f"  WARNING: /start ({arm_name}) got an unusable participant key "
            f"({key_status}): {raw_key!r}. Continuing as {pkey} in cohort "
            f"'unattributed'. Check the Qualtrics ParticipantKey piping."
        )
    # A synthetic key is unique per arrival, so there is nothing to resume ON
    # THE KEY and that directory scan would only ever miss. The survey response
    # is what these arrivals still have, and it is asked separately below.
    #
    # Resume, but only into the arm they arrived for. A participant who reaches
    # the group link after starting a 1:1 run is either taking both blocks or is
    # an operator error, and the two are indistinguishable from here — but
    # handing them their 1:1 run because the key matched would mean the group
    # link quietly served a 1:1 run and recorded it as one, which no amount of
    # later inspection could catch as "the group link was clicked". So the
    # second arm gets its own run, the two are cross-linked in both directions,
    # and the event is logged. A same-arm return still resumes: that is the
    # dropped-connection case this lookup exists for.
    #
    # Asked PER ARM, not asked-then-compared. The old form took the
    # participant's newest run whatever arm it was in, so a third visit — 1:1,
    # then group, then 1:1 again — compared the group run against "one_to_one",
    # called it a cross-arm arrival and built a SECOND 1:1 run: done:false,
    # partial code, four encounters they had already done, and their finished
    # code no longer reachable from the only URL they were given. One human, three
    # participant records. A genuine cross-arm arrival is the case below, where
    # this arm has no run of theirs at all.
    run = runs.find_for_participant(pkey, arm=arm_name) if key_status == "ok" else None
    prior_arm_run = None
    if run is None and key_status == "ok":
        prior_arm_run = runs.find_for_participant(pkey)
    if run is None and key_status != "ok":
        # THE RESUME FOR THE PARTICIPANT WHO HAS NO KEY TO RESUME ON.
        #
        # An unusable key now means an entry check page with a Continue button
        # (see the block above entry_params), and a button can be pressed twice
        # — a double-click, a back-button and a second press, or the link simply
        # opened again. Every one of those used to be a fresh
        # `unattributed_<hex>` identity and therefore a fresh run: one person,
        # two rows, two participant records, two half-finished sequences and two
        # partial completion codes, on precisely the arrivals no later analysis
        # can de-duplicate, because the field that would have told them apart is
        # the field that failed to pipe. Adding a button in front of those
        # people without adding this would have been trading one stray run for
        # another.
        #
        # Their survey response is the identity that survived: one Qualtrics
        # ResponseID is one person's pass through the survey. Merging is
        # refused unless the id is really one (runs.is_joinable_survey_response
        # rejects the unreplaced ${e://Field/ResponseID}, which is one string
        # shared by everybody), and only ever onto a run that is already
        # unattributed, so nobody reaches an attributable participant's run by
        # presenting their survey id beside a broken key.
        run = runs.find_unattributed_for_survey_response(qid, arm=arm_name)
        if run is not None:
            print(f"  NOTE: /start ({arm_name}) got an unusable participant "
                  f"key again for survey response {qid!r}; resuming run "
                  f"{run['run_id']} rather than enrolling this person twice.")
            # And they are that run's person from here on, not the synthetic
            # identity this arrival happened to mint. Every question the rest of
            # this handler asks about the participant — above all whether they
            # withdrew — is a question about the person, and asking it under a
            # key invented four lines ago would answer "no" for somebody who
            # pressed stop on their first visit and came back to the same link.
            pkey = run.get("participant_id") or pkey

    # A withdrawal is a statement about the PERSON, not about one run document.
    # Someone who pressed stop is handed back the run that records it, whichever
    # link they land on next, and nothing new is ever created for them: before
    # this, withdrawing on one arm and then touching the other arm's link minted
    # a fresh run with withdrawn:null and four encounters queued, so /api/runs
    # showed the same person withdrawn and live at once and a withdrawal report
    # read "withdrew and then carried on". runs.withdraw stamps every run they
    # already have; this catches the run that did not exist yet, and repairs any
    # run whose stamp could not be written at the time.
    #
    # Asked of the KEY and of the RECORD, and no longer gated on the key having
    # piped. The gate used to be `if key_status == "ok"`, which skipped the
    # lookup for exactly the arrivals this platform is designed around: a
    # failing Qualtrics pipe is the case /start already bends over backwards for
    # (an unusable key is waved through as `unattributed` rather than refused),
    # and it was also the case in which a withdrawn person was silently
    # re-enrolled. Their participant RECORD id is in the URL this platform
    # handed them — ?participant_id= is one of the spellings the entry link
    # accepts — and nothing asked it. A withdrawal is a statement about the
    # person, and the record is the other name the person arrives under.
    #
    # Two identities arrive here and they are asked SEPARATELY, because the
    # answer from one may not be applied to the other. A withdrawal read off a
    # participant RECORD is a statement about the person that record names, and
    # this used to apply it to whatever run the ?pid= key had already resolved
    # to. Presenting any stranger's key beside a withdrawn record id therefore
    # ended the STRANGER's study: their run stamped, the stop carried by
    # runs.withdraw to every other run under their key, their participant record
    # written, and their own capture gate refusing them afterwards — with no way
    # back, on all three entry links, and with no attacker required. A
    # duplicated study link, a shared browser on a return visit, or a survey
    # piping a stale participant_id next to a fresh pid produces exactly that
    # pair. So the record is believed only when it is this arrival's own.
    withdrawal = runs.participant_withdrawal(pkey)
    record_hint = (participant_id
                   if participant_id and get_participant(participant_id)
                   else None)
    if record_hint and not _record_is_this_arrival(record_hint, pkey,
                                                   key_status, run):
        record_hint = None
    if withdrawal is None and record_hint:
        # _confirmed_withdrawal, not _participant_withdrawal: what follows
        # WRITES, and "the store could not be read" is not a fact to write down.
        # See _confirmed_withdrawal.
        withdrawal = _confirmed_withdrawal(record_hint)
    if withdrawal is not None:
        if run is None:
            run, prior_arm_run = prior_arm_run, None
        if run is None and record_hint:
            # Their key did not pipe, so nothing matched on it; their record
            # still knows which run it belongs to.
            rec = get_participant(record_hint) or {}
            run = runs.get(rec.get("run_id")) if rec.get("run_id") else None
            if run is None:
                run = runs.find_by_participant_record(record_hint)
        if run is not None and not run.get("withdrawn"):
            run = runs.withdraw(run["run_id"],
                                reason=withdrawal.get("reason")) or run
        print(f"  NOTE: participant arriving on the {arm_name!r} link has "
              f"withdrawn (at {withdrawal.get('at')}); returning their existing "
              f"run rather than enrolling them again.")
        # The third place a withdrawal is recorded, and the second one the
        # teardown had never heard of. Somebody who stopped on one arm and then
        # touches the other arm's link gets the stop stamped onto that run here
        # — while an encounter of theirs may still be live in another tab,
        # recording and streaming. Asked of the run AND of the key AND of the
        # record, because at this point in the handler any of the three may be
        # the only one that resolved.
        await _enforce_withdrawal(
            run, pkey=pkey, record_id=record_hint,
            where=f"a withdrawn participant arriving on the {arm_name!r} link")

    if run is None:
        try:
            run = runs.create(
                pkey, variant=variant, qualtrics_id=qid,
                # An explicit ?cohort= is an operator's deliberate choice and is
                # honoured; otherwise a run only counts as study data when its key
                # is one we can actually attribute.
                #
                # A PINNED FORM IS NOT STUDY DATA UNLESS SOMEBODY SAYS IT IS.
                # `variant` only survives the gate above when the researcher key
                # is present, and pinning every construct to one letter is what
                # makes _apply_form_exclusions stand down: measured over 300
                # seeds, runs.create(arm="full", variant="A") produced the
                # forbidden S1A+Teamwork pairing 300 times in 300, each stamped
                # "form was pinned by the caller; exclusion not applied". Doing
                # that is a legitimate operator action — piloting one form — and
                # it is why the pin is honoured at all. What it must not do is
                # default into `cohort=study`, which is where the discriminant-
                # validity pairing lands in the analysis set with an explanation
                # attached. /test already forces cohort="internal" for exactly
                # this reason; this is the containment /start was missing. An
                # operator who really means to put a pinned run in the study
                # cohort still can, by saying &cohort=study.
                cohort=(cohort
                        or ("internal" if variant
                            else ("study" if key_status == "ok"
                                  else "unattributed"))),
                key_status=key_status,
                raw_participant_key=(raw_key if key_status != "ok" else None),
                arm=arm,
            )
        except ValueError as e:
            # An arm with nothing to draw from: the scenario specs it needs are
            # missing or unparseable. A 500 with a traceback would send the
            # operator to the wrong place, and silently widening the pool would
            # hand this participant the wrong arm.
            print(f"  WARNING: entry link for arm {arm_name!r} cannot build a "
                  f"run: {e}")
            raise HTTPException(503, f"the {arm_name} arm is not available: {e}")
        if withdrawal is not None:
            # Reached only when their stop is on record but every run that
            # carried it has gone. The invariant is the point: a person who
            # withdrew is never handed a run that will enrol them, by any path
            # through this handler, so a run that had to be built for them is
            # built already stopped rather than left for the next reader to
            # notice.
            run = runs.withdraw(run["run_id"],
                                reason=withdrawal.get("reason")) or run
        if prior_arm_run is not None:
            _cross_link_arms(prior_arm_run, run, arm_name, runs)
    elif qid and run.get("qualtrics_id") != qid:
        if not run.get("qualtrics_id"):
            # A returning participant may arrive with the qid we did not have yet.
            run["qualtrics_id"] = qid
        else:
            # A second, different survey response id under the same key means
            # the run<->survey join is no longer one-to-one (they retook the
            # survey, or two people are sharing a key). Keep the first id, the
            # one their encounters started under, but record every id seen and
            # flag the conflict so analysis notices instead of quietly joining
            # this run to the wrong response.
            seen = run.setdefault("qualtrics_ids_seen", [run["qualtrics_id"]])
            if qid not in seen:
                seen.append(qid)
            run["qualtrics_id_conflict"] = True
            print(
                f"  WARNING: run {run['run_id']} was presented a second "
                f"qualtrics_id ({qid!r}, first {run['qualtrics_id']!r}); "
                f"keeping the first and flagging the run."
            )
        runs.save(run)

    # Give the run one stable participant record and carry it in the redirect,
    # so identity.assign() hashes the same key across all four encounters (and
    # mid-encounter refreshes) instead of a fresh code per page load.
    #
    # The record is minted here for identity: it is what the page carries
    # across all four encounters, and what the voice socket checks
    # (_participant_may_capture: a record that exists and is not withdrawn).
    # Consent itself is taken in Qualtrics before /start is ever reached, so
    # there is nothing for this endpoint to assert about it.
    pid_record = run.get("participant_record_id")
    if not pid_record:
        # Retried once, then logged. A failed mint here is not cosmetic: the run
        # keeps no participant_record_id, so _run_context cannot join the
        # encounter back to the run, and the manifest records run_id, cohort and
        # participant key as null — which per storage.py means "not study data".
        # A consented participant's encounter then quietly falls out of the
        # analysis set. The mint is a JSON write plus a sqlite row, so the
        # realistic failure (a full or briefly unavailable DATA_DIR — in
        # production an EFS mount) is transient and a second attempt usually
        # takes; when it does not, this has to be visible at the moment it
        # happens, because nothing downstream will ever say so.
        #
        # Two loops, not one. The mint and the write-back fail for the same
        # reason (a full or stalled DATA_DIR) but they are not one unit: a
        # single try around both meant that when the mint SUCCEEDED and
        # runs.save then raised, the handler set pid_record = None and attempt 2
        # minted a second record — leaving an orphaned participant record behind
        # (two of them if the second save also failed) where the old code left
        # exactly one. An identity that exists is still the right one to hand the
        # page, so a save failure keeps it; it is the run write-back, not the
        # record, that is worth a second attempt. If both saves fail the run file
        # still has no participant_record_id and the encounter is recorded as
        # unattributable; the WARNING below says so.
        for attempt in (1, 2):
            try:
                pid_record = create_participant(
                    code=(pkey or run["run_id"]),
                    # Bound here, at the one moment the binding is certain. The
                    # record naming its run is what stops an encounter's cohort
                    # being re-derived later from whichever run happens to be
                    # newest — see _run_context.
                    run_id=run["run_id"],
                    cohort=run.get("cohort", "study"),
                )
                break
            except Exception as e:  # noqa: BLE001 , fall back to today's behavior
                pid_record = None
                if attempt == 2:
                    print(
                        f"  WARNING: /start could not mint a participant record "
                        f"for run {run['run_id']} (participant {pkey}): "
                        f"{type(e).__name__}: {e}. The encounter will proceed but "
                        f"will be recorded as unattributable."
                    )
        if pid_record:
            run["participant_record_id"] = pid_record
            for attempt in (1, 2):
                try:
                    runs.save(run)
                    break
                except Exception as e:  # noqa: BLE001, the record itself is safe
                    if attempt == 2:
                        print(
                            f"  WARNING: /start minted participant record "
                            f"{pid_record} for run {run['run_id']} (participant "
                            f"{pkey}) but could not write it back to the run: "
                            f"{type(e).__name__}: {e}. The record is kept; the "
                            f"run has no pointer to it."
                        )

    q = f"?run={run['run_id']}"
    # Forward the key only when participant routes actually demand one.
    # SESSION_KEY is the researcher credential (see check_key): it opens
    # /api/runs, /api/encounters, the per-session download zips and the
    # researcher/director views. This redirect lands in the address bar of every
    # recruited person, so forwarding it unconditionally handed the whole
    # dataset's credential to ~100 participants. When PARTICIPANT_KEY_REQUIRED
    # is set the participant page cannot load without it, so it is forwarded
    # then and only then, and never in the normal open-collection deployment.
    if key and PARTICIPANT_KEY_REQUIRED:
        q += f"&key={key}"
    # Prefer the run's stable participant record; otherwise never drop a
    # participant_id that was handed to us — but only when it names a record
    # that exists. ?participant_id= is one of the spellings Qualtrics pipes the
    # raw participant key through (see raw_key above), so when the mint failed
    # this fallback used to hand the page the survey's own key dressed up as a
    # record id, which names no record and fails every check that reads one.
    # Passing nothing instead lets the page mint its own record
    # (POST /api/participant) and proceed.
    effective_pid = pid_record or (participant_id if participant_id
                                   and get_participant(participant_id) else None)
    if effective_pid:
        q += f"&participant_id={effective_pid}"
    # 307 preserves the method, which is what a GET arrival wants and what every
    # caller of this link already expects. The Continue button's POST must NOT
    # be preserved — a 307 there would have the browser POST to /v2, which
    # serves GET only — so that one answers 303, the redirect that says "your
    # POST was accepted, now GET this".
    return RedirectResponse(url=f"/v2{q}",
                            status_code=303 if confirmed else 307)


def _cross_link_arms(prior: dict, current: dict, arm_name: str, runs) -> None:
    """Record that one participant key now has runs in two arms, on both runs.

    Written both ways round because either run can be the one an analyst is
    looking at, and "this participant also has a run over there" is not
    recoverable from a run that does not say so — the participant key is the
    only thing they share, and it is exactly the field that is null on the
    unattributed runs.
    """
    for a, b, other_arm in (
        (prior, current, arm_name),
        (current, prior, (prior.get("construct_pool") or {}).get("arm", "full")),
    ):
        try:
            links = a.setdefault("other_arm_runs", [])
            if not any(l.get("run_id") == b["run_id"] for l in links):
                links.append({"run_id": b["run_id"], "arm": other_arm,
                              "at": time.time()})
            runs.save(a)
        except Exception as e:  # noqa: BLE001, the cross-link is a note, not the run
            print(f"  WARNING: could not cross-link runs {a.get('run_id')} and "
                  f"{b.get('run_id')}: {type(e).__name__}: {e}")
    print(
        f"  NOTE: participant key already had run {prior['run_id']} in arm "
        f"{(prior.get('construct_pool') or {}).get('arm', 'full')!r}; the "
        f"{arm_name!r} link started run {current['run_id']} rather than "
        f"resuming the other arm. Both runs are cross-linked."
    )


@app.get("/start")
async def start_run(p: dict = Depends(entry_params)):
    """Entry point from Qualtrics: the full study run, all four constructs.

    Qualtrics passes the participant key through as a query parameter; the exact
    name varies by how the survey is piped, so the common spellings are all
    accepted. The value itself is validated before use (see
    runs.normalize_participant_key): a broken pipe sends either nothing or the
    literal ${e://Field/...} placeholder, and taking either at face value
    silently corrupts the dataset. A returning participant with a usable key
    resumes their run rather than starting a second one under the same key.

    A GET that is a link preview, a prefetch or a scanner — or that carries no
    participant key parameter at all — is answered with the entry check page
    instead of a run. See the block above entry_params.
    """
    return await _enter_study(None, p)


@app.post("/start")
async def start_run_confirmed(p: dict = Depends(entry_params)):
    """The entry check page's Continue: the same handler as the GET, reached by
    a human pressing the button — which is the fact the check page exists to
    establish. Open costs nothing (it does no more than the GET); what POST must
    never be is the ONLY way in — see the block above entry_params."""
    return await _enter_study(None, p)


@app.get("/v2", response_class=HTMLResponse)
async def v2_page(scenario: Optional[str] = None, key: Optional[str] = None):
    """Zoom-like multi-agent voice UI. Works for both single-agent and group
    scenarios (single-agent just shows one tile)."""
    check_participant(key)
    return (STATIC_DIR / "v2.html").read_text(encoding="utf-8")


# --- REST helpers ---

@app.get("/api/scenarios")
async def api_scenarios(key: Optional[str] = None):
    check_participant(key)
    return list_scenarios()


@app.get("/api/scenarios/{scenario_id}")
async def api_scenario_detail(scenario_id: str, key: Optional[str] = Query(None),
                              participant_id: Optional[str] = Query(None)):
    check_participant(key)
    try:
        sc = load_scenario(scenario_id, participant_id or "")
    except FileNotFoundError:
        raise HTTPException(404, "scenario not found")
    return {
        "id": sc.id,
        "title": sc.title,
        "intro": sc.intro,
        "briefing": getattr(sc, "briefing", None),
        "skill": sc.skill,
        "mode": sc.mode,
        "model": sc.model or DEFAULT_MODEL,  # effective default for the pre-start picker
        "intro_image": sc.intro_image,
        "cast": [
            {"id": a.id, "name": a.name, "role": a.role, "photo": a.photo}
            for a in sc.cast
        ],
        # Default persona values per agent, so the launch card can preselect
        # the current bands and stage only the gears the researcher changes.
        "personas": {aid: p.snapshot() for aid, p in sc.initial_personas().items()},
    }


# --- Researcher-initiated launch: configure gears first, then start ---
#
# POST /api/launch stores a one-shot config (scenario, model, gear presets,
# auto steering). The participant tab opens /v2?...&launch=<id>; when the
# participant's WS connects, the session is created and the presets are
# applied BEFORE the first turn, each logged as a knob_set event. The
# researcher page polls GET /api/launch/<id> until session_id appears, then
# connects and tracks the conversation live.

LAUNCHES: dict = {}


async def _apply_launch_config(session, cfg: dict) -> None:
    for aid, knobs in (cfg.get("knobs") or {}).items():
        if not isinstance(knobs, dict):
            continue
        for knob, value in knobs.items():
            try:
                await session.set_knob(
                    knob, float(value), agent_id=aid, reason="preset at launch"
                )
            except (KeyError, ValueError, TypeError, AttributeError) as e:
                session.store.event(
                    "launch_preset_error", agent_id=aid, knob=knob, message=str(e)
                )
    # Explicit both ways: a launch with the toggle Off overrides the
    # on-by-default behavior of participant-initiated sessions.
    await session.set_auto_steering(bool(cfg.get("auto_steering")))
    cfg["session_id"] = session.id


@app.post("/api/launch")
async def api_launch(body: dict, key: Optional[str] = Query(None)):
    check_key(key)
    scenario_id = body.get("scenario") or ""
    try:
        load_scenario(scenario_id)
    except FileNotFoundError:
        raise HTTPException(404, "scenario not found")
    launch_id = secrets.token_hex(4)
    model = body.get("model") or None
    LAUNCHES[launch_id] = {
        "scenario": scenario_id,
        "model": model,
        "auto_steering": bool(body.get("auto_steering")),
        "knobs": body.get("knobs") or {},
        "session_id": None,
    }
    url = f"/v2?scenario={scenario_id}&launch={launch_id}"
    if model:
        url += f"&model={model}"
    return {"launch_id": launch_id, "participant_url": url}


@app.get("/api/launch/{launch_id}")
async def api_launch_status(launch_id: str, key: Optional[str] = Query(None)):
    check_key(key)
    cfg = LAUNCHES.get(launch_id)
    if cfg is None:
        raise HTTPException(404, "unknown launch")
    return {"session_id": cfg["session_id"], "scenario": cfg["scenario"]}


def _session_dir(session_id: str) -> Path:
    """Resolve a session's on-disk directory, blocking path traversal.

    Checked against the shape storage mints (storage.valid_session_id) rather
    than by asking the filesystem what matches, because the filesystem answers
    differently per platform. The old check — no "/", no ".." — let three
    spellings of one encounter through on Windows and on a default macOS
    volume: "S_1772460300_44C9A2" (case-insensitive lookup), and the same id
    with a trailing "." or " " (Windows strips both). All three opened the real
    directory here and 404'd on Linux, and each one HMACs to a DIFFERENT
    rater_packet.rating_code, so one encounter could be issued several blinded
    handles depending on which host the console was run from. It also missed
    "\\", which is a separator on Windows only.
    """
    if not valid_session_id(session_id):
        raise HTTPException(400, "bad session_id")
    sdir = (SESSIONS_DIR / session_id).resolve()
    # Parent identity, not a string prefix: "sessions_old" starts with
    # "sessions" too. Unreachable given the shape check above, kept as the
    # second lock on the one route family that takes a filesystem path from a URL.
    if sdir.parent != SESSIONS_DIR.resolve():
        raise HTTPException(400, "bad session_id")
    if not sdir.is_dir():
        raise HTTPException(404, "session not found")
    return sdir


def _load_manifest(sdir: Path) -> dict:
    """Read a session's manifest, or 404 if it is missing/unreadable."""
    manifest = sdir / "manifest.json"
    if not manifest.exists():
        raise HTTPException(404, "session not found")
    try:
        return json.loads(manifest.read_text(encoding="utf-8"))
    except ValueError:
        raise HTTPException(404, "session not found")


def _require_session_owner(m: dict, participant_id: Optional[str]) -> None:
    """Bind a participant-open session route to the session's real owner.

    Without this, any known/guessed session id would let an outsider act on
    another participant's recorded session.
    """
    owner = m.get("participant_id")
    if not participant_id or not owner or participant_id != owner:
        raise HTTPException(403, "not your session")


def _participant_may_capture(participant_id: Optional[str]) -> Optional[dict]:
    """The participant record a capture socket may open under, or None.

    Consent is taken outside this platform (decision 2026-09-17), so the socket
    asks two things only: does the record exist, and has this person withdrawn.
    THE WITHDRAWAL IS PART OF THE QUESTION: a withdrawal is written on the run
    and on the record, and a socket that asked the record alone opened audio
    and webcam capture for someone who had pressed stop. An absent
    participant_id is None too, so the strict rule is this test alone.
    """
    if not participant_id:
        return None
    rec = get_participant(participant_id)
    if not rec:
        return None
    if _withdrawn(participant_id):
        return None
    return rec


def _participant_withdrawal(participant_id: Optional[str]) -> Optional[dict]:
    """The withdrawal binding this participant record, or None.

    THE ONE PLACE THAT ANSWERS THIS QUESTION. Every route that accepts
    participant-owned data or spends the study's budget asks here and nowhere
    else, because the previous round's fix went into the two routes somebody
    thought of and six others stayed open — a per-route check is a set of checks
    that drift apart, and this one is the promise the consent text makes.

    The record is asked first, and that is the root fix: runs.withdraw now
    writes the stop onto the participant record as well as onto every run, so
    this is one read of the file get_participant already opens on every socket
    open. The directory scan behind it catches a withdrawal recorded before the
    record carried one (or on another run of the same person) and WRITES IT
    BACK, so the scan POPULATES the record rather than being the thing every
    caller has to remember to run.

    Fails CLOSED, unlike _run_context next door, and the difference is
    deliberate: that one is bookkeeping and an encounter must never fail to
    start over bookkeeping, while this one decides whether somebody who said
    stop is recorded anyway. If we cannot tell, we refuse, and we say so.
    """
    if not participant_id:
        return None
    try:
        from . import runs

        on_record = participant_withdrawal(participant_id)
        if on_record:
            return on_record
        found = runs.withdrawal_for_record(participant_id)
        if found:
            # Populate. The next reader — and there are seven of them — gets the
            # answer from the record without a directory pass.
            try:
                record_withdrawal(participant_id, found)
            except Exception as e:  # noqa: BLE001, the refusal still stands
                print(f"  WARNING: could not copy the withdrawal of participant "
                      f"record {participant_id} onto the record itself "
                      f"({type(e).__name__}: {e}); the refusal still holds, but "
                      f"every reader will pay for the directory scan.")
        return found
    except Exception as e:  # noqa: BLE001, see the docstring: closed, and loud
        print(
            f"  WARNING: could not check whether participant record "
            f"{participant_id} belongs to someone who withdrew "
            f"({type(e).__name__}: {e}); refusing. Capture, uploads and paid "
            f"calls are blocked for this record until the store can be read."
        )
        return dict(WITHDRAWAL_STATUS_UNKNOWN)


WITHDRAWAL_STATUS_UNKNOWN = {"reason": "withdrawal_status_unknown",
                             "status_unknown": True}


def _withdrawn(participant_id: str) -> bool:
    """Has the person behind this participant record stopped the study?"""
    return _participant_withdrawal(participant_id) is not None


def _confirmed_withdrawal(participant_id: Optional[str]) -> Optional[dict]:
    """A withdrawal somebody actually recorded, never the fail-closed sentinel.

    _participant_withdrawal answers "I could not read the store" with a stamp,
    because its callers are gates: a gate that cannot tell has to refuse, and
    that refusal lasts exactly as long as the store is unreadable.

    A WRITER must not treat that answer the same way. /start fed it straight
    into runs.withdraw, so one transient read error — an EFS blip, the realistic
    failure this code base retries for everywhere else — stamped a permanent
    stop on a live consenting participant's run, on every other run under their
    key, and on their participant record, with reason "withdrawal_status_unknown"
    in the one field an IRB reads. Nothing clears it: withdraw() is
    idempotent-forward and advance() refuses a withdrawn run, so that person's
    study was over and the next healthy arrival under their key inherited it.
    Refusing for a moment is recoverable; writing down a withdrawal nobody made
    is not. Only a stamp the store actually holds may be written.
    """
    stamp = _participant_withdrawal(participant_id)
    if stamp and stamp.get("status_unknown"):
        return None
    return stamp


def _record_is_this_arrival(record_id: str, pkey: Optional[str],
                            key_status: Optional[str],
                            run: Optional[dict]) -> bool:
    """Does this participant record belong to the person now at the door?

    ?participant_id= is supplied by whoever holds the link, exactly as ?pid= is,
    and the entry path acts on what it says: it will hand back the run that
    record names and stamp a withdrawal onto it. Believing a record that belongs
    to somebody else is how one URL ended another participant's study.

    Believed when it is the only identity present (the key did not pipe, which
    is the arrival this platform is built around — then the record is what the
    handler resolves the run FROM, so it can only reach that person's own run),
    when it was minted under the key that arrived, or when the run the key
    already resolved to names it. A record minted under a different key, beside
    a key that did pipe, is somebody else's and is ignored — not refused, because
    a 400 at the door costs a recruited person their encounter over a parameter
    they did not type.
    """
    rec = get_participant(record_id) or {}
    if key_status != "ok" or not pkey:
        return True
    if record_id == pkey:
        # No ?pid= arrived, so `raw_key = pid or participant_id` made the RECORD
        # id the participant key (see _enter_study). One identity, not two.
        return True
    if rec.get("code") == pkey:
        return True
    if run is not None and run.get("participant_record_id") == record_id:
        return True
    print(f"  WARNING: entry link presented participant record {record_id} "
          f"(minted under key {rec.get('code')!r}) beside participant key "
          f"{pkey!r}. Ignoring the record: it belongs to someone else, and "
          f"acting on it would apply that person's state to this arrival.")
    return False


def _record_owns_run(rec: Optional[dict], run: Optional[dict]) -> bool:
    """Is this participant record the one that run belongs to?

    The question the withdrawal route has to ask before it ends a study.
    Note that it is NOT "does the caller hold this record" — the caller's own
    body is not evidence about somebody else's run. Every one of the three
    answers below is a fact this server wrote at a moment it could not be
    mistaken about: the run naming the record (/start's mint), the record naming
    the run (the same mint, from the other side), and the record's participant
    key matching the run's (which is how the pair is joined when the mint failed
    and the page minted its own record instead).

    Three rather than one because any single one of them is absent in a shape
    that really happens, and a decline that does not stop the run leaves someone
    who just refused still enrolled in the encounters they refused — so this
    must not be narrower than the legitimate cases.

    False for no record and for no run: nothing here may be inferred from
    absence.
    """
    if not rec or not run:
        return False
    if rec.get("id") and run.get("participant_record_id") == rec["id"]:
        return True
    if rec.get("run_id") and rec["run_id"] == run.get("run_id"):
        return True
    if rec.get("code") and rec["code"] == run.get("participant_id"):
        return True
    return False


def _is_operator(key: Optional[str]) -> bool:
    """A caller holding the researcher credential, and only that.

    Not _operator_key, which answers True for everybody when no SESSION_KEY is
    configured. That is the right answer where it is used (a participant-open
    route offering an operator something extra on a laptop) and the wrong one
    for a bypass: a gate that switches itself off on the deployment with no key
    configured is a gate that is off in exactly the deployment running open
    collection. Same comparison /health makes, for the same reason.
    """
    return bool(SESSION_KEY) and bool(key) and secrets.compare_digest(key, SESSION_KEY)


def _may_stop_run(run: dict, participant_id: str, key: Optional[str]) -> bool:
    """May this caller end the study this run belongs to?

    POST /api/run/{id}/withdraw asked nothing. An empty POST to a run id ended
    it: runs.withdraw stamps that run, every other run under the same
    participant key and every participant record those runs name, the live
    encounter is torn out of the registry and the microphone closed, and there
    is no clearing path — advance() 403s them from then on and their record
    reads withdrawn to an IRB. Run ids are not secrets: one travels in the
    participant's address bar as /v2?run=..., and where SESSION_KEY is unset
    GET /api/runs
    hands out the whole roster keylessly.

    THE PROOF IS THE RECORD ID, because it is the one thing the page holds that
    no keyless route echoes back. /start puts it in the participant's own URL
    and static/v2.html sends it here; the run view publishes the participant KEY
    and the completed session ids, so neither of those could tell the person
    from anybody holding their run id.

    Three ways through, and the order is about what has to be readable:

    * The researcher key, which a participant never holds.
    * The record id, compared against the run document FIRST and only then
      resolved to a file. A stop must not depend on a participant file being
      readable at the moment it is pressed — this is the one request that may
      not fail — and the run is already in hand. The file read behind it is what
      catches the shapes the run document cannot: a second record of the same
      person (rec.code == run.participant_id), and a run that adopted its record
      after the fact (rec.run_id == run.run_id).
    * A run with no participant record at all — none named by the run, none
      minted under its key. /start's mint can fail, and it then hands the page
      no participant_id, so the only stop that arrival has carries nothing but
      the run id in the path. There is no enrolment there for a stranger to end
      and nobody for this gate to protect, and refusing would refuse the single
      person it could be. (_records_for_participant_key answers with the empty
      set when it cannot read the directory, so a disk having a bad day lets the
      stop through rather than swallowing it.)
    """
    if _is_operator(key):
        return True
    if participant_id:
        if run.get("participant_record_id") == participant_id:
            return True
        try:
            rec = get_participant(participant_id)
        except Exception:  # noqa: BLE001, an unreadable record proves nothing
            rec = None
        if _record_owns_run(rec, run):
            return True
    if not run.get("participant_record_id") and \
            not _records_for_participant_key(run.get("participant_id")):
        return True
    return False


def _refuse_if_withdrawn(participant_id: Optional[str], key: Optional[str],
                         *, action: str) -> None:
    """Refuse a route that records, stores or spends for somebody who stopped.

    The consent text promises a participant may stop at any time, and honouring
    that is not only about the capture socket. After a 200 from the withdraw
    route, every one of these still worked: the webcam PUT wrote 4096 bytes to
    disk, the presign route signed a write into the IRB bucket, the
    camera-absence report appended to their encounter trail without touching S3
    at all, and the scorer and the debriefer each spent a gateway call and left
    a feedback artefact dated after they stopped.

    READING is not blocked, and that line matters as much as the refusal.
    Someone who stops has still given their time and is still owed the partial
    completion code they take back to the survey to be paid, so GET
    /api/run/{id}, /api/run/config and the page itself stay open. What is
    blocked is anything that takes their data, stores it, or spends money on it.

    The researcher key bypasses, because a withdrawal is a statement to this
    platform about the participant's own session, not an instruction that an
    analyst may never re-derive a score from the partial record they left. A
    participant never holds that key.
    """
    if not participant_id or _is_operator(key):
        return
    stamp = _participant_withdrawal(participant_id)
    if stamp is None:
        return
    print(f"  NOTE: refusing {action} for participant record {participant_id}: "
          f"they withdrew from the study ({stamp.get('reason')}).")
    raise HTTPException(403, "participant withdrew from the study")


#: How long a withdrawal will wait for one capture socket to shut. A close
#: frame is a single write and settles instantly; the ceiling is here because
#: the participant is on the other end of the request that is doing the
#: closing, and their stop must land whatever one wedged socket is doing.
_CAPTURE_CLOSE_TIMEOUT = 5.0


async def _close_capture_socket(session, sid: str) -> None:
    """Shut one live encounter's microphone, having already closed its store.

    See Session.close_participant_socket for what this is for: dropping the
    session stops the audio being SAVED and does not stop it being SENT.

    Asked via getattr because the registry is a plain dict of whatever was put
    in it, and the tests that exercise this teardown put stand-ins there. An
    entry with no socket to close is not a failure, it is an entry with no
    socket to close.
    """
    import asyncio

    closer = getattr(session, "close_participant_socket", None)
    if closer is None:
        return
    try:
        await asyncio.wait_for(closer(), timeout=_CAPTURE_CLOSE_TIMEOUT)
    except Exception as e:  # noqa: BLE001, the withdrawal is already written
        print(f"  WARNING: could not close the capture socket of encounter "
              f"{sid} for a participant who withdrew: {type(e).__name__}: {e}. "
              f"Their recording is stopped; the socket may still be open.")


async def _stop_live_sessions(participant_records: set, run_ids: set) -> list:
    """Tear down any encounter still recording for these participants.

    The gate was at socket OPEN only. Someone who pressed stop mid-encounter
    kept being recorded until they closed the tab, because the withdraw route
    wrote two files and never looked at the registry of live sessions — the
    audio kept flowing and the store kept appending for as long as the socket
    held. A withdrawal that takes effect at the next page load is not the
    promise the consent text makes.

    Matched on the participant record AND on the run id, not on one of them: an
    encounter started before the run knew its record, or a second tab on another
    of their runs, is the same person and the same microphone.

    AND THE STORE IS ONLY HALF OF IT. registry.drop closes the recorder, and
    for a long time that was the whole teardown — so a withdrawn participant's
    microphone socket stayed open, their audio went on being read and forwarded
    to the model provider, and the study went on being billed for it, until they
    closed the tab. The socket is closed here too, after the drop, so the
    encounter is out of the registry before anything can be awaited.

    Returns the session ids it stopped, and never raises: the withdrawal is
    already on disk by the time this runs, and a registry that will not tidy up
    must not turn a participant's stop into a 500.
    """
    stopped = []
    for sid in list(registry.list_ids()):
        try:
            session = registry.get(sid)
        except KeyError:  # already finished on its own
            continue
        owner = getattr(getattr(session, "store", None), "participant_id", None)
        if owner not in participant_records and \
                getattr(session, "run_id", None) not in run_ids:
            continue
        try:
            registry.drop(sid)
            stopped.append(sid)
        except Exception as e:  # noqa: BLE001, the withdrawal is already written
            print(f"  WARNING: could not stop live encounter {sid} for a "
                  f"participant who withdrew: {type(e).__name__}: {e}")
        await _close_capture_socket(session, sid)
    return stopped


def _records_for_participant_key(pkey: Optional[str]) -> set:
    """Every participant record minted under this participant key.

    A person is not one record. A second tab that minted its own record,
    a repair after a failed mint, a demo record under the same key — all of them
    are the same human at the same microphone, and the teardown used to match
    only on the records the RUN documents happened to name. An encounter
    recording under any of the others was left running by the withdrawal.

    Reads the files rather than the index because that is what every other
    withdrawal reader in this module does, and because the record is the
    authority. A file that will not parse is SKIPPED and not fatal: this widens
    a teardown, so the cost of missing one record is the defect above, while the
    cost of raising is a participant's stop returning 500 — and a corrupt file
    belonging to somebody else must refuse nobody.
    """
    out: set = set()
    if not pkey:
        return out
    from .storage import PARTICIPANTS_DIR

    try:
        files = sorted(PARTICIPANTS_DIR.glob("*.json"))
    except OSError as e:
        print(f"  WARNING: could not list participant records while stopping "
              f"the encounters of participant key {pkey!r}: "
              f"{type(e).__name__}: {e}")
        return out
    for f in files:
        try:
            rec = json.loads(f.read_text(encoding="utf-8"))
        except (ValueError, OSError):
            continue
        if isinstance(rec, dict) and rec.get("code") == pkey and rec.get("id"):
            out.add(rec["id"])
    return out


async def _enforce_withdrawal(run: Optional[dict], *, where: str,
                              pkey: Optional[str] = None,
                              record_id: Optional[str] = None) -> list:
    """Make a recorded withdrawal reach the microphone, wherever it was recorded.

    The teardown used to hang off POST /api/run/{id}/withdraw alone, which is
    one of the THREE places this platform writes a withdrawal down. Declining in
    a second tab withdrew the run and stopped nothing; arriving on the other
    arm's link after stopping stamped the run and stopped nothing. In both cases
    the other tab kept recording and kept streaming against a run whose own file
    said the participant had stopped. So this is wired to the fact — a
    withdrawal has just been recorded for this person — and every writer calls
    it.

    The person is resolved three ways, because no single one of them is
    complete: every run under their participant key (a second tab on the other
    arm), every participant record those runs name, and every record minted
    under the key whether or not a run points at it.

    Never raises. The withdrawal is already on disk; tidying up after it must
    not be the thing that turns a participant's stop into a 500.
    """
    from . import runs

    theirs = [run] if run else []
    pkey = pkey or (run.get("participant_id") if run else None)
    if run and pkey:
        try:
            theirs += runs._others_for_participant(pkey, run["run_id"])
        except Exception as e:  # noqa: BLE001, the stop is already on disk
            print(f"  WARNING: could not list the other runs of the participant "
                  f"who withdrew from run {run.get('run_id')}: "
                  f"{type(e).__name__}: {e}")
    records = {r.get("participant_record_id") for r in theirs
               if r.get("participant_record_id")}
    records |= _records_for_participant_key(pkey)
    if record_id:
        records.add(record_id)
    run_ids = {r.get("run_id") for r in theirs if r.get("run_id")}
    if not records and not run_ids:
        return []
    stopped = await _stop_live_sessions(records, run_ids)
    if stopped:
        print(f"  NOTE: a withdrawal recorded at {where} stopped "
              f"{len(stopped)} live encounter(s): {', '.join(stopped)}")
    return stopped


def _session_owner(sdir: Path) -> Optional[str]:
    """The participant record this session was recorded under, or None.

    Tolerant where _load_manifest is strict, and only for the withdrawal gate:
    the manifest is the authority on who owns an encounter, but a session
    directory whose manifest is missing or unreadable must not turn a route that
    would otherwise have worked into a 404. The gate falls back to the id the
    caller presented, which is the same id the owner check compares against.
    """
    try:
        return _load_manifest(sdir).get("participant_id")
    except HTTPException:
        return None


def _require_owner_or_key(sdir: Path, participant_id: Optional[str], key: Optional[str]) -> None:
    """Gate a participant-open route that triggers a paid, blocking Claude call.

    A researcher (valid SESSION_KEY) or the participant who owns the session may
    proceed; anyone else is refused, so the open endpoint can't be used to spend
    the study's API budget on arbitrary session ids. When no SESSION_KEY is
    configured (local dev) the route stays open, matching the rest of the app.
    """
    if not SESSION_KEY:
        return
    if key == SESSION_KEY:
        return
    _require_session_owner(_load_manifest(sdir), participant_id)


def _count_user_turns(sdir: Path) -> int:
    """User turns actually recorded for a session, from its event trail."""
    ev = sdir / "events.jsonl"
    if not ev.exists():
        return 0
    n = 0
    try:
        for line in ev.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                e = json.loads(line)
            except ValueError:
                continue
            if e.get("type") == "user_turn":
                n += 1
    except Exception:  # noqa: BLE001
        return n
    return n


@app.get("/api/sessions/{session_id}/files")
async def api_session_files(session_id: str, key: Optional[str] = Query(None)):
    check_key(key)
    sdir = _session_dir(session_id)
    out = []
    for p in sorted(sdir.iterdir()):
        if p.is_file():
            out.append({"name": p.name, "size_bytes": p.stat().st_size})
    return out


@app.get("/api/sessions/{session_id}/download/{filename}")
async def api_session_file(
    session_id: str, filename: str, key: Optional[str] = Query(None)
):
    check_key(key)
    sdir = _session_dir(session_id)
    if "/" in filename or filename.startswith("."):
        raise HTTPException(400, "bad filename")
    p = (sdir / filename).resolve()
    if not str(p).startswith(str(sdir)) or not p.is_file():
        raise HTTPException(404, "file not found")
    return FileResponse(p, filename=f"{session_id}_{p.name}")


@app.get("/api/sessions/{session_id}/download.zip")
async def api_session_zip(session_id: str, key: Optional[str] = Query(None)):
    check_key(key)
    sdir = _session_dir(session_id)
    # Write the archive to a temp file on disk and stream it back with
    # FileResponse, so memory use is bounded regardless of session size (voice
    # sessions can hold hundreds of MB of WAV audio). ZIP_STORED, audio does
    # not compress, so deflate would only burn CPU.
    import tempfile
    from starlette.background import BackgroundTask

    fd, tmp_path = tempfile.mkstemp(prefix=f"{session_id}_", suffix=".zip")
    os.close(fd)
    try:
        with zipfile.ZipFile(tmp_path, "w", zipfile.ZIP_STORED) as zf:
            for p in sorted(sdir.iterdir()):
                if p.is_file():
                    # Under the encounter id, not bare. Every encounter's files
                    # are named identically (record.json, events.jsonl,
                    # user_audio.wav), so flat entries meant that unpacking a
                    # wave into one directory silently overwrote all but the
                    # last — and the documented bulk pull does exactly that.
                    zf.write(p, arcname=f"{session_id}/{p.name}")
    except Exception:
        try:
            os.remove(tmp_path)
        except OSError:
            pass
        raise
    return FileResponse(
        tmp_path,
        media_type="application/zip",
        filename=f"{session_id}.zip",
        background=BackgroundTask(os.remove, tmp_path),
    )


@app.post("/api/run")
async def api_run_create(request: Request, key: Optional[str] = None):
    """Start a run: four encounters, one per construct, counterbalanced.

    The other entrance to run creation, next to /start. It validates the
    participant key the same way and for the same reason: an unvalidated or
    absent key minted a cohort='study' run that no survey response could ever be
    joined to, and — worse — every arrival carrying the same unreplaced
    ${e://Field/...} placeholder collided into one shared run. Closing that on
    /start alone would have left the failure reachable through this endpoint,
    which is open in the normal deployment.

    NOT open any more. Validating the key was not enough: a well-formed one
    still minted a cohort="study" run for anybody who could reach the port, and
    that run then became the newest run under whatever participant record it
    could be pointed at — which is the first half of the reproduction that moved
    an internal demo encounter into the analysis set. This endpoint has no
    client in this repository: the participant entrance is /start, and the pages
    that show a run read it back rather than creating one. So it is gated the
    way /test and the rest of the researcher surface are gated (check_key): open
    on a local checkout with no SESSION_KEY, the researcher key when one is
    configured. Anybody holding that key keeps the endpoint they had.
    """
    check_key(key)
    from . import runs

    body = {}
    try:
        body = await request.json()
    except Exception:  # noqa: BLE001, empty body is fine
        pass
    # The body is caller-supplied JSON, so participant_id need not be a string.
    # Anything else is treated as absent rather than handed to the validator.
    raw = body.get("participant_id")
    raw_key = raw if isinstance(raw, str) else None
    pkey, key_status = runs.normalize_participant_key(raw_key)
    if pkey is None:
        # Same trade as /start: the caller is not turned away, but the run is
        # given a unique synthetic identifier and marked unattributed so it
        # cannot pass as study data, with the raw value kept for a hand-join.
        pkey = f"unattributed_{secrets.token_hex(6)}"
        print(
            f"  WARNING: POST /api/run got an unusable participant key "
            f"({key_status}): {raw!r}. Continuing as {pkey} in cohort "
            f"'unattributed'."
        )
    run = runs.create(
        pkey,
        cohort=("study" if key_status == "ok" else "unattributed"),
        key_status=key_status,
        raw_participant_key=(raw_key if key_status != "ok" else None),
    )
    return runs.view(run)


@app.get("/api/sessions/{session_id}/video-upload-url")
def api_video_upload_url(session_id: str, participant_id: Optional[str] = None,
                         key: Optional[str] = None):
    """Presigned PUT for the participant's webcam recording. Participant-open:
    it grants a write to exactly one object, for one hour.

    The session must exist and belong to the presenting participant, and a
    presign is refused once the object is already there, so nobody can point a
    write at another participant's session or overwrite a finished recording.

    Deliberately `def`, not `async def`: it makes a synchronous boto3 call, and
    FastAPI runs a plain route in its threadpool. As a coroutine it ran that
    call on the one event loop this process has, so a slow S3 froze every live
    encounter's audio and /health along with it — measured at 6 s of total
    silence with a 3 s HEAD, against an ALB health check that gives up at 5.
    """
    check_participant(key)
    from . import video

    sdir = _session_dir(session_id)  # existence + traversal check
    _require_session_owner(_load_manifest(sdir), participant_id)
    # Before anything is signed. A presigned PUT is a write into the IRB bucket
    # that this process cannot take back once it is handed out, so a withdrawal
    # discovered after the signature is a withdrawal that did not happen.
    _refuse_if_withdrawn(participant_id, key, action="a webcam upload URL")
    # One HEAD, not two. presign_upload does the same one-shot check itself, so
    # asking here first doubled the round trip on the loop for no extra answer.
    #
    # Its three refusals are answered with three different statuses, because the
    # browser acts on the difference. 409 is the only one that means "S3
    # confirms the object is there", and static/v2.html reads that as "the
    # earlier PUT landed" and stops re-sending tens of megabytes. Answering 409
    # to the other two — no such session, and a refusal resting on our own
    # receipt while S3 would not answer — told the page a recording was safe
    # when nothing had checked, which is exactly the quiet false success this
    # chain exists to prevent.
    try:
        presigned = video.presign_upload(session_id)
    except video.NoSuchSession:
        # Unreachable through this route (_session_dir above already 404s), so
        # this is here for the next caller rather than for this one: a missing
        # session must never be reported as a finished recording.
        raise HTTPException(404, "no such session")
    except video.UploadUnconfirmed as e:
        print(
            f"  WARNING: refusing a second webcam write URL for session "
            f"{session_id} on the strength of this server's own receipt — S3 "
            f"would not confirm the object (bucket {video.BUCKET}, region "
            f"{video.REGION}): {e.code}"
        )
        raise HTTPException(503, f"cannot confirm the existing recording ({e.code})")
    except (ClientError, BotoCoreError) as e:
        # Signing is arithmetic, but resolving the credentials to sign WITH is
        # not, and with no credentials at all it raises here. A 500 told the
        # page nothing it could act on and told the operator nothing at all;
        # 503 says "storage, not you", and the log line carries the coordinates.
        code = video._aws_code(e)
        print(
            f"  WARNING: could not sign the webcam upload for session "
            f"{session_id} (bucket {video.BUCKET}, region {video.REGION}): {code}"
        )
        raise HTTPException(503, f"recording storage unavailable ({code})")
    if presigned is None:
        raise HTTPException(409, "video already uploaded")
    return presigned


# The browser's own account of what broke, as a short bare token. It ends up in
# a record a human rater is shown, so anything longer or stranger than
# "put_http_403" is not a diagnosis, it is a paste.
#
# ':' is admitted and the cap is 60 because static/v2.html composes its reasons
# — "recorder_failed:NotSupportedError", "no_camera:NotAllowedError" — and the
# previous [A-Za-z0-9_.\-]{,40} rejected EVERY one of them silently: the route
# still answered 200, the event was still written, and the one field that said
# which leg broke was dropped on the floor. A filter that discards the fact it
# exists to capture is worse than no filter, because it looks like it worked.
_CLIENT_TOKEN_RE = re.compile(r"[A-Za-z0-9_.:\-]+")
_CLIENT_TOKEN_MAX = 60


def _client_token(value: Optional[str]) -> Optional[str]:
    """`value` if it is a short bare token, else None. See _CLIENT_TOKEN_RE."""
    hint = (value or "").strip()
    if hint and len(hint) <= _CLIENT_TOKEN_MAX and _CLIENT_TOKEN_RE.fullmatch(hint):
        return hint
    return None


# "no camera", spelled with any of the separators a client might compose with,
# so the two halves of this contract cannot drift apart silently again: the
# browser reported the absence as `client_error=no_camera:<reason>` while this
# side accepted no ':' at all, and the whole report was discarded unnoticed.
_NO_CAMERA_RE = re.compile(r"no[_.]?camera(?:[:._\-](?P<why>.*))?\Z", re.IGNORECASE)


def _camera_absence_reason(no_camera: Optional[str],
                           client_error: Optional[str]) -> Optional[str]:
    """The reason a camera was never recording, or None if this is not that.

    Two spellings are accepted on purpose. `?no_camera=<reason>` is the explicit
    one; `?client_error=no_camera:<reason>` is what static/v2.html's
    reportNoCamera sends today. Either lands, so a fix on one side of this
    contract cannot leave the other side quietly reporting the wrong thing.

    Returns "unspecified" rather than None for a bare `no_camera` with no
    reason: the absence is still a fact worth recording, and an empty string
    would read as "no report" at every consumer downstream.
    """
    if no_camera is not None:
        # The dedicated parameter says what it means by being present at all,
        # so even an empty or unusable value is still an absence report — the
        # fact does not depend on the reason being legible.
        return _client_token(no_camera) or "unspecified"
    token = _client_token(client_error)
    if token is not None:
        m = _NO_CAMERA_RE.match(token)
        if m:
            return (m.group("why") or "").strip() or "unspecified"
    return None


@app.post("/api/sessions/{session_id}/video-uploaded")
def api_video_uploaded(session_id: str, participant_id: Optional[str] = None,
                       key: Optional[str] = None,
                       client_error: Optional[str] = None,
                       no_camera: Optional[str] = None):
    """Client confirms the upload; verified against S3 and written into the
    session's event trail so verify_record can check for it.

    `no_camera` is the other question this route answers, and it is a different
    one: "there was never a recording to upload". It writes a `video_absent`
    event and contacts S3 not at all, because there is no object to ask about
    and an absent camera is not a storage fault. Reusing the confirm path for it
    — which is what the browser was doing, since this route wrote a
    `video_uploaded` event whatever it was told — turned "this participant's
    camera was never on" into "this encounter WAS recorded and the recording was
    lost", and the rating console blocks that state outright: every participant
    who denied the camera, or whose camera was held by Zoom, produced a paid
    encounter no rater was allowed to score and a false storage-fault report to
    the study team. Two different facts, two different event types.

    The event is written whatever S3 said — including when S3 said nothing at
    all. A failed upload is not a missing video: an encounter with no event was
    never captured, an event with status "failed" was captured and could not be
    stored or confirmed, and only status "ok" is playable. Returning early on an
    AWS error, as this did, collapsed the middle state into the first and left
    the rater packet telling a rater "no webcam recording was captured" about an
    object that may be sitting in the bucket.

    Plain `def` for the same reason as the presign route above: the HEAD is
    synchronous and must not run on the loop that carries every live encounter.
    """
    check_participant(key)
    from . import video

    sdir = _session_dir(session_id)  # existence + traversal check
    _require_session_owner(_load_manifest(sdir), participant_id)
    # This route contacts S3 not at all on the absence branch, so it succeeded
    # on any host, credentials or none, and wrote an event into a withdrawn
    # person's encounter trail. An event appended after they stopped is still
    # their data being collected.
    _refuse_if_withdrawn(participant_id, key, action="a webcam upload report")

    import json as _json
    import time as _time

    absence = _camera_absence_reason(no_camera, client_error)
    if absence is not None:
        # A distinct type, so every reader that asks "what became of the
        # recording?" — rater_packet._last_upload_event, encounter_record.build,
        # verify_record — keeps answering "absent" for this encounter and the
        # rater is told to score it from the transcript with N/A, which is the
        # correct instruction for a conversation nobody filmed.
        #
        # It is APPENDED, never a substitute: a real `video_uploaded` event that
        # arrived earlier still wins at every one of those readers, so a late or
        # duplicated absence report can never write off a recording that landed.
        #
        # No HEAD. There is no object to ask about, the answer could only be
        # "not_found", and with no credentials it would raise — turning "no
        # camera" into a 503 and a storage-fault event, which is the exact
        # confusion this branch exists to end.
        absent_event = {
            "t": None, "wall": _time.time(), "type": "video_absent",
            "reason": absence,
        }
        with open(sdir / "events.jsonl", "a", encoding="utf-8") as fh:
            fh.write(_json.dumps(absent_event) + "\n")
        # 200: the report was accepted and recorded. `ok` is false because there
        # is nothing playable, and `status` is "absent" — the same word
        # rater_packet uses — so a client can tell this from the "failed" that
        # means a recording was made and lost. No `key`, because naming an
        # object key would assert that one was written.
        return {"ok": False, "bytes": None, "key": None,
                "status": "absent", "reason": absence}

    probe = video.head_video(session_id)
    size = probe["bytes"]
    ok = (size or 0) > 0

    event = {
        "t": None, "wall": _time.time(), "type": "video_uploaded",
        "key": video.video_key(session_id), "bytes": size,
        "status": "ok" if ok else "failed",
    }
    if not ok:
        # A short, specific reason: the AWS code when S3 refused to answer, and
        # "not_found" when it answered plainly that nothing is there. Both are
        # recoverable facts; neither is recoverable from an absent event.
        event["error"] = probe["error"] or "not_found"
        # The browser knows things this side cannot: which of presign, PUT or
        # timeout broke (static/v2.html sends it as ?client_error=). It is kept
        # in its own field rather than merged into `error`, because that field
        # is this server's own finding and this one is a claim from a client.
        # Accepted only as a short bare token — see _client_token.
        hint = _client_token(client_error)
        if hint is not None:
            event["client_error"] = hint
    with open(sdir / "events.jsonl", "a", encoding="utf-8") as fh:
        fh.write(_json.dumps(event) + "\n")

    # Rebuild the aligned record now that the video is known.
    #
    # record.json is written by SessionStore.close, and the browser only
    # confirms the upload afterwards — it finishes the recording and closes the
    # socket in the same breath — so the record built at close always said the
    # encounter had no video. Measured over a synthetic wave: 25 of 27
    # encounters had a video and record.json reported none on every one. That
    # matters because record.json is the artefact the study ships to raters and
    # analysts, and the video is the thing Phase 2 rates. The API rebuilds the
    # record on read, so this only ever affected the stored copy — which is
    # exactly the copy that leaves the machine, in the download zip.
    try:
        from .encounter_record import write as _write_record
        _write_record(sdir)
    except Exception:  # noqa: BLE001, never fail the upload confirmation on this
        pass
    body = {"ok": ok, "bytes": size, "key": video.video_key(session_id),
            "status": event["status"]}
    if not ok:
        body["error"] = event["error"]
    if probe["error"]:
        # The event is already on disk, so the recording is recoverable whatever
        # happens next; the status code exists so a retrying client can tell
        # "S3 could not be asked, ask again" from "asked, and there is nothing
        # there", which is a settled 200 with ok=false. Never a 500: an AWS
        # error here is not a bug in this process, and a 500 is the one answer
        # that made the whole thing disappear.
        return JSONResponse(body, status_code=503)
    return body


# Ceiling on a body this route will accept, in bytes.
#
# The presigned path never puts a webcam recording through this process — the
# browser PUTs it straight to S3 — so nothing here has ever had to bound one.
# This route lands the bytes on the task's own filesystem, next to every live
# encounter's WAV writer and events.jsonl, and an unbounded body is therefore a
# way for anyone holding a session id to fill the disk out from under a
# conversation that is still being recorded. A ten-minute encounter out of
# MediaRecorder at its default bitrate is tens of megabytes; 512 MB is a
# ceiling no honest recording reaches and a disk-filling one passes in seconds.
#
# Read from the environment because the honest ceiling depends on how long the
# study's encounters run, and patched as a module global by tests for the same
# reason SESSION_KEY is (see tests/test_app.py's `fakes`).
#
# An unusable value warns and falls back rather than raising. This is module
# level, and this module's rule is that an import decides nothing and breaks
# nothing: verify_record, retranscribe and every pytest process import
# it, and a stray MAX_VIDEO_UPLOAD_BYTES=512MB would otherwise stop all of them
# with a ValueError naming nothing anyone would connect to a webcam upload.
_DEFAULT_VIDEO_UPLOAD_BYTES = 512 * 1024 * 1024


def _upload_ceiling(raw: Optional[str]) -> int:
    """A byte count from MAX_VIDEO_UPLOAD_BYTES, or the default with a warning."""
    text = (raw or "").strip()
    if not text:
        return _DEFAULT_VIDEO_UPLOAD_BYTES
    try:
        value = int(text)
        if value <= 0:
            raise ValueError("must be positive")
    except ValueError as exc:
        print(f"  WARNING: MAX_VIDEO_UPLOAD_BYTES={raw!r} is not a byte count "
              f"({exc}); using {_DEFAULT_VIDEO_UPLOAD_BYTES}")
        return _DEFAULT_VIDEO_UPLOAD_BYTES
    return value


MAX_VIDEO_UPLOAD_BYTES = _upload_ceiling(os.getenv("MAX_VIDEO_UPLOAD_BYTES"))


class _UploadTooLarge(Exception):
    """The request body ran past MAX_VIDEO_UPLOAD_BYTES while it was arriving."""


def _bounded_body(stream, limit: int):
    """The request body as a SYNCHRONOUS iterator of chunks, capped at `limit`.

    video.store_local is a blocking writer, so it has to run in the threadpool;
    the body it consumes only exists as an async iterator owned by the event
    loop. anyio.from_thread.run reaches back across that boundary from inside
    the worker thread this route is already using, and that is what keeps the
    upload to a single pass. Without it the only way to hand a blocking writer
    an async body is to spool it to a temp file first, which writes every byte
    of an IRB recording to disk twice.

    The cap is counted here, on arrival, rather than taken from Content-Length.
    A chunked upload declares no length at all and a declared one is a claim by
    the sender, so the only number that actually bounds what reaches the disk is
    the one added up as it lands.
    """
    chunks = stream.__aiter__()
    seen = 0
    while True:
        try:
            chunk = anyio.from_thread.run(chunks.__anext__)
        except StopAsyncIteration:
            return
        if not chunk:
            # Starlette yields a final empty chunk to close the body. Passing it
            # on would have the writer perform a write of nothing.
            continue
        seen += len(chunk)
        if seen > limit:
            # Raised INTO store_local, so its temp-file-then-os.replace never
            # reaches the replace and no half-recording appears at local_path.
            # A refused upload that left a truncated file behind would satisfy
            # video.exists() and lock the encounter out of every retry.
            raise _UploadTooLarge(seen)
        yield chunk


def _record_local_video(sdir: Path, session_id: str, written: int) -> dict:
    """Write the upload into the event trail and rebuild the aligned record.

    Blocking, and called through the threadpool for it: appending is cheap but
    encounter_record.write re-reads the manifest, the transcript and the whole
    event trail, and this process shares one event loop with every live
    encounter's audio.
    """
    from . import video

    import json as _json
    import time as _time

    # The SAME event type, and the same three fields, that
    # /api/sessions/{id}/video-uploaded writes. Every reader of this fact —
    # rater_packet._last_upload_event, encounter_record.build, verify_record,
    # video.upload_receipt, _video_state below — matches on `type` and then on
    # `bytes`, so a recording that arrived through this route is one uniform
    # fact to all five. A second event shape here would mean five second
    # branches, and the readers that were never updated would go on reporting a
    # perfectly good recording as absent.
    #
    # `key` is the canonical object key for this encounter even though the bytes
    # are on local disk, because that key is this encounter's name for its
    # recording everywhere else in the system and video.local_path mirrors it.
    # `via` is additive diagnosis on top — every reader ignores fields it does
    # not know — and it is the only thing in the trail that says which path the
    # bytes took, which is what an operator needs when a recording is on the
    # task's disk rather than in the bucket.
    event = {
        "t": None, "wall": _time.time(), "type": "video_uploaded",
        "key": video.video_key(session_id), "bytes": written, "status": "ok",
        "via": "local",
    }
    with open(sdir / "events.jsonl", "a", encoding="utf-8") as fh:
        fh.write(_json.dumps(event) + "\n")

    # Same rebuild, and the same reason, as the confirm endpoint above:
    # record.json is written by SessionStore.close, before any recording is
    # stored, so the stored copy always says the encounter had no video. That
    # copy is the one that leaves the machine in the download zip.
    try:
        from .encounter_record import write as _write_record
        _write_record(sdir)
    except Exception:  # noqa: BLE001, never fail a stored recording on this
        pass
    return event


@app.put("/api/sessions/{session_id}/video")
async def api_video_upload(request: Request, session_id: str,
                           participant_id: Optional[str] = None,
                           key: Optional[str] = None):
    """The participant's webcam recording, PUT to this server instead of to S3.

    The fallback for every reason the presigned path can be unavailable: no AWS
    credentials at all (a researcher's laptop, CI), a task role that can sign
    nothing, a bucket in the wrong region, an S3 endpoint the VPC cannot reach.
    On any of those, /api/sessions/{id}/video-upload-url answers 503 and today
    that is the end of it — the encounter is over, the bytes are in a Blob in a
    page that is about to close, and the artefact Phase 2 exists to rate is
    gone. This route is where they land instead.

    The authorisation is the presign route's, not an approximation of it: the
    same participant key check, the same session-directory resolution (which is
    also the traversal check), the same owner check, and the same one-shot
    guard. That guard is the load-bearing one. Without it, anyone who learns a
    session id can PUT bytes over a finished IRB recording, or seed the study's
    storage with objects nothing recorded — which is precisely what the presign
    route refuses, and a second door that does not refuse it is not a fallback,
    it is the hole reopened.

    Streamed to disk, never buffered: see _bounded_body. Deliberately
    `async def` where the two routes above are `def`, and for the same
    underlying rule rather than against it — the blocking half (the write, the
    event append, the record rebuild) is what goes to the threadpool, while the
    request body can only be read from the loop.
    """
    check_participant(key)
    from . import video

    from starlette.concurrency import run_in_threadpool

    sdir = _session_dir(session_id)  # existence + traversal check
    _require_session_owner(_load_manifest(sdir), participant_id)
    # The loudest of the eight. This route lands webcam bytes on the task's own
    # filesystem and had no withdrawal check of any kind: after a 200 from the
    # withdraw route, a PUT of 4096 bytes answered 200 and the bytes were on
    # disk. Refused before the body is read, not after it is stored.
    _refuse_if_withdrawn(participant_id, key, action="a webcam upload")

    # Two questions, because they fail in different places and the guard has to
    # hold when either one answers yes. video.exists() asks where the BYTES are
    # — a local file or an object in the bucket — and answers False rather than
    # raising when it cannot reach S3. upload_receipt() asks what this server
    # itself already acknowledged, which is a local read that keeps working with
    # no credentials at all. Consulting only the first would mean an S3 outage
    # silently reopened the overwrite that the presign route refuses on exactly
    # this evidence (see video.UploadUnconfirmed).
    #
    # 409 with the presign route's own wording, so static/v2.html reads it the
    # way it already reads that one: the recording is stored, stop re-sending
    # tens of megabytes.
    if video.exists(session_id) or video.upload_receipt(session_id) is not None:
        raise HTTPException(409, "video already uploaded")

    # Refused before a byte is read when the sender declares an oversized body.
    # The counted cap below is the real guard — a declared length is only a
    # claim — but honouring the claim when it is made costs nothing and saves
    # the disk a partial write of half a gigabyte.
    declared = request.headers.get("content-length")
    if declared and declared.strip().isdigit() and int(declared) > MAX_VIDEO_UPLOAD_BYTES:
        raise HTTPException(413, f"recording larger than {MAX_VIDEO_UPLOAD_BYTES} bytes")

    try:
        written = await run_in_threadpool(
            video.store_local, session_id,
            _bounded_body(request.stream(), MAX_VIDEO_UPLOAD_BYTES),
        )
    except _UploadTooLarge as exc:
        print(f"  WARNING: refused an oversized webcam upload for session "
              f"{session_id}: {exc.args[0]} bytes past a "
              f"{MAX_VIDEO_UPLOAD_BYTES} byte ceiling")
        raise HTTPException(413, f"recording larger than {MAX_VIDEO_UPLOAD_BYTES} bytes")
    except OSError as exc:
        # A full disk, a read-only mount, a permission error. 503 rather than
        # 500 for the same reason the presign route uses it: this is storage
        # failing, not a bug in this process, and a client that is told so can
        # retry instead of discarding the recording.
        print(f"  WARNING: could not store the webcam upload for session "
              f"{session_id} at {SESSIONS_DIR}: {type(exc).__name__}: {exc}")
        raise HTTPException(503, "recording storage unavailable")

    if written <= 0:
        # An empty body is not a recording, and leaving the empty file behind
        # would be worse than refusing it: video.exists() would answer True for
        # this encounter from now on, the one-shot guard above would refuse
        # every retry, and the encounter would be permanently unrateable with
        # nothing in the bucket and nothing on disk — the exact failure this
        # route was added to end, reintroduced by its own success path.
        try:
            video.local_path(session_id).unlink()
        except OSError:
            pass
        raise HTTPException(400, "empty body: no recording to store")

    event = await run_in_threadpool(_record_local_video, sdir, session_id, written)
    # The confirm endpoint's body shape, so a client can handle both paths with
    # one branch.
    return {"ok": True, "bytes": written, "key": event["key"], "status": "ok"}


@app.get("/api/run/config")
async def api_run_config(key: Optional[str] = None):
    """Where a finished participant is sent back to."""
    check_participant(key)
    return {
        "return_url": os.getenv("SURVEY_RETURN_URL", "").strip(),
        "return_label": os.getenv("SURVEY_RETURN_LABEL", "Return to the survey"),
        # Who a participant contacts about the study. Consent and its contact
        # details live outside this platform (2026-09-17); these three feed the
        # closing and withdrawal cards, and the page falls back to "the study
        # team" when they are blank.
        "contact_name": os.getenv("STUDY_CONTACT_NAME", "").strip(),
        "contact_email": os.getenv("STUDY_CONTACT_EMAIL", "").strip(),
        "irb_protocol": os.getenv("STUDY_IRB_PROTOCOL", "").strip(),
    }


@app.post("/api/participant")
async def api_post_participant(payload: Optional[dict] = None,
                               key: Optional[str] = Query(None)):
    """Mint a participant record for a page that was opened without one.

    A study arrival never needs this: /start mints the record and hands its id
    to the page. The researcher's single-scenario links (/v2?scenario=…) and a
    keyless development box do, because the voice socket refuses to open with
    no record at all. Never cohort "study": a record minted here belongs to no
    run, so it is internal under the researcher key and unattributed otherwise.
    """
    check_participant(key)
    body = payload or {}
    cohort = "internal" if _operator_key(key) else "unattributed"
    code = str(body.get("code") or "").strip() or f"direct_{secrets.token_hex(4)}"
    pid = await anyio.to_thread.run_sync(
        functools.partial(create_participant, code=code, cohort=cohort))
    return {"participant_id": pid, "cohort": cohort}


@app.get("/api/run/{run_id}")
async def api_run_get(run_id: str, key: Optional[str] = None):
    check_participant(key)
    from . import runs

    run = runs.get(run_id)
    if run is None:
        raise HTTPException(404, "no such run")
    return runs.view(run)


@app.post("/api/run/{run_id}/withdraw")
async def api_run_withdraw(run_id: str, payload: Optional[dict] = None,
                           key: Optional[str] = None,
                           participant_id: Optional[str] = None):
    """The participant stopped the study.

    The consent text promises they may stop at any time, and honouring that
    needs more than ending the current conversation: the run has to stop handing
    out encounters, or reopening the study link enrols them in the rest. It also
    has to leave a trace, so an analyst can tell a withdrawal from a dropout.

    Returns the run view, which carries the completion code. Someone who stops
    part-way has still given us their time, and the partial code is what they
    take back to the survey to be paid.

    Three things happen, and the third one was missing. runs.withdraw stamps
    every run of theirs and every participant record those runs name, so the
    refusal survives in the data and every reader gets the same answer; and then
    any encounter that is STILL RECORDING is torn down here. Without that last
    step the gate was at socket open only: somebody who pressed stop mid-
    conversation kept being recorded until they closed the tab.

    And whose run it is, is asked first. A stop cannot be undone, so the keyless
    empty POST that ended any run by id was not a smaller version of this
    route's job — it was the opposite of it.
    """
    check_participant(key)
    from . import runs

    body = payload or {}
    # WHOSE STUDY DOES THIS END. Asked before anything is written, because
    # nothing here can be taken back: see _may_stop_run for what counts as
    # proof and, more importantly, for the three legitimate callers it must
    # not refuse. The run is read rather than withdrawn-and-inspected so that a
    # refusal leaves the run document untouched.
    existing = runs.get(run_id)
    if existing is None:
        raise HTTPException(404, "no such run")
    pid = (participant_id or body.get("participant_id") or "").strip()
    if not _may_stop_run(existing, pid, key):
        print(
            f"  WARNING: a stop was POSTed for run {run_id} (participant key "
            f"{existing.get('participant_id')!r}, record "
            f"{existing.get('participant_record_id') or 'none'}) by a caller "
            f"presenting participant record {pid or 'none'}, which does not "
            f"belong to that run. The run is left alone: ending it would end "
            f"somebody else's study, permanently and with no way back."
        )
        raise HTTPException(403, "not your run")

    run = runs.withdraw(
        run_id,
        session_id=(body.get("session_id") or "").strip() or None,
        reason=(body.get("reason") or "").strip() or None,
    )
    if run is None:
        raise HTTPException(404, "no such run")

    # Every run of theirs, every record those runs name, and every record minted
    # under their key: a second tab on the other arm's run, or on a second
    # record of the same person, is the same person and the same microphone.
    # Through the shared helper, because this is one of three places a
    # withdrawal is recorded and the teardown used to hang off this one alone.
    await _enforce_withdrawal(run, where=f"the stop control on run {run_id}")
    return runs.view(run)


@app.post("/api/run/{run_id}/advance")
async def api_run_advance(run_id: str, session_id: Optional[str] = None,
                          key: Optional[str] = None):
    """Mark the current encounter complete and move to the next.

    The completion code is proof of completion, so an encounter is only marked
    done against a real recorded session that belongs to this run's
    participant, matches the current encounter's scenario, and actually had the
    participant speak. Otherwise the open endpoint would mint valid completion
    codes for runs that never happened.
    """
    check_participant(key)
    from . import runs

    run = runs.get(run_id)
    if run is None:
        raise HTTPException(404, "no such run")

    if not session_id:
        raise HTTPException(400, "session_id required")

    # Idempotency comes first: if this session was already recorded complete (a
    # retried or duplicated advance — e.g. the client re-POSTs after a dropped
    # response, or the End button races the auto-complete), return the run
    # unchanged. Re-running the checks below would compare against the NOW-current
    # encounter (the next scenario), so the scenario/owner checks would wrongly
    # 409/403 and permanently strand the participant.
    if any(c.get("session_id") == session_id for c in run.get("completed", [])):
        return runs.view(run)

    # A withdrawn run does not advance, and until now it said it had. runs.advance
    # no-ops on the stamp, so the encounter was never recorded against the run —
    # but the caller was told it was, which is the one thing a completion
    # endpoint may not get wrong: static/v2.html reads a 200 as "this encounter
    # is counted" and carries on to the next one for somebody who has stopped.
    #
    # BELOW the idempotency check, deliberately. Someone who finishes an
    # encounter and then presses stop can have a retried advance for that
    # finished encounter still in flight, and refusing it would strand the
    # encounter it was confirming. Reading is never what a withdrawal blocks:
    # GET /api/run/{id} still answers, so the partial completion code they take
    # back to the survey to be paid is still theirs, and 403 is a status
    # static/v2.html already treats as terminal-with-an-exit rather than a loop.
    if run.get("withdrawn") and not _is_operator(key):
        print(f"  NOTE: refusing to advance run {run_id}: the participant "
              f"withdrew ({(run.get('withdrawn') or {}).get('reason')}).")
        raise HTTPException(403, "participant withdrew from the study")

    sdir = _session_dir(session_id)  # existence + traversal check
    m = _load_manifest(sdir)

    expected_owner = run.get("participant_record_id")
    if expected_owner and m.get("participant_id") != expected_owner:
        raise HTTPException(403, "session does not belong to this run")

    idx = run.get("index", 0)
    scenarios = run.get("scenarios", [])
    if idx < len(scenarios):
        expected_scenario = scenarios[idx].get("id")
        if m.get("scenario") != expected_scenario:
            raise HTTPException(409, "session scenario does not match current encounter")

    # An encounter that recorded nothing is a real problem, but refusing to
    # advance is the wrong response to it: the participant would be stuck on
    # encounter 1 for the rest of the study with no completion code and no way
    # out, and a broken microphone would cost us the whole session rather than
    # one encounter. Let the run move on and record that this one was empty, so
    # the run is visibly incomplete in the data instead of silently missing from
    # it. `runs.advance` copies the entry through, so the flag lands on the
    # completed encounter.
    # THE SEVEN-MINUTE FLOOR (docs/study1-plan.md, E4.1). The encounter is the
    # measurement, and a three-minute conversation has not exercised it, so a
    # study encounter is not marked complete before ENCOUNTER_MIN_SECONDS have
    # passed since its socket opened — the same clock the page's ring fills on.
    # The runner holds its own exits (auto-advance, the actor's end tool, the
    # participant's move-on) to the same floor, so this is the backstop for the
    # one exit that does not pass through it: the page's End button, which
    # closes the session and POSTs here. Withdrawal is a different route and is
    # never gated; internal runs and the operator are exempt so the team can
    # walk the study fast.
    if run.get("cohort", "study") != "internal" and not _is_operator(key):
        from .storage import encounter_timing
        floor = encounter_timing()["min_seconds"]
        elapsed = m.get("duration_s")
        if elapsed is None and m.get("started_at"):
            elapsed = time.time() - float(m["started_at"])
        if elapsed is not None and float(elapsed) < floor:
            print(f"  NOTE: refusing to advance run {run_id}: encounter {session_id} "
                  f"ran {float(elapsed):.0f}s, under the {floor:.0f}s floor.")
            raise HTTPException(
                409,
                f"this conversation has run {int(float(elapsed))}s of the "
                f"{int(floor)}s the study asks for; keep going, or use "
                f"'Stop and leave the study' to withdraw")

    empty = _count_user_turns(sdir) < 1 and (m.get("n_turns") or 0) < 1
    if empty:
        idx = run.get("index", 0)
        if idx < len(run.get("scenarios", [])):
            run["scenarios"][idx]["no_participant_turns"] = True
            runs.save(run)

    run = runs.advance(run_id, session_id)
    if run is None:
        raise HTTPException(404, "no such run")
    return runs.view(run)


@app.get("/director", response_class=HTMLResponse)
async def director_page(key: Optional[str] = None):
    """Steering dashboard, what the director told each actor, and what it said."""
    check_key(key)
    return (STATIC_DIR / "director.html").read_text(encoding="utf-8")


@app.get("/evidence", response_class=HTMLResponse)
async def evidence_page(key: Optional[str] = None):
    """Evidence Trace: the aligned research view — participant, the direction
    that shaped each reply, the actor's line, fidelity against the instrument,
    and a replay synced to turns."""
    check_key(key)
    return (STATIC_DIR / "evidence.html").read_text(encoding="utf-8")


@app.get("/demo", response_class=HTMLResponse)
async def demo_page(key: Optional[str] = None):
    """The demo view: what this platform is, over a recorded wave, for showing
    somebody who has not seen it.

    It was the one console with no route. /, /chat, /researcher, /director,
    and /evidence all have one; demo.html was reachable only through the
    /static mount, so the page a colleague is most likely to be shown is the
    page that could not be found — and nothing in the app links to it either.
    MEASURED before this existed: GET /demo -> 404 {"detail":"Not Found"},
    GET /static/demo.html -> 200.

    Gated like /evidence and for the same reason: the page reads /health and
    renders encounters out of a recorded wave.
    """
    check_key(key)
    return (STATIC_DIR / "demo.html").read_text(encoding="utf-8")


# The two files a browser asks for without being told to, and the only two
# 404s a healthy install logs.
#
# /start/... serves _ENTRY_CHECK_PAGE, a self-contained page built in this file
# rather than one of the static documents, and it carried no <link rel="icon">
# — so every first arrival at an entry link logged
# "Failed to load resource: 404 (Not Found)" in the console before the
# participant had done anything at all. The icon link is on the page now; these
# routes are the belt to that brace, because the request is made for any page
# whose icon declaration the browser has not seen yet, and for 404s and error
# responses that carry no <head> to declare one from.
#
# Served from the files the other seven pages already point at, so there is one
# icon and not two.
def _icon_response(name: str):
    from fastapi.responses import FileResponse

    path = STATIC_DIR / name
    if not path.exists():
        raise HTTPException(404, "no icon installed")
    return FileResponse(path, media_type="image/png")


@app.get("/favicon.ico", include_in_schema=False)
async def favicon():
    return _icon_response("favicon.png")


@app.get("/apple-touch-icon.png", include_in_schema=False)
async def apple_touch_icon():
    return _icon_response("apple-touch-icon.png")


@app.get("/apple-touch-icon-precomposed.png", include_in_schema=False)
async def apple_touch_icon_precomposed():
    return _icon_response("apple-touch-icon.png")


# How much of the tail of events.jsonl the video probe reads. Both video event
# types are written by /api/sessions/{id}/video-uploaded, which the browser
# posts AFTER the encounter has ended and SessionStore.close has closed the
# event file — so they are always the last lines, and nothing but a repeated
# confirmation can follow them. 32 KB is thousands of times more room than the
# handful of retries a browser makes, and it keeps a polled dashboard call off
# the O(transcript) read that scanning the whole file would cost on every row.
_VIDEO_TAIL_BYTES = 32768


def _video_state(session_dir: Path) -> str:
    """What became of this encounter's webcam recording.

    Three answers, in encounter_record's own words so the console, record.json
    and the rater packet cannot end up describing the same fact differently:
    "ok" (a recording exists), "failed" (one was made and could not be stored or
    confirmed), "absent" (no confirmation ever arrived). Plus "unknown", which
    is what this returns when the evidence is unreadable — never "absent",
    because "nobody filmed this" and "I could not tell" are different claims and
    only one of them should downgrade an encounter.

    The rule is encounter_record.build's rule, deliberately: last event wins, an
    upload of zero bytes is not a recording, and a local webcam* file counts (a
    dev capture never goes near S3).
    """
    try:
        if any(session_dir.glob("webcam*")):
            return "ok"
    except OSError:
        return "unknown"
    ev = session_dir / "events.jsonl"
    try:
        with open(ev, "rb") as fh:
            fh.seek(0, os.SEEK_END)
            size = fh.tell()
            fh.seek(max(0, size - _VIDEO_TAIL_BYTES))
            chunk = fh.read()
    except OSError:
        return "unknown"
    text = chunk.decode("utf-8", "replace")
    if size > _VIDEO_TAIL_BYTES:
        # The window almost certainly opened mid-line; that fragment is not a
        # JSON object and must not be parsed as one.
        text = text.split("\n", 1)[-1]
    attempts = []
    for line in text.splitlines():
        # Cheap prefilter, as in rater_packet._last_upload_event: only a couple
        # of lines in an encounter are ever this type.
        if '"video_uploaded"' not in line:
            continue
        try:
            e = json.loads(line)
        except ValueError:
            continue
        if e.get("type") == "video_uploaded":
            attempts.append(e)
    if any((e.get("bytes") or 0) > 0 for e in attempts):
        return "ok"
    if attempts:
        return "failed"
    return "absent"


def _encounter_status(m: dict, video: str = "unknown") -> str:
    """A coarse, cheap quality status for the encounter list: active (still
    recording), complete, partial (a channel is missing), or failed (nothing
    recorded). 'flagged' (a validity flag needing a look) is a later, richer
    check.

    `video` is that channel's state as _video_state reports it. It used to be
    absent from this judgement entirely, which meant an encounter nobody filmed
    — the webcam recording being the artefact Phase 2 rates and the thing the
    whole reliability design rests on — came back "complete" and rendered as a
    green badge in the evidence console. The docstring promised "partial (a
    channel is missing)" while the one channel most worth missing could not
    produce it. "unknown" never downgrades: the manifest cannot say, and a
    confident wrong answer here routes a whole day's triage.
    """
    if m.get("status") == "active":
        return "active"
    n = m.get("n_turns") or 0
    if n <= 0:
        return "failed"
    audio = m.get("audio") or {}
    ua = audio.get("user_audio_duration_s")
    aa = audio.get("assistant_audio_duration_s_by_agent") or {}
    if ua is None:
        # Text-mode session: no audio channel expected, and no camera either, so
        # a missing recording is not a missing channel here.
        return "complete"
    if not (ua and any(v for v in aa.values())):
        return "partial"
    if video in ("failed", "absent"):
        return "partial"
    return "complete"


# How many session directories one /api/encounters call may open. A wave is
# ~100 participants x 4 encounters, so this is several waves' worth of sessions
# plus test traffic: a filtered dashboard poll cannot turn into a full scan of
# the volume, and no realistic study loses an encounter to the bound.
ENCOUNTER_SCAN_LIMIT = 2000


@app.get("/api/encounters")
async def api_encounters(key: Optional[str] = None, limit: int = 60,
                         cohort: Optional[str] = None):
    """Recorded encounters, newest first, for the steering dashboard.

    `cohort` filters on the manifest's own cohort tag, so internal test traffic
    can be excluded here rather than only through the /api/runs join. Three
    values are minted: "study", "internal" (the /test entrance) and
    "unattributed" (a participant whose survey key did not pipe, kept out of the
    study set until someone hand-joins them). Sessions recorded before the tag
    existed, and sessions started outside a run, have no cohort and are
    therefore excluded by any filter.
    """
    check_key(key)
    from .storage import SESSIONS_DIR

    out = []
    dirs = sorted(
        (d for d in SESSIONS_DIR.iterdir() if d.is_dir()),
        key=lambda d: d.stat().st_mtime,
        reverse=True,
    )
    for scanned, d in enumerate(dirs):
        # Collect `limit` entries rather than slicing the directory list first:
        # with a cohort filter a leading slice would return fewer than `limit`
        # matches (often none) even when older matching encounters exist. The
        # cost is that a filtered call no longer stops early — it keeps opening
        # manifests until it has `limit` matches or runs out of directories —
        # and the researcher dashboard polls this endpoint, so the scan is
        # bounded: newest-first, at most ENCOUNTER_SCAN_LIMIT directories
        # examined. (The listdir and the stat-based sort above are O(all
        # sessions) either way; what this bounds is the JSON parse.) A filter
        # that reaches the bound stops at the oldest encounter it saw; older
        # ones are still reachable through the /api/runs join.
        if len(out) >= limit or scanned >= ENCOUNTER_SCAN_LIMIT:
            break
        manifest = d / "manifest.json"
        if not manifest.exists():
            continue
        try:
            m = json.loads(manifest.read_text(encoding="utf-8"))
        except ValueError:
            continue
        if cohort and (m.get("cohort") or "") != cohort:
            continue
        # Only for a finished encounter. The browser posts the upload
        # confirmation after the socket closes, so an encounter still recording
        # can only ever answer "unknown" — and this route is polled.
        video = "unknown" if m.get("status") == "active" else _video_state(d)
        entry = {
            "id": d.name,
            "scenario": m.get("scenario"),
            "started_at": m.get("started_at"),
            "participant_id": m.get("participant_id"),
            # Self-describing study context, straight off the manifest.
            "run_id": m.get("run_id"),
            "cohort": m.get("cohort"),
            "encounter_index": m.get("encounter_index"),
            "status": _encounter_status(m, video),
            # Carried in its own right as well as folded into `status`, so the
            # console can tell "the camera was never on" from "the recording was
            # made and lost" without re-fetching every record: one is
            # measurement working as designed and the other is a fault to chase.
            "video": video,
        }
        try:
            from .scenarios_v3 import load_spec
            spec = load_spec(m.get("scenario"))
            entry.update(
                construct=spec["construct"], variant=spec["variant"],
                title=spec["title"], study=True,
            )
        except Exception:
            entry["study"] = False
        out.append(entry)
    # Sorted on the encounter's own start time, not on the directory's mtime.
    #
    # The scan above stays mtime-ordered: it is what bounds the work
    # (newest-touched first, at most ENCOUNTER_SCAN_LIMIT manifests opened) and
    # it is the only ordering available before a manifest has been read. But
    # mtime is not when the encounter happened. MEASURED on the demo wave: the
    # session directories' mtimes span 0.4 seconds while their manifests'
    # `started_at` spans 7.9 days, so the sort key carried essentially no time
    # signal and the order was filesystem tie-breaking noise — this route's
    # first entry was an encounter from the 6th while /api/sessions' (SQLite-
    # ordered, same server, same 27 encounters) was the 10th. Anything that
    # touches a session directory after the encounter decouples the two, and
    # several ordinary things do: the post-close `video_uploaded` append, a
    # retranscribe pass, an rsync, a restore, or simply copying DATA_DIR onto
    # another machine.
    #
    # This route is what static/evidence.html's session list is built from, so
    # the docstring's "newest first" is a promise a researcher reads off the
    # screen. The honest caveat: this corrects the order WITHIN the page that
    # was scanned, not which encounters entered it once the scan bound is hit.
    # For a wave of tens or hundreds it is exactly right; at the bound, mtime
    # still decides membership and the /api/runs join remains the way to reach
    # older encounters.
    out.sort(key=lambda e: e.get("started_at") or 0, reverse=True)
    return out


@app.get("/api/encounters/{session_id}/record")
async def api_encounter_record(session_id: str, key: Optional[str] = None):
    """The aligned record: transcript, stage directions, triggers, provenance."""
    check_key(key)
    from .encounter_record import build
    from .storage import SESSIONS_DIR

    d = SESSIONS_DIR / session_id
    if not d.exists():
        raise HTTPException(status_code=404, detail="No such encounter")
    record = build(d)
    # Trigger firings are not in the aligned record; the dashboard wants them.
    #
    # The event log is append-only, so a beat that was briefed and then not
    # delivered leaves its trigger_fired line behind, cancelled by a later
    # trigger_undelivered. Counting the raw firings would report a beat nobody
    # spoke as reached, and that count is what a researcher uses to decide
    # whether an encounter is scoreable. verify_record owns the netting rule
    # (it is positional, because a retry re-fires at the same index); reuse it
    # rather than keeping a second copy that can drift.
    from .verify_record import _net_fired

    fired = []
    ev = d / "events.jsonl"
    if ev.exists():
        events = []
        for line in ev.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            try:
                events.append(json.loads(line))
            except ValueError:
                continue
        fired = [
            {
                "t": e.get("t"), "trigger_id": e.get("trigger_id"),
                "interaction": e.get("interaction"), "esci": e.get("esci", []),
                "probing": e.get("probing"),
            }
            for e in _net_fired(events)
        ]
    record["triggers_fired"] = fired

    # Attach the scenario's own plan so the dashboard can show coverage, which
    # interaction each turn belongs to by name, and which planted triggers were
    # reached out of those the instrument specifies.
    try:
        from .scenarios_v3 import load_spec, parallel_forms
        spec = load_spec(record.get("scenario"))
        record["spec"] = {
            "construct": spec["construct"],
            "variant": spec["variant"],
            "title": spec["title"],
            # BOTH, and the console renders the list. `parallel_form` is a
            # scalar the specs carry as provenance — the sibling each was
            # written to match — and every construct now has three forms, so a
            # console that renders the scalar as "pair" names one sibling and
            # silently hides the other. That is the misreading scenarios_v3's
            # own comment above parallel_forms() warns about, on the surface a
            # researcher reads mid-run to see what a retest would draw from.
            "parallel_form": spec.get("parallel_form"),
            "parallel_forms": parallel_forms(record.get("scenario")),
            "skill_measured": spec.get("skill_measured", "").strip(),
            "esci_items": spec.get("esci_items", {}),
            "interactions": [
                {
                    "id": i["id"], "label": i.get("label", ""), "mode": i["mode"],
                    "observe": i.get("observe", ""),
                    "triggers": [
                        {"id": t["id"], "esci": t.get("esci", []), "cue": t.get("cue", ""),
                         "probe": bool(t.get("on_silence")), "scored": bool(t.get("scores"))}
                        for t in i.get("triggers", [])
                    ],
                }
                for i in spec.get("interactions", [])
            ],
        }
    except Exception:
        record["spec"] = None  # legacy scenario, not part of the study bank

    # The plan attached above is today's file, but this encounter ran against
    # the plan as it stood when it started, and the two diverge: S2 A gained two
    # planted beats between waves, which silently moved the denominator of every
    # S2 A encounter recorded before the edit from 4/4 to 4/6. Compare the
    # fingerprint stamped at session start (storage.spec_fingerprint) with the
    # current file and say so, so a coverage figure that dropped because the
    # instrument moved is not read as an encounter that went worse. Null when
    # they agree, and when the encounter predates the stamp — in that case
    # nothing can be said either way, which is why the stamp exists.
    record["spec_drift"] = None
    try:
        from .storage import spec_fingerprint

        stamped = record.get("spec_fingerprint") or None
        current = spec_fingerprint(record.get("scenario"))
        if stamped and current and stamped.get("sha256") != current.get("sha256"):
            was = list(stamped.get("trigger_ids") or [])
            now = list(current.get("trigger_ids") or [])
            record["spec_drift"] = {
                "recorded_sha256": stamped.get("sha256"),
                "current_sha256": current.get("sha256"),
                "recorded_trigger_ids": was,
                "current_trigger_ids": now,
                "added_since": [t for t in now if t not in was],
                "removed_since": [t for t in was if t not in now],
            }
    except Exception:  # noqa: BLE001, a drift check must not 500 the record view
        pass
    return record


@app.get("/api/sessions")
async def api_sessions(key: Optional[str] = None, cohort: Optional[str] = None,
                       limit: int = 30, offset: int = 0):
    """Live and recent sessions. `cohort` filters to exactly one of the three
    tags a run can carry — "study", "internal", "unattributed" — so internal
    test traffic and unpiped-key arrivals can both be kept out of a listing.
    Sessions started outside a run have no tag and match no filter.

    `limit`/`offset` page the closed sessions. The default of 30 is the
    dashboard's page size, but this is also the only bulk source of per-encounter
    duration, so a fixed cap of 30 made 370 of a 400-encounter wave invisible to
    every analysis that needed it. Capped at 1000 per call so a stray request
    cannot pull a whole wave through one synchronous query."""
    check_key(key)
    out = []
    active_ids = set(registry.list_ids())
    for sid in active_ids:
        s = registry.get(sid)
        if cohort and (s.cohort or "") != cohort:
            continue
        out.append({
            "id": s.id,
            "scenario": s.scenario.id,
            "title": s.scenario.title,
            "mode": s.scenario.mode,
            "model": s.model,
            "cast_size": len(s.scenario.cast),
            "turn_count": sum(1 for h in s.shared_history if h["speaker"] == "user"),
            "status": "active",
            "started_at": s.store.started_at,
            "run_id": s.run_id,
            "cohort": s.cohort,
        })
    # Append recent closed sessions from SQLite so the researcher can browse +
    # download past data even after a session ends.
    #
    # sqlite3 is synchronous and this coroutine shares its event loop with every
    # live encounter: while it blocks, participant PCM is not relayed and the
    # silence detector that fires the planted probes is not fed. The researcher
    # dashboard polls this route every few seconds throughout a wave, so the
    # disk-bound half — the query, and the scenario-title map on its cold call —
    # runs in a worker thread and the loop stays free for the audio.
    from starlette.concurrency import run_in_threadpool

    def _read_closed() -> tuple:
        import sqlite3
        from .storage import DB_PATH

        conn = sqlite3.connect(DB_PATH)
        try:
            conn.row_factory = sqlite3.Row
            # run_id/cohort were added to the sessions table later. A database
            # written before that migration must still list its closed sessions
            # (the caller swallows any failure here, so naming a missing column
            # would silently empty the researcher's list), so select them only
            # when they are actually there.
            have = {r[1] for r in conn.execute("PRAGMA table_info(sessions)")}
            extra = [c for c in ("run_id", "cohort") if c in have]
            # Every filter belongs in the WHERE clause, because LIMIT/OFFSET
            # counts rows the query returns, not rows the caller keeps. Filtering
            # in Python afterwards meant a page could be emptied by rows that
            # were never wanted — ?cohort=study&limit=1&offset=0 came back with
            # nothing at all on the fixture wave, because the single internal
            # test encounter is the newest row and it occupied the whole page.
            # A pager that stops on a short page then exports a fraction of the
            # wave and cannot tell that from the end of the data.
            where = ["status != 'active'"]
            params: list = []
            if cohort:
                if "cohort" not in have:
                    # A pre-migration index cannot answer a cohort question, and
                    # no row in it matches one. Same answer as before, reached
                    # without pretending to page.
                    return [], {s["id"]: s["title"] for s in list_scenarios()}
                where.append("cohort = ?")
                params.append(cohort)
            if active_ids:
                # A row can be both indexed and live (the index is written at
                # open); the live copy is already in `out` above.
                where.append(f"id NOT IN ({','.join('?' * len(active_ids))})")
                params.extend(sorted(active_ids))
            params.extend((max(1, min(int(limit), 1000)), max(0, int(offset))))
            rows = conn.execute(
                "SELECT id, scenario, model, started_at, n_turns, status, duration_s"
                + "".join(f", {c}" for c in extra)
                + " FROM sessions WHERE " + " AND ".join(where)
                + " ORDER BY started_at DESC LIMIT ? OFFSET ?",
                params,
            ).fetchall()
        finally:
            # Closed in the thread that opened it, and closed even on failure:
            # a leaked connection here would hold the WAL open against the
            # writers that every live session's storage layer uses.
            conn.close()
        # Plain dicts, so nothing sqlite-owned outlives the worker thread.
        # Missing columns read back as None rather than raising, which is what
        # the pre-migration database needs.
        # Map scenario id → title without re-reading every YAML each call.
        titles = {s["id"]: s["title"] for s in list_scenarios()}
        return [dict(r) for r in rows], titles

    try:
        rows, titles = await run_in_threadpool(_read_closed)
    except Exception:  # noqa: BLE001, a missing index must not empty the listing
        rows, titles = [], {}
    # No filtering here: the query above already excluded the live ids and the
    # other cohorts, so every row it returned is a row this page returns and the
    # page length means what a pager thinks it means.
    for r in rows:
        row_cohort = r.get("cohort")
        out.append({
            "id": r["id"],
            "scenario": r["scenario"],
            "title": titles.get(r["scenario"], r["scenario"]),
            "model": r["model"],
            "turn_count": r["n_turns"] or 0,
            "status": r["status"] or "closed",
            "started_at": r["started_at"],
            "duration_s": r["duration_s"],
            "run_id": r.get("run_id"),
            "cohort": row_cohort,
        })
    return out


def _run_context(participant_id: Optional[str], run_id: Optional[str] = None) -> Optional[dict]:
    """Resolve the run this encounter belongs to, server-side.

    A recorded encounter has to be self-describing. Without run id, cohort and
    participant key on its own manifest, the only link from a session back to
    its run was the entry the browser POSTs to /api/run/{id}/advance, so an
    encounter whose client never reported back was orphaned, and no offline tool
    (verify_record, retranscribe) could tell internal test traffic from
    study data.

    The socket carries the participant *record* id, so the run is looked up from
    that; a ?run= hint is honoured only when it names this same participant's
    run, so nobody can attach their encounter to a stranger's run. Any failure
    resolves to None: an encounter must never fail to start over bookkeeping.

    THE RECORD'S OWN RUN COMES FIRST, and that is the fix for a reproduced
    cohort forgery. This used to fall straight through to
    find_by_participant_record, which answers with the NEWEST run pointing at
    the record — so an encounter's cohort was not a property of the encounter at
    all, it was re-derived at every read from whatever the run directory looked
    like at that moment. Three keyless requests moved a demo encounter tagged
    cohort=internal into the analysis set: mint a second run, point it at the
    demo participant's record, and the cohort this function reports flips to
    "study". An encounter's cohort has to be decided once, when the encounter
    starts, and not be re-derivable by anything that happens later. So the
    record carries the run it was minted for (storage.create_participant), and
    that binding is what is read here; the scan is the fallback for a record
    that predates the binding.
    """
    if not participant_id:
        return None
    try:
        from . import runs

        rec = get_participant(participant_id) or {}
        run = runs.get(run_id) if run_id else None
        if run is None or run.get("participant_record_id") != participant_id:
            run = runs.get(rec.get("run_id")) if rec.get("run_id") else None
        if run is None:
            run = runs.find_by_participant_record(participant_id)
        if run is None:
            return None
        return {
            "run_id": run.get("run_id"),
            # The record's own tag wins where it has one: it was written when
            # the encounter's run was chosen, and the run file can be edited
            # afterwards by anything that can write to the directory.
            "cohort": rec.get("cohort") or run.get("cohort", "study"),
            "participant_key": run.get("participant_id"),
            # 1-based position in the four-encounter sequence as this encounter
            # starts, so the record keeps its place in the run's order even if
            # the run file is later lost.
            "encounter_index": (run.get("index") or 0) + 1,
        }
    except Exception:  # noqa: BLE001, never block an encounter on this
        return None


# --- The live failure channel ---
#
# The researcher console is the only window in which a collapsing encounter can
# still be rescued, and until this existed it carried three message types —
# state, transcript, steering — none of which can say that anything went wrong.
# A director whose every routing call 401s, a steering pass that errors on every
# turn, a planted beat briefed and never spoken: all of them landed in
# events.jsonl and were broadcast to nobody, so on screen a dying encounter and
# a healthy one were the same picture for the full 7-12 minutes.
#
# The fourth frame is:
#
#   {"type": "encounter_event", "kind": <str>, "t": <float|null>,
#    "agent_id": <str|null>, "detail": <str|null>, "severity": "warn"|"error"}
#
# It is emitted by the runner (realtime_voice_session) through session.broadcast
# and rendered by static/researcher.html. Those three shapes have to agree, so
# do not add or rename a field on one side alone.

# How many encounter_events one session keeps for a late joiner. A researcher
# opening the console two minutes into a failing encounter needs the failures
# that already happened, not only the ones still to come — that is the whole
# point of a monitoring surface. Bounded because the failure mode this exists
# for is the repeating one: a gateway that refuses every call produces an event
# per turn, and an unbounded list would be an unbounded send on connect.
ENCOUNTER_EVENT_REPLAY_LIMIT = 200


def _arm_encounter_event_log(session) -> None:
    """Start keeping this session's encounter_events, so late joiners see them.

    session.broadcast already delivers the frame to whoever is connected; what
    was missing is the record for whoever is not. This wraps the session's own
    broadcast rather than adding a list to Session, because broadcast is the one
    point every emitter already goes through — an emitter that forgets to
    append to a second list would be exactly the silent gap this fixes.

    Idempotent: called from both participant sockets when the session is minted,
    and again when a researcher attaches, so a session created by some other
    path still starts recording from the moment anyone is watching.
    """
    if getattr(session, "encounter_events", None) is not None:
        return
    session.encounter_events = []
    inner = session.broadcast

    async def broadcast(message: dict) -> None:
        if isinstance(message, dict) and message.get("type") == "encounter_event":
            log = session.encounter_events
            log.append(message)
            # Keep the newest. An encounter that has produced 200 failures is
            # already lost; what a researcher needs off this list is what is
            # happening now.
            if len(log) > ENCOUNTER_EVENT_REPLAY_LIMIT:
                del log[:-ENCOUNTER_EVENT_REPLAY_LIMIT]
        await inner(message)

    session.broadcast = broadcast


async def _report_encounter_failure(session, kind: str, detail: str, *,
                                    severity: str = "error") -> None:
    """Put one failure this module caught onto the researcher's live channel.

    The runner emits its own; this covers the failures that escape it entirely
    and reach the socket handler, which is where a gateway that will not connect
    at all arrives. `detail` is redacted for the same reason the stored event
    beside it is: it is an exception message from the gateway.

    Never raises. A monitoring frame that could take down the encounter it is
    reporting on would be worse than no frame.
    """
    try:
        started = getattr(getattr(session, "store", None), "started_at", None)
        await session.broadcast({
            "type": "encounter_event",
            "kind": kind,
            # Seconds into the encounter, so a frame replayed to a late joiner
            # can be placed against the transcript rather than floating at the
            # bottom of it. Null when the store is gone, which is what a failure
            # during teardown looks like.
            "t": round(time.time() - started, 3) if started else None,
            "agent_id": None,
            "detail": redact_key(detail),
            "severity": severity,
        })
    except Exception:  # noqa: BLE001
        pass


# --- Participant: voice path ---

@app.websocket("/ws/participant/voice")
async def ws_participant_voice(
    ws: WebSocket,
    scenario: str = Query(...),
    participant_id: Optional[str] = Query(None),
    model: Optional[str] = Query(None),
    launch: Optional[str] = Query(None),
    key: Optional[str] = Query(None),
    run: Optional[str] = Query(None),
):
    if PARTICIPANT_KEY_REQUIRED and SESSION_KEY and key != SESSION_KEY:
        await ws.close(code=4401)
        return
    # Voice capture needs a participant record that exists and is not
    # withdrawn (_participant_may_capture). Consent is taken in Qualtrics
    # before the participant reaches this app, so it is not asked of here.
    if not _participant_may_capture(participant_id):
        await ws.close(code=4403)
        return
    await ws.accept()

    # The model is a study variable: honour only a researcher-authenticated
    # launch override, never a participant-supplied ?model=.
    launch_cfg = LAUNCHES.get(launch) if launch else None
    effective_model = launch_cfg.get("model") if launch_cfg else None

    try:
        session = registry.create(
            scenario, model=effective_model, participant_id=participant_id,
            capture_audio=True, run_context=_run_context(participant_id, run),
        )
        _arm_encounter_event_log(session)
    except FileNotFoundError as e:
        await ws.send_json({"type": "error", "message": str(e)})
        await ws.close()
        return

    # From here a Session exists in the registry, so everything runs inside the
    # try whose finally drops it, an early disconnect during setup would
    # otherwise leak the session forever.
    try:
        # Researcher-configured launch: apply gear presets before the first turn.
        if launch_cfg:
            await _apply_launch_config(session, launch_cfg)

        session.participant_ws = ws
        await ws.send_json({
            "type": "session",
            "session_id": session.id,
            "scenario": {
                "id": session.scenario.id,
                "title": session.scenario.title,
                "intro": session.scenario.intro,
                "mode": session.scenario.mode,
            },
            "cast": [
                {"id": a.id, "name": a.name, "role": a.role, "photo": a.photo}
                for a in session.scenario.cast
            ],
            "audio": {"sample_rate": 16000, "channels": 1, "encoding": "pcm_s16le"},
        })

        # Every scenario runs as consecutive 1:1 conversations on Gemini Live,
        # the cast is played one character at a time, in order.
        runner = RealtimeVoiceSessionRunner(session, ws)
        await runner.run()
    except WebSocketDisconnect:
        pass
    except Exception as e:
        # The credential-disclosure site. RealtimeVoiceSessionRunner.run() has no
        # except of its own, so a failure in the first connect() to the realtime
        # gateway lands here whole — and that is precisely the exception that
        # carries the key: a key pasted wrapped across two lines makes websockets
        # raise InvalidHeaderValue whose message quotes the entire Authorization
        # header. Unredacted it went into events.jsonl (archived per encounter,
        # shipped in the download zip) and down the participant's own socket.
        safe = redact_key(str(e))
        session.log.event("error", where="voice_ws", message=safe)
        # And onto the researcher's live channel: this is the encounter dying
        # outright, which is the one failure a watching researcher could not see
        # at all before — the participant's page shows a neutral notice and the
        # console showed nothing whatever.
        await _report_encounter_failure(session, "voice_ws_error", safe)
        try:
            await ws.send_json({"type": "error", "message": safe})
        except Exception:
            pass
    finally:
        registry.drop(session.id)


# --- Researcher ---

@app.websocket("/ws/researcher")
async def ws_researcher(ws: WebSocket, session_id: str = Query(...), key: Optional[str] = Query(None)):
    # The researcher channel exposes the full live transcript and the steering
    # controls, so it must require the session key whenever one is configured,
    # exactly like the HTTP researcher routes (check_key). It must NOT depend on
    # PARTICIPANT_KEY_REQUIRED, which is off in the normal deployment.
    if SESSION_KEY and key != SESSION_KEY:
        await ws.close(code=4401)
        return

    try:
        session = registry.get(session_id)
    except KeyError:
        await ws.close(code=4404)
        return

    await ws.accept()
    session.researcher_wss.add(ws)
    await ws.send_json({"type": "state", **session.snapshot()})
    # Replay shared history so the researcher view is populated even if they
    # joined late. Each entry carries speaker (user|agent_id).
    for entry in session.shared_history:
        role = "user" if entry["speaker"] == "user" else "assistant"
        await ws.send_json({
            "type": "transcript",
            "role": role,
            "agent_id": entry["speaker"] if role == "assistant" else None,
            "text": entry["text"],
        })
    # Replay gear switches (launch presets, manual, auto) so the steering log
    # is complete even for late joiners.
    for entry in session.steering_log:
        await ws.send_json(entry)
    # And replay the failures. A researcher opens this console *because*
    # something looks wrong, which means they almost always connect after the
    # first failure rather than before it; a channel that carried only future
    # events would show a clean screen for an encounter whose director died two
    # minutes ago. Armed first so a session that was created outside the two
    # participant sockets starts recording from here on rather than never.
    _arm_encounter_event_log(session)
    for entry in session.encounter_events:
        await ws.send_json(entry)

    try:
        while True:
            msg = await ws.receive_json()
            # A malformed control frame (missing key, non-numeric value, unknown
            # knob/agent/branch) must not tear the socket down: report it and
            # keep the researcher's live monitoring/steering connection open.
            try:
                t = msg.get("type")
                agent_id = msg.get("agent_id")  # optional, applies to that agent only
                if t == "set_knob":
                    await session.set_knob(msg["knob"], float(msg["value"]), agent_id=agent_id)
                elif t == "note":
                    await session.add_note(msg["text"], agent_id=agent_id)
                elif t == "clear_notes":
                    await session.clear_notes(agent_id=agent_id)
                elif t == "branch":
                    await session.trigger_branch(msg["id"])
                elif t == "set_model":
                    await session.set_model(msg["model"])
                elif t == "set_auto_steering":
                    await session.set_auto_steering(bool(msg.get("enabled")))
                else:
                    await ws.send_json({"type": "error", "message": f"unknown type: {t}"})
                    continue
                await session.broadcast({"type": "state", **session.snapshot()})
            except (KeyError, ValueError, TypeError) as e:
                await ws.send_json(
                    {"type": "error", "message": f"{type(e).__name__}: {e}"}
                )
    except WebSocketDisconnect:
        pass
    finally:
        session.researcher_wss.discard(ws)


if __name__ == "__main__":
    import sys
    import uvicorn

    # Windows consoles default to cp1252, which cannot encode most of the
    # punctuation in this codebase — and the very first thing `python -m
    # server.app` did was print an arrow, so on Windows the documented way to
    # start the server crashed before uvicorn was reached. Every scenario title,
    # persona brief and participant transcript in this study also carries em
    # dashes and curly quotes, so any later print of study content would have
    # failed the same way. Reconfigure the streams once, here, rather than
    # policing every print; errors="replace" means an unprintable character
    # degrades to a placeholder instead of killing the process.
    for _stream in (sys.stdout, sys.stderr):
        try:
            _stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, OSError):
            # Not a real console (a pipe, a service manager, an older Python).
            # Nothing to reconfigure, and the ASCII banner below still prints.
            pass

    # Refuse to serve a public hostname with no researcher credential. Checked
    # here rather than at import so pytest and the offline tools (verify_record,
    # retranscribe) can still load this module on a laptop with no
    # SESSION_KEY set; `python -m server.app` is how the container starts, so
    # this is the door.
    _refusal = _refuse_unprotected_public_start()
    if _refusal:
        print(f"  REFUSING TO START: {_refusal}")
        sys.exit(2)

    host = os.getenv("HOST", "127.0.0.1")
    port = int(os.getenv("PORT", "8765"))
    print(f"Relational Fluency Platform -> http://{host}:{port}")
    uvicorn.run("server.app:app", host=host, port=port, reload=False, log_level="info")
