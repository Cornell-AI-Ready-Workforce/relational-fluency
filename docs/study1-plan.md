# Study 1 plan — collect the video / audio / transcript dataset

Working plan for the first data-collection study. This file is the source the
GitHub issues are cut from; when an issue changes scope, change it here too.

Decisions recorded 2026-09-17 (JL):

| Topic | Decision |
|---|---|
| Group rooms | S3 and S4 run as authored (group room). JL's Sep 8–15 room fixes are the reference behaviour; verify they survive the audit merge, fix what does not, update the docs that say rooms do not run. |
| Forms | Study 1 fields **variant A only** (S1A S2A S3A S4A). Encounter **order** is randomized per participant. B forms are reserved for a later study; **C forms are removed**. |
| Duration | **7 minutes** per encounter minimum, enforced both ways: a visible timer + End button that unlocks at 7:00 (soft), and a server-side floor that will not complete an encounter earlier (hard). Withdrawal stays possible at any time. |
| Video | Webcam video is **required** and stored (S3). The pre-merge capture path (`53bc440`) is the reference behaviour. |
| Return to Qualtrics | **Two-survey chain**: Survey 1 (consent + self-report) → app → Survey 2 (remaining questions) via `SURVEY_RETURN_URL`, with `participantId`, `run_id`, `code` as embedded data. No `window.close()`. |
| Scope | Everything not on this path is carved out — see §6. |

Open items are in §8. Nothing in §8 blocks the carve-out or the chain work.

---

## 1. Participant flow

```
CloudResearch Connect
  │  participantId on the survey URL
  ▼
Qualtrics — Survey 1              embedded: participantId, ResponseID
  consent (IRB-approved text, version X)
  self-report battery
  last page: link → app (opens new tab)
  │  /start?participantId=${e://Field/participantId}&qid=${e://Field/ResponseID}
  ▼
App — one run, four encounters     variant A, order shuffled, cohort=study
  entry check page → Continue
  camera + mic check
  encounter 1..4  (S1A S2A S3A S4A in the run's order; ≥ 7:00 each; timer shown)
    voice both ways, live captions, webcam → S3, audio + transcript + events → volume
  completion screen: code shown, then
  │  SURVEY_RETURN_URL?participantId=…&run_id=…&code=…
  ▼
Qualtrics — Survey 2              embedded: participantId, run_id, code
  remaining questions
  end-of-survey redirect → CloudResearch completion
```

Data joins: Survey 1 ↔ run on `qid` (ResponseID) and `participantId`;
Survey 2 ↔ run on `run_id`/`code`; encounters ↔ run on the run document.
`python -m server.qualtrics join` does this for one survey today and needs the
second (E1.6).

Encounter shapes (from `scenarios/v3/*A*.yaml`):

| | Construct | Interactions | Cast |
|---|---|---|---|
| S1A | Conflict management | 1:1 → 1:1 | 2 |
| S2A | Influence | 1:1 → 1:1 | 1 |
| S3A | Inspirational leadership | group → series of 1:1 | 3 |
| S4A | Teamwork | group → group | 3 |

---

## 2. Epics and issues

Sizes: S = under a day, M = 1–3 days, L = a week or more.
Team: JL (@Jinsook-Jennie-Lee) and Tanvi (@tmavani23) are the coding contacts;
Tanvi manages the backend and overall app. Andrew (@andrewc8) contributes study
design and ideas, not code. Issues are created **unassigned**; owners are set
afterwards by JL. Nothing is assigned to @Ben-K-Jordan for now.
"AC" = acceptance criteria.

### E1 — Recruitment and the Qualtrics chain

**1.1 Survey 1: consent + self-report, hands off to the app** — M · Qualtrics
- Embedded data `participantId` from the Connect URL; `ResponseID` piped.
- Consent block is the IRB-approved text; its version string is what
  `UPSTREAM_CONSENT_VERSION` will name.
- Screener/attention items for headset, webcam, desktop browser.
- Final page: app link with `participantId` and `qid`, opens in a new tab;
  survey set to record partial responses so an abandoned Survey 1 is still a row.
- AC: a test response reaches `/start` with both parameters and creates a run
  in cohort `study`; the response row carries `participantId`.

**1.2 Survey 2: post-encounter questions, completion** — M · Qualtrics
- Anonymous link; embedded data `participantId`, `run_id`, `code` read from the URL.
- Remaining self-report items; end-of-survey redirect to the CloudResearch
  completion URL.
- AC: arriving from the app pre-fills the three fields; completing the survey
  marks the Connect assignment complete.

**1.3 App: hand-off to Survey 2** — S · app — note: the page appends `run`, `code`, `pid` (not `run_id`/`participantId`); Survey 2's embedded-data fields must use those names, or change `static/v2.html`. Also: `QUALTRICS_SURVEY_ID` in `.env` still points at the survey named "[Don't USE] … Aug 2026"; a newer "AIW - Relational Fluency - Connect Study 1" (SV_bClj80jCRO4Dmdw, 2026-09-15) exists and is inactive.
- `SURVEY_RETURN_URL` = Survey 2 anonymous link (Terraform var + `.env`).
- Completion screen shows the code, then auto-continues after ~10 s (button stays).
- Confirm the query keys the app appends match the embedded-data names in 1.2.
- AC: finishing four encounters lands on Survey 2 with fields populated;
  withdrawing mid-run lands there too with the partial code.

**1.4 CloudResearch Connect project** — S · research ops
- Eligibility (desktop, webcam, headset, English), payment, time estimate
  (4 × 7–12 min + surveys), completion handling via Survey 2 redirect.
- AC: dry run with an internal account end to end.

**1.5 Remove consent from the app** — S · app (carve-out K) — ✅ done 2026-09-17: no consent routes, gate, config or version variable; `qid` stays as the survey join key; withdrawal unchanged
- Set `UPSTREAM_CONSENT_VERSION` to the Survey 1 consent version; keep the gate
  that refuses to record without it and without `qid`.
- Remove `config/consent.yaml`, `server/consent_check.py`, the in-app consent
  page and `/api/consent` text; keep whatever records "consented upstream" on the
  participant record.
- AC: a run created without `qid` records nothing; with it, records everything;
  no consent text is served by the app.

**1.6 Join script for two surveys** — S · analysis ✅ 2026-09-17: set `QUALTRICS_SURVEY2_ID`; `python -m server.qualtrics join` joins Survey 1 → run → Survey 2 (on `run`, then `code`) and lists Survey 2 orphans.
- `server/qualtrics.py` takes two survey ids and produces one table:
  participant, Survey 1 response, run (order, forms, completion), four session
  ids, Survey 2 response.
- AC: pilot data joins with zero unattributed runs.

### E2 — Scenarios: eight forms, four fielded

**2.1 Choose the base text for each of the 8 A/B forms** — M · JL + scenario team
- Pre-merge forms were ~130 lines each; the audit branch expanded each to
  550–830 lines (372 of S1A's 725 are measurement notes; content the model
  reads grew 115 → 338 lines).
- Decision: keep the audit structure (2nd-person cues, per-family
  `realtime_voice` casting map, the two behavioural repairs). Move the style
  rules that are repeated in every brief into one shared block in the compiler
  so each `system_prompt` is roughly half its current length. Trim each file's
  comment header to ~10 lines (what the form measures, what was repaired, date).
- Do S1A first as the template; review with JL; then the other seven.
- AC: S1A's two briefs ≤ ~55 lines each with behaviour unchanged on three
  playthroughs; shared rules appear once in the rendered prompt (no duplicate
  sentence-length or acknowledgement rules); header ≤ 10 lines.

**2.2–2.5 Refine S1A, S2A, S3A, S4A** — one issue each, M
- Criteria to write into each: opening lands in the first 20 s; every planted
  beat fires inside 7–12 minutes; the character holds the scene to the floor
  (E4); no narration, no meta-talk, English regardless of what was heard.
- Three internal playthroughs per form on the production model, with the
  transcript reviewed against the ESCI item map.
- AC: three clean playthroughs; duration inside 7–12; reviewer sign-off.

**2.6 Park the B forms** — S
- Leave S1B–S4B in the bank; `DEFAULT_RUN_VARIANT=A` is the study default;
  refuse `variant=B` on `study` cohort links. Note in the file headers that B is
  reserved for a later study.

**2.7 Remove the C forms** — M · app — ✅ done 2026-09-17 (`study1/carve-out`)
- Delete `S1C S2C S3C S4C`; update `EXPECTED_V3` in `.github/workflows/ci.yml`,
  references in `server/runs.py`, `server/scenarios_v3.py`,
  `server/realtime_voice_session.py`, `server/voice/realtime.py`; drop
  `tests/test_s1c.py test_s2c.py test_s3c.py test_s4c.py test_reserve_draw.py`
  and the C cases in `test_s1_parity.py test_s3_parity.py test_round7_parity.py`;
  regenerate `docs/scenario-map.md`; fix README/OPERATIONS/competency-framework
  mentions.
- AC: `python -m pytest` green; CI's scenario job expects the eight.

**2.8 Encounter order** — S · app + JL — ✅ done 2026-09-17 (`WILLIAMS_4` in `server/runs.py`, `order` on the run document, tests in `tests/test_order_counterbalance.py`)
- Decision (2026-09-17): **balanced 4×4 Latin square (Williams design)** —
  each construct appears in each position equally often and each construct
  follows each other construct equally often; participants are assigned rows
  in rotation. Replaces the seeded shuffle in `runs.create`. Record the row
  index and scheme name on the run document.
- Note for the analysis plan: A-only pins S1A on every run, which switches off
  the S1A-beside-S4 exclusion (`FORM_EXCLUSIONS`). Both are about misattributed
  credit; state the overlap in the analysis plan rather than in code.
- AC: scheme documented in this file and in `docs/OPERATIONS.md`.

### E3 — Group rooms (S3, S4)

**3.1 Live verification of rooms on the production model** — M · JL ✅ 2026-09-17
- Reference: behaviour at `53bc440` (captions survive, no duplicate captions,
  no parroted context notes, no consecutive unnamed turns for one character,
  held replies play when the floor moves).
- Run ≥ 5 S4A and ≥ 5 S3A encounters on `HEAD`; count empty turns, unfloored
  replies, missing captions, mid-reply cut-offs. Compare against the same runs
  on a `53bc440` checkout.
- AC: a short table of the counts for both builds, committed to
  `docs/rooms-verification.md`. **Done** — driven with a scripted participant
  on `nto.gemini-live-2.5-flash-native-audio` (the model the study runs; the
  default moved to it the same day). See the page for the counts and the
  caveats.

**3.2 Fix room regressions found in 3.1** — L (unknown until 3.1) · JL — partly done 2026-09-17: parroted context notes in captions fixed (PR 18). Open: a room does not rebuild after the gateway drops every socket; second-speaker latency of 3–5 s; ~1 uncaptioned turn per room. See `docs/rooms-verification.md`.
- Likely suspects from the audit layer: `_direct_member` "sends nothing" on the
  Gemini family; the 1500 ms `server_vad` window; retry/replay paths in a room.
- AC: `HEAD` matches or beats `53bc440` on the 3.1 counts.

**3.3 Room docs** — S ✅ 2026-09-17 (README, scenario-spec, rooms-verification page)
- Remove "group rooms are not runnable live on this gateway" and "demo and
  field 1:1 only" from README, `docs/OPERATIONS.md`, `docs/migration-plan.md`;
  describe the room architecture as it actually runs.
- AC: no doc contradicts the 3.1 table.

### E4 — Seven-minute floor and timer

**4.1 Server-side encounter floor** — M · app — ✅ done 2026-09-17 (`storage.encounter_timing`, runner `_hold_at_floor`, 409 on `/advance`; `tests/test_encounter_floor.py`)
- `ENCOUNTER_MIN_SECONDS=420`. An encounter may not emit `encounter_complete`
  or be advanced before 420 s from its first participant turn; the actor's
  `end_conversation` and the per-interaction auto-advance (`INTERACTION_MIN_*`)
  are bounded by it (e.g. interaction floors derived so the last interaction
  is still open at 7:00). Withdrawal (`/api/run/{id}/withdraw`) is never gated.
- AC: an encounter driven to finish at 4:00 is held open and completes at
  ≥ 7:00; `advance` before the floor is refused with a reason; withdrawal at
  any time works.

**4.2 Client timer and End button** — S · app — ✅ done 2026-09-17 (End held until the floor with a reason; `Stop and leave the study` never held; clock served on the run as `timing`)
- `v2.html` already shows the elapsed timer with a ring that fills at 7:00 and
  copy "about N more minutes". Change: **End conversation** is disabled until
  7:00 for the *advance* path, with a separate always-available **Stop the
  study** (withdraw) control so the consent promise holds.
- AC: before 7:00 the End control is visibly locked with a reason; Stop works;
  after 7:00 End advances.

**4.3 Ceiling** — S · app (confirmed 2026-09-17: 12 min wrap, 13 min hard stop) — ✅ done 2026-09-17 (`ceiling_wrap` / `ceiling_reached` events; the page also stops on its own clock). Note: on the configured Gemini family a mid-session wrap direction does not reach the actor, so the record says the wrap was *called*; the hard stop is what guarantees the ceiling.
- At 12:00 the actor is directed to close the scene within two turns; at 13:00
  the encounter completes regardless. Record both on the events trail.
- AC: no encounter in the pilot exceeds 13:00.

**4.4 Duration report** — S · analysis ✅ 2026-09-17: `tools/encounter_health.py` prints duration, turns, floor/wrap/ceiling marks per encounter.
- `tools/encounter_health.py` prints per-encounter duration, turns, floor and
  ceiling events; used in the pilot review.

### E5 — Capture and storage: video, audio, transcript

**5.1 Verify webcam capture end to end on `HEAD`** — M · JL + app
- The audit rewrote `server/video.py` (presign now optional, fallback PUT
  through the app, range streaming). Reference is the pre-merge browser →
  presigned S3 PUT which JL verified working.
- Test on Chrome, Firefox, Safari: recording starts with the first encounter,
  uploads on completion, object appears under `encounters/<session>/` in the
  study bucket, `video-uploaded` receipt recorded. Decide whether the local
  fallback stays (it puts video on the ephemeral disk).
- AC: 3 browsers × 1 run each; every encounter has an S3 object and a receipt.

**5.2 Persistent storage for audio, transcript and events** — L · infra
- Records today live on the task's ephemeral disk; a deploy loses them. Choose
  EFS mount at `/data` (Terraform exists on the audit branch) or per-encounter
  archive to S3 at completion. Prefer both: EFS for durability during the wave,
  S3 archive as the analysis copy.
- AC: deploy during a test encounter; the encounter is still complete afterwards.

**5.3 Record completeness check per wave** — S · analysis
- `server/verify_record.py` run over a wave: participant audio present, both
  transcript sides, video object present, four encounters per run.
- AC: pilot wave passes; failures are listed by session id.

**5.4 Data pull runbook** — S · docs
- One page: pull encounters, pull runs, pull Qualtrics, join. Replace the
  scattered sections in `docs/OPERATIONS.md`.

### E6 — Carve-out (accepted 2026-09-17; done 2026-09-17 on branch `study1/carve-out`)

Status: A, B, C, E, F, G, H, J and the test pruning (N) are committed as seven
commits on `study1/carve-out` (tag `pre-carveout-2026-09` and branch
`phase2-rating` mark the pre-removal state): −37,258 / +394 lines, 143 files.
K (in-app consent) is folded into E1.5. One deviation from the table: the
evidence trace (`static/evidence.html`, `/evidence`) was **kept** — it is a QA
view of recorded encounters, reads only `/api/encounters`, and the demo page's
replay lane is built around it. E2.7 (remove the C forms) is done on the same
branch. Suite after the branch: 1,749 passed, 6 skipped, 5 pre-existing
failures in `tests/test_final_redaction.py` (`No module named 'httpx2'`, a
local-environment issue that predates the branch).

Before any deletion: tag `pre-carveout-2026-09` on `main`; create branch
`phase2-rating` at the same commit so removed code is one checkout away.
One PR per line; each removes the code, its routes, its tests, and its docs.

| # | Remove | Files (indicative) |
|---|---|---|
| A ✅ | Phase-2 rater pipeline | `server/raters.py rater_packet.py ratings.py reliability.py esci.py`; `static/rater.html evidence.html`; routes `/rate*`, `/api/rater*`, `/api/raters*`, `/api/ratings`, `/api/reliability`, `/api/encounters*`, `/evidence`; `docs/RATING.md`; tests `test_rater* test_ratings test_reliability test_esci test_rating_console test_final_rater_ui test_final_icc test_phase2_blockers`; the rater bits of `test_app.py test_api_blockers.py test_links.py test_video_route.py test_os_compat.py test_deploy_portability.py` |
| B ✅ | Offline LLM scoring | `server/scoring.py server/rubrics.py`; `/api/sessions/{id}/score`; cases in `test_final_app.py test_withdrawal_record.py` |
| C ✅ | Group debrief | `server/debrief.py`; `/api/sessions/{id}/debrief` |
| E ✅ | Legacy scenarios and UI | `scenarios/0*.yaml`, `scenarios/g*.yaml`, `scenarios/archive/`; `static/participant.html`; `/?scenario=`, `/chat`; scenario picker in `landing.html`; legacy loader paths in `server/scenarios.py` that `scenarios_v3` does not use |
| F ✅ | Text-only chat path | `/ws/participant` (text websocket, used only by `participant.html`), `server/claude_engine.py` (no importers). Live captions in `v2.html` come from `/ws/participant/voice` and are unaffected. |
| G ✅ | Arm links | `/start/one-to-one`, `/start/group`; `ARMS`, `arm_constructs`, `_resolve_pool` restriction in `server/runs.py`; arm sections in OPERATIONS |
| H ✅ | Legacy services | `agents/` (self-described not on the live path), `finetuning/` |
| J ✅ | Infra leftovers | `fly.toml`, `infra/apprunner-pilot.md`, root `btn_tmp.py`, root `.zip` |
| K ✅ | In-app consent | removed entirely 2026-09-17 (decision: consent is taken offline); contact details now from `STUDY_CONTACT_*` env |
| N ✅ | Tests | pruned with the modules above; keep everything covering the voice path, runs, storage, video, entry, withdrawal, browser compat |

Kept: D `demo.html` + `/demo`; I `reddit-analysis/` (canonical specs);
L researcher console, `/ws/researcher`, `director.html`, steering — hold.

### E7 — Docs

**7.1 README rewrite** — M ✅ 2026-09-17
- Rewritten as an introduction to the project and the app: what we measure,
  the Study 1 participant path, how the app works, repository map, quick
  start, configuration, browsers, the data, testing, deploying, where things
  stand. The live-measurement narrative ("What the participant hears", the
  2026-09-14 hesitant-participant round, transcription quality) moved verbatim
  to `docs/field-notes.md`. Every consent-gate description in the docs and the
  server comments was removed with it; dead consent-era helpers
  (`_adopt_participant_record`, `_bind_record_to_run`, `_record_is_the_callers`,
  `is_placeholder_value`, `_run_for_record`) deleted. CI's job for the removed
  `agents/` service deleted.

**7.2 OPERATIONS / DEPLOY refresh after E6 and E3** — S

### E8 — Deploy and ops

**8.1 Set the deployed task's environment** — S · infra
- `UPSTREAM_CONSENT_VERSION`, `SURVEY_RETURN_URL`, `CLAUDE_MODEL`, `DATA_DIR`,
  `ENCOUNTER_MIN_SECONDS`, `DEFAULT_RUN_VARIANT=A`, `REALTIME_MODEL` as decided.
- AC: `/health` reports all of them set; a study-cohort run records.

**8.2 Terraform state** — M · infra
- `53bc440` added a shared state backend and a second-deployer setup. Confirm
  state is recovered or rebuilt so E5.2 is applied by Terraform, not by hand.

**8.3 Wave monitoring** — S · ops
- CloudWatch counts (sessions, participants, completions/day), dropout by
  encounter index, encounter length distribution; one dashboard or a script.

### E9 — Pilot and launch

**9.1 Internal walkthrough (n = 3)** — S — full chain from Connect to Survey 2.
**9.2 Pilot (n = 5–10) on Connect** — M — review durations, audio quality,
video completeness, room behaviour, dropout; fix list.
**9.3 IRB amendment** — external — video capture, consent wording, retention;
the consent version in E1.5 is whatever the IRB approves.
**9.4 Go/no-go checklist** — S — every AC above green; PI sign-off.
**9.5 Wave 1: 100 participants × 4 encounters.**

---

## 3. Order of work

1. E6 carve-out + E2.7 (remove C) — shrinks everything that follows.
2. E2.1 base-text decision, then E2.2–2.5 refinement (can run alongside 3–4).
3. E3 rooms verification and fixes; E5.1 video verification.
4. E4 floor/ceiling; E1 chain; E5.2 storage; E8.1 env.
5. E7 docs.
6. E9.1 walkthrough → E9.2 pilot → E9.4 go/no-go → wave.

Milestones (no dates yet):
**M1** repo carved, eight forms with a chosen base ·
**M2** rooms verified, video verified, floor enforced, chain works end to end ·
**M3** pilot reviewed · **M4** wave 1 complete.

---

## 4. Open items (§8)

- **Q1 — base text per form**: decided 2026-09-17. Keep the merged (audit)
  structure and its two behavioural repairs; cut each character brief roughly
  in half by moving shared style rules (turn length, no help-desk
  acknowledgements, no repeated questions, busy-vs-gone) into the compiler's
  shared blocks (`scenarios_v3._render_prompt` / `engine.SPEECH_RULES`);
  reduce each file's comment header to ~10 lines. S1A is the worked example
  (E2.1), then the same pass on the other seven.
- **Q2 — carve-out F**: accepted; remove.
- **Q3 — ceiling**: confirmed (E4.3).
- **Q4 — ordering**: balanced Latin square (E2.8).
- **Q5 — owners**: see §2 header; issues created unassigned.
- **Q6 — tooling**: `gh` is not installed on this machine; `brew install gh &&
  gh auth login`, or reconnect the GitHub connector, before issues are created.
