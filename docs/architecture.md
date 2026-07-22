# Architecture

```
Participant (Prolific)
        │
        ▼
┌─────────────────┐     HTTP /chat      ┌──────────────────┐
│  app/ (Next.js)  │ ──────────────────▶ │ agents/ (FastAPI) │──▶ LLM API
│  simulation UI   │ ◀────────────────── │ persona + prompts │
└─────────────────┘                      └──────────────────┘
        │                                        │
        ▼                                        ▼
   transcripts store  ◀──────────────  scenario configs
        │                              (from reddit-analysis/)
        ▼
 raters via Qualtrics (studies/) ──▶ ratings ──▶ finetuning/
```

## Components

- **app/** — Next.js (App Router, TypeScript). Renders the encounter flow and chat UI, proxies chat turns to the agent service, logs transcripts.
- **agents/** — Python FastAPI service. Owns personas, scenario prompts, model calls, and guardrails. Scenario definitions come from `reddit-analysis/scenarios/`.
- **reddit-analysis/** — offline analysis pipeline; its output is the scenario library.
- **studies/** — no code dependencies; consumes exported transcripts, produces ratings.
- **finetuning/** — consumes rated transcripts, produces steered models that `agents/` serves in Study 2.

## Key decisions to record (docs/decisions/)

- 0001: repo organized by component, not by phase
- LLM provider(s) and model for the base agent
- Transcript storage (start: JSON files / SQLite; scale up only if needed)
- Hosting for app + agent service during data collection
