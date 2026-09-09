# Phase 2 — human rating and reliability

Phase 1 records encounters. Phase 3 trains a scorer against them. Phase 2 is the
part in between, and it is the part that decides whether Phase 3 has anything to
learn from: **2–3 independent raters score every recorded encounter on the 22
ESCI Relationship Management items, and inter-rater reliability is computed per
construct before anything is modelled.**

> **⚠️ The items are a proprietary instrument.** The 22 statements are ESCI items
> (Boyatzis, Goleman & Korn Ferry), reproduced in this repository for research
> reference only. **Licensing / permission for this use has not been confirmed
> in writing.** They are shown to raters by the console, exported by
> `GET /api/ratings`, and carried in
> [`studies/study1/qualtrics/esci_items.csv`](../studies/study1/qualtrics/esci_items.csv)
> — the notice travels with them in all three places and must not be removed.
> Confirm the licence before fielding this to anyone outside the study team.
> See [Open questions](#open-questions).

The instrument itself — the scale, the rater task, the scenario × focal-item map,
the administration notes — is
[`studies/study1/qualtrics/rating-instrument.md`](../studies/study1/qualtrics/rating-instrument.md).
This document is the operational guide: how to run it.

---

## What was decided, and why

Four choices are baked into the code. They are recorded here because each one
closed a question the earlier documents left open.

**1. The rateable artefact is the webcam video, with the transcript beside it.**
The roadmap says raters watch video; the rating instrument says raters read
transcripts. Video wins, because it is the only artefact that plays back as a
conversation: the participant page mixes the microphone and the agent audio into
one webcam recording, while `user_audio.wav` and `assistant_audio_*.wav` are
separate channels that cannot be listened to as a dialogue without work nobody
has done. The transcript is shown alongside for search and for the moments the
audio is unclear. Where an encounter has no video — the upload does fail
occasionally — the console says so and the rater works from the transcript,
which is the old plan as the degraded case rather than the design.

**2. Raters use a console this platform serves, not Qualtrics.** Qualtrics can
present the items, but this platform cannot get the answers back out of it: that
would need a survey, a per-encounter loop-and-merge, an embedded-data round trip
and an API pull that do not exist. So `/rate` is the working path. The Qualtrics
route stays open and supported — the item bank still exports in importable form,
and `ratings.import_qualtrics(rows, mapping)` ingests an export — but it is the
fallback, not the route of record. (Which of the two is the route of record for
the published study is a decision for the researchers; see
[Open questions](#open-questions).)

**3. Raters never hold the session key.** `SESSION_KEY` reaches every encounter,
every participant record and every steering log in the study. A rater gets a
scoped token instead — `rt_` plus 32 hex characters — that reaches their own
assignments, their own packets, the media for those packets, and their own
submissions. Nothing else. Requesting an assignment that belongs to another
rater answers **404, not 403**: a rater must not be able to probe for which
encounters exist.

**4. Packets are blinded.** A rater sees what the participant saw — the
situation, the counterparts, the video, the transcript — and nothing that would
tell them what the encounter was designed to elicit. Specifically **not**: the
participant key or record id, the stage directions, the planted triggers, the
ESCI tags on those triggers, the actor briefs, the scenario's construct or
variant label, or the offline judge's score. An encounter is identified to a
rater only by its **rating code** (`RC-` plus ten characters, HMAC-derived from
the session id and stable across runs), so two raters can talk about the same
encounter without either of them being able to reach it in the study data.

---

## The pieces

| File | What it is |
|---|---|
| `server/esci.py` | the 22 items, the 1–5 scale, N/A as `None`, reverse scoring, validation |
| `server/raters.py` | raters, scoped tokens, assignment of encounters to raters |
| `server/ratings.py` | submitted ratings, export, Qualtrics ingest |
| `server/rater_packet.py` | the rating code, and the blinded packet a rater is served |
| `server/reliability.py` | ICC, quadratic weighted κ, Krippendorff's α, and the report |
| `server/video.py` | `playback_url()` — the presigned GET the console plays |
| `static/rater.html` | the console, served at `/rate` |
| `studies/study1/qualtrics/esci_items.csv` | the canonical machine-readable bank, with the ids the code uses |

All statistics are pure Python. `requirements.txt` carries no numpy and no
scipy, and adding one for a 22 × 27 matrix would not be a trade worth making.

---

## Running it

Set these once per shell. `$KEY` is the researcher session key — the same one
`/researcher` uses. Everything in this section needs it; nothing a rater does
does.

bash / zsh (macOS, Linux, Git Bash):

```bash
export RF=http://127.0.0.1:8765          # or the deployed host
export KEY=...                            # SESSION_KEY
```

Windows PowerShell:

```powershell
$RF = "http://127.0.0.1:8765"            # or the deployed host
$KEY = "..."                              # SESSION_KEY
```

**Windows note, once, for this whole section.** The `curl` blocks below are
bash. In Windows PowerShell `curl` is an alias for `Invoke-WebRequest` and
rejects `-s`, and in cmd.exe the single-quoted JSON bodies are passed through
*literally* — apostrophes and all — so the server rejects them as malformed
JSON and the error looks like a bug in the API rather than in the shell. Phase 2
setup is where a coordinator on Windows would otherwise stall, so each of the
three POST commands below carries a PowerShell form beside the bash one. There
are only three; the read-only `curl -s` lines just need `curl.exe`.

### 1. Register the raters

```bash
curl -s -X POST "$RF/api/raters?key=$KEY" \
  -H 'content-type: application/json' \
  -d '{"name":"R. Okonkwo","kind":"trained","email":"ro99@example.edu"}'
```

```powershell
$body = @{ name = "R. Okonkwo"; kind = "trained"; email = "ro99@example.edu" } | ConvertTo-Json
Invoke-RestMethod -Method Post -Uri "$RF/api/raters?key=$KEY" -ContentType application/json -Body $body
```

`kind` is one of `crowd`, `trained`, `expert`. It is recorded because
reliability is expected to differ between them and the report breaks down by
rater; it does not change what a rater can do. `email` is optional and is only
somewhere to keep the address you send the link to — the platform sends no mail.

```bash
curl -s "$RF/api/raters?key=$KEY"        # everyone registered
```

### 2. Issue each rater a token

```bash
curl -s -X POST "$RF/api/raters/rtr_ab12cd34/token?key=$KEY" \
  -H 'content-type: application/json' -d '{"days":30}'
# {"token":"rt_9f2c…"}
```

```powershell
$body = @{ days = 30 } | ConvertTo-Json
Invoke-RestMethod -Method Post -Uri "$RF/api/raters/rtr_ab12cd34/token?key=$KEY" -ContentType application/json -Body $body
```

**The token is shown once.** Only a hash is stored, so it cannot be recovered —
if a rater loses their link, issue a new token and revoke the old one. Send them:

```
$RF/rate?token=rt_9f2c…
```

That URL is the rater's whole world. It is also a bearer credential in a query
string, which means it lands in browser history and in any proxy log between
them and the server, so: one token per rater, a short expiry, and revoke on the
day a rater finishes rather than at the end of the study.

### 3. Assign encounters

```bash
curl -s -X POST "$RF/api/rater-assignments?key=$KEY" \
  -H 'content-type: application/json' \
  -d '{"cohort":"study","rater_ids":["rtr_ab12cd34","rtr_ef56ab78","rtr_1234abcd"],
       "per_encounter":3,"seed":20260401}'
```

```powershell
$body = @{
  cohort        = "study"
  rater_ids     = @("rtr_ab12cd34", "rtr_ef56ab78", "rtr_1234abcd")
  per_encounter = 3
  seed          = 20260401
} | ConvertTo-Json
Invoke-RestMethod -Method Post -Uri "$RF/api/rater-assignments?key=$KEY" -ContentType application/json -Body $body
```

Either `cohort` (every encounter in that cohort) or an explicit `session_ids`
list. `per_encounter` is how many independent raters each encounter gets — the
design says k ≥ 3, and reliability with two raters is a much weaker claim.
`seed` makes the allocation reproducible, which matters when you have to explain
in a paper how encounters were distributed.

Two things to know before you run it:

- **`cohort:"study"` is doing real work.** Internal test encounters carry
  `cohort:"internal"` and must not reach a rater — they are not participants and
  they would enter the reliability report as if they were. Check the count in
  the response against the wave you expect.
- **Assignments are the unit of work and the unit of payment.** A rater's queue
  length is what you agreed to pay for. Assigning in batches you can pay for,
  rather than the whole wave at once, is the difference between a rater who
  finishes and one who abandons twenty half-rated encounters.

```bash
curl -s "$RF/api/rater-assignments?key=$KEY"     # everything assigned, with status
```

### 4. Watch it come in

```bash
curl -s "$RF/api/ratings?cohort=study&key=$KEY" | python -m json.tool | head -40
```

Every submitted rating, with its scores (N/A as `null`), the two open-ended
answers, the seconds the rater spent, and the assignment and rating code. This
is the export: it is what goes to the analysts, and it carries the item
statements, so **it carries the licensing notice with it**.

### 5. Read the reliability report

```bash
curl -s "$RF/api/reliability?cohort=study&key=$KEY"
python -m server.reliability --cohort study --json      # same thing, offline
python -m server.reliability --cohort study             # human-readable
```

---

## What a rater actually sees

Open `$RF/rate?token=rt_…` and the console shows:

1. **Their queue.** One card per assignment, by rating code, marked *To rate* or
   *Rated*, with progress in the top bar (`3 of 9 rated`). No encounter that is
   not theirs is visible or reachable.
2. **One encounter.** Left: the situation the participant was given, the
   counterparts by name and role, the video, and the transcript. Clicking a
   transcript line jumps the video to roughly that moment — roughly, because the
   transcript clock comes from the conversation log rather than from the video
   file, and the console says so rather than implying a precision it does not
   have. Right: the licensing notice, the rater task, the scale, and the items.
3. **The items**, in the order the server served them (randomised within group,
   group order randomised across raters — see the instrument's administration
   notes; the console renders whatever order it is given and does not shuffle
   again). Reverse-scored items are shown unmarked, exactly like the rest,
   because they double as straight-lining checks.
4. **N/A on every item**, styled apart from the 1–5 buttons and never
   pre-selected. Nothing is pre-selected: a rater who answers nothing submits
   nothing.
5. **Two open-ended boxes** — what the participant could have done better, and
   anything notable.

The console is built to make a careful rating easy and a careless one awkward:

| Behaviour | What happens |
|---|---|
| Submit with items unanswered | Refused, with the count and the numbers of the missing items, the missing rows flagged, and focus moved to the first one |
| Every item given the same answer | Warned once — reverse-worded items make a uniform column unlikely — and submitted only on a second, explicit click |
| Submitting in under 2 minutes, or without ever playing the video | Same one-warning-then-allow treatment (`MIN_SECONDS_BEFORE_SUBMIT` at the top of `static/rater.html`) |
| Closing the tab mid-rating | Browser warns about unsaved work; answers are also kept in that browser's local storage and restored on reopen, then cleared on submit |
| Double-clicking Submit | The second click does nothing: the button locks on the first and the page never re-enables it after the server accepts. A `409` from the server is treated as success, not as a retry |
| No video on the encounter | Says so, tells the rater to work from the transcript, and expects more N/A |

Keyboard: tab between items, arrows within an item, or press `1`–`5` to score
and `0` / `n` for N/A — which then jumps to the next unanswered item. It fits a
laptop window; below about 1100px the two columns stack.

## What a rater plays

**Recordings do not all arrive in the same container, and nothing upstream
normalises them.** The participant page (`static/v2.html`) picks the first
`MediaRecorder` type the participant's browser admits, in this order:

| Order | Type tried | Who takes it |
|---|---|---|
| 1 | `video/webm;codecs=vp8,opus` | Chrome, Firefox |
| 2 | `video/webm` | Chrome, Firefox (older builds) |
| 3 | `video/mp4;codecs=h264,aac` | Safari |
| 4 | `video/mp4` | Safari (fallback) |

So a wave produces **WebM/VP8+Opus** files from Chrome and Firefox
participants and **MP4/H.264** files from Safari participants, because Safari
implements `MediaRecorder` but supports only MP4/H.264. Both are stored as-is
and both are handed to raters.

What this means operationally:

- **The rater's own browser has to play both.** Chrome and Firefox play WebM
  and MP4. Safari's WebM support is the uncertain one: it has been partial and
  version-dependent, and this has **not been tested on real hardware for the
  Safari versions raters will actually use** — treat it as unverified rather
  than as either a yes or a no. The safe operational rule needs no such test:
  **ask raters to use Chrome or Firefox.** Both play every file a wave can
  produce, so the question never arises. If a rater reports "some videos won't
  play" — especially if the ones that fail are most of them — ask which browser
  they are using before looking for a bug in the console or a failed upload.
  (This is a constraint on *raters*, who are recruited and instructed by the
  study team. It is not a constraint on participants, who are the public and
  must be able to use any of the three.)
- **"No video on the encounter" has two distinct causes** that look identical in
  the console: the upload genuinely failed, or the file is there and the rater's
  browser will not decode it. The console's message covers the first; the second
  is a browser question. Check whether *other* raters can play the same rating
  code before treating an encounter as video-less.
- **A participant whose browser admitted none of the four types was never
  recorded at all.** The participant page says so in their transcript rather
  than letting the camera preview imply otherwise, but there is no
  researcher-side alert — the encounter simply arrives with no video.

The supported participant matrix, and why Safari is a first-class target rather
than an afterthought, is in the [README](../README.md#browsers).

---

## Reading the reliability report

`report()` returns, per construct and per item, the reliability statistics with
the n and the rater count they were computed from. Read the n first: an ICC over
six encounters is not evidence of anything, and the report gives you the number
precisely so a thin cell is visible as thin rather than as a bad result.

**ICC(2,1) and ICC(2,k)** — two-way random effects, absolute agreement.
Two-way random because the raters in the study are a sample of possible raters,
not the population of interest; absolute agreement rather than consistency
because a rater who is systematically two points generous is a problem, not a
calibration offset to be quietly removed. ICC(2,1) is the reliability of a
single rater — the number to quote when asking "could one rater do this job?".
ICC(2,k) is the reliability of the mean of k raters, which is what the analysis
actually uses, and it is always the higher of the two. Quote both, and say which
is which.

**Quadratic weighted κ** — pairwise, for the ordinal 1–5 scale, weighting a
1-vs-5 disagreement far more heavily than a 3-vs-4. This is the per-pair
diagnostic: an ICC that is fine overall can hide one rater disagreeing with
everyone.

**Krippendorff's α (ordinal)** — the robustness check, and the one statistic
here that handles missing cells natively, which matters because N/A is expected
and is stored as null rather than imputed.

Conventional bands, for orientation only:

| | Poor | Fair / moderate | Good / substantial | Excellent |
|---|---|---|---|---|
| ICC (Cicchetti 1994) | < .40 | .40–.59 | .60–.74 | ≥ .75 |
| ICC (Koo & Li 2016) | < .50 | .50–.75 | .75–.90 | > .90 |
| Weighted κ (Landis & Koch 1977) | < .20 | .41–.60 | .61–.80 | > .80 |
| Krippendorff's α | — | ≥ .667 tentative | ≥ .800 | — |

**These are conventions, not the study's thresholds.** Which convention the
study pre-registers, and what happens to a construct that lands below it, is a
research decision that has not been made — see below. What the code will not do
is pick one for you and quietly drop the encounters that fail it.

Two things the report will show and that are not defects:

- **Items 3 and 49** (support from key people, behind-the-scenes support) draw
  heavy N/A in the dyadic S2 scenarios — the instrument predicts this. A cell
  with a high N/A rate is a finding about the scenario, not about the raters.
- **Non-target items** are rated on every encounter by design. They are supposed
  to show lower means and different variance; the MTMM structure is what
  discriminant validity is computed from.

---

## The Qualtrics route

Still supported, still second choice.

- `studies/study1/qualtrics/esci_items.csv` is the importable bank —
  `item_id`, `item_no`, `construct`, `item_text`, `reverse_scored`, the scale
  bounds, whether N/A is offered, and the licensing notice on every row (a
  repeated column, so the warning survives a load into Qualtrics, a spreadsheet,
  or a dataframe — which a header comment would not).
  `esci_construct4_items.csv` is the older four-column source file and remains
  what `server/esci.py` loads at import; `esci_items.csv` is the export, and the
  two are checked against each other in `tests/test_rating_console.py`.
- `ratings.import_qualtrics(rows, mapping=None)` ingests an export. `mapping`
  maps Qualtrics column names onto item ids where the survey did not use them.
- A Qualtrics rating still needs a rating code to attach to. Present the code
  (not the session id) in the survey and carry it back in the export, or the
  ratings cannot be joined to encounters without exposing session ids to raters.

---

## Open questions

These belong to the researchers, not to the code, and each one is currently
unanswered in writing.

1. **Licensing.** The ESCI items are proprietary. Nothing in this repository
   establishes permission to show them to paid raters, and "reproduced for
   research reference" is not a licence. Resolve before fielding.
2. **Where raters come from**, and whether crowd raters are used at all for a
   construct-level judgement that the instrument was not validated for at the
   single-conversation level.
3. **Payment and pacing** — per encounter or per hour, and what a fair rate is
   for an eight-minute video plus 22 items plus two written answers. This sets
   the batch size in step 3 above.
4. **Calibration and the entry threshold.** The instrument calls for two
   practice transcripts and agreement with gold ratings before a rater enters the
   pool. Who produces the gold ratings, what agreement level admits a rater, and
   whether a rater who drifts is re-calibrated or dropped.
5. **The reliability threshold**, which convention it comes from, and the
   consequence of failing it — more raters, rater replacement, or a construct
   reported as unreliable.
6. **Attention checks.** The instrument specifies two instructed-response items
   and a comprehension check per session. The console does not implement them:
   an instructed-response item inserted into a 22-item proprietary block changes
   what the block is, and where it goes is an instrument decision.
7. **Whether the console or Qualtrics is the route of record** for the published
   study, and whether both being available is a strength or a two-source
   reconciliation problem at analysis time.
