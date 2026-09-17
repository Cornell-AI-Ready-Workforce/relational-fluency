# Testing the study yourself, on your own machine

For a researcher who wants to walk the participant journey, open the consoles,
and satisfy themselves the platform works — without deploying anything and
without asking anyone.

Everything on this page was measured on a Windows laptop against a local
checkout. Where a claim is about what a participant sees, it is what a real
browser showed, not what the code reads like.

`docs/OPERATIONS.md` is the reference for running a *wave*. This page is the
reference for running *yourself* through it.

---

## The four things nothing else tells you

Read these before you start, because each one costs an afternoon if you find it
the hard way.

### 1. A participant entry link needs **both** `?pid=` and `&qid=`

```
http://127.0.0.1:8765/start/one-to-one?pid=selftest1            ← dead end
```

That link — the one you will type first, because it is the obvious one — gets
past the fiction gate and then stops forever on a blocking card:

> **We could not confirm your consent record.** The link that brought you here
> did not carry the reference we need to match you to the survey you just
> completed…

The refusal is **permanent**, and the card says so: pressing Try again will not
fix it, because the link is missing a piece it will still be missing however long
anyone waits. `qid` carries the Qualtrics `ResponseID`, which is the only
evidence this platform has that anyone consented; without a usable one,
`_why_consent_was_refused` returns `CONSENT_REFUSAL_NO_QID` for every
non-internal run.

**This is the check working, not a broken install.**

### 2. `&cohort=internal` is your own way in

```
http://127.0.0.1:8765/start/one-to-one?pid=selftest1&cohort=internal   ← works
http://127.0.0.1:8765/start/group?pid=selftest2&cohort=internal        ← works
```

On a server with no `SESSION_KEY` — which is every local checkout — appending
`&cohort=internal` does three things at once:

- it **satisfies the consent-provenance check with no `qid` at all**, so the
  link above runs end to end;
- it **skips the seven-minute encounter gate and the 180-second advance floor**,
  so a self-test takes three minutes instead of thirty;
- it **tags the run `cohort=internal`**, which every analysis filter and every
  study export drops.

That last one is the important one. It is the difference between a clean dataset
and one with your own walkthroughs sitting in it as participants. **Use it on
every link you type by hand.**

The alternative escape, if you want to exercise the real consent path, is
`&qid=R_anything` — anything that is not the literal unreplaced
`${e://Field/...}` placeholder. That produces a `cohort=study` run, so only do
it deliberately.

> On a **deployed** server, `cohort=` and `variant=` are ignored on a link that
> does not also carry the researcher key, and a `WARNING` naming the discarded
> parameter is logged. See
> [OPERATIONS → The URLs](OPERATIONS.md#the-urls), which also explains why that
> backstop is not a licence to put either on a participant link.

### 3. `/health` is the one-line proof that anything can be recorded

```bash
curl -s localhost:8765/health | python -m json.tool
```

```powershell
(Invoke-RestMethod http://127.0.0.1:8765/health) | ConvertTo-Json -Depth 4
```

- `status: ok`, `ready: true`, `config.missing_required_env: []` — consent will
  be recorded and conversations will be captured.
- `status: degraded`, `ready: false`, or anything in `missing_required_env` —
  **every participant will hit the blocking card and nothing will be recorded**,
  while the server keeps answering 200 and keeps handing out runs.

Check it immediately after start, every time. It is one request and it is the
difference between a wave and a lost wave.

**The boot warning is not enough on its own.** The line naming a missing
`UPSTREAM_CONSENT_VERSION` is printed a few seconds **after** uvicorn's
`Uvicorn running on http://127.0.0.1:8765` line — that is, after the line that
invites you to open your browser. It is easy to scroll past, and the symptom it
predicts looks like a participant-side problem rather than a configuration one.

### 4. On the configured model, the director's directions never arrive

`REALTIME_MODEL` is `nto.gemini-live-2.5-flash`. On that model a stage direction
sent **mid-encounter** is composed, logged, and discarded in transit: measured
across two 1:1 sessions and one group room, **5 sent, 0 received**, with no error
and no warning.

The conversation you will hear is fluent, in character, on topic — and
**unsteered**. Nothing in the audio or the transcript shows it. The only trace is
a `steer_unacked` line in the event log and `tools/encounter_health.py` marking
the encounter failed.

**Do not conclude from a good-sounding encounter that the steering works.**

What does still land is the connect-time brief: character identity, backstory,
personality, opening severity, and the planted beats that four rounds of work
moved into it. And the participant's own speech is fully recorded and
transcribed in both 1:1 and group — the dependent variable is intact.

The decision about whether to stay on this model is a PI and IRB decision,
because the consent form names the provider. It is written up separately, in the
`PI-DECISION-realtime-model.md` memo that accompanies this repository.

---

## Port 8765, and which surfaces actually care

**Use 8765.** The study bucket's CORS allowlist has exactly three origins, and
`http://127.0.0.1:8765` and `http://localhost:8765` are the only local ones.

On any other port the webcam upload is blocked, **and the page reports a
CORS-blocked PUT as a "network" error — which does not trigger the local
fallback.** The recording is lost with nothing naming the cause.

But a busy port is not a broken app. Only the participant webcam upload is
port-bound:

| Surface | Cares about the port? |
|---|---|
| Participant webcam upload | **Yes.** 8765 only. |
| Participant voice, transcript, consent, completion | No |
| Evidence trace `/evidence` | No |
| Researcher console `/researcher`, steering trail `/director` | No |
| Landing page `/`, demo view `/static/demo.html` | No |

So if 8765 is taken, you can still test everything except recording. Find what
has it:

```powershell
Get-NetTCPConnection -LocalPort 8765 -State Listen
```

---

## Your local tests write to the real S3 bucket

The AWS credentials in `.env` work, and the browser PUTs the webcam recording
**straight to the real study bucket** from your laptop:

```
s3://<study bucket>/encounters/<session id>/webcam.webm
```

`&cohort=internal` keeps the *run* out of the study set. It does **not** stop the
upload. Anyone testing should know their test recordings land beside real ones
and need sweeping up before a wave.

---

## Things a participant sees that will look like bugs

All measured in a real browser.

### A refresh, or any dropped connection, starts that conversation over

Refreshing mid-encounter reopens the situation card, opens a new socket, empties
the transcript and restarts the timer. Server-side the run then carries **two**
closed sessions for the same scenario — in one measured case 25.3 s / 4 turns
and 20.1 s / 3 turns — and `run.completed` stays empty.

That is the real cost of reconnecting, and the page is honest about it: the drop
card says the part will start again from the beginning and the other person will
greet you afresh. (It used to say "Nothing is lost on your side", which was
false.) The orphan fragments are not part of any run's completed encounters.

One residue remains: a recording lost because the server was unreachable is
filed as `video_upload: {state: "absent", attempts: 0}` — the same record a
session that never tried to upload gets. Counting encounters with no recording
cannot tell "we lost it" from "there was nothing to send".

### The fiction gate and the audio check are remembered per run

Both are keyed on the **run id** in `sessionStorage` (an earlier version of this
page said per tab, which was wrong). Returning to the same participant link in
the same tab resumes the same run and skips both; a different arm, a different
participant id, or a new tab mints a new run and asks again. Measured: accept
on `/start/one-to-one`, then open `/start/group` in the same tab → asked again.
Chosen deliberately; named here so walking them twice is not read as a fault.

### A microphone prompt that is ignored is now bounded, and a missing microphone is named

If the browser's permission prompt appears and is **ignored or dismissed** —
rather than Allowed or Blocked — the audio check used to sit on *"Listening…"*
with *Continue* disabled and no message, forever. It is now bounded at 15 s,
after which it says *"Still waiting for your permission"* with help. The same
hazard in the encounter room was already bounded (`getUserMediaBounded` falls
into the "we couldn't turn on your microphone" message with Start re-enabled).

A machine with **no microphone at all** used to be told the microphone was
*blocked* and sent to find a permission prompt that never appeared; *"Skip the
check"* then walked it into the room to be stopped again. It now says *"No
microphone found"*, with help that says what to plug in and that skipping will
not get past it. Everything except a live conversation works on such a machine
— the consoles and the demo replay lane.

An explicit **Block** is handled properly: *"Mic blocked"* with help text in the
check, and *"We couldn't turn on your microphone. Please allow microphone access
and try again."* in the room, with Start re-enabled.

### The character pauses for about eight seconds and then says the line again

Not a bug you have found, and the most important thing on this page to know
before you listen. The gateway (`nto.gemini-live-2.5-flash` through
`api.ai.it.cornell.edu`) sometimes drops a reply's voice — either mid-sentence
with the caption whole (about 1 reply in 9) or before it starts (before this
build: 47 s of dead air, with your own turns refused meanwhile). The bridge now
notices both and asks the gateway **once** more for the turn, after 8 s for a
stall or one second after a truncation, replacing the broken line on the page
rather than appending to it.

What you will hear: the first beat of a sentence, a short silence, then the
character starts again — usually from the same point, not always in the same
words. Measured on the final wave, 4 stalls in 145 replies, all one-to-one,
7.4–8.5 s of silence each. The retry is a text prompt into the model's context
(`(I didn't hear that - the audio dropped. Could you say it again?)`), written
on the `audio_retry` event; the character's line answers *it*, which is why it
sometimes comes back as "What was the question?" rather than the lost sentence.
That is a methods question the PI has to rule on before a wave, and it is in
the decision memo.

### A much longer silence, and then the character answers what you said

A *request* the gateway never answers at all is a different fault, and since
2026-09-14 it no longer waits out the 45 s watchdog. It is called unanswered at
`REQUEST_UNANSWERED_S` = 6 s and re-asked with **your own audio** — not the text
nudge, because a nudge in front of a lost line was measured to draw an answer to
the nudge rather than to you (a dropped *"Good morning."* came back as *"You
booked this meeting. What's on your mind."*). Unanswered again at
`REPLAY_UNANSWERED_S` = 4 s, the session is rebuilt and your line is replayed
into it, bounded at two rebuilds per encounter.

What you will hear is 9–22 s of quiet and then the character answering the thing
you actually said. The seven live recoveries of that day measured 9.2, 9.3,
11.8, 14.9, 15.3, 19.2 and 21.7 s — median 14.9 — about one per 90–120 s of
talking. **Do not report it** unless the quiet runs past about 30 s, or the
character comes back answering something you never said.

### A pause no longer ends your turn

A 700–1300 ms mid-thought pause used to close your turn twice over — the
runner's own 900 ms end-of-turn and the gateway's default detection under it —
so one sentence arrived as two transcripts, drew two replies, and the first was
cut off by the second half of your own sentence. That is what *"the agent jumps
in when I pause"*, the doubled caption bubbles and the empty agent turns all
were. Both ends now hold the turn open for **1500 ms**. Do report it if a pause
still splits a sentence.

**Wear a headset — a requirement, not comfort advice.** With loudspeakers the
character's own voice comes back through the microphone, the gateway
transcribes it *as you*, and the transcriber invents words on the bleed; it also
counts as you talking and withholds a recovery. The end-of-turn detector adapts
to steady room noise — a fan or a keyboard no longer cuts the character off
(false cut-offs 5 in 22 agent turns before, 0 in 19 after) — but a loudspeaker
is not steady noise.

### After a group handover the room can sit silent

Measured in S4B with a deliberately silent participant: the handover confirm and
banner were correct, but no character opened the new interaction for 20 s, and
the session record shows zero agent turns in that window. In the 1:1 handover the
next character opened within seconds. It may be the designed behaviour of a close
beat; it reads oddly against the situation card's promise that "the scene moves
on by itself".

### Captions in the demo fixture glue a repeated fragment on

> Dan: …The cleanest thing is I walk the client through it.**I got the deck done
> last night**

The same shape is in the stored transcript, so it was what the server wrote
rather than a rendering fault. The code that writes transcripts now repairs
these seams before storing them; the demo fixture was recorded before that and
is left byte-for-byte as it was, so you will still see them there and not in a
new recording.

### The last line of an encounter no longer stops mid-sentence

If you tested before this build, you will remember the closing line of every
encounter being cut off. The server closes the last turn while the browser is
still holding seconds of scheduled audio, and the page used to tear the audio
graph down on the spot. It now releases the camera and microphone immediately
and lets the audio it has already been handed finish, bounded at 12 s (the
longest single reply seen live was 6.5 s). In a group room, a reply that was
already being heard when the director moved the floor is likewise allowed to
finish rather than being cut mid-word.

---

## Before you show it to colleagues

### The demo view is `/demo`

`GET /demo` and `GET /static/demo.html` serve the same page; the short route was
added after a round in which the page existed but could only be reached by
guessing the static path.

It is the best thing to put in front of colleagues: it reads `/health` and states
what will and will not work *before* you present, and it offers a **replay** lane
that walks through already-recorded encounters with no model gateway, no S3 and
no microphone — so there is nothing in it that can fail while people are
watching.

### Three panels of the evidence trace are placeholders

Director latency, Connection/reconnects and Gemini resumptions under Session
Health, plus the per-turn latency pill and video-to-transcript sync, are
unimplemented and render *"pending instrumentation"*. They are labelled, but only
once you are looking at them.

The session list itself is fine: it is ordered newest-first by when the encounter
started, and each row carries a date as well as a time.

### There is no dark mode

No stylesheet on `/evidence`, `/researcher`, `/` or `demo.html` contains
a `prefers-color-scheme` rule. All four render light even with the operating
system set to dark. A choice, not an oversight — but one worth stating rather
than discovering in front of an audience.

---

## The end of the journey: where "Return to the survey" goes

After the fourth encounter the participant sees a completion code and a **Return
to the survey** button, which sends them to `SURVEY_RETURN_URL` with

```
?run=<run_id>&code=RF-XXXXXXXX&pid=<participant key>
```

appended. Two things follow from that, and both matter before a wave:

1. **The value must be a survey continuation link, not the Qualtrics host.** With
   `SURVEY_RETURN_URL=https://cornell.qualtrics.com` — which is what a local
   checkout has today — following that URL with redirects ends at
   `https://shibidp.cit.cornell.edu/idp/profile/SAML2/Redirect/SSO`, page title
   *"Cornell University Web Login"*. A CloudResearch participant has no Cornell
   NetID, so the study ends by showing them a credential prompt they cannot
   satisfy. Paste the survey's own end-of-survey continuation URL in instead.
2. **Whatever host you configure receives the run id, the completion code and
   the participant key** as query parameters. So it must be a URL it is
   acceptable to send those three things to.

See [OPERATIONS → Sending them back](OPERATIONS.md#sending-them-back).

---

## The contact sentence every participant reads

Until `config/consent.yaml`'s contact block is filled in, every completion,
withdrawal and consent-failure screen ends with, verbatim:

> …contact whoever sent you this study link, the consent form you were shown
> carries no contact details for the research team, **which is a fault on our
> side.**

It is true: `contact.pi_name`, `contact.email` and `contact.irb_protocol` are
still `[FILL IN: …]`, and the deletion right the consent form promises points at
a blank. Filling those three fields removes the sentence. The full list of what
must be filled before fielding is in
[OPERATIONS → the blanks in `config/consent.yaml`](OPERATIONS.md#before-fielding-the-blanks-in-configconsentyaml).
