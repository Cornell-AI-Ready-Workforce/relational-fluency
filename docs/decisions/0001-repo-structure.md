# ADR 0001: Organize repo by component, not by phase

**Status:** accepted, partially superseded 2026-08-19 · **Date:** 2026-07-22

> The component-not-phase principle still holds. The specific layout changed:
> `app/` was removed when the Python simulation platform became the repo root,
> so the participant-facing code now lives in `server/` + `static/` at top level
> rather than in a separate `app/` folder. `agents/` is legacy — see its README.

## Context

The project has six sequential-ish research phases, but the code artifacts (app, agent service, scenario library) are shared across phases — e.g. the agent service is built in Phase 2 and modified again in Phase 6.

## Decision

Top-level folders by component (`app/`, `agents/`, `reddit-analysis/`, `studies/`, `finetuning/`, `docs/`), with the phase plan tracked in `docs/roadmap.md` rather than in the folder layout.

## Consequences

- Shared code has one home; no duplication between phase folders.
- Phase progress is tracked in the roadmap and GitHub issues/milestones, not by folder.
