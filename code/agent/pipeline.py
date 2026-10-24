"""End-to-end deletion audit: request -> identity -> links -> state -> policy -> steps."""
from __future__ import annotations

import json
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path

from .data import Dataset, Request
from .evidence import load_all, resource_prefix, snapshot_coverage
from .identity import IdentityIndex
from .intent import IntentClassifier
from .linker import CaseLinks, Linker
from .planner import ResourceState, order_and_number, resolve_state, steps_for

POLICY_PREFIXES = {"account", "search", "cache", "export", "marketing", "support", "object", "analytics",
                   "telemetry", "email_delivery", "audit_record", "legal_hold", "backup", "data_flow", "tombstone"}


@dataclass
class CaseResult:
    case_id: str
    status: str
    steps: list
    trace: dict = field(default_factory=dict)   # explanation for debugging / reports

    def row(self) -> dict:
        return {"case_id": self.case_id, "deletion_status": self.status,
                "steps_json": json.dumps(self.steps, ensure_ascii=False, separators=(",", ":"))}


class Auditor:
    def __init__(self, dataset_dir: Path, intent: IntentClassifier, media_facts: dict | None = None):
        self.ds = Dataset(dataset_dir)
        self.ids = IdentityIndex(dataset_dir)
        self.obs = load_all(dataset_dir, media_facts)
        self.coverage = snapshot_coverage(dataset_dir)
        self.by_resource = defaultdict(list)
        for o in self.obs:
            self.by_resource[o.resource_id].append(o)
        self.links = Linker(self.ds, self.obs, self.ids).link_all()
        self.intent = intent

    # ------------------------------------------------------------------
    def fallback_evidence(self, system: str, audit_at: str) -> str:
        """Latest snapshot captured <= audit_at that covers `system` (proves absence by omission)."""
        best = None
        for fid, systems in self.coverage.items():
            cap = self.ds.file_captured[fid]
            if system in systems and cap <= audit_at and (best is None or cap > best[0]):
                best = (cap, fid)
        return best[1] if best else ""

    def audit(self, req: Request) -> CaseResult:
        decision = self.intent.classify(req)
        if not decision.is_deletion:
            return CaseResult(req.case_id, "NOT_APPLICABLE", [], {"intent": decision.reason})
        cl: CaseLinks = self.links[req.case_id]
        states: list[ResourceState] = []
        for rid in sorted(cl.resources):
            if resource_prefix(rid) not in POLICY_PREFIXES:
                continue
            states.append(resolve_state(rid, self.by_resource.get(rid, []), req.audit_at))
        raw = []
        for rs in states:
            raw += steps_for(rs)
        incomplete = any(s["phase"] == "FINDING" for s in raw)
        for s in raw:
            if s["phase"] != "VERIFICATION":
                continue
            if incomplete:
                s["evidence_id"] = ""
            elif not s["evidence_id"]:
                s["evidence_id"] = self.fallback_evidence(s["system_id"], req.audit_at)
        steps = order_and_number(raw)
        status = "INCOMPLETE" if incomplete else "COMPLETE"
        trace = {"user_id": cl.user_id, "account": cl.account, "direct": sorted(cl.direct),
                 "workflow": sorted(cl.workflow), "intent": decision.reason,
                 "states": {rs.resource_id: (rs.category, rs.treatment, vars(rs.signals), rs.evidence_id) for rs in states}}
        return CaseResult(req.case_id, status, steps, trace)
