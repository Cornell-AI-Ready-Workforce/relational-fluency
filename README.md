# Relational Fluency

Research project: measuring soft skills (relational fluency / relationship management) through AI-simulated interview encounters, with a framework built on conversational AI agents.

**Cornell AI-Ready Workforce**

## Repository layout

| Folder | Purpose | Phase |
|---|---|---|
| `app/` | Next.js web app hosting the AI simulation encounters | 1 |
| `agents/` | Python (FastAPI) service for the conversational AI agent(s) embedded in the app | 2, 6 |
| `reddit-analysis/` | Reddit data analysis that grounds the simulation scenarios | 3 |
| `studies/` | Human-subjects materials: Prolific recruitment, rater workflows, Qualtrics instruments, analysis | 4, 5 |
| `finetuning/` | Fine-tuning / steering the agent models on relationship-management skills identified in Study 1 | 6 |
| `docs/` | Roadmap, architecture, and decision records | all |

## Research phases

1. **Simulation app** — build the web app that delivers AI simulation encounters to participants.
2. **Conversational agents** — create the AI agent(s) embedded in the app (see `agents/`).
3. **Scenario grounding** — derive simulation scenarios from Reddit analysis (see `reddit-analysis/`).
4. **Recruitment** — recruit Prolific participants, raters, and a second participant set (see `studies/`).
5. **Ratings** — raters evaluate encounters via Qualtrics (see `studies/*/qualtrics`).
6. **Fine-tuning** — steer the agent models using the good relationship-management behaviors identified in Study 1 (see `finetuning/`).

The full roadmap with milestones lives in [`docs/roadmap.md`](docs/roadmap.md).

## Getting started

```bash
# Frontend (simulation app)
cd app && npm install && npm run dev

# Agent service
cd agents && pip install -e ".[dev]" && uvicorn agents.main:app --reload
```
