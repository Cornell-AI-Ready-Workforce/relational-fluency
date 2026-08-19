# agents/ — Conversational AI agent service (Phases 2 & 6)

FastAPI service that powers the conversational agents embedded in the simulation app.

## Run locally

```bash
pip install -e ".[dev]"
cp .env.example .env          # set your LLM API key
uvicorn agents.main:app --reload   # http://localhost:8000
```

## Structure

```
src/agents/
  main.py           # FastAPI app, /chat endpoint
  personas/         # persona definitions (who the agent is in each scenario)
  prompts/          # system prompt templates
tests/
```

## Design notes

- Scenario definitions come from `../reddit-analysis/scenarios/` — the agent
  loads a scenario config (persona + situation + goals) at conversation start.
- Phase 6 swaps the base model for a fine-tuned / steered model; the API
  surface stays the same so `app/` needs no changes.
- Every conversation must be logged with scenario ID, participant ID, and
  full turn history for later rating.
