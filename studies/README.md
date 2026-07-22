# studies/ — Human-subjects research (Phases 4 & 5)

Materials and analysis for the two studies. No app code lives here.

## Study 1 — Measurement study

Prolific participants complete simulation encounters; trained raters evaluate
transcripts in Qualtrics on relationship-management dimensions.

```
study1/
  recruitment/   # Prolific study config, prescreeners, consent, payment records
  raters/        # rater recruitment, training materials, calibration protocol
  qualtrics/     # survey exports (.qsf), rating instrument documentation
  analysis/      # reliability (ICC/alpha), descriptives, marker extraction for Phase 6
```

## Study 2 — Validation with steered agents

Second participant set interacts with the fine-tuned/steered agents (Phase 6 output).

```
study2/
  recruitment/
  analysis/
```

## Conventions

- Keep `.qsf` exports of every Qualtrics survey version in `qualtrics/`.
- Raw participant data stays out of git (see root `.gitignore`); commit only
  de-identified processed data and analysis scripts.
- Log Prolific study IDs, dates, and payment per wave in `recruitment/log.md`.
