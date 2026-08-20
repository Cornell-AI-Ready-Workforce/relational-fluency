# Scenario map

Generated from `scenarios/v3/*.yaml` — the specs are the source of truth.
Regenerate with `python tools/gen_scenario_map.py`.

## Encounters

| ID | Construct | Var | Pair | Agents | Interaction 1 | Interaction 2 |
|---|---|---|---|---|---|---|
| `S1A` | conflict management | A | `S1B` | Riley, Sam | **1:1** — Riley corners you (Riley) | **1:1** — Hallway run-in with Sam (Sam) |
| `S1B` | conflict management | B | `S1A` | Mel, Drew | **1:1** — Mel pings you first thing (Mel) | **1:1** — Coffee-machine run-in with Drew (Drew) |
| `S2A` | influence | A | `S2B` | Morgan | **1:1** — Making the case (Morgan) | **1:1** — The deflection ladder (Morgan) |
| `S2B` | influence | B | `S2A` | Sasha | **1:1** — Making the case (Sasha) | **1:1** — The deflection ladder (Sasha) |
| `S3A` | inspirational leadership | A | `S3B` | Alex, Jordan, Casey | **group** — Team meeting (Alex + Jordan + Casey) | **1:1 series** — Brief one-on-ones (Jordan + Casey) |
| `S3B` | inspirational leadership | B | `S3A` | Toni, Lee, Ari | **group** — Team meeting (Toni + Lee + Ari) | **1:1 series** — Brief one-on-ones (Lee + Ari) |
| `S4A` | teamwork | A | `S4B` | Dan, Priya, Chris | **group** — Working session (Dan + Priya + Chris) | **group** — The close, who owns what (Dan + Priya + Chris) |
| `S4B` | teamwork | B | `S4A` | Dan, Priya, Chris | **group** — Working session (Dan + Priya + Chris) | **group** — The close, speaking roles & credit (Dan + Priya + Chris) |

## Planted triggers → ESCI items

Triggers fire in order within their interaction. **probe** means the trigger
carries an `on_silence` line, so a participant who says nothing still
produces scoreable behaviour. **scored** means the spec carries the
high/low sample answers from the research note, used as judge anchors.

| Scenario | Interaction | Trigger | ESCI items | |
|---|---|---|---|---|
| `S1A` | `i1` | `t1_retaliation_fork` | resolve_not_fester, bring_into_open, fester_r | probe scored |
| `S1A` | `i2` | `t2_the_opening` | talk_openly, bring_into_open, fester_r | probe scored |
| `S1A` | `i2` | `t3_defensiveness` | de_escalate, talk_openly |  |
| `S1A` | `i2` | `t4_half_concession` | de_escalate, resolve_not_fester |  |
| `S1B` | `i1` | `t1_retaliation_fork` | resolve_not_fester, bring_into_open, fester_r | probe scored |
| `S1B` | `i2` | `t2_the_opening` | talk_openly, bring_into_open, fester_r | probe scored |
| `S1B` | `i2` | `t3_defensiveness` | de_escalate, talk_openly |  |
| `S1B` | `i2` | `t4_half_concession` | de_escalate, resolve_not_fester |  |
| `S2A` | `i1` | `t1_the_opening` | through_discussion, anticipates | probe scored |
| `S2A` | `i2` | `t2_rung1_no_budget` | multiple_approaches, anticipates |  |
| `S2A` | `i2` | `t3_rung2_everyone_stretched` | multiple_approaches, self_interest |  |
| `S2A` | `i2` | `t4_rung3_no_precedent` | behind_scenes, key_people, multiple_approaches | scored |
| `S2B` | `i1` | `t1_the_opening` | through_discussion, anticipates, self_interest | probe scored |
| `S2B` | `i2` | `t2_rung1_company_wide` | multiple_approaches, anticipates |  |
| `S2B` | `i2` | `t3_rung2_one_exception` | multiple_approaches, self_interest |  |
| `S2B` | `i2` | `t4_rung3_director_wont_sign` | behind_scenes, key_people, multiple_approaches | scored |
| `S3A` | `i1` | `t1_public_challenge` | compelling_vision, builds_pride, not_inspire_r | probe scored |
| `S3A` | `i2` | `t2_jordans_flat_fine` | brings_out_best, inspires | probe scored |
| `S3A` | `i2` | `t3_caseys_overload` | brings_out_best, builds_pride |  |
| `S3B` | `i1` | `t1_public_challenge` | compelling_vision, builds_pride, not_inspire_r | probe scored |
| `S3B` | `i2` | `t2_resignation_in_place` | brings_out_best, inspires | probe scored |
| `S3B` | `i2` | `t3_anxious_junior` | brings_out_best, builds_pride |  |
| `S4A` | `i1` | `t1_priya_interrupted` | solicits_input, encourages_participation, not_cooperate_r | probe scored |
| `S4A` | `i1` | `t2_idea_relabelled` | respectful, encourages_cooperation |  |
| `S4A` | `i1` | `t3_decisions_close_with_priya_silent` | encourages_participation, solicits_input |  |
| `S4A` | `i2` | `t4_dan_claims_the_writeup` | supportive, respectful, encourages_cooperation | probe scored |
| `S4B` | `i1` | `t1_priya_interrupted` | solicits_input, encourages_participation, not_cooperate_r | probe scored |
| `S4B` | `i1` | `t2_idea_relabelled` | respectful, encourages_cooperation |  |
| `S4B` | `i1` | `t3_runthrough_without_priya` | encourages_participation, solicits_input |  |
| `S4B` | `i2` | `t4_dan_claims_the_walkthrough` | supportive, respectful, encourages_cooperation | probe scored |

## ESCI item keys

**conflict management**

- `resolve_not_fester` — Tries to resolve conflict instead of allowing it to fester
- `de_escalate` — Resolves conflict by de-escalating the emotions in a situation
- `fester_r` — Allows conflict to fester (R)
- `talk_openly` — Tries to resolve conflict by openly talking about disagreements with those involved
- `bring_into_open` — Resolves conflict by bringing it into the open

**influence**

- `key_people` — Convinces others by getting support from key people
- `multiple_approaches` — Convinces others by using multiple approaches
- `self_interest` — Convinces others by appealing to their self-interest
- `anticipates` — Anticipates how others will respond when trying to convince them
- `behind_scenes` — Convinces others by developing behind-the-scenes support
- `through_discussion` — Convinces others through discussion

**inspirational leadership**

- `builds_pride` — Leads by building pride in the group
- `inspires` — Leads by inspiring people
- `not_inspire_r` — Does not inspire followers (R)
- `brings_out_best` — Leads by bringing out the best in people
- `compelling_vision` — Leads by articulating a compelling vision

**teamwork**

- `not_cooperate_r` — Does not cooperate with others (R)
- `supportive` — Works well in teams by being supportive
- `encourages_cooperation` — Works well in teams by encouraging cooperation
- `solicits_input` — Works well in teams by soliciting others' input
- `respectful` — Works well in teams by being respectful of others
- `encourages_participation` — Works well in teams by encouraging participation of everyone present

