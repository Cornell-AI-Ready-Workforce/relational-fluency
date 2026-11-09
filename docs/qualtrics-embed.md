# The study inside one Qualtrics survey

The four conversations run inside a single Qualtrics survey. Each conversation
is a block of its own, with the app framed on the page and the survey's own
**Next** button to move on. Questions can go before, between and after the
conversations. This replaces the two-survey chain (Survey 1 → app → Survey 2);
that chain still works and nothing about it changed.

## How it works

- Every conversation block frames the same entry link, with two extra
  parameters: `embed=qualtrics` and the block's place, `step=1` to `step=4`.
- `/start` resumes the participant's one run each time, so block *n* opens the
  run's *n*-th conversation. The order is still drawn by the app and shuffled per
  run; the survey does not need to know it.
- When the conversation ends and its video is saved, the app shows "Press Next
  below this window" and sends the survey a message. The block's script shows
  Next only then, and saves what the app reported as embedded data.
- The app never starts the next conversation inside the same block, and never
  opens a survey link inside the frame.
- A block reloaded after its conversation finished says so and lets the survey
  go on; it does not start the next conversation in the wrong block.
- Only a page on `https://*.qualtrics.com` is told anything, the completion
  code included.

## Build the survey

### 1. Survey flow

At the top of the Survey Flow, one **Embedded Data** element with:

| Field | Value |
|---|---|
| `participantId` | *Value will be set from Panel or URL* (as today, from the CloudResearch Connect URL) |
| `rf_run` | empty |
| `rf_status` | empty |
| `rf_code` | empty |
| `rf_scenario_1` … `rf_scenario_4` | empty |

A field the script sets is only recorded when it is declared here. If the
survey uses the **Simple** layout, declare each of these with the prefix
`__js_` as well (`__js_rf_run`, `__js_rf_status`, …): that layout stores
script-set fields under that name.

Then the blocks, in order:

1. Consent and pre-survey questions.
2. Conversation 1, Conversation 2, Conversation 3, Conversation 4, one block
   each, one page each. Questions about a conversation can follow it in its own
   block.
3. Post-survey questions, then the end-of-survey redirect to CloudResearch.

After each conversation block, a **Branch**: if `rf_status` is not
`in_progress` and not `complete` (the participant stopped, or the run could
not go on), go to the post-survey or the end of survey.

### 2. Look & feel

Set the layout's page width to the widest it offers. The room needs about
1100 px for the side-by-side layout; narrower, it stacks into one column, which
still works.

### 3. Each conversation block

One **Text / Graphic** question. In its rich content editor, choose
**HTML View** and paste this, with `step=1` in block 1, `step=2` in block 2,
and so on. Nothing else changes between blocks.

```html
<iframe data-rf title="Conversation"
  src="https://rf.ai-ready-workforce.ai.cornell.edu/start?participantId=${e://Field/participantId}&qid=${e://Field/ResponseID}&embed=qualtrics&step=1"
  allow="microphone; camera; autoplay; fullscreen"
  style="width:100%;height:85vh;min-height:720px;border:0;border-radius:12px"></iframe>
```

`allow="microphone; camera; …"` is what lets the browser ask for the
microphone and camera inside the frame. Without it the room cannot start.

Then the question's **JavaScript**: replace what is there with the contents of
[`tools/qualtrics/conversation_block.js`](../tools/qualtrics/conversation_block.js),
unchanged, in all four blocks. It reads the app's address and the block's step
from the frame's own link.

### What the survey records

| Field | Set when | Value |
|---|---|---|
| `rf_run` | any message | the app's run id, the join to the run and its recordings |
| `rf_scenario_n` | conversation *n* ends | the scenario it was, e.g. `S3A` |
| `rf_status` | each block ends | `in_progress` after conversations 1–3, `complete` after the fourth, or how the study ended early (`withdrawn`, `encounter_failed`, …) |
| `rf_code` | the fourth conversation ends, or the study ends early | the completion code (the code so far, when early) |

`qid` (the ResponseID) is on every entry link as before, so
`python -m server.qualtrics join` joins the survey to the run on it with no
change. With one survey there is no Survey 2: leave `QUALTRICS_SURVEY2_ID`
and `SURVEY_RETURN_URL` unset for this design.

## Leaving the study

The room has no "Stop and leave the study" button any more. A participant who
wants to stop does it in the survey: closing it, or a way out the survey offers.
Make sure the consent text says that, and does not promise a Stop button in the
app. The app's own cards still offer a way out when the microphone cannot be
made to work, or a conversation cannot be saved; the survey is then told
(`study_ended`) and the branch above skips the remaining blocks.

## Test it

**Locally, without Qualtrics.** `tools/qualtrics/harness.html` is a stand-in
survey: four blocks, the same block script, a Next button it hides and shows,
and the embedded data printed under the page. Serve that folder on a different
port from the app, so the frame is cross-origin as it is in Qualtrics:

```bash
.venv/bin/python -m http.server 8794 --bind 127.0.0.1 --directory tools/qualtrics
```

Then open:

```text
http://localhost:8794/harness.html?app=http://localhost:8765&pid=RFLOCAL01
```

With the researcher key, add `&key=<key>&cohort=internal` so the run is
recorded as internal and not as study data. The app accepts a `localhost`
parent only while it is itself served from `localhost`.

**In Qualtrics.** Preview the survey with a test `participantId` in the URL
(the researcher key and `cohort=internal` on the link keep it out of the
study data). Check, in a desktop Chrome and a Safari:

1. The browser asks for the microphone and camera inside block 1.
2. Next is hidden until the conversation ends and the "Press Next" note shows.
3. Block 2 opens the second conversation, not the first again.
4. Reloading a block after its conversation ended shows "already done", not
   the next conversation.
5. After block 4, the response has `rf_run`, `rf_code`, `rf_status = complete`
   and the four `rf_scenario_n` fields.
