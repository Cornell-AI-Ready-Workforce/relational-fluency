# Deploying the relational-fluency platform to Fly.io

> **Demo path only (2026-08).** Study deployment targets **AWS ECS/Fargate**
> behind an ALB, with study data in encrypted S3 — see
> [`architecture.md`](architecture.md) and `infra/terraform/`. Fly is fine for
> quick demos and dogfooding, but participant data must not be collected here:
> the persistent volume below sits outside the IRB data-management plan.

A demo/dogfood deployment with HTTPS, persistent volume for the dataset, and
WebSocket support. Roughly 30 minutes end-to-end.

> **One thing this path has that the study deployment does not: a volume.**
> Step 4 below creates a 1 GB volume and `fly.toml` mounts it at `/data`, so a
> Fly demo keeps its sessions across deploys. The AWS study service has **no**
> persistent volume today — every live task-definition revision has
> `volumes=[]`, and there is no EFS file system in the account — so its
> `/data` is the container's writable layer and a deploy takes the recordings
> with it. That is not a reason to collect participant data here instead; it is
> a reason not to generalise from "the demo kept my sessions". See
> [`DEPLOY-AWS.md`](DEPLOY-AWS.md#no-persistent-volume-yet).

## What you'll have when you're done

- A URL like `https://rf-yourname.fly.dev` accessible from anywhere
- HTTPS (required for browser mic and webcam access on every supported browser
  — see the [supported browser matrix](../README.md#browsers), which also
  explains why recordings come back in two different containers)
- A 1 GB persistent volume at `/data` holding sessions and SQLite
- Access protected by a session key (anyone without it gets `401`)
- API keys stored as Fly secrets, not in the image

## Cost expectation

- Fly's `shared-cpu-1x@512mb` VM + 1 GB volume: roughly **$3–6/month** at idle, more if traffic ramps.
- **API costs are separate**: the live voice stack is Gemini Live (`nto.gemini-live-2.5-flash`) served through the Cornell LiteLLM gateway, so every voice turn is billed against your gateway virtual key. Track spend and set a per-key cap in the gateway/LiteLLM console rather than budgeting per external STT/TTS provider — the retired v1 Deepgram/ElevenLabs cascade is no longer used.

---

## One-time setup

### 1. Install the Fly CLI

One line per platform rather than one line plus a POSIX fallback: the fallback
below fails twice over in Windows PowerShell, where `curl` is an alias for
`Invoke-WebRequest` (and rejects `-L`) and `sh` does not exist at all.

macOS:

```bash
brew install flyctl
```

Linux (and macOS without Homebrew):

```bash
curl -L https://fly.io/install.sh | sh
```

Windows (PowerShell):

```powershell
iwr https://fly.io/install.ps1 -useb | iex
```

> **Confirm the Windows URL before relying on it.** Fly publishes a PowerShell
> installer, but the exact path above has not been checked against fly.io from
> this machine (no network access during the portability pass). If it 404s, take
> the current command from Fly's own install page rather than guessing.

Then, in every shell:

```
fly version
```

### 2. Sign in

```bash
fly auth signup       # or: fly auth login
```

Fly requires a credit card on file even for the free-ish tier. The default plan ("Hobby") fits this app.

### 3. Pick an app name and region

From the repository root (`cd` there however your shell spells it — the old
`cd ~/relational_fluency` here named a directory nothing in this repo creates,
and `~` is not a path in cmd.exe):

```
fly apps create rf-jennie     # replace rf-jennie with whatever you want; must be globally unique
```

Then edit `fly.toml`:
- Change `app = "CHANGE_ME"` to `app = "rf-jennie"` (matching what you just created)
- Optional: change `primary_region` to a region closer to you. List options with `fly platform regions`. Common picks: `ewr` (Newark), `sjc` (San Jose), `lhr` (London), `nrt` (Tokyo).

### 4. Create the persistent volume

```bash
fly volumes create rf_data --region ewr --size 1 --yes
```

(Use the same region as `primary_region` in `fly.toml`. `--size 1` = 1 GB; you can grow it later with `fly volumes extend`.)

### 5. Set the secrets

Generate a strong session key first — anyone with this string can use your deployed app:

```
python -c "import secrets; print(secrets.token_urlsafe(32))"
```

(`python`, not `python3` — on Windows there is no `python3.exe` from a
python.org install, only `python.exe` and the `py` launcher. Inside an
activated virtualenv `python` is the project interpreter everywhere. This same
command is the portable way to generate the AWS secret too, see
[`DEPLOY-AWS.md`](DEPLOY-AWS.md#2-set-the-secrets).)

Then set the secrets. `ANTHROPIC_API_KEY` here is your **LiteLLM virtual key** for
the Cornell gateway — the server sends it as the bearer token to the gateway, so a
raw `sk-ant-…` Anthropic key will fail preflight with a `401` and every encounter
will error (pages still load, but `/health` reports `gateway.ok=false`):

```bash
fly secrets set \
  ANTHROPIC_API_KEY="<your LiteLLM virtual key>" \
  SESSION_KEY="<paste the generated key here>"
```

Only override the gateway endpoint if you are **not** targeting the default
`https://api.ai.it.cornell.edu`; in that case also set the base URL (either name works —
`LLM_BASE_URL` takes precedence, then `ANTHROPIC_BASE_URL`):

```bash
fly secrets set LLM_BASE_URL="https://your-gateway.example.edu"
```

You can verify with `fly secrets list` (shows names, not values). No Deepgram or
ElevenLabs secrets are needed — those providers are retired.

**If this deployment will ever serve a `/start` arrival, it also needs
`UPSTREAM_CONSENT_VERSION`** — not a secret, but set the same way:

```bash
fly secrets set UPSTREAM_CONSENT_VERSION="cornell-irb-2026-09-v3"
```

It names the approved consent wording the Qualtrics survey is showing. Consent
is taken there now, so this platform cannot work out which text a participant
agreed to, and `server/storage.py` refuses to record any study consent until
this says. Unset, nothing looks broken and nothing is collected: `/health`
answers 200, `/start` assigns runs, `POST /api/consent` answers 503 naming the
variable (the build deployed on AWS today still answers 404 `no such
participant record` — the record exists, the message is wrong), and every voice
socket closes 4403. A `/test` run is unaffected, so your own walkthrough will
not show it. See
[`OPERATIONS.md`](OPERATIONS.md#the-consent-version-unset-this-records-nothing).

> Use **a different gateway virtual key than your personal one** for deployment — that way you can rotate the deployed key without breaking your local work, and you can see deployed traffic separately.

### 6. Deploy

```bash
fly deploy
```

This builds the image, pushes it, and rolls out one machine. First deploy takes ~3 minutes; subsequent deploys ~60 seconds.

Watch logs: `fly logs`

---

## Using it

Your URL is `https://<app-name>.fly.dev`.

> **`SESSION_KEY` never goes in a link you hand to someone being studied.** An
> earlier version of this page said "every URL must include `?key=<SESSION_KEY>`"
> and listed the links below as *participant* URLs. They are not. `SESSION_KEY`
> is the only credential in the system, and it is the one `check_key` demands
> for `/researcher`, `/director`, `GET /api/runs` (every participant key,
> Qualtrics response id and completion code), `GET /api/encounters`, and every
> `download/<file>` and `download.zip` (every microphone WAV, agent WAV,
> transcript and event log). Anyone who reads it out of their own address bar
> can download the whole dataset. See the same warning, and the reasoning behind
> it, in [`OPERATIONS.md`](OPERATIONS.md#the-participant-url-qualtrics--app--qualtrics).

**Operator links.** These reach key-gated routes, so they carry the key and are
for your own browser only — a bookmark, not something to send:

```
Researcher:            https://rf-jennie.fly.dev/researcher?key=YOUR_KEY
Director view:         https://rf-jennie.fly.dev/director?key=YOUR_KEY
Legacy v1 chat UI:     https://rf-jennie.fly.dev/?scenario=missed_deadlines&key=YOUR_KEY
```

**The link a person actually being studied opens.** `/v2` and `/start` go
through `check_participant`, which demands the key only when the deployment sets
`PARTICIPANT_KEY_REQUIRED`; leave that unset and the link needs no key at all:

```
One scenario:          https://rf-jennie.fly.dev/v2?scenario=S1B
Assigned four-encounter run: https://rf-jennie.fly.dev/start?pid=<their-key>&qid=<their-survey-response-id>
```

`&qid=` is not optional on a `/start` link that a real participant follows. It
carries the Qualtrics `ResponseID`, which since consent moved upstream is the
only evidence this platform has that anyone consented: without it the consent is
refused, the record stays unconsented and the voice socket closes 4403. The
arm-specific links (`/start/one-to-one`, `/start/group`) and the rater entrance
(`/rate/start?token=…`) take the same form; the canonical wording for all three,
as they go into Qualtrics, is in
[`OPERATIONS.md`](OPERATIONS.md#the-participant-url-qualtrics--app--qualtrics).

If you do set `PARTICIPANT_KEY_REQUIRED` — reasonable while the demo is not
meant to be open — understand that you are choosing to publish the dataset key
to whoever opens the link, and that this deployment is demo-only for exactly
that kind of reason (see the note at the top of this page). Unset it, or move to
the AWS path, before anyone whose data matters uses it.

## Webcam recordings here, with no AWS credentials

This path needs none, and that is deliberate rather than a gap. The participant
page asks this server for a presigned S3 URL, this server cannot sign one, it
answers `503`, and the page PUTs the recording to `PUT /api/sessions/{id}/video`
instead — so the bytes land on the volume as `sessions/<id>/webcam.webm` and the
rating console plays them back through `/api/rater/video/{assignment_id}` like
any other recording. Set no `AWS_*` secret on a Fly app: there is no study
bucket in this deployment's IRB scope, and an app that could write to the real
one would be a demo writing into the study's data.

The trail records which leg carried the bytes. A `video_uploaded` event with
`"via": "local"` is this path; the same event without it is a browser-direct
upload to S3, which only the AWS deployment produces. Everything else about the
event — `type`, `key`, `bytes`, `status` — is identical on both, so nothing
downstream has to know the difference. See
[`DEPLOY-AWS.md`](DEPLOY-AWS.md#webcam-recordings-and-the-study-bucket) for the
credentialed half.

A side effect worth knowing when you compare notes with someone testing against
AWS: because nothing here PUTs to a bucket from the browser, **this path has no
CORS surface at all**. The bucket's CORS allowlist — three origins, one of them
`http://localhost:8765` — only constrains the credentialed deployments, where a
missing origin loses every recording silently. That failure cannot happen here.

## Day-to-day operations

| Task | Command |
|---|---|
| Deploy a code change | `fly deploy` |
| Tail server logs | `fly logs` |
| SSH into the machine | `fly ssh console` |
| Browse the data volume | `fly ssh console -C "ls -lh /data/sessions"` |
| Pull a session's files locally | `fly ssh sftp shell` then `get -r /data/sessions/<sid> ./` |
| Rotate a secret | `fly secrets set SECRET_NAME="new value"` (auto-redeploys) |
| Scale memory up | `fly scale memory 1024` |
| Grow the volume | `fly volumes extend <vol_id> --size 5` |
| Stop the app | `fly scale count 0` |
| Start it again | `fly scale count 1` |

## Pulling the dataset for analysis

The download endpoints work over the public URL, gated by `SESSION_KEY`:

```bash
# List sessions
curl "https://rf-jennie.fly.dev/api/sessions?key=$KEY" | jq

# Download one session as a ZIP
curl -OJ "https://rf-jennie.fly.dev/api/sessions/<sid>/download.zip?key=$KEY"
```

Or for bulk pulls, SSH-sftp the whole `/data/sessions/` directory.

## Common gotchas

- **`fly deploy` builds an empty `data/` dir.** That's fine — the volume mount at `/data` shadows it.
- **First request after idle is slow** (~1.5s extra). That's the machine warming. Set `auto_stop_machines = "off"` (already done in `fly.toml`) to avoid hard suspends.
- **WebSocket connection drops after exactly 60 seconds of silence** = Fly's idle timeout. Voice conversations send audio continuously so this shouldn't trigger; if it does on text mode, we'll add a keepalive ping.
- **Browser mic access requires the full HTTPS URL.** `https://...` not `http://...`. Fly forces HTTPS so this should be automatic, but if a participant hits the bare `http://` they'll get a redirect and mic won't initialize.
- **`fly logs` doesn't show secrets.** If you suspect a key issue, `fly ssh console` then `echo $ANTHROPIC_API_KEY | head -c 20`.

## When to revisit this setup

- Moving to **real participant data collection** → revisit data residency (likely needs Cornell-controlled infrastructure for IRB).
- More than **~10 concurrent sessions** → scale up VM memory and possibly run multiple machines (changes assumptions about in-memory session registry; we'd need to add Redis).
- **Public demo / open signup** → add rate limiting and cost caps (set a per-key spend limit on your LiteLLM gateway virtual key).
