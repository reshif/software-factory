#!/usr/bin/env python3
"""Phase 0 go/no-go analysis for the software-factory experiment.

Usage: python experiment/analyze.py experiment/results.csv

GO rule (software-factory-final-draft.md §19.1):
  1. >= 6 items reached staging with zero interventions outside approvals, AND
  2. total packet-review minutes (H1 + HM + H2) < total baseline review minutes.
Health check (reported, not part of the rule): rejection rate should be > 0.
"""
import csv
import sys

REQUIRED_CLEAN_ITEMS = 6


def as_int(value, default=0):
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return default


def as_float(value, default=0.0):
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def is_yes(value):
    return str(value).strip().lower() in {"yes", "y", "true", "1"}


def main(path):
    with open(path, newline="") as f:
        rows = [r for r in csv.DictReader(f) if r.get("item", "").strip()]
    if not rows:
        sys.exit(f"No rows in {path}. Fill in one row per experiment item.")

    clean = [r for r in rows
             if is_yes(r["reached_staging_untouched"])
             and as_int(r["interventions_outside_approvals"]) == 0]
    packet_minutes = sum(as_float(r["h1_minutes"]) + as_float(r["hm_minutes"]) + as_float(r["h2_minutes"])
                         for r in rows)
    baseline_minutes = sum(as_float(r["baseline_review_minutes"]) for r in rows)
    rejections = sum(1 for r in rows if is_yes(r["rejected"]))
    interventions = sum(as_int(r["interventions_outside_approvals"]) for r in rows)
    llm_usd = sum(as_float(r["llm_usd"]) for r in rows)
    ci_minutes = sum(as_float(r["ci_minutes"]) for r in rows)

    cond_clean = len(clean) >= REQUIRED_CLEAN_ITEMS
    cond_cheaper = baseline_minutes > 0 and packet_minutes < baseline_minutes

    print(f"Items analysed:                      {len(rows)}")
    print(f"Clean items (staging, 0 interventions): {len(clean)}  (need >= {REQUIRED_CLEAN_ITEMS})  "
          f"{'PASS' if cond_clean else 'FAIL'}")
    if baseline_minutes > 0:
        ratio = packet_minutes / baseline_minutes
        print(f"Packet review minutes vs baseline:   {packet_minutes:.0f} vs {baseline_minutes:.0f} "
              f"({ratio:.0%})  {'PASS' if cond_cheaper else 'FAIL'}")
    else:
        print("Packet review minutes vs baseline:   baseline missing  FAIL (fill baseline_review_minutes)")
    print(f"Total interventions outside approvals: {interventions}")
    print(f"Rejection rate:                      {rejections}/{len(rows)}"
          + ("  WARNING: 0 rejections, check for rubber-stamping" if rejections == 0 else ""))
    print(f"LLM spend:                           ${llm_usd:.2f}  (${llm_usd / len(rows):.2f}/item)")
    print(f"CI minutes:                          {ci_minutes:.0f}")
    by_type = {}
    for r in rows:
        by_type.setdefault(r["type"].strip() or "unknown", []).append(r)
    for t, items in sorted(by_type.items()):
        c = sum(1 for r in items if r in clean)
        print(f"  {t:<8} clean {c}/{len(items)}")

    decision = "GO" if (cond_clean and cond_cheaper) else "NO-GO"
    print(f"\nDECISION: {decision}")
    if decision == "NO-GO":
        print("Next: improve specs/verification (see intervention_notes), then rerun Phase 0.")
    return 0 if decision == "GO" else 1


if __name__ == "__main__":
    if len(sys.argv) != 2:
        sys.exit(__doc__)
    sys.exit(main(sys.argv[1]))
