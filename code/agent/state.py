"""Interpretation of a single observation into normalized state signals.

Signals (each True / False / None=unknown):
  exists      - the record is present in the observed copy
  pii         - prohibited direct identifiers are still present
  enabled     - (data flows) the process can still copy data
  restorable  - (backups) the archive can still be restored
"""
from __future__ import annotations

import re
from dataclasses import dataclass

from .evidence import Observation, resource_prefix
from .identity import EMAIL_RE

NULLS = (None, "", "null", "NULL", "None")
TRUE = ("true", "1", "yes", "allow", "allowed")
FALSE = ("false", "0", "no", "deny", "denied")

# fields that are direct identifiers when non-null
PII_FIELDS = ("email", "phone", "full_name", "name", "display_name", "email_hash", "requester_email",
              "requester_name", "requester_phone", "owner_email", "owner_phone", "customer_email",
              "uploaded_by", "matched_value")
# analytics "still attributable to a user" also covers stable references
ATTRIBUTION_FIELDS = ("user_id", "customer_reference")


def _b(v):
    if v in NULLS:
        return None
    if isinstance(v, bool):
        return v
    s = str(v).strip().lower()
    if s in TRUE:
        return True
    if s in FALSE:
        return False
    return None


@dataclass
class Signals:
    exists: bool | None = None
    pii: bool | None = None
    enabled: bool | None = None
    restorable: bool | None = None


def interpret(o: Observation) -> Signals:
    a = o.attrs
    s = Signals()
    prefix = resource_prefix(o.resource_id)
    result = str(a.get("result") or "").upper()
    event = str(a.get("event") or "")

    # ---- existence
    status = a.get("status_code", a.get("http_status"))
    if status not in NULLS:
        s.exists = str(status) == "200"
    rows = a.get("rows")
    if rows not in NULLS and s.exists is None:
        s.exists = str(rows) != "0"
    if result in ("NOT_FOUND", "MISS", "ABSENT", "DELETED"):
        s.exists = False
    elif result in ("FOUND", "IDENTIFIABLE", "ANONYMIZED", "REDACTED", "ACTIVE", "BLOCKED",
                    "DISABLED", "HIT", "PRESENT", "RETAINED"):
        s.exists = True
    if event == "index_query_completed":
        s.exists = result == "HIT" if result else s.exists
    total = a.get("hits.total", a.get("total_hits"))
    if total not in NULLS:
        s.exists = str(total) != "0"
    for k in ("download_available", "available", "archive_present"):
        if _b(a.get(k)) is not None:
            s.exists = _b(a.get(k))
    if s.exists is None:
        # a row in a snapshot / a completed read of the record is a present copy unless the
        # observation explicitly reports absence (404, rows=0, NOT_FOUND, miss) - handled above
        s.exists = True
    status_txt = str(a.get("account_status") or "").upper()
    if prefix == "account" and status_txt == "DELETED":
        s.exists = False

    # ---- direct identifiers
    if result == "IDENTIFIABLE":
        s.pii = True
    elif result in ("ANONYMIZED", "REDACTED"):
        s.pii = False
    di = a.get("direct_identifiers")
    if di not in NULLS:
        s.pii = str(di) != "0"
    if s.pii is None and s.exists:
        fields_seen = [f for f in PII_FIELDS if f in a or ("profile." + f) in a or ("document." + f) in a]
        vals = [a.get(f) for f in PII_FIELDS if a.get(f) not in NULLS]
        text = a.get("message_text") or ""
        if text and text not in NULLS:
            if "[REDACTED]" in text and not EMAIL_RE.search(text):
                text_pii = False
            else:
                text_pii = bool(EMAIL_RE.search(text) or re.search(r"\+\d[\d\s()-]{6,}", text))
        else:
            text_pii = None
        if prefix == "analytics" and any(a.get(f) not in NULLS for f in ATTRIBUTION_FIELDS):
            vals.append("attrib")
        if vals or text_pii:
            s.pii = True
        elif fields_seen or text_pii is False:
            s.pii = False

    # ---- data flows
    if prefix == "data_flow":
        for k in ("enabled", "scheduler_registered"):
            if _b(a.get(k)) is not None:
                s.enabled = _b(a.get(k))
                break
        if s.enabled is None and result in ("ACTIVE", "DISABLED"):   # explicit flags win over the label
            s.enabled = result == "ACTIVE"
        lds = str(a.get("last_delivery_status") or "").lower()
        if s.enabled is None and lds:
            s.enabled = lds != "stopped"

    # ---- backups
    if prefix == "backup":
        if _b(a.get("restore_allowed")) is not None:
            s.restorable = _b(a.get("restore_allowed"))
        if a.get("decision") not in NULLS:
            s.restorable = str(a["decision"]).lower() in ("allow", "allowed", "permit")
        if _b(a.get("eligible")) is not None:
            s.restorable = bool(_b(a.get("eligible"))) and not bool(_b(a.get("deletion_block")))
        if s.restorable is None and result in ("ACTIVE", "BLOCKED"):  # explicit flags win over the label
            s.restorable = result == "ACTIVE"
    return s
