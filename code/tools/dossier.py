"""Dev tool: print every observation directly linked to a case's identity.

usage: python code/tools/dossier.py case_11 [case_12 ...]
"""
import csv
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "code"))

from agent.evidence import load_all  # noqa: E402
from agent.identity import IdentityIndex, obs_keys  # noqa: E402

DS = ROOT / "dataset"
NOISE = {"instance", "pod", "region", "level", "severity", "logger", "_ts", "attempt", "duration_ms",
         "took_ms", "shard", "pool", "wait_ms", "dependency", "retry_in_ms", "reason", "route", "events",
         "ctx_path", "ctx_service", "trace_id", "service", "metrics.sessions_30d", "sessions_30d",
         "metrics.products_viewed_30d", "products_viewed_30d", "metrics.orders_completed_90d",
         "orders_completed_90d", "metrics.revenue_90d_usd", "revenue_90d_usd", "recent_activity"}


def main(case_ids):
    reqs = {}
    for fn in ("sample_deletion_requests.csv", "deletion_requests.csv"):
        with open(DS / fn, encoding="utf-8") as f:
            for r in csv.DictReader(f):
                reqs[r["case_id"]] = r
    answers = {}
    with open(DS / "sample_output.csv", encoding="utf-8") as f:
        for r in csv.DictReader(f):
            answers[r["case_id"]] = r
    ids = IdentityIndex(DS)
    obs = load_all(DS)
    activity = list(csv.DictReader(open(DS / "system_activity.csv", encoding="utf-8")))
    for cid in case_ids:
        q = reqs[cid]
        uid = ids.resolve_email(q["email"])
        print("#" * 100)
        print(cid, q["email"], "->", uid, "| req", q["requested_at"], "| rep", q["reported_status"], q["reported_at"], "| audit", q["audit_at"])
        print("  text:", q["request_text"])
        if not uid:
            continue
        ident = ids.users[uid]
        print("  identity:", ident.name, sorted(ident.emails), sorted(ident.phones), "audit:", ident.audit_ids)
        keys = ident.keys()
        linked = [o for o in obs if obs_keys(o.attrs) & keys]
        rids = sorted({o.resource_id for o in linked})
        for o in sorted(linked, key=lambda o: (o.resource_id, o.ts)):
            a = {k: v for k, v in o.attrs.items() if k not in NOISE and v not in (None, "null")}
            flag = "  " if o.captured_at <= q["audit_at"] else "!!"
            print(f"  {flag}{o.resource_id:18} {o.file_id} cap={o.captured_at} ts={o.ts} {o.source:40} {str(a)[:260]}")
        for a in activity:
            if a["resource_id"] in rids or uid in a["details"] or q["email"].lower() in a["details"].lower():
                print("    ACT", a["activity_id"], a["event_time"], a["event_type"], a["source_system"], a["target_system"], a["resource_id"], a["reported_result"], "|", a["details"][:140])
        if cid in answers:
            import json
            print("  ANSWER:", answers[cid]["deletion_status"])
            for s in json.loads(answers[cid]["steps_json"]):
                print("     ", s["step_no"], s["phase"][:4], s["system_id"], s["resource_id"], s["operation"], s["evidence_id"])


if __name__ == "__main__":
    main(sys.argv[1:])
