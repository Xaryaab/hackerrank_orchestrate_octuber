# HANDOFF — session continuity notes

Read this first in any new Claude Code session (it is imported by `CLAUDE.md`).
Also obey `AGENTS.md` (append to `log.txt` every turn: tool=Claude Code, model=<exact runtime id>).

## Goal
Ghost Data Still Exists: for each of 518 rows in `dataset/deletion_requests.csv` write
`output.csv` (repo root) = `case_id,deletion_status,steps_json`. Spec: `problem_statement.md`.
Must also ship `code/evaluation/evaluation_report.md` + `code/evaluation/usage_report.md`.

## How to run
```
.venv/Scripts/python code/main.py --split sample --no-llm      # 32 samples -> code/evaluation/sample_predictions.csv
.venv/Scripts/python code/evaluation/main.py --details         # grade vs dataset/sample_output.csv
.venv/Scripts/python code/main.py                              # test -> ./output.csv (+ usage_report.md)
.venv/Scripts/python code/tools/dossier.py case_11             # dev: everything linked to a case
```
venv at repo root `.venv` (pypdf, openai, python-dotenv). Python 3.14.
LLM: `.env` GEMINI_API_KEY (Google AI Studio FREE tier: 20 requests/day PER MODEL, some 3.x models
share/exhaust fast; gemini-3.8-flash queues ~2 min/call). Current: VISION_MODEL=gemini-2.5-flash
(images), TEXT_MODEL=gemini-3.5-flash-lite (intent + PDF text) = code defaults. All needed calls are cached in
`code/cache/llm/` (keyed on bare model ids, provider-independent) -> reruns need no key. Changing a prompt or model invalidates its cache entries.

## Architecture (code/agent/)
- `evidence.py`  parse every evidence file -> `Observation(file_id, captured_at, ts, resource_id, system, attrs)`;
                 `snapshot_coverage()` = systems a snapshot fully covers (absence-by-omission citations).
- `identity.py`  current + historical emails/phones/hashes (sha256(email)) per user; key extraction from attrs.
- `linker.py`    case -> resources: direct identifier links + deletion_worker activity windows around
                 reported_at, resolved globally (account result==status mapping; unlinked records
                 assigned by small exact matching using family, time direction, bundle order).
                 Repeat subjects (same user, several cases) split by time.
- `state.py`     observation -> signals (exists, pii, enabled, restorable).
- `planner.py`   category/treatment per rules, latest state <= audit_at, steps, ordering.
- `pipeline.py`  Auditor: intent -> links -> states -> steps -> status/citations.
- `intent.py`    LLM batch classification of distinct texts + negation-aware rules fallback.
- `media.py`     LLM extraction of PNG (vision) / PDF (pypdf text) evidence -> facts (cached).
- `llm.py`       OpenAI-compatible client (Gemini direct if GEMINI_API_KEY, else OpenRouter); fails fast on
                 401/402/403, per-model skip on daily 429, disk cache `code/cache/llm/`, usage ledger. `usage.py` writes report.
- `validate.py`  output contract checks.

## Status
- [x] Repo cloned, spec read, dataset profiled, plan agreed
- [x] Evidence parsing (all text formats), identity, linking, state, policy, planner, validator
- [x] Local grader; samples (no LLM): status 0.969, step F1 0.974, exact sequence 0.844
- [x] LLM media extraction + LLM intent (Gemini); samples: status 1.000, step F1 0.987, seq 0.875
- [x] Full test run -> output.csv (355 INCOMPLETE / 153 COMPLETE / 10 N/A), usage_report.md, validator clean
- [x] usage_report priced (gemini-3.5-flash-lite 0.30/2.50 per 1M, user-supplied)
- [x] evaluation_report.md (17 custom checks in code/evaluation/custom_cases.py), README, code/requirements.txt
- [x] ultrareview: 10 findings fixed (see evaluation_report.md); outputs unchanged; no-key run reproduces output.csv
- [ ] code.zip

## Known unrecoverable / open sample misses
- case_04 account_004: no activity or evidence mentions it (naming convention only) -> skipped on purpose.
- case_18/20 audit_record_6645/5569: absent from all participant data. 85 test cases similar.
- case_23: two FAILED unlinked accounts in same batch (410 vs 7015) -> true tie.
- case_07: fixed by PDF extraction (support_001 in evidence_58).

## Findings log
- Sample answers: 22 INCOMPLETE, 10 COMPLETE, 0 NOT_APPLICABLE. Test: 10 non-deletion texts (case_533..541, use negations).
- Every deletion case verifies primary account ABSENT + security audit_record PRESENT.
- Ordering within a phase = operation table order, then system_id, then resource_id (string).
- COMPLETE: every verification cites evidence (latest explicit observation <= audit_at, else latest
  snapshot <= audit_at covering that system). INCOMPLETE: findings cite evidence; others "".
- Findings require evidence captured <= audit_at; linking may use any evidence (even later).
- Account deletion activity reported_result == reported_status (FAILED=NOT_STARTED, PARTIAL=PARTIALLY_COMPLETE, SUCCESS=COMPLETE).
- Primary account deleted at reported_at-(45..75min) ("submitted primary profile"); hand-made cases
  use "Target resource: X" at reported_at +-20min. "Policy action completed" before reported_at,
  "received the final result" after.
- Snapshot bundles (records_assurance) list records in subject (user number) order.
- Support: attachment/legacy contact index -> DELETE; message -> REDACT. Analytics: ANONYMIZE,
  except hash-only contact record (contact_lookup) -> DELETE. Tombstone -> REDACT (keep user ref).
- Intent prompt must say "verify/confirm/audit my earlier deletion" = deletion (samples 21,25-28).
- Media screens/documents show ALIASES, not resource ids. `evidence.catalog_aliases` reads
  LINK_RESOURCE_IDENTITY rows "X (alias)": tombstone_17 (user_17) [evidence_42],
  object_19 (delivery_confirmation_19.pdf) [evidence_46], export_1842 (NST-2026-0718-1842) [evidence_18 PDF].
  Fact alias match needs same system; document-style aliases (with - or .) also matched in PDF text.
- PDF facts are grounded: resource ids not literally in the PDF text are dropped (model invented account_18).
