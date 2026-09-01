# Deploying the relational-fluency platform to Fly.io

> **Demo path only (2026-08).** Study deployment targets **AWS ECS/Fargate**
> behind an ALB, with study data in encrypted S3 — see
> [`architecture.md`](architecture.md) and `infra/terraform/`. Fly is fine for
> quick demos and dogfooding, but participant data must not be collected here:
> the persistent volume below sits outside the IRB data-management plan.

A demo/dogfood deployment with HTTPS, persistent volume for the dataset, and
WebSocket support. Roughly 30 minutes end-to-end.

## What you'll have when you're done

- A URL like `https://rf-yourname.fly.dev` accessible from anywhere
- HTTPS (required for browser mic access)
- A 1 GB persistent volume at `/data` holding sessions and SQLite
- Access protected by a session key (anyone without it gets `401`)
- API keys stored as Fly secrets, not in the image

## Cost expectation

- Fly's `shared-cpu-1x@512mb` VM + 1 GB volume: roughly **$3–6/month** at idle, more if traffic ramps.
- **API costs are separate**: the live voice stack is Gemini Live (`nto.gemini-live-2.5-flash`) served through the Cornell LiteLLM gateway, so every voice turn is billed against your gateway virtual key. Track spend and set a per-key cap in the gateway/LiteLLM console rather than budgeting per external STT/TTS provider — the retired v1 Deepgram/ElevenLabs cascade is no longer used.

---

## One-time setup

### 1. Install the Fly CLI

```bash
brew install flyctl   # or: curl -L https://fly.io/install.sh | sh
fly version
```

### 2. Sign in

```bash
fly auth signup       # or: fly auth login
```

Fly requires a credit card on file even for the free-ish tier. The default plan ("Hobby") fits this app.

### 3. Pick an app name and region

```bash
cd ~/relational_fluency
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

```bash
python3 -c "import secrets; print(secrets.token_urlsafe(32))"
```

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

> Use **a different gateway virtual key than your personal one** for deployment — that way you can rotate the deployed key without breaking your local work, and you can see deployed traffic separately.

### 6. Deploy

```bash
fly deploy
```

This builds the image, pushes it, and rolls out one machine. First deploy takes ~3 minutes; subsequent deploys ~60 seconds.

Watch logs: `fly logs`

---

## Using it

Your URL is `https://<app-name>.fly.dev`. Every URL must include `?key=<SESSION_KEY>`:

```
Participant (voice):   https://rf-jennie.fly.dev/?mode=voice&scenario=missed_deadlines&key=YOUR_KEY
Participant (text):    https://rf-jennie.fly.dev/?scenario=missed_deadlines&key=YOUR_KEY
Researcher:            https://rf-jennie.fly.dev/researcher?key=YOUR_KEY
```

Bookmark the researcher URL; for collaborators, share the participant URL (you can pre-fill the scenario).

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
