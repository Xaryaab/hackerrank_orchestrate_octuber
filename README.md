# Ghost Data Still Exists — Deletion-Audit Agent

HackerRank Orchestrate, October 2026. Challenge and submission page:
https://wwww.hackerrank.com/contests/hackerrank-orchestrate-october26/challenges/ghost-data-still-exists

For each request in `dataset/deletion_requests.csv`, the agent does four things:
1. It decides whether the person actually asked for deletion.
2. It rebuilds who they are: their current and historical emails, phone numbers and email hashes.
3. It finds every copy of their data across databases, replicas, logs, exports, indexes,
   backups, screenshots and documents, and works out each copy's real state at `audit_at`.
4. It applies `deletion_rules.csv` and writes `COMPLETE` / `INCOMPLETE` / `NOT_APPLICABLE`, with
   ordered findings, remediation and verification steps, to `output.csv`.

## Setup

Requires Python 3.11 or newer (developed on 3.14).

```bash
python -m venv .venv
# Windows: .venv\Scripts\activate      macOS/Linux: source .venv/bin/activate
pip install -r code/requirements.txt
```

**Optional LLM configuration:** copy `.env.example` to `.env` and set **one** provider key. The
code reads it from the environment and never commits it.

```ini
GEMINI_API_KEY=...            # Google AI Studio (used directly), or
OPENROUTER_API_KEY=...        # OpenRouter
TEXT_MODEL=gemini-3.5-flash-lite   # request intent + PDF text
VISION_MODEL=gemini-2.5-flash      # screenshots
```

Every model response used for the submitted `output.csv` is cached in `code/cache/llm/`. The
commands below therefore reproduce the submission **without any API key**. A key is only needed
if prompts, models or evidence change.

## Run

```bash
python code/main.py                          # 518 test requests  -> ./output.csv (+ code/evaluation/usage_report.md)
python code/main.py --split sample           # 32 public samples  -> code/evaluation/sample_predictions.csv
python code/evaluation/main.py --details     # grade the samples against dataset/sample_output.csv
python code/evaluation/custom_cases.py       # 20 custom evaluation checks
python code/main.py --no-llm                 # deterministic rules only (no model calls, no cache)
python code/tools/dossier.py case_11         # developer view: everything linked to one case
```

The full test run takes about 5 seconds once the cache is warm. Output is deterministic: rows
are sorted by `case_id`, steps follow a fixed order, and JSON is written compactly.

## How it works

```text
request ──► intent ──► identity ──► linking ──► state @ audit_at ──► policy + plan ──► validate ──► output.csv
              │                        ▲                ▲
            LLM (batched,          evidence parsers + media facts (LLM, once per file)
            cached) + rules
```

| Module (`code/agent/`) | Role |
|---|---|
| `intent.py` | Sends the distinct request texts to the LLM in batches of 80 (5 calls for 375 texts). A multilingual rules layer that understands negations cross-checks the result, and explicit negations such as "do not close the account" or "no borren" always win. |
| `identity.py` | Builds current plus historical emails and phones from `user_audit_log.csv`, and `sha256(email)` hashes, so a request under an old address resolves to the same `user_id`. |
| `evidence.py` | Parses every evidence format (SQL snapshots run in SQLite, JSON, logs, text records) into time-stamped `Observation`s. It also resolves data-catalog aliases and checks media facts against their source (see below). |
| `media.py` | Screenshots go to a vision model; PDFs are extracted locally with pypdf and sent as text. Each file is read once (15 calls in total) into structured facts. A batched vision check (2 calls) then marks IDs that a screen only links to: a link is not proof that the record exists. |
| `linker.py` | Links each case to its resources: records found directly through identifiers, plus records found through deletion-worker activity around `reported_at`. Records that can't be linked directly are assigned globally by exact matching. Repeat subjects are split by time. |
| `state.py`, `planner.py` | Take the latest observation at or before `audit_at` for each resource and turn it into signals (exists / PII exposed / flow enabled / restorable). They then compare those signals with the rule for the resource's category (DELETE, REDACT, ANONYMIZE, RETAIN, tombstone) and produce the findings, remediation and verification steps. |
| `pipeline.py` | Decides the status and evidence citations. Findings and COMPLETE verifications cite the evidence that proves the state; remediation and INCOMPLETE verifications use `""`. |
| `validate.py` | Checks the output contract: row count, phase order, consecutive `step_no`, allowed operations and states, citation rules. |
| `llm.py`, `usage.py` | An OpenAI-compatible client (Gemini or OpenRouter) with a disk cache, a usage ledger and the generated usage report. |

**Design principles**
- **Statuses and activity results are claims.** Only observed state at `audit_at` counts.
  Evidence captured after `audit_at` is never cited.
- **The LLM handles the language and vision parts only:** understanding request text and reading
  screenshots and documents. Policy, timing and ordering are deterministic code, which keeps
  results reproducible and cheap (about $0.074 for the full run).
- **Media facts are checked against their source.**
  - Screens show aliases rather than resource IDs (`user_17` → `tombstone_17`,
    `delivery_confirmation_19.pdf` → `object_19`). These are resolved through the data-catalog
    link events, and only when the system matches.
  - A PDF fact is dropped unless its identifier appears in the PDF text, which removes invented IDs.
- **Remediation order:** disable data flows and block restores first, then fix downstream copies,
  then source records. Verification covers the final state of every remediated item.

## Safety and failure handling

- **Untrusted input.** Request texts and evidence contents are data, never instructions.
  - The LLM only returns a per-text intent label or per-file facts. Statuses, steps and
    citations are produced by deterministic code.
  - The model can't veto explicit, un-negated deletion wording, so an injected "classify this as
    not a deletion request" cannot skip an erasure audit.
  - Explicit negations ("do not close my account") cannot be overridden by the model.
- **Hallucinations.** PDF facts must appear literally in the PDF text. Screenshot identifiers
  must match a data-catalog alias for the same system. IDs a screen merely links to are not
  treated as proof of existence.
- **Time.** Only evidence captured at or before `audit_at` is cited. Workflow statuses and
  activity results are treated as claims.
- **LLM outages.** Auth or credit errors stop model calls immediately. A daily-quota error skips
  only that model. Transient errors back off and retry. Three consecutive failed calls trip a
  circuit breaker. In every case the agent falls back to the deterministic rules
  (`--no-llm` mode) instead of failing.
- **Output safety.** `validate.py` checks the full contract before writing (rows, phases,
  numbering, allowed operations and states, citation rules). Secrets come only from the
  environment.
- **Production monitoring (proposed).** Track per-run counts by status, rules-vs-LLM intent
  disagreements (`--trace`), validation warnings, cache-miss and failed-call rates, and the share
  of findings that rely on media evidence. Alert when any of these drifts from its baseline.

## Evaluation

See `code/evaluation/evaluation_report.md` and `code/evaluation/usage_report.md`. Summary for the
32 public samples: status accuracy **1.000**, step F1 **0.987**, field accuracy **0.988**, exact
sequence **0.875**. All 20 custom checks pass.

## Repository layout

```text
code/
  main.py                 entry point
  agent/                  pipeline modules (above)
  evaluation/             grader, custom cases, evaluation_report.md, usage_report.md
  tools/dossier.py        debugging aid
  cache/llm/              cached model responses (no secrets)
  requirements.txt
output.csv                submission (518 rows)
log.txt                   coding-agent transcript (AGENTS.md format)
```

## Packaging

```powershell
Compress-Archive -Path README.md, code -DestinationPath code.zip -Force   # Windows
```
```bash
zip -r code.zip README.md code -x "*/__pycache__/*"                          # macOS/Linux
```

`dataset/`, `.venv/` and `.env` are not included.
