"""Does the request text actually authorize deletion?

Primary path: an LLM classifies every request text (batched, JSON output, cached).
Fallback / cross-check: deterministic multilingual rules that understand negations such as
"Do not close the account" or "without removing any of my records".
Only *distinct* texts are sent to the model, so 518 requests cost a handful of calls.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass

from .data import Request
from .llm import LLMClient

DELETE_PATTERNS = [
    r"\bdelet", r"\berase", r"\berasure", r"\bremove (my|the|this) (account|profile)", r"\bclose (my|this|the) account",
    r"\bclosed\b.*\baccount", r"\bpermanent(ly)? (account[- ])?delet", r"supprim", r"suppression", r"eliminaci", r"eliminar",
    r"exclus[aã]o", r"excluir", r"삭제", r"削除", r"löschen", r"cancel(l)?ar (mi|minha) cuenta",
]
NON_DELETION_MARKERS = [
    r"\bdo not (close|delete|remove)", r"\bdon'?t (close|delete|remove)", r"\bwithout (removing|deleting|closing)",
    r"\bleave (the|my) (account|profile)[^.]*(active|in place|as it is)", r"\bkeep the account\b", r"\bkeep my account\b",
    r"\b(marketing|promotional)[^.]*\bonly\b", r"\bonly\b[^.]*\b(marketing|promotional)", r"\bcontinue using\b",
    r"\bstop the promotional", r"\bunsubscribe\b", r"\bsend me a copy\b", r"\bcopy of (last|my|the) (year|invoice)",
    r"\bdownload my account information\b", r"\bportable format\b", r"\bcorrect it without\b",
    r"\bchange the email address\b", r"\breset(ting)? the login\b", r"what personal information you hold",
    r"\bout of date\b[^.]*\breplace\b",
]

SYSTEM_PROMPT = """You are a privacy-operations triage assistant for an e-commerce company.
For each customer message decide whether it AUTHORIZES DELETION of the customer's account /
personal data (erasure, account closure with data removal, "right to be forgotten").

Return is_deletion=false for any other request, even if it mentions data or the account:
access/copy requests, data export/portability, corrections or updates of details, login help,
marketing or promotional opt-outs, invoice copies, or messages that explicitly say to keep the
account. Messages may be in any language. A request that asks for deletion AND adds conditions
(e.g. "keep backups if required", "security records can stay") is still a deletion request. A message asking to verify, confirm, check or audit that a deletion
the customer already requested or performed really happened ("verify my account is gone",
"my request shows failed, check whether my data was removed") is ALSO is_deletion=true: it
refers to the customer's own erasure request and asks for it to be audited.

Respond with JSON: {"results": [{"id": <id>, "is_deletion": true|false, "request_type": "<deletion|access|export|correction|marketing_opt_out|account_support|other>", "language": "<iso code>"}]}"""


@dataclass
class IntentDecision:
    is_deletion: bool
    reason: str


def rule_intent(text: str) -> IntentDecision:
    t = text.lower()
    neg = [p for p in NON_DELETION_MARKERS if re.search(p, t)]
    pos = [p for p in DELETE_PATTERNS if re.search(p, t)]
    if neg:
        return IntentDecision(False, f"rules: non-deletion marker {neg[0]!r}")
    if pos:
        return IntentDecision(True, f"rules: deletion marker {pos[0]!r}")
    # every request in this intake queue is a privacy request about an account; without an
    # explicit non-deletion marker the safe reading is "audit the deletion"
    return IntentDecision(True, "rules: no explicit non-deletion marker (default)")


class IntentClassifier:
    def __init__(self, llm: LLMClient | None, batch_size: int = 80):
        self.llm = llm
        self.batch_size = batch_size
        self.llm_labels: dict[str, dict] = {}

    def prepare(self, requests: list[Request]) -> None:
        """Classify all distinct texts with the LLM in batches (no-op without an API key)."""
        if not self.llm or not self.llm.available and not any(self.llm.cache_dir.glob("*.json")):
            return
        texts = sorted({r.request_text for r in requests})
        for i in range(0, len(texts), self.batch_size):
            chunk = texts[i:i + self.batch_size]
            payload = json.dumps([{"id": j, "text": t} for j, t in enumerate(chunk)], ensure_ascii=False)
            res = self.llm.chat_json("intent_classification", SYSTEM_PROMPT,
                                     "Classify these customer messages:\n" + payload)
            if not res:
                continue
            for item in res.get("results", []):
                try:
                    self.llm_labels[chunk[int(item["id"])]] = item
                except (KeyError, ValueError, IndexError):
                    continue

    def classify(self, req: Request) -> IntentDecision:
        rule = rule_intent(req.request_text)
        label = self.llm_labels.get(req.request_text)
        if label is None:
            return rule
        llm_says = bool(label.get("is_deletion"))
        if llm_says == rule.is_deletion:
            return IntentDecision(llm_says, f"llm+rules agree ({label.get('request_type')})")
        # disagreement: trust explicit negation markers, otherwise trust the model
        if rule.reason.startswith("rules: non-deletion marker"):
            return IntentDecision(False, f"conflict, negation marker wins: {rule.reason}")
        return IntentDecision(llm_says, f"conflict, llm wins ({label.get('request_type')}); {rule.reason}")
