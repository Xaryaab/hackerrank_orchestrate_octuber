"""Output contract validation (run before writing output.csv)."""
from __future__ import annotations

import json

from .planner import OP_RANK, PHASE_ORDER, STATE_OF

PHASE_OF = {}
for op in ("REPORT_REMAINING_RECORD", "REPORT_EXPOSED_PII", "REPORT_ACTIVE_DATA_FLOW", "REPORT_RESTORE_RISK"):
    PHASE_OF[op] = "FINDING"
for op in ("DISABLE_DATA_FLOW", "BLOCK_RESTORE", "DELETE_RECORD", "REDACT_RECORD", "ANONYMIZE_RECORD"):
    PHASE_OF[op] = "REMEDIATION"
for op in STATE_OF:
    PHASE_OF.setdefault(op, "VERIFICATION")
KEYS = {"step_no", "phase", "system_id", "resource_id", "operation", "state", "evidence_id"}


def validate_rows(rows: list[dict], expected_ids: list[str]) -> list[str]:
    errs = []
    ids = [r["case_id"] for r in rows]
    if sorted(ids) != sorted(expected_ids) or len(set(ids)) != len(ids):
        errs.append("case_id set mismatch or duplicates")
    if ids != sorted(ids, key=lambda c: int(c.split("_")[1])):
        errs.append("rows not sorted by case_id")
    for r in rows:
        cid, status = r["case_id"], r["deletion_status"]
        try:
            steps = json.loads(r["steps_json"])
        except json.JSONDecodeError:
            errs.append(f"{cid}: steps_json is not JSON")
            continue
        if status not in ("COMPLETE", "INCOMPLETE", "NOT_APPLICABLE"):
            errs.append(f"{cid}: bad status {status}")
        if status == "NOT_APPLICABLE" and steps:
            errs.append(f"{cid}: NOT_APPLICABLE must have []")
        prev = None
        for i, s in enumerate(steps, 1):
            if set(s) != KEYS:
                errs.append(f"{cid}: step keys {sorted(s)}")
            if s.get("step_no") != i:
                errs.append(f"{cid}: step_no not consecutive")
            op = s.get("operation")
            if op not in STATE_OF or STATE_OF[op] != s.get("state") or PHASE_OF[op] != s.get("phase"):
                errs.append(f"{cid}: bad op/state/phase {op}/{s.get('state')}/{s.get('phase')}")
                continue
            key = (PHASE_ORDER[s["phase"]], OP_RANK[op], s["system_id"], s["resource_id"])
            if prev and key < prev:
                errs.append(f"{cid}: steps out of order at {i}")
            prev = key
            if status == "COMPLETE" and (s["phase"] != "VERIFICATION" or not s["evidence_id"]):
                errs.append(f"{cid}: COMPLETE step {i} must be evidence-backed verification")
            if status == "INCOMPLETE" and s["phase"] != "FINDING" and s["evidence_id"]:
                errs.append(f"{cid}: INCOMPLETE future step {i} must have empty evidence_id")
            if status == "INCOMPLETE" and s["phase"] == "FINDING" and not s["evidence_id"]:
                errs.append(f"{cid}: finding {i} lacks evidence")
        if status == "INCOMPLETE" and not any(s["phase"] == "FINDING" for s in steps):
            errs.append(f"{cid}: INCOMPLETE without findings")
    return errs
