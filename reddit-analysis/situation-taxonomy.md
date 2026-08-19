# Situation taxonomy — r/antiwork as an evidence base for simulation scenarios

Reframes the dataset analysis around **interpersonal situations** (who the encounter is
with, what triggered it) rather than discussion topics. Basis: the 99,347 usable-text
posts; conservative phrase-pattern classification (counts are **lower bounds** — only
posts that state the situation explicitly are counted; categories are not mutually
exclusive). Reproduce: `notebooks/02_situation_taxonomy.py` →
`data/processed/antiwork_situation_taxonomy.json`. Figures: `figures/fig1_counterparts.png`,
`fig2_situations.png`, `fig3_scenario_evidence.png`.

## 1. How interpersonal is the dataset?

**39.6% of posts (39,301) name a specific workplace counterpart** — these are the
posts describing encounters rather than general commentary.

| Counterpart | Posts | % of all |
|---|---|---|
| Boss / manager / supervisor | 18,931 | 19.1% |
| Coworker / colleague / teammate | 13,218 | 13.3% |
| Owner / executives / corporate | 9,140 | 9.2% |
| HR | 7,021 | 7.1% |
| Customer / client | 4,011 | 4.0% |
| Own team (poster is the lead) | 2,309 | 2.3% |

Upward encounters (boss/manager) dominate, consistent with the scenario design where
S2 is upward, S1/S4 are peer-level, and S3 (leading a team) is the rarest natural
perspective in the data (2.3%) — participants will experience S3 as the least
familiar role, which is worth noting when interpreting scores.

## 2. Situation types, prevalence, and scenario mapping

The 13 detected situation types condense into **six families** (posts can match more
than one category, so family counts are approximate):

| Family | What's in it | Posts | % | Maps to |
|---|---|---|---|---|
| **Job security & discipline** | firing/write-up disputes; quitting and resignation encounters | ~9,900 | 10.0% | **gap** (quitting = context only) |
| **Time off & scheduling** | sick-leave/PTO denials; shift and schedule conflicts | ~6,000 | 6.1% | **gap** / partial S2-C |
| **Workload & unfair rules** | understaffing, covering for others, micromanagement, arbitrary policies | ~4,300 | 4.3% | S2-C + context |
| **Direct mistreatment** | public blame/humiliation, hostile after-hours messages, credit-taking | ~1,800 | 1.8% | **S1** (all variations) |
| **Negotiation & flexibility** | raise negotiation with outside offers; RTO/hybrid disputes | ~1,900 | 2.0% | **S2-A / S2-B** |
| **Team dynamics** | being excluded/ignored/talked over; post-exodus team morale | ~670 | 0.7% | **S3 / S4** |

Reading it as a whole: the everyday conflicts people actually post about are
concentrated in *job security, time off, and workload* — the top three families —
while the scenario set samples deliberately from the lower three, because those are
the encounters where relationship-management skill (rather than legal standing or
policy) determines the outcome. The full 13-category breakdown with per-category
counterpart rates is in the appendix.

## 3. What this says about the scenario set

**Well-grounded.** Blame/public humiliation (1,631 posts) is the best-attested S1
trigger — supporting the assignment rule that prefers S1-C (with S1-B) over S1-A.
Workload/understaffing (2,333 + 2,755 schedule) grounds S2-C; RTO disputes (1,202)
ground S2-B; raise negotiations (740, with the outside-offer pattern recurring
verbatim) ground S2-A.

**Rare as explicit phrases, but real.** Credit misattribution (77) and hostile
after-hours messages (78) are strict-phrase lower bounds — both patterns also appear
throughout the mined high-score posts (e.g. credit-taking stories with 100k+ upvotes)
and in moderator flair categories ("Workplace Abuse"). They are valid triggers; the
counts just say posters rarely describe them in these exact words.

**Perspective note for S3.** Team-morale-aftermath posts are rare (194) because the
sub's population is mostly non-managers — the *aftermath* is everywhere (burnout
topic: 15.2% of the corpus; venting: 5.3%) but narrated from below. S3's persona
scripts (Casey's cynicism, Jordan's disengagement) are therefore grounded in how
*team members* in the data actually talk, which is the right direction: participants
lead AI partners whose emotional register comes from real demoralized workers.

**Gaps → candidate future scenarios.** The two most common encounter types have no
scenario: discipline/firing disputes (6.3%) and sick-leave/PTO conflicts (3.3%).
Both are strong candidates for additional variations or a Study 2 scenario pool —
a PTO-denial conversation is a natural Influence variant; a write-up dispute is a
natural Conflict Management variant. Customer conflicts (4.0% of posts name
customers) are a third uncovered class if service contexts are ever in scope.

## 4. Traceability

Each scenario YAML in `scenarios/` carries `source_situations` keys referencing the
taxonomy rows above (machine-readable link from scenario variation → situation type →
post counts). The emotional-register grounding (personas) traces to the topic model in
`ANALYSIS.md` (burnout 15.2%, venting 5.3%, boss conflict 9.9%).

## Appendix: full 13-category breakdown

`cp%` = share of those posts that also name a counterpart (specificity check).

| Situation type | Posts | % | cp% | Maps to |
|---|---|---|---|---|
| Discipline / firing dispute | 6,305 | 6.3% | 61% | **gap** (no scenario) |
| Quitting / resignation encounter | 3,647 | 3.7% | 60% | context for S1/S2 stakes |
| Sick leave / PTO conflict | 3,276 | 3.3% | 55% | **gap** (no scenario) |
| Schedule / shift conflict | 2,755 | 2.8% | 56% | partial S2-C |
| Workload / understaffing / covering | 2,333 | 2.3% | 64% | S2-C |
| Unfair rule / policy / micromanagement | 1,966 | 2.0% | 65% | context (S3-C register) |
| Blame / public humiliation | 1,631 | 1.6% | 74% | S1-C |
| RTO / remote-work dispute | 1,202 | 1.2% | 47% | S2-B |
| Pay-raise negotiation / outside offer | 740 | 0.7% | 62% | S2-A |
| Excluded / ignored / talked over | 473 | 0.5% | 65% | S4 |
| Team morale aftermath (exodus, cuts) | 194 | 0.2% | 59% | S3 |
| Hostile after-hours message | 78 | 0.1% | 68% | S1-B |
| Credit misattribution | 77 | 0.1% | 78% | S1-A, S4 |
