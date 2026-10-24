"""Evidence normalization.

Every evidence file (SQL snapshot, JSON export, log, text handover, PDF, image)
is turned into a flat list of `Observation`s: "at time T, file F saw resource R
in system S with these attributes".  Everything downstream (identity linking,
state reconstruction, policy) works only on observations, never on raw files.
"""
from __future__ import annotations

import csv
import json
import re
import sqlite3
from dataclasses import dataclass, field
from pathlib import Path

RESOURCE_RE = re.compile(
    r"\b(account|search|analytics|audit_record|support|email_delivery|data_flow|cache|export|"
    r"backup|tombstone|legal_hold|marketing|object|telemetry)_\d+\b"
)

PREFIX_SYSTEM = {
    "account": "account_service",
    "search": "search_service",
    "analytics": "analytics_platform",
    "audit_record": "security_service",
    "support": "support_platform",
    "email_delivery": "email_delivery_service",
    "data_flow": "integration_service",
    "cache": "cache_service",
    "export": "export_service",
    "backup": "backup_service",
    "tombstone": "identity_service",
    "legal_hold": "legal_service",
    "marketing": "marketing_platform",
    "object": "object_storage",
    "telemetry": "product_telemetry",
}


def resource_prefix(resource_id: str) -> str:
    return re.sub(r"_\d+$", "", resource_id)


def system_for(resource_id: str) -> str:
    return PREFIX_SYSTEM.get(resource_prefix(resource_id), "unknown")


@dataclass
class Observation:
    file_id: str
    captured_at: str          # file capture time (citation cut-off)
    ts: str                   # observation time
    resource_id: str
    system: str
    source: str               # parser family, e.g. "sql:customer_profiles"
    attrs: dict = field(default_factory=dict)

    def get(self, *keys, default=None):
        for k in keys:
            if k in self.attrs and self.attrs[k] not in (None, "", "null", "NULL"):
                return self.attrs[k]
        return default


# ---------------------------------------------------------------- helpers
def _flatten(d: dict, prefix: str = "") -> dict:
    out = {}
    for k, v in d.items():
        key = f"{prefix}{k}"
        if isinstance(v, dict):
            out.update(_flatten(v, f"{key}."))
        else:
            out[key] = v
    return out


def _norm_attrs(d: dict) -> dict:
    """Flatten nested dicts and also expose leaf keys without their prefix."""
    flat = _flatten(d)
    out = dict(flat)
    for k, v in flat.items():
        leaf = k.rsplit(".", 1)[-1]
        if leaf not in out or out[leaf] in (None, ""):
            out[leaf] = v
    return out


# ---------------------------------------------------------------- SQL
def parse_sql(path: Path, file_id: str, captured_at: str) -> list[Observation]:
    con = sqlite3.connect(":memory:")
    con.executescript(path.read_text(encoding="utf-8"))
    obs = []
    tables = [r[0] for r in con.execute("select name from sqlite_master where type='table'")]
    for t in tables:
        cols = [c[1] for c in con.execute(f'pragma table_info("{t}")')]
        if "resource_id" not in cols:
            continue
        for row in con.execute(f'select * from "{t}"'):
            rec = dict(zip(cols, row))
            attrs = {}
            if "record_json" in rec:
                attrs.update(_norm_attrs(json.loads(rec["record_json"])))
                attrs["collection"] = rec.get("collection_name")
                attrs["origin_system"] = rec.get("origin_system")
            attrs.update({k: v for k, v in rec.items() if k != "record_json"})
            attrs["table"] = t
            rid = rec["resource_id"]
            obs.append(Observation(file_id, captured_at, rec.get("updated_at") or captured_at, rid,
                                   rec.get("origin_system") or system_for(rid), f"sql:{t}", attrs))
    con.close()
    return obs


# ---------------------------------------------------------------- JSON
TIME_KEYS = ("observed_at", "recorded_at", "captured_at", "updated_at")


def parse_json(path: Path, file_id: str, captured_at: str) -> list[Observation]:
    data = json.loads(path.read_text(encoding="utf-8"))
    obs: list[Observation] = []

    def walk(node, ctx: dict):
        if isinstance(node, dict):
            ctx = dict(ctx)
            for k in TIME_KEYS + ("data_store", "collection", "system"):
                if isinstance(node.get(k), str) and "resource_id" not in node:
                    ctx[k] = node[k]
            if "resource_id" in node and isinstance(node["resource_id"], str):
                attrs = _norm_attrs({k: v for k, v in node.items() if not isinstance(v, list)})
                for k, v in node.items():
                    if isinstance(v, list):
                        attrs[k] = v
                for k, v in ctx.items():
                    attrs.setdefault("ctx_" + k, v)
                rid = node["resource_id"]
                ts = next((node[k] for k in TIME_KEYS if isinstance(node.get(k), str)), None) \
                    or next((ctx[k] for k in TIME_KEYS if k in ctx), captured_at)
                system = node.get("system") or ctx.get("data_store") or ctx.get("service") or system_for(rid)
                obs.append(Observation(file_id, captured_at, ts, rid, system, "json:" + str(ctx.get("collection", ctx.get("path", ""))), attrs))
                # nested resources (e.g. search hits documents) are walked too
            for k, v in node.items():
                c2 = dict(ctx)
                if k.endswith("_service") or k.endswith("_platform"):
                    c2["service"] = k
                if isinstance(v, (dict, list)):
                    c2["path"] = (ctx.get("path", "") + "/" + k).strip("/")
                    if "resource_id" in node and k in ("profile", "metrics", "document"):
                        continue
                    walk(v, c2)
        elif isinstance(node, list):
            for item in node:
                walk(item, ctx)

    walk(data, {})
    return obs


# ---------------------------------------------------------------- logs
TS_RE = re.compile(r"^(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z)")
KV_RE = re.compile(r'([A-Za-z_][\w.]*)=("((?:[^"\\]|\\.)*)"|[^\s"]+)')
TRACE_RE = re.compile(r"\b((?:tr|rb)-[\w-]+)\b")


def _parse_log_line(line: str) -> dict | None:
    m = TS_RE.match(line)
    if not m:
        return None
    rec: dict = {"_ts": m.group(1)}
    rest = line[m.end():].strip()
    if rest.startswith("{"):
        try:
            rec.update(json.loads(rest))
        except json.JSONDecodeError:
            pass
    else:
        # cloud runtime style: message="k=\"v\" ..."
        text = rest.replace('\\"', '"')
        for k, full, quoted in KV_RE.findall(text):
            val = quoted if full.startswith('"') else full
            if k == "message":
                continue
            rec[k] = val
        # "[LEVEL] [service] [trace] event_name: ..." style
        m2 = re.match(r"\[(\w+)\]\s+\[(\w+)\]\s+\[([\w-]+)\]\s+(\w+):", rest)
        if m2:
            rec.setdefault("level", m2.group(1))
            rec.setdefault("service", m2.group(2))
            rec.setdefault("trace_id", m2.group(3))
            rec.setdefault("event", m2.group(4))
        # "| LEVEL | service | trace | event |" style
        parts = [p.strip() for p in rest.strip("| ").split("|")]
        if len(parts) >= 5 and re.fullmatch(r"[A-Z]+", parts[0] or ""):
            rec.setdefault("level", parts[0])
            rec.setdefault("service", parts[1])
            rec.setdefault("trace_id", parts[2])
            rec.setdefault("event", parts[3])
        # syslog "<14>1 node-4 account_service tr-xxx - -"
        m3 = re.match(r"<\d+>\d+\s+\S+\s+(\w+)\s+([\w-]+)\s", rest)
        if m3:
            rec.setdefault("service", m3.group(1))
            rec.setdefault("trace_id", m3.group(2))
    if "requestId" in rec:
        rec.setdefault("trace_id", rec["requestId"])
    if "function" in rec:
        rec.setdefault("service", rec["function"])
    if "logger" in rec:
        rec.setdefault("service", rec["logger"])
    if "trace_id" not in rec:
        t = TRACE_RE.search(rest)
        if t:
            rec["trace_id"] = t.group(1)
    return rec


LINK_KEYS = ("lookup_key", "correlation_key", "subject_ref", "subject_reference", "matched_value")


def parse_log(path: Path, file_id: str, captured_at: str) -> list[Observation]:
    lines = [_parse_log_line(l) for l in path.read_text(encoding="utf-8").splitlines()]
    lines = [l for l in lines if l]
    traces: dict[str, list[dict]] = {}
    standalone = []
    for i, l in enumerate(lines):
        tid = l.get("trace_id")
        if tid:
            traces.setdefault(tid, []).append(l)
        else:
            standalone.append([l])
    obs = []
    for group in list(traces.values()) + standalone:
        merged: dict = {}
        for l in group:
            for k, v in l.items():
                if k == "resource_id" or k not in merged or merged[k] in (None, "", "null"):
                    merged[k] = v
        events = [l.get("event") for l in group if l.get("event")]
        merged["events"] = events
        for l in group:
            rid = l.get("resource_id")
            if not rid:
                continue
            attrs = dict(merged)
            attrs.update(l)
            attrs["events"] = events
            # link keys can come from the *_started line of the same trace
            for k in LINK_KEYS:
                if k not in attrs or attrs[k] in (None, "", "null"):
                    for o in group:
                        if o.get(k) not in (None, "", "null"):
                            attrs[k] = o[k]
                            break
            system = l.get("system") or system_for(rid)
            obs.append(Observation(file_id, captured_at, l["_ts"], rid, system, "log:" + str(l.get("event")), attrs))
    # de-duplicate (started + completed lines both carry resource_id)
    seen, out = set(), []
    for o in reversed(obs):
        key = (o.resource_id, o.attrs.get("trace_id") or id(o))
        if key in seen:
            continue
        seen.add(key)
        out.append(o)
    return list(reversed(out))


# ---------------------------------------------------------------- text handover
def parse_txt(path: Path, file_id: str, captured_at: str) -> list[Observation]:
    obs = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.startswith("- ") or "|" not in line:
            continue
        parts = [p.strip() for p in line[2:].split("|")]
        if len(parts) < 6:
            continue
        ts, system, rid, rtype, result, subject = parts[:6]
        attrs = {"record_type": rtype, "result": result, "subject_reference": subject}
        obs.append(Observation(file_id, captured_at, ts, rid, system, "txt:handover", attrs))
    return obs


# ---------------------------------------------------------------- coverage
def snapshot_coverage(dataset: Path) -> dict[str, set]:
    """file_id -> systems whose full point-in-time state the file captures.

    A snapshot that covers a system but does not list a record is evidence that the
    record is absent from that copy (e.g. `"records": []` for account_service).
    Logs and lookup traces are not snapshots and never prove absence by omission.
    """
    cov: dict[str, set] = {}
    for row in load_manifest(dataset):
        path = dataset / row["file_path"]
        fid = row["file_id"]
        systems: set = set()
        if path.suffix == ".json":
            data = json.loads(path.read_text(encoding="utf-8"))
            for snap in data.get("snapshots", []) if isinstance(data, dict) else []:
                if snap.get("data_store"):
                    systems.add(snap["data_store"])
        elif path.suffix == ".sql":
            con = sqlite3.connect(":memory:")
            con.executescript(path.read_text(encoding="utf-8"))
            tables = [r[0] for r in con.execute("select name from sqlite_master where type='table'")]
            if "snapshot_scope" in tables:
                systems |= {r[0] for r in con.execute("select origin_system from snapshot_scope")}
            for t in tables:
                cols = [c[1] for c in con.execute(f'pragma table_info("{t}")')]
                if "origin_system" in cols:
                    systems |= {r[0] for r in con.execute(f'select distinct origin_system from "{t}"')}
                elif t.startswith("customer_profiles"):
                    systems.add("account_service")
                elif t.startswith("search_documents"):
                    systems.add("search_service")
            con.close()
        if systems:
            cov[fid] = systems
    return cov


# ---------------------------------------------------------------- entry
def load_manifest(dataset: Path) -> list[dict]:
    with open(dataset / "evidence_files.csv", encoding="utf-8") as f:
        return list(csv.DictReader(f))


def load_all(dataset: Path, media_facts: dict | None = None) -> list[Observation]:
    """Parse every evidence file in the manifest.

    `media_facts` maps file_id -> list of fact dicts already extracted (by the
    multimodal extractor) from PDFs/images.
    """
    obs: list[Observation] = []
    aliases = catalog_aliases(dataset) if media_facts else {}
    for row in load_manifest(dataset):
        path = dataset / row["file_path"]
        fid, cap = row["file_id"], row["captured_at"]
        suffix = path.suffix.lower()
        if suffix == ".sql":
            obs += parse_sql(path, fid, cap)
        elif suffix == ".json":
            obs += parse_json(path, fid, cap)
        elif suffix == ".log":
            obs += parse_log(path, fid, cap)
        elif suffix == ".txt":
            obs += parse_txt(path, fid, cap)
        elif media_facts and fid in media_facts:
            obs += media_observations(path, fid, cap, media_facts[fid], aliases)
    return obs


ALIAS_RE = re.compile(r"\b([a-z]+(?:_[a-z]+)*_\d+) \(([^)]+)\)")


def catalog_aliases(dataset: Path) -> dict[str, str]:
    """alias -> resource_id from data-catalog link events, e.g.
    "linked tombstone_17 (user_17)", "confirmed object_19 (delivery_confirmation_19.pdf)",
    "linked export_1842 (NST-2026-0718-1842)".  Screens and documents show the alias, not the id."""
    aliases = {}
    with open(dataset / "system_activity.csv", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            if row["event_type"] != "LINK_RESOURCE_IDENTITY":
                continue
            for rid, alias in ALIAS_RE.findall(row["details"]):
                if alias != rid:
                    aliases[alias] = rid
    return aliases


def _pdf_text(path: Path) -> str:
    from pypdf import PdfReader
    return "\n".join(p.extract_text() or "" for p in PdfReader(str(path)).pages)


def media_observations(path: Path, fid: str, cap: str, facts: list[dict],
                       aliases: dict[str, str]) -> list[Observation]:
    """Observations from model-extracted media facts, grounded and alias-resolved."""
    text = _pdf_text(path) if path.suffix.lower() == ".pdf" else None
    out: list[Observation] = []
    for fact in facts:
        rid = fact.get("resource_id")
        if not rid or (text is not None and rid not in text):
            continue  # PDF text is available locally: drop identifiers the model did not read there
        target = aliases.get(rid) or aliases.get(fact.get("file_name") or "")
        if target and (fact.get("system") or system_for(rid)) == system_for(target):
            rid = target
        out.append(Observation(fid, cap, fact.get("observed_at") or cap, rid,
                               fact.get("system") or system_for(rid), "media", fact))
    if text is not None:
        # document references (statement numbers, file names) the extractor does not list as records;
        # bare user_N keys are excluded because registers mention many customers by key
        seen = {o.resource_id for o in out}
        for alias, rid in aliases.items():
            if rid in seen or not re.search(r"[-.]", alias) or alias not in text:
                continue
            emails = sorted(set(EMAIL_IN_TEXT.findall(text)))
            out.append(Observation(fid, cap, cap, rid, system_for(rid), "media:document",
                                   {"result": "FOUND", "document_reference": alias,
                                    "email": emails[0] if emails else None}))
    return out


EMAIL_IN_TEXT = re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+")
