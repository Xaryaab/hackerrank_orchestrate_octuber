"""Policy application and ordered step generation for one case."""
from __future__ import annotations

from dataclasses import dataclass

from .evidence import Observation, resource_prefix, system_for
from .state import Signals, interpret

# ------------------------------------------------------------------ contract tables
FINDING_ORDER = ["REPORT_REMAINING_RECORD", "REPORT_EXPOSED_PII", "REPORT_ACTIVE_DATA_FLOW", "REPORT_RESTORE_RISK"]
REMEDIATION_ORDER = ["DISABLE_DATA_FLOW", "BLOCK_RESTORE", "DELETE_RECORD", "REDACT_RECORD", "ANONYMIZE_RECORD"]
VERIFICATION_ORDER = ["VERIFY_RECORD_ABSENT", "VERIFY_RECORD_PRESENT", "VERIFY_RECORD_REDACTED",
                      "VERIFY_RECORD_ANONYMIZED", "VERIFY_DATA_FLOW_DISABLED", "VERIFY_RESTORE_BLOCKED"]
PHASE_ORDER = {"FINDING": 0, "REMEDIATION": 1, "VERIFICATION": 2}
OP_RANK = {op: i for i, op in enumerate(FINDING_ORDER + REMEDIATION_ORDER + VERIFICATION_ORDER)}
STATE_OF = {
    "REPORT_REMAINING_RECORD": "PRESENT", "REPORT_EXPOSED_PII": "PRESENT",
    "REPORT_ACTIVE_DATA_FLOW": "ACTIVE", "REPORT_RESTORE_RISK": "ACTIVE",
    "DISABLE_DATA_FLOW": "DISABLED", "BLOCK_RESTORE": "BLOCKED", "DELETE_RECORD": "ABSENT",
    "REDACT_RECORD": "REDACTED", "ANONYMIZE_RECORD": "ANONYMIZED",
    "VERIFY_RECORD_ABSENT": "ABSENT", "VERIFY_RECORD_PRESENT": "PRESENT", "VERIFY_RECORD_REDACTED": "REDACTED",
    "VERIFY_RECORD_ANONYMIZED": "ANONYMIZED", "VERIFY_DATA_FLOW_DISABLED": "DISABLED",
    "VERIFY_RESTORE_BLOCKED": "BLOCKED",
}

# treatment -> (finding, remediation, verification)
TREATMENT = {
    "DELETE": ("REPORT_REMAINING_RECORD", "DELETE_RECORD", "VERIFY_RECORD_ABSENT"),
    "REDACT": ("REPORT_EXPOSED_PII", "REDACT_RECORD", "VERIFY_RECORD_REDACTED"),
    "ANONYMIZE": ("REPORT_EXPOSED_PII", "ANONYMIZE_RECORD", "VERIFY_RECORD_ANONYMIZED"),
    "RETAIN": (None, None, "VERIFY_RECORD_PRESENT"),
    "FLOW": ("REPORT_ACTIVE_DATA_FLOW", "DISABLE_DATA_FLOW", "VERIFY_DATA_FLOW_DISABLED"),
    "BACKUP": ("REPORT_RESTORE_RISK", "BLOCK_RESTORE", "VERIFY_RESTORE_BLOCKED"),
}


def category(resource_id: str, history: list[Observation]) -> tuple[str, str]:
    """(data_category from deletion_rules.csv, treatment)."""
    p = resource_prefix(resource_id)
    events = {str(o.attrs.get("event")) for o in history}
    rtypes = {str(o.attrs.get("record_type")) for o in history}
    if p == "account":
        return "USER_PROFILE", "DELETE"
    if p == "search":
        return "SEARCH_INDEX_ENTRY", "DELETE"
    if p == "cache":
        return "CACHE_ENTRY", "DELETE"
    if p == "export":
        return "USER_EXPORT", "DELETE"
    if p == "marketing":
        return "MARKETING_PROFILE", "DELETE"
    if p == "support" or p == "object":
        attach = ("SUPPORT_ATTACHMENT" in rtypes or "attachment_pii_scan" in events
                  or any(o.attrs.get("ctx_collection") == "ticket_attachments" or o.attrs.get("file_name")
                         or o.attrs.get("source") == "legacy_contact_index" for o in history)
                  or p == "object")
        if attach:
            return "SUPPORT_ATTACHMENT", "DELETE"
        return "SUPPORT_MESSAGE", "REDACT"
    if p == "analytics":
        if "contact_lookup_completed" in events:
            return "SECONDARY_IDENTIFIER", "DELETE"
        return "ANALYTICS_PROFILE", "ANONYMIZE"
    if p == "telemetry":
        return "PRODUCT_TELEMETRY", "ANONYMIZE"
    if p == "email_delivery":
        return "EMAIL_DELIVERY_LOG", "REDACT"
    if p == "audit_record":
        return "SECURITY_AUDIT_LOG", "RETAIN"
    if p == "legal_hold":
        return "LEGAL_HOLD_RECORD", "RETAIN"
    if p == "backup":
        return "BACKUP_ARCHIVE", "BACKUP"
    if p == "data_flow":
        return "DATA_FLOW", "FLOW"
    if p == "tombstone":
        return "ACCOUNT_TOMBSTONE", "REDACT"
    return "UNKNOWN", "DELETE"


@dataclass
class ResourceState:
    resource_id: str
    system: str
    category: str
    treatment: str
    signals: Signals
    evidence_id: str        # file proving the state at audit_at ("" if none)
    observed: bool          # any evidence captured <= audit_at


def resolve_state(resource_id: str, history: list[Observation], audit_at: str) -> ResourceState:
    cat, treat = category(resource_id, history)
    usable = [o for o in history if o.captured_at <= audit_at and o.ts <= audit_at]
    system = next((o.system for o in history if o.system and o.system != "unknown"), system_for(resource_id))
    if not usable:
        return ResourceState(resource_id, system, cat, treat, Signals(), "", False)
    latest_ts = max(o.ts for o in usable)
    # all copies seen at the latest instant (primary + replicas of one snapshot)
    group = [o for o in usable if o.ts == latest_ts]
    sig = Signals()
    for o in group:
        s = interpret(o)
        for f in ("exists", "pii", "enabled", "restorable"):
            v = getattr(s, f)
            if v is None:
                continue
            cur = getattr(sig, f)
            setattr(sig, f, v if cur is None else (cur or v))
    # signals that the latest observation did not carry: inherit from older copies
    for o in sorted(usable, key=lambda o: o.ts, reverse=True):
        s = interpret(o)
        for f in ("enabled", "restorable", "pii"):
            if getattr(sig, f) is None and getattr(s, f) is not None and sig.exists is not False:
                setattr(sig, f, getattr(s, f))
    ev = max(group, key=lambda o: (o.captured_at, o.file_id)).file_id
    return ResourceState(resource_id, system, cat, treat, sig, ev, True)


def steps_for(rs: ResourceState) -> list[dict]:
    """Return (finding, remediation, verification) step dicts for one resource."""
    find, fix, verify = TREATMENT[rs.treatment]
    s = rs.signals
    violation = False
    if rs.treatment == "DELETE":
        violation = s.exists is True
    elif rs.treatment in ("REDACT", "ANONYMIZE"):
        violation = s.exists is not False and s.pii is True
    elif rs.treatment == "FLOW":
        violation = s.enabled is True
    elif rs.treatment == "BACKUP":
        violation = s.restorable is True
    out = []
    if violation and find:
        out.append({"phase": "FINDING", "operation": find, "evidence_id": rs.evidence_id})
        out.append({"phase": "REMEDIATION", "operation": fix, "evidence_id": ""})
    out.append({"phase": "VERIFICATION", "operation": verify, "evidence_id": None if violation else rs.evidence_id,
                "_future": violation})
    for st in out:
        st["system_id"] = rs.system
        st["resource_id"] = rs.resource_id
        st["state"] = STATE_OF[st["operation"]]
    return out


def order_and_number(steps: list[dict]) -> list[dict]:
    steps = sorted(steps, key=lambda s: (PHASE_ORDER[s["phase"]], OP_RANK[s["operation"]], s["system_id"], s["resource_id"]))
    out = []
    for i, s in enumerate(steps, 1):
        out.append({"step_no": i, "phase": s["phase"], "system_id": s["system_id"], "resource_id": s["resource_id"],
                    "operation": s["operation"], "state": s["state"], "evidence_id": s["evidence_id"] or ""})
    return out
