# Study 1 rating instrument — ESCI Construct 4 items applied to recorded encounters

> ## ⚠️ Proprietary instrument — licensing unresolved
>
> Item source: ESCI item bank (Boyatzis, Goleman & Korn Ferry; see the project's "ESCI
> Items by Cluster and Competency" doc).
>
> **Proprietary instrument — items reproduced for research reference only;
> confirm licensing/permission before fielding.**
>
> That sentence is the canonical wording. `server/esci.py` and `server/rater_packet.py`
> quote it verbatim so the same warning reaches a rater's screen and every export; if you
> reword it here, reword it there in the same commit. Licensing / permission for this use
> has **not** been confirmed in writing. Confirm it before these items are put in front of
> a rater, paid or unpaid, or exported to anyone outside the study team.
>
> The warning travels with the items wherever they go, and must keep doing so: the rating
> console prints it above the item block (`static/rater.html`), the machine-readable bank
> carries it as a column on every row (`esci_items.csv`), and the operational guide
> repeats it (`docs/RATING.md`). Do not strip it to tidy up an export.

Raters take one recorded encounter at a time and rate the **participant** (not the AI
agent) on the ESCI Relationship Management items below.

**What a rater is given.** The rateable artefact is the **webcam video**, with the aligned
transcript beside it. That settles a contradiction this document previously had with the
roadmap, in favour of both: the video is the only artefact that plays back as a
conversation (the participant page mixes microphone and agent audio into it, while the WAV
files are per-channel), and the transcript is shown alongside for search and for the
moments the audio is unclear. An encounter whose webcam upload failed is rated from the
transcript alone, and the console tells the rater that is what is happening — the
transcript-only case is the degraded mode, not the design.

Machine-readable versions of the item bank:

| File | What it is |
|---|---|
| `esci_construct4_items.csv` | the original four-column source file; `server/esci.py` loads it at import and it remains the source of truth |
| `esci_items.csv` | the canonical export — adds the stable `item_id` the code and the API use (`ESCI-08`), the scale bounds, the N/A flag, and the licensing notice on every row. This is the file to import into Qualtrics |

## Rater task

> "You will watch a conversation between a study participant and one or more workplace
> counterparts. For each statement below, rate how consistently the participant
> demonstrated the behavior **within this conversation**."

Scale (ESCI 1–5): 1 Never · 2 Rarely · 3 Sometimes · 4 Often · 5 Consistently,
plus **"Not enough information to judge" (N/A)** — required because a single encounter
cannot exhibit every behavior (see administration notes). N/A is stored as `null`, never
as a number: any numeric stand-in would be averaged into a competency mean downstream.

Every encounter is rated on **all 22 items** regardless of the scenario's primary
competency (this preserves the multitrait-multimethod structure: 4 competencies ×
8 scenarios × k raters).

Two open-ended answers are collected with every rating: **what the participant could have
done better**, and **anything else notable**. They are not scored; they are what makes a
disagreement between two raters readable after the fact, and they are the material for the
qualitative pass.

## Items (Construct 4, excluding Coach & Mentor)

### Conflict Management (5 items)

| # | Item | Rev. |
|---|---|---|
| 8 | Tries to resolve conflict instead of allowing it to fester | |
| 14 | Resolves conflict by de-escalating the emotions in a situation | |
| 15 | Allows conflict to fester | (R) |
| 26 | Tries to resolve conflict by openly talking about disagreements with those involved | |
| 46 | Resolves conflict by bringing it into the open | |

### Influence (6 items)

| # | Item | Rev. |
|---|---|---|
| 3 | Convinces others by getting support from key people | |
| 17 | Convinces others by using multiple approaches | |
| 20 | Convinces others by appealing to their self-interest | |
| 38 | Anticipates how others will respond when trying to convince them | |
| 49 | Convinces others by developing behind-the-scenes support | |
| 68 | Convinces others through discussion | |

### Inspirational Leadership (5 items)

| # | Item | Rev. |
|---|---|---|
| 5 | Leads by building pride in the group | |
| 7 | Leads by inspiring people | |
| 24 | Does not inspire followers | (R) |
| 27 | Leads by bringing out the best in people | |
| 61 | Leads by articulating a compelling vision | |

### Teamwork (6 items)

| # | Item | Rev. |
|---|---|---|
| 11 | Does not cooperate with others | (R) |
| 12 | Works well in teams by being supportive | |
| 25 | Works well in teams by encouraging cooperation | |
| 33 | Works well in teams by soliciting others' input | |
| 37 | Works well in teams by being respectful of others | |
| 56 | Works well in teams by encouraging participation of everyone present | |

## Scenario × focal-item map

All 22 items are rated for every encounter; the focal items are those the scenario is
*designed* to elicit (they anchor rater training and the analysis of the target
competency). Raters aggregate across each scenario's **pressure points** — the full
specs (`reddit-analysis/scenarios/scenario-specifications.md`) map every pressure
point to its observable items with skill-shown / skill-missed anchors.

| Scenario | Target competency | Focal items | Pressure points (items made observable) |
|---|---|---|---|
| S1 Conflict Management | Conflict mgmt | 8, 14, 15(R), 26, 46 | retaliation fork (8, 26) · raising it (26, 46) · defensive spike (14) · audience decision (8, 14, 26) · face-saving concession (8, 14, 15) |
| S2 Influence | Influence | 3, 17, 20, 38, 49, 68 | opening frame (68, 20) · first deflection (38, 17) · second deflection (17, 3, 49, 20) · leverage moment (20, 38) · the close (68, 20) |
| S3 Inspirational Leadership | Insp. leadership | 5, 7, 24(R), 27, 61 | honest message vs. spin (61, 7, 24) · public challenge (7, 27, 24) · the silent one (27, 24) · building pride (5) · commitments (61, 7) |
| S4 Teamwork | Teamwork | 11(R), 12, 25, 33, 37, 56 | takeover bid (25, 56, 11) · the interruption (56, 33) · idea laundering (37, 12, 25) · soliciting the quiet (33, 56) · credit allocation (12, 25, 11) |

Notes: items 3/49 (key-people / behind-the-scenes support) are frequently N/A in S2's
dyadic setting — expect and report high N/A rates for that cell. Non-target items
should still show variance where encounters blend competencies (e.g. Conflict
Management items at S2's leverage moment if the manager goes cold).
Empathy/organizational-awareness (Construct 3) items can be added as a block if the
SA sensing layer should be rated directly rather than inferred — decide before
programming the survey.

## Two ways to field this

The instrument is administered through **the platform's own rating console**, with the
Qualtrics build kept as a supported alternative. Both present the same 22 items on the
same scale; they differ in how the answers come back.

| | Rating console (`/rate`) | Qualtrics |
|---|---|---|
| Route | `static/rater.html`, served by this platform | external survey |
| Media | video + transcript in the page, from a presigned S3 GET | transcript pasted or linked; video needs its own hosting |
| Answers come back | straight into `server/ratings.py`, per assignment | export file, ingested by `ratings.import_qualtrics(rows, mapping)` |
| Rater identity | scoped `rt_…` token; reaches only that rater's assignments | Qualtrics panel or link |
| Encounter identity | rating code `RC-…`, opaque and stable | must carry the same rating code as embedded data, or ratings cannot be joined |
| Randomization | server-side; the console renders the order it is served | Qualtrics block randomization |
| Attention checks | not implemented — see note 4 | supported natively |
| Status | **the working path** | open, and the fallback |

The console exists because the platform cannot get ratings back out of Qualtrics without
a survey, a loop-and-merge and an API round trip that do not exist. Which of the two is
the *route of record* for the published study is still a researchers' decision, and the
one place where running both would cost something is reconciliation at analysis time.

Full operational guide — registering raters, issuing tokens, assigning encounters, pulling
ratings, reading the reliability report — is [`docs/RATING.md`](../../../docs/RATING.md).

## Administration notes

1. **One encounter per block**; item order randomized within competency group,
   competency-group order randomized across raters (reduces order effects on the 68-item
   original ordering). In the console this randomization happens server-side, in
   `server/rater_packet.py`, and the page renders the order it is handed rather than
   shuffling again — one place to change it, and the order that was served is the order
   that can be recorded.
2. **Reverse-scored items (11, 15, 24)** presented as-is; reverse-code at analysis. Do
   not reword — they double as straight-lining checks. The console shows them unmarked
   for exactly that reason, and warns a rater who has given every item the same answer.
3. **N/A handling:** items rated N/A are excluded pairwise; a scenario-competency cell
   with systematic N/A (e.g. leadership items in peer scenarios) is informative — report
   N/A rates per cell. In the console N/A is a styled-apart, never-preselected button, and
   a rating cannot be submitted until every item carries a 1–5 **or** an N/A — a blank is
   never silently read as N/A.
4. **Attention checks:** 2 instructed-response items per session ("select 'Rarely' for
   this statement") + 1 comprehension check about the transcript's topic. **Not implemented
   in the console**: inserting an instructed-response item into a proprietary 22-item block
   changes what the block is, and where it goes is an instrument decision, not a UI one.
   The console instead makes careless rating visible in the data — every submission records
   the seconds the rater spent, and uniform-column and too-fast submissions each cost a
   second explicit click.
5. **Rater training/calibration:** train on 2 practice encounters with the scenario's
   `behavioral_markers` as anchor examples for 1 vs 3 vs 5; require agreement with gold
   ratings before entering the main pool. The gold ratings, the agreement level that
   admits a rater, and what happens to a rater who drifts are open.
6. **Design:** each encounter rated by k ≥ 3 raters; raters blind to condition and to
   the scenario's primary-competency designation. The console enforces the blinding in the
   packet: a rater sees the situation the participant saw, the counterparts, the video and
   the transcript, and never the participant key, the stage directions, the planted
   triggers, the ESCI tags on those triggers, the actor briefs, the construct/variant
   label, or the offline judge's score.

## Reliability & scoring plan

- Inter-rater reliability: ICC(2,k) per item and per competency scale; Krippendorff's α
  as ordinal-scale robustness check.
- Competency scores: mean of items within competency (after reverse-coding), per
  transcript, averaged over raters.
- Validity structure: with 2 scenarios per competency, test convergent (same competency
  across scenarios) vs. discriminant (different competencies within scenario)
  correlations — the MTMM matrix.

Implemented in `server/reliability.py` — ICC(2,1) and ICC(2,k) (two-way random, absolute
agreement), quadratic weighted κ pairwise, and Krippendorff's α on the ordinal scale, in
pure Python, per construct and per item with the n and rater count each figure was
computed from:

```bash
python -m server.reliability --cohort study        # human-readable
python -m server.reliability --cohort study --json
```

The bands to read those against, and the fact that the study's own threshold is not yet
chosen, are in [`docs/RATING.md`](../../../docs/RATING.md#reading-the-reliability-report).
