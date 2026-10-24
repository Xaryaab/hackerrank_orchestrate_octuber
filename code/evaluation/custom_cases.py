"""Participant-created evaluation cases (beyond the 32 public samples).

usage: python code/evaluation/custom_cases.py [--no-llm]

Each case runs the real pipeline (same evidence, linker, planner) on a request we constructed or
re-labelled by hand, and checks an expected outcome:

  A. identity   - sample requests re-submitted under a historical / intermediate e-mail must
                  produce exactly the public gold answer (metamorphic test with known labels).
  B. intent     - a real subject's request rewritten as a non-deletion request -> NOT_APPLICABLE;
                  a set of adversarial texts (negations, conditions, other languages, "verify my
                  earlier deletion") with hand labels.
  C. time       - the first INCOMPLETE sample audited one day before its finding evidence was captured
                  must not cite that (future) evidence.
  D. media      - test cases whose answer depends on screenshots / PDFs, labelled by reading the
                  images and documents ourselves (alias resolution, hallucination guard).
  E. invariants - over all 518 test outputs: no citation captured after audit_at, findings and
                  COMPLETE verifications cite evidence, remediation / INCOMPLETE verification do not.
"""
from __future__ import annotations

import csv
import json
import sys
from dataclasses import replace
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "code"))

from agent.intent import IntentClassifier  # noqa: E402
from agent.linker import Linker  # noqa: E402
from agent.llm import LLMClient  # noqa: E402
from agent.media import MediaExtractor  # noqa: E402
from agent.pipeline import Auditor  # noqa: E402

DATASET = ROOT / "dataset"


def load_gold() -> dict:
    with open(DATASET / "sample_output.csv", encoding="utf-8") as f:
        return {r["case_id"]: (r["deletion_status"], json.loads(r["steps_json"])) for r in csv.DictReader(f)}


def relink(aud: Auditor) -> None:
    aud.links = Linker(aud.ds, aud.obs, aud.ids).link_all()


def run_variant(aud: Auditor, case_id: str, **changes):
    """Audit a sample request with some fields changed (linking recomputed), then restore it."""
    idx = next(i for i, r in enumerate(aud.ds.samples) if r.case_id == case_id)
    original = aud.ds.samples[idx]
    aud.ds.samples[idx] = replace(original, **changes)
    relink(aud)
    try:
        aud.intent.prepare([aud.ds.samples[idx]])
        return aud.audit(aud.ds.samples[idx])
    finally:
        aud.ds.samples[idx] = original
        relink(aud)


def citation_times(aud: Auditor) -> dict[str, str]:
    t = dict(aud.ds.file_captured)
    t.update({a.activity_id: a.event_time for a in aud.ds.activity})
    with open(DATASET / "user_audit_log.csv", encoding="utf-8") as f:
        t.update({r["audit_id"]: r["event_time"] for r in csv.DictReader(f)})
    return t


INTENT_CASES = [
    ("Bitte löschen Sie mein Konto und alle personenbezogenen Daten endgültig.", True),
    ("Por favor, no borren mi cuenta; solo quiero una copia de mis pedidos.", False),
    ("Delete my account. Invoices you must keep for tax law can stay, everything else should go.", True),
    ("I already deleted my profile last week - please confirm nothing identifiable is left.", True),
    ("Please stop sending me newsletters, but do not delete anything else.", False),
    ("Merci de supprimer définitivement mon compte et mes données personnelles.", True),
    ("Can you update my delivery address? I am not asking you to remove my account.", False),
    ("アカウントと個人データを削除してください。", True),
]


def main() -> int:
    sys.stdout.reconfigure(encoding="utf-8")
    llm = None if "--no-llm" in sys.argv else LLMClient(ROOT / "code" / "cache" / "llm")
    media = MediaExtractor(llm, DATASET).extract_all()
    intent = IntentClassifier(llm)
    aud = Auditor(DATASET, intent, media)
    gold = load_gold()
    results: list[tuple[str, str, bool, str]] = []

    def check(cid: str, title: str, ok: bool, detail: str = "") -> None:
        results.append((cid, title, ok, detail))

    # ---- A. identity resolution through historical e-mails (gold-labelled)
    for case_id, email, note in [
        ("case_12", "minjun.kim90@gmail.com", "pre-2026-05-18 address of user_12"),
        ("case_10", "nadia.petrescu@yahoo.com", "intermediate address of user_10 (2025-12 .. 2026-04)"),
        ("case_10", "n.petrescu@yahoo.com", "current address of user_10 (request used the oldest)"),
    ]:
        r = run_variant(aud, case_id, email=email)
        gs, gsteps = gold[case_id]
        ok = r.status == gs and r.steps == gsteps
        check(f"A-{case_id}-{email.split('@')[0]}", f"{case_id} via {note}", ok,
              f"status {r.status} (gold {gs}), steps {'identical' if r.steps == gsteps else 'DIFFER'}")

    # ---- B. intent
    r = run_variant(aud, "case_09", request_text="Please send me a copy of all the personal data you hold. "
                                                  "I want to keep using my account.")
    check("B-case_09-access", "real subject, access request instead of deletion",
          r.status == "NOT_APPLICABLE" and r.steps == [], f"status {r.status}, {len(r.steps)} steps")
    intent.prepare([replace(aud.ds.samples[0], case_id=f"x{i}", request_text=t)
                    for i, (t, _) in enumerate(INTENT_CASES)])
    for i, (text, expected) in enumerate(INTENT_CASES):
        d = intent.classify(replace(aud.ds.samples[0], request_text=text))
        check(f"B-intent-{i + 1}", text[:60], d.is_deletion == expected,
              f"expected {'deletion' if expected else 'not deletion'}; {d.reason}")

    # ---- C. audit_at cut-off: audit the first INCOMPLETE sample one day before its first
    #         finding's evidence existed
    times = citation_times(aud)
    case_c = next(c for c, (gs, _) in sorted(gold.items(), key=lambda x: int(x[0][5:])) if gs == "INCOMPLETE")
    gs, gsteps = gold[case_c]
    first = next(s for s in gsteps if s["phase"] == "FINDING")
    cap = times[first["evidence_id"]]
    early = cap[:8] + f"{int(cap[8:10]) - 1:02d}" + cap[10:]
    r = run_variant(aud, case_c, audit_at=early)
    future = [s["evidence_id"] for s in r.steps if s["evidence_id"] and times.get(s["evidence_id"], "") > early]
    check(f"C-{case_c}-early-audit", f"{case_c} audited at {early} (before {first['evidence_id']} at {cap})",
          not future, f"status {r.status}; future citations: {future or 'none'}")

    # ---- D. media-dependent test cases, labelled by reading the screenshots / PDFs ourselves
    by_id = {r.case_id: r for r in aud.ds.requests}
    out = {c: aud.audit(by_id[c]) for c in ("case_61", "case_46", "case_33")}
    s61 = [(s["phase"], s["resource_id"], s["operation"], s["evidence_id"]) for s in out["case_61"].steps]
    check("D-case_61", "object_19 shown as delivery_confirmation_19.pdf in storage browser (evidence_46)",
          out["case_61"].status == "INCOMPLETE"
          and ("FINDING", "object_19", "REPORT_REMAINING_RECORD", "evidence_46") in s61, str(s61[:1]))
    s46 = [(s["resource_id"], s["operation"], s["evidence_id"]) for s in out["case_46"].steps]
    check("D-case_46", "tombstone_17 shown as user_17 'Retained reference' without identifiers (evidence_42)",
          out["case_46"].status == "COMPLETE"
          and ("tombstone_17", "VERIFY_RECORD_REDACTED", "evidence_42") in s46, str(s46))
    s33 = {s["resource_id"] for s in out["case_33"].steps}
    f33 = [(s["resource_id"], s["evidence_id"]) for s in out["case_33"].steps if s["phase"] == "FINDING"]
    check("D-case_33", "statement NST-2026-0718-1842 = export_1842 (PDF evidence_18); no invented account_18",
          ("export_1842", "evidence_18") in f33 and "account_18" not in s33, f"findings {f33}")

    # ---- E. contract / temporal invariants over the full test output
    with open(ROOT / "output.csv", encoding="utf-8") as f:
        rows = list(csv.DictReader(f))
    audit_at = {r.case_id: r.audit_at for r in aud.ds.requests}
    viol = []
    for row in rows:
        st, steps = row["deletion_status"], json.loads(row["steps_json"])
        for s in steps:
            ev, ph = s["evidence_id"], s["phase"]
            if ev and times.get(ev, "9999") > audit_at[row["case_id"]]:
                viol.append(f"{row['case_id']}: {ev} after audit_at")
            needs = ph == "FINDING" or (ph == "VERIFICATION" and st == "COMPLETE")
            if needs != bool(ev):
                viol.append(f"{row['case_id']}: step {s['step_no']} {ph} evidence_id={ev!r}")
    check("E-invariants", f"temporal + citation invariants over {len(rows)} test rows", not viol,
          f"{len(viol)} violations {viol[:3]}")

    passed = sum(ok for *_, ok, _ in results)
    print(f"| id | case | result | detail |\n|---|---|---|---|")
    for cid, title, ok, detail in results:
        print(f"| {cid} | {title} | {'PASS' if ok else 'FAIL'} | {detail} |")
    print(f"\n{passed}/{len(results)} custom checks passed")
    return 0 if passed == len(results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
