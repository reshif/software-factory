# Baseline: today's human PR review cost

The experiment compares **packet-review minutes** against **what reviewing comparable human-written PRs costs today**. Measure the baseline in week 1, in parallel with the first items.

## Method

1. Take the **last 10 merged human PRs** of similar size and type to the experiment items.
2. For each PR, record:
   - **review minutes** for all reviewers. Use each reviewer's estimate, or time-tracking if you have it. Include re-reviews.
   - **review rounds** (the number of "changes requested" plus 1)
   - lead time from PR open to merge
3. Also record, for the last 3 months if available:
   - change fail rate (deployments needing a hotfix or rollback)
   - deployment frequency

## Record

| PR | Size (lines) | Type | Review minutes (total) | Rounds | Open→merge (h) |
|---|---|---|---|---|---|
| #… | … | patch/feature | … | … | … |

**Baseline review minutes per change (median):** ___
**Change fail rate:** ___ %   **Deploys/week:** ___

Put the median in the `baseline_review_minutes` column of `results.csv` for every item of the same type. Use separate medians for patch and feature if they differ a lot.
