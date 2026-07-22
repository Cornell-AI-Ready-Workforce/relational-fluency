# finetuning/ — Model steering (Phase 6)

Fine-tune / steer the conversational agent models based on the relationship-management
behaviors that Study 1 identifies as effective.

## Structure

```
data/      # training datasets built from rated Study 1 transcripts — GITIGNORED
configs/   # training configs (SFT / DPO / steering), versioned
scripts/   # dataset construction + training + eval scripts
```

## Planned pipeline

1. **Marker extraction** — from `studies/study1/analysis/`, identify behaviors that
   correlate with high rater scores.
2. **Dataset construction** — build instruction/preference pairs from rated transcripts
   (e.g. high-rated vs low-rated responses in comparable contexts).
3. **Training** — approach depends on model access: SFT/DPO on open weights, or
   prompt-level steering / few-shot conditioning for API-only models.
4. **Evaluation** — held-out rated transcripts + Study 2 with the second participant set.

The steered model plugs back into `agents/` behind the same `/chat` API.
