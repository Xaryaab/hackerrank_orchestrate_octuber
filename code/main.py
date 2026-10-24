"""Ghost Data Still Exists - deletion audit agent.

usage:
    python code/main.py                      # audit dataset/deletion_requests.csv -> ./output.csv
    python code/main.py --split sample       # audit the 32 sample requests -> code/evaluation/sample_predictions.csv
    python code/main.py --no-llm             # deterministic rules only (no API calls)
"""
from __future__ import annotations

import argparse
import csv
import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "code"))

from agent.intent import IntentClassifier  # noqa: E402
from agent.llm import LLMClient  # noqa: E402
from agent.media import MediaExtractor  # noqa: E402
from agent.pipeline import Auditor  # noqa: E402
from agent.validate import validate_rows  # noqa: E402
from agent.usage import write_usage_report  # noqa: E402

DATASET = ROOT / "dataset"
CACHE = ROOT / "code" / "cache" / "llm"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--split", choices=["test", "sample"], default="test")
    ap.add_argument("--out", type=Path, default=None)
    ap.add_argument("--no-llm", action="store_true", help="disable model calls (rules + cached facts only)")
    ap.add_argument("--trace", type=Path, default=None, help="write per-case reasoning trace (jsonl)")
    args = ap.parse_args()

    t0 = time.time()
    llm = None if args.no_llm else LLMClient(CACHE)
    media = MediaExtractor(llm, DATASET).extract_all()
    intent = IntentClassifier(llm)
    auditor = Auditor(DATASET, intent, media)
    reqs = auditor.ds.requests if args.split == "test" else auditor.ds.samples
    intent.prepare(reqs)

    results = [auditor.audit(r) for r in reqs]
    results.sort(key=lambda r: int(r.case_id.split("_")[1]))
    rows = [r.row() for r in results]
    errors = validate_rows(rows, [r.case_id for r in reqs])
    if errors:
        print(f"VALIDATION WARNINGS ({len(errors)}):", *errors[:20], sep="\n  ")

    out = args.out or (ROOT / "output.csv" if args.split == "test" else ROOT / "code" / "evaluation" / "sample_predictions.csv")
    with open(out, "w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=["case_id", "deletion_status", "steps_json"])
        w.writeheader()
        w.writerows(rows)
    if args.trace:
        with open(args.trace, "w", encoding="utf-8") as f:
            for r in results:
                f.write(json.dumps({"case_id": r.case_id, "status": r.status, **r.trace}, default=str) + "\n")

    counts = {}
    for r in results:
        counts[r.status] = counts.get(r.status, 0) + 1
    print(f"wrote {len(rows)} rows to {out} in {time.time() - t0:.1f}s  {counts}")
    if llm is not None and args.split == "test":
        write_usage_report(llm.ledger, len(reqs), ROOT / "code" / "evaluation" / "usage_report.md",
                           llm.provider)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
