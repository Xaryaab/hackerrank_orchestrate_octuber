"""Local grader: compare predictions with the 32 public sample answers.

usage: python code/evaluation/main.py [predictions.csv] [gold.csv] [--details]

Metrics
  status accuracy    - deletion_status exact match per case
  step precision/recall/F1 - steps matched as full tuples (phase, system_id, resource_id,
                       operation, state, evidence_id) ignoring step_no (multiset match)
  field accuracy     - for every gold step, fraction of fields matched by the best aligned
                       predicted step (same resource_id when possible)
  resource recall    - fraction of gold (resource_id) referenced anywhere in prediction
  sequence accuracy  - fraction of cases whose full steps array equals gold exactly
"""
from __future__ import annotations

import csv
import json
import sys
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
FIELDS = ("phase", "system_id", "resource_id", "operation", "state", "evidence_id")


def load(path: Path) -> dict:
    with open(path, encoding="utf-8") as f:
        return {r["case_id"]: (r["deletion_status"], json.loads(r["steps_json"])) for r in csv.DictReader(f)}


def tup(s):
    return tuple(s.get(k, "") for k in FIELDS)


def evaluate(pred: dict, gold: dict, details: bool = False) -> dict:
    n = len(gold)
    st_ok = seq_ok = 0
    tp = fp = fn = 0
    field_hits = field_tot = 0
    res_hit = res_tot = 0
    per_case = []
    for cid, (gs, gsteps) in sorted(gold.items(), key=lambda x: int(x[0].split("_")[1])):
        ps, psteps = pred.get(cid, ("MISSING", []))
        st_ok += ps == gs
        g = Counter(tup(s) for s in gsteps)
        p = Counter(tup(s) for s in psteps)
        inter = sum((g & p).values())
        tp += inter
        fp += sum(p.values()) - inter
        fn += sum(g.values()) - inter
        exact = [tup(s) + (s["step_no"],) for s in gsteps] == [tup(s) + (s["step_no"],) for s in psteps]
        seq_ok += exact
        for gsx in gsteps:
            cands = [q for q in psteps if q["resource_id"] == gsx["resource_id"]] or psteps
            best = max((sum(q.get(k) == gsx[k] for k in FIELDS) for q in cands), default=0)
            field_hits += best
            field_tot += len(FIELDS)
        gres = {s["resource_id"] for s in gsteps}
        pres = {s["resource_id"] for s in psteps}
        res_hit += len(gres & pres)
        res_tot += len(gres)
        if details and (ps != gs or not exact):
            per_case.append((cid, gs, ps, sorted(g - p), sorted(p - g)))
    prec = tp / (tp + fp) if tp + fp else 1.0
    rec = tp / (tp + fn) if tp + fn else 1.0
    out = {
        "cases": n,
        "status_accuracy": st_ok / n,
        "step_precision": prec,
        "step_recall": rec,
        "step_f1": 2 * prec * rec / (prec + rec) if prec + rec else 0.0,
        "field_accuracy": field_hits / field_tot if field_tot else 1.0,
        "resource_recall": res_hit / res_tot if res_tot else 1.0,
        "sequence_accuracy": seq_ok / n,
    }
    if details:
        for cid, gs, ps, missing, extra in per_case:
            print(f"--- {cid}: gold={gs} pred={ps}")
            for m in missing:
                print("   missing:", m)
            for e in extra:
                print("   extra:  ", e)
    return out


def main():
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    pred_path = Path(args[0]) if args else ROOT / "code" / "evaluation" / "sample_predictions.csv"
    gold_path = Path(args[1]) if len(args) > 1 else ROOT / "dataset" / "sample_output.csv"
    res = evaluate(load(pred_path), load(gold_path), details="--details" in sys.argv)
    for k, v in res.items():
        print(f"{k:20} {v:.3f}" if isinstance(v, float) else f"{k:20} {v}")


if __name__ == "__main__":
    main()
