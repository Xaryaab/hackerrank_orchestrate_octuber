"""Multimodal evidence extraction (PNG screenshots, PDF documents) -> structured facts.

Each file is processed once; results are cached by the LLM client, so the full run costs
one call per media file (15 files), independent of the number of cases.
PDF text is extracted locally with pypdf and sent as text to the text model (cheaper than vision).
"""
from __future__ import annotations

import json
from pathlib import Path

from .evidence import _pdf_text
from .llm import LLMClient

SYSTEM_PROMPT = """You extract machine-readable facts from internal business artifacts of an
e-commerce company (admin-console screenshots, customer statements, support attachments,
retention registers, audit-journal extracts). The facts feed a GDPR deletion audit.

List EVERY record identifier you can see whose form is <prefix>_<digits>, with prefixes such as
account_, search_, analytics_, support_, export_, data_flow_, backup_, audit_record_, tombstone_,
legal_hold_, marketing_, object_, cache_, email_delivery_. Also list order/statement/ticket
references only when they are attached to such an identifier.

For each record return an object with these keys (null when not shown):
  resource_id        exact identifier text
  system             owning system if inferable (account_service, search_service, analytics_platform,
                     support_platform, export_service, integration_service, backup_service,
                     security_service, identity_service, legal_service, marketing_platform, object_storage)
  record_type        e.g. USER_PROFILE, SEARCH_INDEX_ENTRY, ANALYTICS_PROFILE, SUPPORT_MESSAGE,
                     SUPPORT_ATTACHMENT, USER_EXPORT, DATA_FLOW, BACKUP_ARCHIVE, SECURITY_AUDIT_LOG,
                     LEGAL_HOLD_RECORD, ACCOUNT_TOMBSTONE, MARKETING_PROFILE
  result             FOUND | NOT_FOUND | IDENTIFIABLE | ANONYMIZED | REDACTED | ACTIVE | DISABLED | BLOCKED
                     (state the screen/document shows for the record)
  customer_reference user_<n> if shown
  full_name, email, phone, email_hash   direct identifiers shown for the record (verbatim)
  enabled            true/false for connectors / data flows if shown
  restore_allowed    true/false for backups if shown
  file_name          attachment file name if the record is a file
  observed_at        ISO-8601 UTC timestamp of the capture if shown
  note               <= 20 words of other relevant facts (retention dates, ownership, links)

Respond with JSON: {"records": [ ... ]}. Do not invent identifiers that are not visible."""


class MediaExtractor:
    def __init__(self, llm: LLMClient | None, dataset: Path):
        self.llm = llm
        self.dataset = dataset

    def extract_all(self) -> dict[str, list[dict]]:
        import csv
        facts: dict[str, list[dict]] = {}
        if self.llm is None:
            return facts
        with open(self.dataset / "evidence_files.csv", encoding="utf-8") as f:
            manifest = list(csv.DictReader(f))
        for row in manifest:
            path = self.dataset / row["file_path"]
            suffix = path.suffix.lower()
            if suffix not in (".png", ".jpg", ".jpeg", ".pdf"):
                continue
            header = (f"file_id={row['file_id']} origin={row['origin']} captured_at={row['captured_at']} "
                      f"file={path.name}\n")
            if suffix == ".pdf":
                res = self.llm.chat_json("media_extraction_pdf", SYSTEM_PROMPT,
                                         header + "Document text:\n" + _pdf_text(path))
            else:
                res = self.llm.chat_json("media_extraction_image", SYSTEM_PROMPT,
                                         header + "Extract the records shown in this screenshot.",
                                         image_paths=[path])
            if res and isinstance(res.get("records"), list):
                facts[row["file_id"]] = [r for r in res["records"] if isinstance(r, dict) and r.get("resource_id")]
        return facts


def dump_facts(facts: dict, path: Path) -> None:
    path.write_text(json.dumps(facts, indent=1, ensure_ascii=False), encoding="utf-8")
