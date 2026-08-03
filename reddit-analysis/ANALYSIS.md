# r/antiwork dataset — descriptive statistics & topics

Dataset: `data/raw/subreddit_antiwork/r_antiwork_posts.jsonl` (885 MB, Pushshift-style dump)
Analyzed 2026-07-22. Stats: `data/processed/antiwork_descriptive_stats.json` · Topics: `data/processed/antiwork_topics.json`

## Descriptive statistics

| Metric | Value |
|---|---|
| Total posts | 228,657 |
| Date range | 2023-01-01 → 2026-07-22 |
| Unique authors | 126,757 |
| Self (text) posts | 142,783 (62%) |
| Posts with usable text (>50 chars, not removed) | 99,347 (43%) |
| Removed/deleted text | 62,839 (27%) |
| Posts per year | 2023: 114,634 · 2024: 55,183 · 2025: 39,777 · 2026 (Jan–Jul): 19,063 |
| Score | median 4, mean 478 (heavy right skew; p99 = 9,731, max 168k) |
| Comments | median 3, mean 39 (p99 = 674) |
| Selftext length | median 735 chars, mean 1,063 (p90 = 2,233) |

Notes: volume declines steadily after the early-2023 peak (~14k posts/month → ~2.7k/month in 2026). Engagement is extremely skewed — a small share of viral posts carries most of the score mass. About a quarter of posts have moderator-removed or author-deleted bodies, so text analysis should use the 99k usable-text subset (extracted to `data/raw/subreddit_antiwork/posts_text_only.jsonl.gz`).

## Major topics (NMF, k=14, TF-IDF on title+selftext, 99,347 posts)

| Share | Topic | Signature terms |
|---|---|---|
| 15.2% | Burnout, life dissatisfaction, career direction | life, feel, money, live, family, school |
| 10.7% | Labor politics: unions, strikes, workers' rights, AI | union, strike, labor, rights, wages, ai |
| 9.9% | Conflicts with bosses; quitting | boss, quit, coworker, tell, new boss |
| 7.6% | Manager/store-level workplace stories | manager, store, management, team, training |
| 7.0% | Hours, shifts, breaks, overtime | hours, shift, break, schedule, overtime |
| 7.0% | Job applications & interviews | interview, applied, resume, hiring, offer |
| 6.9% | Firings, HR disputes, formal communications | fired, email, hr, supervisor, notice |
| 6.8% | Pay, paychecks, raises, bills | pay, paid, salary, paycheck, raise |
| 6.1% | Remote work / return-to-office | office, remote, wfh, rto, hybrid, commute |
| 5.9% | Company-level events: layoffs, severance, CEOs | company, laid, ceo, severance, bonus |
| 5.3% | Pure venting/rants (profanity-heavy) | fucking, hate, tired, anymore |
| 4.7% | Sick leave & PTO policies | sick, pto, vacation, doctor, policy |
| 4.0% | Health (esp. mental health) & insurance | mental health, insurance, anxiety, stress |
| 2.9% | Minimum/living wage, wage theft | minimum wage, living wage, wage theft, tips |

## Relevance to scenario design (Phase 3)

The interpersonal-conflict topics are the richest scenario sources: boss conflict/quitting (9.9%), manager stories (7.6%), firings & HR disputes (6.9%), plus negotiation-adjacent topics — pay/raises (6.8%), sick leave/PTO (4.7%), and RTO pushback (6.1%). The venting topic (5.3%) and burnout topic (15.2%) characterize the emotional register of the AI partners' personas rather than seeding situations.

**Situation-level analysis:** [`situation-taxonomy.md`](situation-taxonomy.md) reframes this around concrete encounters — 39.6% of posts name a workplace counterpart — and maps each situation type (with prevalence) to the S1–S4 scenario variations, including grounding strength and coverage gaps. That document is the canonical link between this dataset and the scenario set.

## Reproduce

```bash
python notebooks/01_descriptive_stats_and_topics.py
```
