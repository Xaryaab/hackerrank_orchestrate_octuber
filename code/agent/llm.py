"""Thin OpenAI-compatible client (Google Gemini API or OpenRouter) with a token/cost ledger and disk cache.

* Every call is appended to `usage_ledger` so `evaluation/usage_report.md` can be generated
  from the exact run that produced output.csv.
* Responses are cached on disk keyed by (model, prompt hash) so re-runs are free and
  deterministic; cached hits are recorded as cache hits (0 new tokens).
* If no API key is configured the client reports `available == False` and callers fall back
  to deterministic rules.
"""
from __future__ import annotations

import base64
import hashlib
import json
import os
import time
from dataclasses import dataclass, field
from pathlib import Path

try:  # optional dependency
    from dotenv import load_dotenv
    load_dotenv(Path(__file__).resolve().parents[2] / ".env")
except Exception:  # pragma: no cover
    pass

OPENROUTER_URL = "https://openrouter.ai/api/v1"
GEMINI_URL = "https://generativelanguage.googleapis.com/v1beta/openai/"

# USD per 1M tokens (input, output) for cost estimates in usage_report.md.
# List prices at the time of the final run; edit if they change, or set LLM_PRICE_IN / LLM_PRICE_OUT.
# Keys without the "google/" prefix also match the direct Gemini API ids.
PRICES = {
    "google/gemini-2.5-flash": (0.30, 2.50),
    "google/gemini-2.5-flash-lite": (0.10, 0.40),
    "gemini-3.5-flash-lite": (0.30, 2.50),
    "openai/gpt-4o-mini": (0.15, 0.60),
    "anthropic/claude-haiku-4.5": (1.00, 5.00),
    "anthropic/claude-sonnet-4.5": (3.00, 15.00),
}


@dataclass
class CallRecord:
    purpose: str
    model: str
    input_tokens: int
    output_tokens: int
    cached: bool
    latency_s: float


@dataclass
class LLMClient:
    cache_dir: Path
    text_model: str = field(default_factory=lambda: os.getenv("TEXT_MODEL", "google/gemini-2.5-flash"))
    vision_model: str = field(default_factory=lambda: os.getenv("VISION_MODEL", "google/gemini-2.5-flash"))
    max_tokens: int = field(default_factory=lambda: int(os.getenv("LLM_MAX_TOKENS", "8192")))
    attempts: int = field(default_factory=lambda: int(os.getenv("LLM_ATTEMPTS", "5")))
    ledger: list = field(default_factory=list)

    def __post_init__(self):
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        # GEMINI_API_KEY (Google AI Studio, direct) wins over OPENROUTER_API_KEY when both are set.
        gemini_key = os.getenv("GEMINI_API_KEY", "").strip()
        self.provider, self.api_key, base_url = (
            ("Google Gemini API", gemini_key, GEMINI_URL) if gemini_key
            else ("OpenRouter", os.getenv("OPENROUTER_API_KEY", "").strip(), OPENROUTER_URL))
        if self.provider == "Google Gemini API":  # the direct API uses bare ids: gemini-2.5-flash
            self.text_model = self.text_model.removeprefix("google/")
            self.vision_model = self.vision_model.removeprefix("google/")
        self._client = None
        self._exhausted: set[str] = set()
        if self.api_key:
            from openai import OpenAI
            self._client = OpenAI(base_url=base_url, api_key=self.api_key, timeout=180, max_retries=1)

    @property
    def available(self) -> bool:
        return self._client is not None

    def _cache_path(self, model: str, payload: str) -> Path:
        h = hashlib.sha256((model + "\n" + payload).encode()).hexdigest()[:32]
        return self.cache_dir / f"{h}.json"

    def chat_json(self, purpose: str, system: str, user: str, model: str | None = None,
                  image_paths: list[Path] | None = None) -> dict | None:
        """Single JSON-mode completion.  Returns parsed JSON or None on failure."""
        model = model or (self.vision_model if image_paths else self.text_model)
        content: list | str = user
        img_digest = ""
        if image_paths:
            content = [{"type": "text", "text": user}]
            for p in image_paths:
                data = p.read_bytes()
                img_digest += hashlib.sha256(data).hexdigest()
                content.append({"type": "image_url",
                                "image_url": {"url": "data:image/png;base64," + base64.b64encode(data).decode()}})
        cpath = self._cache_path(model, system + "\n" + user + img_digest)
        if cpath.exists():
            cached = json.loads(cpath.read_text(encoding="utf-8"))
            self.ledger.append(CallRecord(purpose, model, cached["usage"]["in"], cached["usage"]["out"], True, 0.0))
            return cached["result"]
        if not self.available or model in self._exhausted:
            return None
        for attempt in range(self.attempts):
            t0 = time.time()
            try:
                resp = self._client.chat.completions.create(
                    model=model, temperature=0, max_tokens=self.max_tokens,
                    response_format={"type": "json_object"},
                    messages=[{"role": "system", "content": system}, {"role": "user", "content": content}],
                )
                text = resp.choices[0].message.content or ""
                text = text.strip().removeprefix("```json").removeprefix("```").removesuffix("```").strip()
                result = json.loads(text)
                u = resp.usage
                rec = CallRecord(purpose, model, u.prompt_tokens if u else 0, u.completion_tokens if u else 0,
                                 False, time.time() - t0)
                self.ledger.append(rec)
                cpath.write_text(json.dumps({"model": model, "purpose": purpose, "result": result,
                                             "usage": {"in": rec.input_tokens, "out": rec.output_tokens}},
                                            ensure_ascii=False, indent=1), encoding="utf-8")
                return result
            except Exception as e:  # network / JSON errors -> retry, then give up (caller falls back)
                last = e
                status = getattr(e, "status_code", None)
                if status in (401, 402, 403):  # auth / no credits: retrying cannot help, stop calling
                    print(f"[llm] {purpose}: HTTP {status}, disabling model calls for this run")
                    self._client = None
                    return None
                if status == 429 and "PerDay" in str(e):  # daily quota is per model: skip only this one
                    print(f"[llm] {purpose}: daily quota exhausted for {model}, skipping it for this run")
                    self._exhausted.add(model)
                    return None
                print(f"[llm] {purpose}: attempt {attempt + 1} failed after {time.time() - t0:.0f}s: "
                      f"{status or type(e).__name__}")
                # shared free-tier pools rate-limit (429): back off much longer than for transient errors
                time.sleep((20 if status == 429 else 2) * (attempt + 1))
        print(f"[llm] {purpose}: giving up after retries: {last}")
        return None
