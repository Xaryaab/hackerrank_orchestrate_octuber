"""Identity resolution: current + historical identifiers for every user."""
from __future__ import annotations

import csv
import hashlib
import re
from dataclasses import dataclass, field
from pathlib import Path

EMAIL_RE = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
USER_RE = re.compile(r"^user_\d+$")
HASH_RE = re.compile(r"^[0-9a-f]{64}$")


def norm_email(v: str) -> str:
    return v.strip().lower()


def norm_phone(v: str) -> str:
    return re.sub(r"\D", "", v)


def sha(v: str) -> str:
    return hashlib.sha256(v.encode()).hexdigest()


@dataclass
class Identity:
    user_id: str
    name: str
    emails: set = field(default_factory=set)
    phones: set = field(default_factory=set)
    hashes: set = field(default_factory=set)
    names: set = field(default_factory=set)
    audit_ids: list = field(default_factory=list)   # audit rows that revealed history

    def keys(self) -> set:
        ks = {("user", self.user_id)}
        ks |= {("email", e) for e in self.emails}
        ks |= {("phone", p) for p in self.phones if len(p) >= 7}
        ks |= {("hash", h) for h in self.hashes}
        return ks


class IdentityIndex:
    def __init__(self, dataset: Path):
        self.users: dict[str, Identity] = {}
        self.current_owner: dict[str, str] = {}     # current e-mail -> user id
        with open(dataset / "user_identities.csv", encoding="utf-8") as f:
            for r in csv.DictReader(f):
                ident = Identity(r["user_id"], r["name"])
                self.current_owner[norm_email(r["email"])] = r["user_id"]
                ident.emails.add(norm_email(r["email"]))
                ident.phones.add(norm_phone(r["phone"]))
                ident.hashes.add(r["email_hash"].lower())
                ident.names.add(r["name"].strip().lower())
                self.users[r["user_id"]] = ident
        with open(dataset / "user_audit_log.csv", encoding="utf-8") as f:
            for r in csv.DictReader(f):
                ident = self.users.get(r["user_id"])
                if not ident:
                    continue
                fname = r["field_name"]
                for v in (r["old_value"], r["new_value"]):
                    if not v:
                        continue
                    if fname == "email":
                        ident.emails.add(norm_email(v))
                    elif fname == "phone":
                        ident.phones.add(norm_phone(v))
                    elif fname == "email_hash":
                        ident.hashes.add(v.lower())
                    elif fname == "name":
                        ident.names.add(v.strip().lower())
                ident.audit_ids.append(r["audit_id"])
        for ident in self.users.values():
            ident.hashes |= {sha(e) for e in ident.emails}
        # reverse index key -> user ids
        self.by_key: dict[tuple, set] = {}
        for ident in self.users.values():
            for k in ident.keys():
                self.by_key.setdefault(k, set()).add(ident.user_id)

    def resolve_email(self, email: str) -> str | None:
        key = norm_email(email)
        hits = self.by_key.get(("email", key), set())
        if len(hits) == 1:
            return next(iter(hits))
        # an address held by several users over time: its current holder owns it; otherwise the
        # request cannot be attributed safely (never audit another customer's records)
        owner = self.current_owner.get(key)
        return owner if owner in hits else None


def value_keys(value) -> set:
    """Normalized identity keys mentioned in a single attribute value."""
    out = set()
    if value is None or isinstance(value, bool):
        return out
    if isinstance(value, (list, tuple)):
        for v in value:
            out |= value_keys(v)
        return out
    if isinstance(value, dict):
        for v in value.values():
            out |= value_keys(v)
        return out
    s = str(value).strip()
    if not s or s.lower() in ("null", "none"):
        return out
    if USER_RE.match(s):
        out.add(("user", s))
    elif HASH_RE.match(s.lower()):
        out.add(("hash", s.lower()))
    elif "@" in s and EMAIL_RE.fullmatch(s):
        out.add(("email", norm_email(s)))
    else:
        for e in EMAIL_RE.findall(s):
            out.add(("email", norm_email(e)))
        if re.fullmatch(r"\+?[\d\s().-]{7,}", s):
            out.add(("phone", norm_phone(s)))
        for u in re.findall(r"\buser_\d+\b", s):
            out.add(("user", u))
    return out


SKIP_KEYS = {"trace_id", "requestId", "_ts", "instance", "pod", "region", "resource_id", "integrity_seal"}


def obs_keys(attrs: dict) -> set:
    out = set()
    for k, v in attrs.items():
        if k in SKIP_KEYS or k.startswith("ctx_"):
            continue
        out |= value_keys(v)
    return out
