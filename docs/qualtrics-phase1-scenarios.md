# Scenario fields in the Phase 1 survey

The app serves each participant's four conversations in a counterbalanced
order (a Williams Latin square, `server/runs.py`), so "conversation 1" is S4A
for one participant and S1A for another. These steps put the order into the
Phase 1 survey (SV_bClj80jCRO4Dmdw) as embedded data, so questions after the
conversations can name the right one, and the response records it.

| Field | Value |
|---|---|
| `rf_scenario_1` … `rf_scenario_4` | the scenario of conversation 1 … 4, e.g. `S4A` |
| `rf_completed` | how many of the four the participant finished, `0`–`4` |
| `rf_scenario_status` | `ok`, or `missing` when the app had no run for this response or could not be reached |

The survey asks the app, `GET /api/survey/scenarios?qid=<ResponseID>`, which
answers with only those values, and only to a page on `https://*.qualtrics.com`.
The run is found by the ResponseID that the page 7 link already passes as
`qid`. The analysis join (`python -m server.qualtrics join`) has the same order
in its `run.encounters`, whether or not these fields were set.

## Add it to the survey

1. **Survey flow.** In the Embedded Data element at the top, add
   `rf_scenario_1`, `rf_scenario_2`, `rf_scenario_3`, `rf_scenario_4`,
   `rf_completed` and `rf_scenario_status`, each left empty. A field the script
   sets is recorded only when it is declared here. If the survey uses the
   **Simple** layout, also declare each one with the prefix `__js_`
   (`__js_rf_scenario_1`, …).
2. **A new page right after the completion-code page.** Add a page break after
   the code box, then one **Text / Graphic** question with a line such as
   "Thank you. Press Next to continue." Open that question's **JavaScript** and
   replace its contents with
   [`tools/qualtrics/phase1_scenarios.js`](../tools/qualtrics/phase1_scenarios.js),
   unchanged. Next stays hidden for a few seconds at most while it asks the app.
3. **Use the fields on later pages only.** A value set by JavaScript can be
   piped from the next page on, as `${e://Field/rf_scenario_1}`, or used in
   display logic ("Embedded Data rf_scenario_1 is equal to S4A").

## Check it

Preview the survey with a test `participantId` in the link, and the researcher
key and `cohort=internal` on the page 7 link, so the run is not study data.
Go through at least one conversation, enter the code, and on the next page
check that Next appears. In the preview's response (Data & Analysis), the six
fields are filled in, and `rf_scenario_1` matches the first conversation you
had.
