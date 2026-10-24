# Evaluation Report — Ghost Data Still Exists

This report evaluates the deletion-audit agent in `code/`. Every number here can be reproduced
with the commands in [Reproducing](#reproducing). It uses only participant-facing data under `dataset/`.

## 1. How we evaluate

| Set | Size | Labels | What it measures |
|---|---|---|---|
| Public samples | 32 requests | `dataset/sample_output.csv` (official) | status, step, field and sequence accuracy |
| Custom cases | 17 checks | built and hand-labelled by us (section 3) | identity history, intent, `audit_at` cut-off, media evidence, contract invariants |
| Test split | 518 requests | none (hidden) | contract validity, invariants, LLM impact, distribution |

The sample grader (`code/evaluation/main.py`) compares the predicted `steps_json` with the gold
answer on six fields: `phase, system_id, resource_id, operation, state, evidence_id`.

- **Case-level status accuracy**: the fraction of cases whose `deletion_status` matches exactly.
- **Step (row) precision / recall / F1**: steps are matched as whole tuples; `step_no` is ignored.
- **Field accuracy**: for each gold step, the share of its six fields matched by the best
  predicted step for the same resource.
- **Resource recall**: the fraction of gold `resource_id`s that the prediction references at all.
- **Sequence accuracy**: the fraction of cases whose full step array, including `step_no` and
  order, equals the gold array exactly.

## 2. Results on the 32 public samples

| Metric | Rules only (`--no-llm`) | **Final agent (LLM intent + media)** |
|---|---:|---:|
| Case-level status accuracy | 0.969 (31/32) | **1.000 (32/32)** |
| Step precision | 0.989 | **0.995** |
| Step recall | 0.959 | **0.979** |
| Step F1 | 0.974 | **0.987** |
| Field-level accuracy | 0.975 | **0.988** |
| Resource recall | 0.955 | **0.964** |
| Sequence accuracy (exact step array) | 0.844 (27/32) | **0.875 (28/32)** |

The LLM layer fixes case_07. That case's only proof that `support_001` remains is a PDF support
attachment (`evidence_58`), and only media extraction can read it.

### Remaining sample differences

Four cases differ from gold. In each one the deciding fact is missing from the participant data,
so we chose not to guess:

| Case | Difference | Why it is not recoverable |
|---|---|---|
| case_04 | missing `VERIFY_RECORD_ABSENT account_004` | No activity row or evidence file mentions `account_004`. Only the naming convention hints at it. Emitting resources nobody observed would add false positives on the test set. |
| case_18 | missing `VERIFY_RECORD_PRESENT audit_record_6645` | The record appears in no participant file. |
| case_20 | missing `VERIFY_RECORD_PRESENT audit_record_5569` | Same as case_18. |
| case_23 | `account_7015` predicted instead of `account_410` | Two unlinked accounts fail in the same deletion batch, and the evidence cannot tell them apart (a true tie). |

The status is correct in all four cases, and every other step in them matches gold.

## 3. Custom evaluation cases

The cases live in `code/evaluation/custom_cases.py` and run through the real pipeline: the same
evidence, linker and planner. Sample-based variants rebuild the case links after the change.

| ID | Case | Expected | Result |
|---|---|---|---|
| A-case_12-minjun.kim90 | Sample case_12 resubmitted under the address user_12 used before 2026-05-18 | Exactly the gold answer of case_12 | **PASS**: status and all steps identical |
| A-case_10-nadia.petrescu | Sample case_10 resubmitted under the *intermediate* address (2025-12 to 2026-04) | Exactly the gold answer of case_10 | **PASS** |
| A-case_10-n.petrescu | Sample case_10 resubmitted under the *current* address (the original used the oldest) | Exactly the gold answer of case_10 | **PASS** |
| B-case_09-access | A real subject's request rewritten as "send me a copy … I want to keep using my account" | `NOT_APPLICABLE`, `[]` | **PASS** |
| B-intent-1 | German: "Bitte löschen Sie mein Konto und alle personenbezogenen Daten endgültig." | deletion | **PASS** |
| B-intent-2 | Spanish negation: "Por favor, no borren mi cuenta; solo quiero una copia de mis pedidos." | not deletion | **PASS** |
| B-intent-3 | Conditional: "Delete my account. Invoices you must keep for tax law can stay …" | deletion | **PASS** |
| B-intent-4 | "I already deleted my profile last week — please confirm nothing identifiable is left." | deletion (an audit of the person's own erasure) | **PASS** |
| B-intent-5 | "Please stop sending me newsletters, but do not delete anything else." | not deletion | **PASS** |
| B-intent-6 | French: "Merci de supprimer définitivement mon compte et mes données personnelles." | deletion | **PASS** |
| B-intent-7 | "Can you update my delivery address? I am not asking you to remove my account." | not deletion | **PASS** |
| B-intent-8 | Japanese: "アカウントと個人データを削除してください。" | deletion | **PASS** |
| C-case_05-early-audit | Sample case_05 audited one day *before* its finding evidence (`evidence_54`) was captured | no citation of evidence captured after `audit_at` | **PASS**: no future citations |
| D-case_61 | Test case: storage browser screenshot `evidence_46` shows `delivery_confirmation_19.pdf`, which the catalog links to `object_19` | `INCOMPLETE`, `REPORT_REMAINING_RECORD object_19 evidence_46` | **PASS** |
| D-case_46 | Test case: identity screen `evidence_42` shows `user_17` as a "Retained reference" with no name, email or phone (`tombstone_17`) | `COMPLETE`, `VERIFY_RECORD_REDACTED tombstone_17 evidence_42` | **PASS** |
| D-case_33 | Test case: customer statement PDF `evidence_18` is `NST-2026-0718-1842`, which the catalog links to `export_1842`; the model also invented `account_18` | finding on `export_1842` citing `evidence_18`; no `account_18` | **PASS** |
| E-invariants | All 518 test rows | no citation captured after `audit_at`; findings and COMPLETE verifications cite evidence; remediation and INCOMPLETE verifications use `""` | **PASS**: 0 violations |

**17/17 custom checks pass.** We labelled the D cases ourselves by opening the screenshots and
PDFs. Those are the only test-set answers we can check, and none of them is hardcoded: they come
out of the general alias-resolution and grounding logic described in section 5.

## 4. Test split (518 requests)

| | Count |
|---|---:|
| INCOMPLETE | 355 |
| COMPLETE | 153 |
| NOT_APPLICABLE | 10 (all ten are the access, correction, export or opt-out texts case_533–542) |
| Total steps | 3,278 (718 findings, 718 remediations, 1,842 verifications) |
| Output-contract validation warnings | 0 |
| Invariant violations (section 3, E) | 0 |

Effect of the LLM layer on the test split, compared with `--no-llm`: 12 rows change, and 9 of
those change status. Every change traces back to evidence that exists only in screenshots or PDFs
(for example case_33, case_46 and case_61 above). Intent labels do not change: rules and LLM agree
on all 518 texts, including the 10 non-deletion requests.

## 5. Failures found and improvements made

These are listed in the order we hit them; each fix was re-graded on the samples.

| # | Failure found | Fix | Effect |
|---|---|---|---|
| 1 | The first rules-only pipeline could not see facts inside PNG/PDF evidence (case_07 `support_001`). | Added `media.py`, an LLM that reads each screenshot or PDF **once** and returns structured facts, cached on disk. | case_07 fixed. Status 0.969 → 1.000. |
| 2 | The LLM intent classifier labelled "verify my account was deleted" texts as non-deletion (samples case_21 and case_25–28, status 0.844). | The prompt now states that a request to verify, confirm or audit the person's own earlier deletion counts as a deletion case. | Status back to 1.000. Custom check B-intent-4 covers it. |
| 3 | Screens and documents show *aliases*, not resource IDs (`user_17`, `delivery_confirmation_19.pdf`, statement `NST-2026-0718-1842`). case_61 came out COMPLETE although the attachment was still stored. | `evidence.catalog_aliases` reads data-catalog `LINK_RESOURCE_IDENTITY` events ("`object_19 (delivery_confirmation_19.pdf)`") and maps a media fact to its resource when the alias **and the system** match. Document-style aliases are also matched in the PDF text. | case_61 → INCOMPLETE. case_46 and case_33 cite the right evidence. The 2 validation warnings went to 0. |
| 4 | The vision model invented `account_18` from a PDF that only says `user_18`. | **Grounding check**: a PDF fact is kept only if its identifier appears literally in the PDF text, which is extracted locally with pypdf. | Hallucinated finding removed (custom check D-case_33). |
| 5 | Alias matching on raw text would wrongly match `user_17` in a finance register that lists many customers. | Raw-text alias matching is limited to document-style aliases (containing `-` or `.`). Bare customer keys must match a fact from the same system. | No false links. |
| 6 | The rules fallback missed non-English negations ("no borren mi cuenta") and "I am not asking you to remove …". Our custom intent cases exposed it: the LLM was correct, the rules were not. | Added negation markers for es/pt/fr/de and "not asking to remove". | `--no-llm` now passes B-intent-2 and B-intent-7. Test output unchanged. |
| 7 | API problems: OpenRouter returned 402 (no credits); the free Gemini tier allows 20 requests per model per day, returns 503s, and `gemini-3.8-flash` queued about 2 minutes per call. Retries hung the run. | `llm.py` stops calling on 401/402/403, skips only the exhausted model on a daily-quota 429, backs off on rate limits, caps `max_tokens`, logs each failed attempt, and caches every response. | A full rerun now makes 0 API calls; total cost is about $0.07 (see `usage_report.md`). |

### Independent code review

A multi-agent cloud review (`/code-review ultra`, branch `agent-v1` against `main`) reported 10
robustness findings. We fixed all ten. After each fix we re-ran the sample grader (unchanged),
the test split (`output.csv` byte-identical) and the 17 custom checks (all pass).

| Area | Finding | Fix |
|---|---|---|
| `llm.py` | A non-object JSON reply (e.g. a bare array) was cached, and every later run crashed on `.get`. | Replies that aren't JSON objects are rejected as failures and never cached. Callers check item types. |
| `llm.py` | The cache key depended on the provider (`google/` prefix), so the committed cache missed without a Gemini key. | Model ids are normalized before keying; OpenRouter gets its prefix only in the API call. Defaults and `.env.example` now name the cached models. **Verified:** a run with no key, and one with only an OpenRouter key, both reproduce `output.csv` byte-for-byte. |
| `llm.py` | The cache write sat inside the retry block: a failed write meant paying for the call again and leaving a truncated file. | The write is now atomic (temp file + `os.replace`), happens outside the retry block and is best-effort. An unreadable cache entry counts as a miss. |
| `llm.py` | OpenRouter's daily-cap 429 ("free-models-per-day") wasn't detected, leading to hours of back-off. | A case-insensitive `per.?day` match, plus a circuit breaker that stops after 3 consecutive failed calls. |
| `evidence.py` | The model-supplied `observed_at` was used without parsing, risking crashes or wrong string ordering against `audit_at`. | It's normalized to `YYYY-MM-DDTHH:MM:SSZ`, falling back to the capture time. |
| `linker.py` | The exact assignment solver had no "leave unassigned" branch. If a component couldn't be fully assigned, it linked nothing at all. | Added a skip branch, scored like an implausible owner. |
| `intent.py` | When rules and LLM disagreed, any negation marker overrode the LLM, so "erase my account, but don't delete the invoices" became NOT_APPLICABLE. | A negation only wins a conflict when the text has no deletion wording outside the negated phrases. The rules-only mode stays conservative. |
| `pipeline.py` | The absence-by-omission snapshot was also cited for PRESENT, DISABLED and BLOCKED verifications on resources that were never observed. | That fallback is limited to `VERIFY_RECORD_ABSENT`. |
| `identity.py` | An email held by several users fell to the lowest `user_id`. | The address's current holder wins; otherwise the request stays unattributed rather than auditing another customer. (The dataset has no such collision today.) |
| `state.py` | A coarse `result` label (e.g. `ACTIVE`) overrode explicit `enabled` / `restore_allowed` flags from the same observation. | Explicit booleans win; the label is only a fallback. |

## 6. Known limitations

- **Records that appear nowhere in the data.** As in case_04, 18 and 20, about 85 test cases
  likely expect such records, and the agent will miss them. Emitting unobserved resources would
  trade those misses for false positives elsewhere.
- **Ties between unlinked accounts in the same failure batch** (case_23) are broken
  deterministically, so they may be wrong.
- **Screenshot extraction cannot be grounded** the way PDF text can. It relies on the vision
  model plus the requirement that the alias and the system match.

## Reproducing

```bash
python code/main.py --split sample                 # samples -> code/evaluation/sample_predictions.csv
python code/evaluation/main.py --details           # metrics in section 2 (right-hand column)
python code/main.py --split sample --no-llm        # then re-grade for the left-hand column
python code/evaluation/custom_cases.py             # section 3 (17 checks)
python code/main.py                                # test split -> ./output.csv + usage_report.md
```

LLM responses are cached in `code/cache/llm/`, so these commands reproduce the numbers above
without an API key.
