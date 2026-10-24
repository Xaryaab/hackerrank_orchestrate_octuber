"""Loading of the tabular dataset files and small time helpers."""
from __future__ import annotations

import csv
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path


def ts(s: str) -> datetime:
    return datetime.fromisoformat(s.replace("Z", "+00:00"))


def minutes(delta: timedelta) -> float:
    return delta.total_seconds() / 60.0


def read_csv(path: Path) -> list[dict]:
    with open(path, encoding="utf-8", newline="") as f:
        return list(csv.DictReader(f))


@dataclass
class Request:
    case_id: str
    email: str
    requested_at: str
    reported_status: str
    reported_at: str
    audit_at: str
    request_text: str
    is_sample: bool = False


@dataclass
class Activity:
    activity_id: str
    event_time: str
    event_type: str
    source_system: str
    target_system: str
    resource_id: str
    reported_result: str
    details: str


class Dataset:
    def __init__(self, root: Path):
        self.root = root
        self.requests = [Request(**r) for r in read_csv(root / "deletion_requests.csv")]
        self.samples = [Request(**r, is_sample=True) for r in read_csv(root / "sample_deletion_requests.csv")]
        self.activity = [Activity(**r) for r in read_csv(root / "system_activity.csv")]
        self.rules = read_csv(root / "deletion_rules.csv")
        self.manifest = read_csv(root / "evidence_files.csv")
        self.file_captured = {r["file_id"]: r["captured_at"] for r in self.manifest}

    @property
    def all_requests(self) -> list[Request]:
        """Samples and evaluation requests share one world; linking is done over all of them."""
        return self.samples + self.requests
