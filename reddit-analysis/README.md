# reddit-analysis/ — Scenario grounding (Phase 3)

Analysis of Reddit posts about workplace/relational situations, used to derive
ecologically valid simulation scenarios.

## Structure

```
data/
  raw/         # collected Reddit data — GITIGNORED, never committed
  processed/   # de-identified, aggregated derivatives (committable if IRB-clean)
notebooks/     # exploration and analysis notebooks
scenarios/     # OUTPUT: scenario configs consumed by agents/
```

## Pipeline (to build)

1. **Collect** — target subreddits (e.g. r/work, r/jobs, r/AskManagers, r/careerguidance);
   document collection method and date range.
2. **Analyze** — thematic coding / topic modeling → taxonomy of relational situations
   (conflict with coworker, negotiating with manager, delivering bad news, ...).
3. **Author scenarios** — each scenario file: situation summary, agent persona pointer,
   participant goal, difficulty markers, source themes (not raw quotes).

## Ethics notes

- Never commit raw Reddit content; never quote posts verbatim in scenarios.
- Scenarios should be composites/paraphrases of themes, not identifiable stories.
- Check current Reddit Data API terms before collection.
