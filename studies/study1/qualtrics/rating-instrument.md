# Study 1 rating instrument — ESCI Construct 4 items applied to transcripts

Raters read one simulation transcript at a time and rate the **participant** (not the AI
agent) on the ESCI Relationship Management items below.

Item source: ESCI item bank (Boyatzis, Goleman & Korn Ferry; see the project's "ESCI Items
by Cluster and Competency" doc). **Proprietary instrument — items reproduced for research
reference only; confirm licensing/permission before fielding.** Machine-readable version
for Qualtrics import: `esci_construct4_items.csv`.

## Rater task

> "You will read a conversation between a study participant and a workplace counterpart.
> For each statement below, rate how consistently the participant demonstrated the
> behavior **within this conversation**."

Scale (ESCI 1–5): 1 Never · 2 Rarely · 3 Sometimes · 4 Often · 5 Consistently,
plus **"Not enough information to judge" (N/A)** — required because a single transcript
cannot exhibit every behavior (see administration notes).

Every transcript is rated on **all 22 items** regardless of the scenario's primary
competency (this preserves the multitrait-multimethod structure: 4 competencies ×
8 scenarios × k raters).

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

## Administration notes (Qualtrics build)

1. **One transcript per block**; item order randomized within competency group,
   competency-group order randomized across raters (reduces order effects on the 68-item
   original ordering).
2. **Reverse-scored items (11, 15, 24)** presented as-is; reverse-code at analysis. Do
   not reword — they double as straight-lining checks.
3. **N/A handling:** items rated N/A are excluded pairwise; a scenario-competency cell
   with systematic N/A (e.g. leadership items in peer scenarios) is informative — report
   N/A rates per cell.
4. **Attention checks:** 2 instructed-response items per session ("select 'Rarely' for
   this statement") + 1 comprehension check about the transcript's topic.
5. **Rater training/calibration:** train on 2 practice transcripts with the scenario's
   `behavioral_markers` as anchor examples for 1 vs 3 vs 5; require agreement with gold
   ratings before entering the main pool.
6. **Design:** each transcript rated by k ≥ 3 raters; raters blind to condition and to
   the scenario's primary-competency designation.

## Reliability & scoring plan

- Inter-rater reliability: ICC(2,k) per item and per competency scale; Krippendorff's α
  as ordinal-scale robustness check.
- Competency scores: mean of items within competency (after reverse-coding), per
  transcript, averaged over raters.
- Validity structure: with 2 scenarios per competency, test convergent (same competency
  across scenarios) vs. discriminant (different competencies within scenario)
  correlations — the MTMM matrix.
