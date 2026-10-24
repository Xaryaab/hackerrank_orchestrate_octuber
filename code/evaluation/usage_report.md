# Token Usage and Cost Report

Generated automatically by `code/main.py` at the end of the final full-dataset run (2026-10-24 05:26 UTC) that produced `output.csv`.

* Requests audited: **518**
* Provider: **Google Gemini API** (OpenAI-compatible API)
* Token counts are those reported by the provider. Calls served from the local response cache are counted with the tokens of the original call (they cost nothing on re-runs but are included so the totals reflect the work needed to produce the output from scratch).

## Per model

| Provider | Model | Model calls | (from cache) | Input tokens | Output tokens | Total tokens | Est. cost (USD) |
|---|---|---:|---:|---:|---:|---:|---:|
| Google Gemini API | `gemini-2.5-flash` | 11 | 11 | 8,740 | 8,148 | 16,888 | 0.0230 |
| Google Gemini API | `gemini-3.5-flash-lite` | 9 | 9 | 22,126 | 15,430 | 37,556 | 0.0452 |
| **All** | | **20** | | **30,866** | **23,578** | **54,444** | **0.0682** |

## Per request

| Metric | Value |
|---|---:|
| Model calls per request | 0.039 |
| Average input tokens per request | 59.6 |
| Average output tokens per request | 45.5 |
| Average total tokens per request | 105.1 |
| Estimated cost per request (USD) | 0.000132 |

## Per purpose

| Purpose | Model | Calls | Input tokens | Output tokens |
|---|---|---:|---:|---:|
| intent_classification | `gemini-3.5-flash-lite` | 5 | 18,856 | 14,806 |
| media_extraction_image | `gemini-2.5-flash` | 11 | 8,740 | 8,148 |
| media_extraction_pdf | `gemini-3.5-flash-lite` | 4 | 3,270 | 624 |

## Why the cost is this low

* The deletion decision, identity resolution, state reconstruction, policy application and step
  ordering are deterministic code - no tokens are spent per case on them.
* Request-intent classification sends only the *distinct* request texts, batched 80 per call.
* Each PDF/image is interpreted once (not once per case) and the structured facts are cached.
* Prices: list prices per 1M tokens configured in `code/agent/llm.py` (`PRICES`).
