"""Generate evaluation/usage_report.md from the LLM call ledger of the final run."""
from __future__ import annotations

import os
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

from .llm import PRICES


def _cost(model: str, tin: int, tout: int) -> float | None:
    if os.getenv("LLM_PRICE_IN") and os.getenv("LLM_PRICE_OUT"):  # USD per 1M tokens, overrides PRICES
        pin, pout = float(os.environ["LLM_PRICE_IN"]), float(os.environ["LLM_PRICE_OUT"])
    elif model in PRICES or "google/" + model in PRICES:  # direct Gemini ids have no prefix
        pin, pout = PRICES.get(model) or PRICES["google/" + model]
    else:
        return None
    return tin / 1e6 * pin + tout / 1e6 * pout


def write_usage_report(ledger: list, n_requests: int, path: Path, provider: str = "OpenRouter") -> None:
    per_model = defaultdict(lambda: {"calls": 0, "cached": 0, "in": 0, "out": 0})
    per_purpose = defaultdict(lambda: {"calls": 0, "in": 0, "out": 0})
    for r in ledger:
        m = per_model[r.model]
        m["calls"] += 1
        m["cached"] += int(r.cached)
        m["in"] += r.input_tokens
        m["out"] += r.output_tokens
        p = per_purpose[(r.purpose, r.model)]
        p["calls"] += 1
        p["in"] += r.input_tokens
        p["out"] += r.output_tokens
    lines = [
        "# Token Usage and Cost Report",
        "",
        f"Generated automatically by `code/main.py` at the end of the final full-dataset run "
        f"({datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}) that produced `output.csv`.",
        "",
        f"* Requests audited: **{n_requests}**",
        f"* Provider: **{provider}** (OpenAI-compatible API)",
        "* Token counts are those reported by the provider. Calls served from the local response cache "
        "are counted with the tokens of the original call (they cost nothing on re-runs but are included "
        "so the totals reflect the work needed to produce the output from scratch).",
        "",
        "## Per model",
        "",
        "| Provider | Model | Model calls | (from cache) | Input tokens | Output tokens | Total tokens | Est. cost (USD) |",
        "|---|---|---:|---:|---:|---:|---:|---:|",
    ]
    tot = {"calls": 0, "in": 0, "out": 0, "cost": 0.0}
    for model, m in sorted(per_model.items()):
        c = _cost(model, m["in"], m["out"])
        tot["calls"] += m["calls"]
        tot["in"] += m["in"]
        tot["out"] += m["out"]
        tot["cost"] += c or 0.0
        lines.append(f"| {provider} | `{model}` | {m['calls']} | {m['cached']} | {m['in']:,} | {m['out']:,} | "
                     f"{m['in'] + m['out']:,} | {'n/a' if c is None else f'{c:.4f}'} |")
    total_tokens = tot["in"] + tot["out"]
    lines += [
        f"| **All** | | **{tot['calls']}** | | **{tot['in']:,}** | **{tot['out']:,}** | **{total_tokens:,}** | "
        f"**{tot['cost']:.4f}** |",
        "",
        "## Per request",
        "",
        "| Metric | Value |",
        "|---|---:|",
        f"| Model calls per request | {tot['calls'] / max(n_requests, 1):.3f} |",
        f"| Average input tokens per request | {tot['in'] / max(n_requests, 1):,.1f} |",
        f"| Average output tokens per request | {tot['out'] / max(n_requests, 1):,.1f} |",
        f"| Average total tokens per request | {total_tokens / max(n_requests, 1):,.1f} |",
        f"| Estimated cost per request (USD) | {tot['cost'] / max(n_requests, 1):.6f} |",
        "",
        "## Per purpose",
        "",
        "| Purpose | Model | Calls | Input tokens | Output tokens |",
        "|---|---|---:|---:|---:|",
    ]
    for (purpose, model), p in sorted(per_purpose.items()):
        lines.append(f"| {purpose} | `{model}` | {p['calls']} | {p['in']:,} | {p['out']:,} |")
    lines += [
        "",
        "## Why the cost is this low",
        "",
        "* The deletion decision, identity resolution, state reconstruction, policy application and step",
        "  ordering are deterministic code - no tokens are spent per case on them.",
        "* Request-intent classification sends only the *distinct* request texts, batched 80 per call.",
        "* Each PDF/image is interpreted once (not once per case) and the structured facts are cached.",
        "* Prices: list prices per 1M tokens configured in `code/agent/llm.py` (`PRICES`).",
        "",
    ]
    path.write_text("\n".join(lines), encoding="utf-8")
