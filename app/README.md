# app/ — Simulation encounter web app (Phase 1)

Next.js (App Router, TypeScript) app that participants use for AI simulation encounters.

## Run locally

```bash
npm install
cp .env.example .env.local   # set AGENT_API_URL
npm run dev                   # http://localhost:3000
```

## Structure

```
src/
  app/
    layout.tsx        # root layout
    page.tsx          # landing / consent entry
    simulation/
      page.tsx        # chat encounter screen
  components/
    Chat.tsx          # chat UI (messages + input)
  lib/
    api.ts            # client for the agents/ service
```

## Participant flow (to build)

1. Entry with Prolific PID from URL params (`?PROLIFIC_PID=...`)
2. Consent + instructions
3. Simulation chat (one or more scenarios)
4. Transcript saved; completion code / redirect to Prolific
