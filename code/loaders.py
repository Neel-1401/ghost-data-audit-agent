"""Data loading, identity resolution and evidence search for the deletion audit.

Standard library only, except pypdf for PDF text (see code/requirements.txt).
Every path is resolved from the repository root, so the module works from any
working directory on Windows, macOS and Linux.

Run ``python code/loaders.py case_05 case_07`` to compare what the search finds
for sample cases with the expected answers in ``dataset/sample_output.csv``.
"""

from __future__ import annotations

import csv
import hashlib
import json
import re
import sqlite3
import sys
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
DATASET_DIR = REPO_ROOT / "dataset"

# ---------------------------------------------------------------------------
# Generic helpers
# ---------------------------------------------------------------------------

EMAIL_RE = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
HASH_RE = re.compile(r"(?<![0-9a-fA-F])[0-9a-fA-F]{64}(?![0-9a-fA-F])")
USER_RE = re.compile(r"(?<![A-Za-z0-9_])user_\d+(?![A-Za-z0-9_])")
PHONE_RE = re.compile(r"\+?\d[\d\s().-]{6,}\d")
MIN_PHONE_DIGITS = 10  # shorter digit runs are order numbers, pods, trace ids

# Resource-id prefixes seen in the dataset. Longer prefixes come first so that
# "audit_record_12" is not read as something shorter.
RESOURCE_PREFIXES = (
    "email_delivery", "audit_record", "data_flow", "legal_hold", "tombstone",
    "analytics", "marketing", "account", "support", "backup", "search",
    "export", "object", "cache",
)
RESOURCE_RE = re.compile(
    r"(?<![A-Za-z0-9_])(?:" + "|".join(RESOURCE_PREFIXES) + r")_\d+(?![A-Za-z0-9_])"
)

# Fallback system for each resource prefix. Activity data refines this at load
# time (see ``Dataset._build_prefix_systems``).
DEFAULT_PREFIX_SYSTEMS = {
    "account": "account_service",
    "search": "search_service",
    "analytics": "analytics_platform",
    "support": "support_platform",
    "export": "export_service",
    "data_flow": "integration_service",
    "backup": "backup_service",
    "audit_record": "security_service",
    "tombstone": "identity_service",
    "cache": "cache_service",
    "email_delivery": "email_delivery_service",
    "legal_hold": "legal_service",
    "marketing": "marketing_platform",
    "object": "object_storage",
}


def parse_time(value: str | None) -> datetime | None:
    """Parse the ISO-8601 'Z' timestamps used across the dataset."""
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError:
        return None


def sha256_email(email: str) -> str:
    """The dataset's email_hash scheme: SHA-256 of the lower-cased email."""
    return hashlib.sha256(email.strip().lower().encode("utf-8")).hexdigest()


def phone_digits(value: str) -> str:
    return re.sub(r"\D", "", value)


def resource_prefix(resource_id: str) -> str:
    return resource_id.rsplit("_", 1)[0]


def iter_strings(value):
    """Yield every string (and stringified scalar) nested inside ``value``."""
    if isinstance(value, dict):
        for v in value.values():
            yield from iter_strings(v)
    elif isinstance(value, (list, tuple)):
        for v in value:
            yield from iter_strings(v)
    elif value is not None:
        yield str(value)


def extract_tokens(value) -> set[tuple[str, str]]:
    """Return normalised identifier tokens found anywhere inside ``value``.

    Token kinds: email, hash, user, res (resource id), phone (digits only) and
    value (a whole lower-cased string, used for exact name matches).
    """
    tokens: set[tuple[str, str]] = set()
    for text in iter_strings(value):
        stripped = text.strip()
        if not stripped:
            continue
        if len(stripped) <= 80:
            tokens.add(("value", stripped.lower()))
        for m in EMAIL_RE.findall(stripped):
            tokens.add(("email", m.lower()))
        for m in HASH_RE.findall(stripped):
            tokens.add(("hash", m.lower()))
        for m in USER_RE.findall(stripped):
            tokens.add(("user", m))
        for m in RESOURCE_RE.findall(stripped):
            tokens.add(("res", m))
        # Remove emails and hashes before looking for phone numbers so that
        # digit runs inside them are not mistaken for phones.
        remainder = HASH_RE.sub(" ", EMAIL_RE.sub(" ", stripped))
        for m in PHONE_RE.findall(remainder):
            digits = phone_digits(m)
            if len(digits) >= MIN_PHONE_DIGITS:
                tokens.add(("phone", digits))
    return tokens


# ---------------------------------------------------------------------------
# 1. CSV loading
# ---------------------------------------------------------------------------


def load_csvs(dataset_dir: Path = DATASET_DIR) -> dict[str, list[dict[str, str]]]:
    """Load every CSV in ``dataset/`` keyed by file stem."""
    tables = {}
    for path in sorted(dataset_dir.glob("*.csv")):
        with path.open(encoding="utf-8", newline="") as handle:
            tables[path.stem] = list(csv.DictReader(handle))
    return tables


# ---------------------------------------------------------------------------
# 2. Identity resolution
# ---------------------------------------------------------------------------


@dataclass
class Identity:
    user_id: str
    request_email: str
    matched_via: str  # "current" or "historical"
    emails: set[str] = field(default_factory=set)
    phones: set[str] = field(default_factory=set)
    names: set[str] = field(default_factory=set)
    email_hashes: set[str] = field(default_factory=set)
    computed_hashes: set[str] = field(default_factory=set)  # derived, not supplied
    audit_ids: list[str] = field(default_factory=list)

    def tokens(self) -> set[tuple[str, str]]:
        toks = {("user", self.user_id)}
        toks |= {("email", e.lower()) for e in self.emails}
        toks |= {("hash", h.lower()) for h in self.email_hashes}
        toks |= {("phone", phone_digits(p)) for p in self.phones}
        toks |= {("value", n.lower()) for n in self.names}
        return toks


class IdentityResolver:
    """Map a request email (current or historical) to a user and all their identifiers."""

    FIELD_SETS = {"email": "emails", "phone": "phones", "email_hash": "email_hashes", "name": "names"}

    def __init__(self, identities: list[dict], audit_log: list[dict]):
        self.users = {row["user_id"]: row for row in identities}
        self.audit_by_user: dict[str, list[dict]] = defaultdict(list)
        for row in audit_log:
            self.audit_by_user[row["user_id"]].append(row)

        self.current_email: dict[str, str] = {}
        for row in identities:
            self.current_email[row["email"].strip().lower()] = row["user_id"]

        # Any email that appears on either side of an email change.
        self.any_email: dict[str, set[str]] = defaultdict(set)
        for row in audit_log:
            if row["field_name"] == "email":
                for value in (row["old_value"], row["new_value"]):
                    if value:
                        self.any_email[value.strip().lower()].add(row["user_id"])

        self.hash_is_sha256 = self._check_hash_scheme(identities, audit_log)

    @staticmethod
    def _check_hash_scheme(identities, audit_log) -> bool:
        """True when every supplied email_hash equals sha256(lower(email))."""
        checked = matched = 0
        for row in identities:
            checked += 1
            matched += sha256_email(row["email"]) == row["email_hash"].lower()
        changes = defaultdict(dict)
        for row in audit_log:
            if row["field_name"] in ("email", "email_hash"):
                changes[(row["user_id"], row["event_time"])][row["field_name"]] = row
        for pair in changes.values():
            if len(pair) == 2:
                for side in ("old_value", "new_value"):
                    checked += 1
                    matched += sha256_email(pair["email"][side]) == pair["email_hash"][side].lower()
        return checked > 0 and matched == checked

    def resolve(self, email: str) -> Identity | None:
        key = email.strip().lower()
        if key in self.current_email:
            user_id, via = self.current_email[key], "current"
        elif len(self.any_email.get(key, ())) == 1:
            user_id, via = next(iter(self.any_email[key])), "historical"
        else:
            return None  # unknown or ambiguous email

        row = self.users[user_id]
        ident = Identity(user_id=user_id, request_email=email, matched_via=via)
        ident.emails.add(row["email"])
        ident.phones.add(row["phone"])
        ident.names.add(row["name"])
        ident.email_hashes.add(row["email_hash"].lower())
        for change in self.audit_by_user.get(user_id, []):
            target = self.FIELD_SETS.get(change["field_name"])
            if target is None:
                continue
            ident.audit_ids.append(change["audit_id"])
            bucket = getattr(ident, target)
            for value in (change["old_value"], change["new_value"]):
                if value:
                    bucket.add(value.lower() if target == "email_hashes" else value)
        if self.hash_is_sha256:
            for addr in ident.emails:
                digest = sha256_email(addr)
                if digest not in ident.email_hashes:
                    ident.computed_hashes.add(digest)
                ident.email_hashes.add(digest)
        return ident


# ---------------------------------------------------------------------------
# 3 & 4. Evidence loading
# ---------------------------------------------------------------------------


@dataclass
class EvidenceRecord:
    """One searchable unit of evidence: a SQL row, JSON record, text/log line or PDF block."""

    file_id: str
    kind: str  # sql | json | txt | log | pdf
    location: str  # table:rowid, JSON path, or line number
    resource_id: str | None
    system_id: str | None  # system named by the evidence itself, if any
    observed_at: str | None  # row/line timestamp, if any
    fields: dict
    group: str | None = None  # log trace id: lines in one trace belong together
    tokens: set = field(default_factory=set)
    snapshot_at: str | None = None  # nested snapshot captured_at inside a bundle file


@dataclass
class EvidenceFile:
    file_id: str
    path: Path
    origin: str
    captured_at: str
    captured_dt: datetime | None


@dataclass
class Appearance:
    file_id: str
    captured_at: str
    location: str
    resource_id: str
    system_id: str
    evidence_system: str | None
    observed_at: str | None
    matched_on: str
    fields: dict
    citable: bool  # file captured at or before audit_at, so it may be an evidence_id


@dataclass
class ActivityMatch:
    activity_id: str
    event_time: str
    event_type: str
    target_system: str
    resource_id: str
    reported_result: str
    details: str
    matched_on: str


SQL_TABLE_SYSTEMS = {
    "customer_profiles": "account_service",
    "customer_search_documents": "search_service",
    "customer_analytics_profiles": "analytics_platform",
    "support_records": "support_platform",
    "customer_exports": "export_service",
    "connector_subscriptions": "integration_service",
    "backup_restore_entries": "backup_service",
    "security_audit_records": "security_service",
}

KV_RE = re.compile(r'(\w+)=("(?:[^"\\]|\\.)*"|\S+)')
LINE_TS_RE = re.compile(r"^\s*(?:-\s*)?(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z)\s*(.*)$")


def load_sql_databases(files: dict[str, EvidenceFile]) -> dict[str, sqlite3.Connection]:
    """Execute each .sql snapshot into its own in-memory SQLite database."""
    dbs = {}
    for fid, ef in files.items():
        if ef.path.suffix.lower() == ".sql":
            conn = sqlite3.connect(":memory:")
            conn.row_factory = sqlite3.Row
            conn.executescript(ef.path.read_text(encoding="utf-8"))
            dbs[fid] = conn
    return dbs


def sql_records(fid: str, conn: sqlite3.Connection) -> list[EvidenceRecord]:
    records = []
    tables = [r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")]
    for table in tables:
        if table == "snapshot_scope":
            continue
        for row in conn.execute(f'SELECT rowid AS _rowid, * FROM "{table}"'):
            data = dict(row)
            rowid = data.pop("_rowid")
            if isinstance(data.get("record_json"), str):
                try:
                    data["record_json"] = json.loads(data["record_json"])
                except json.JSONDecodeError:
                    pass
            records.append(EvidenceRecord(
                file_id=fid,
                kind="sql",
                location=f"{table}:row{rowid}",
                resource_id=data.get("resource_id"),
                system_id=data.get("origin_system") or SQL_TABLE_SYSTEMS.get(table),
                observed_at=data.get("updated_at") or data.get("indexed_at"),
                fields={"_table": table, **data},
            ))
    return records


def json_records(fid: str, payload) -> list[EvidenceRecord]:
    """Every dict carrying a resource_id becomes a record; context is inherited.

    ``observed_at`` is the most specific timestamp (record-level observed_at /
    recorded_at, else the nearest enclosing one). ``snapshot_at`` is a
    ``captured_at`` set below the top level: snapshot bundles stamp each
    snapshot separately, so one file can hold snapshots from several days.
    """
    records = []
    system_keys = ("system", "data_store", "origin_system")
    time_keys = ("observed_at", "recorded_at", "captured_at", "exported_at")

    def walk(node, path, system, when, snapshot, depth):
        if isinstance(node, dict):
            system = next((node[k] for k in system_keys if isinstance(node.get(k), str)), system)
            when = next((node[k] for k in time_keys if isinstance(node.get(k), str)), when)
            if depth > 0 and isinstance(node.get("captured_at"), str):
                snapshot = node["captured_at"]
            if "resource_id" in node:
                records.append(EvidenceRecord(
                    file_id=fid, kind="json", location=path or "$",
                    resource_id=node.get("resource_id"), system_id=system,
                    observed_at=when, fields=node, snapshot_at=snapshot,
                ))
                return
            for key, value in node.items():
                child_system = key if key.endswith(("_service", "_platform")) else system
                walk(value, f"{path}.{key}", child_system, when, snapshot, depth + 1)
        elif isinstance(node, list):
            for i, value in enumerate(node):
                walk(value, f"{path}[{i}]", system, when, snapshot, depth + 1)

    walk(payload, "$", None, None, None, 0)
    return records


BRACKET_LINE_RE = re.compile(r"^\[(\w+)\]\s*\[([\w.-]+)\]\s*\[([\w.-]+)\]\s*([\w.-]+):\s*(.*)$")
# The first column must be a log level, so handover tables
# ("| system | resource | type | result | subject") are not read as logs.
PIPE_LOG_RE = re.compile(
    r"^\|\s*(TRACE|DEBUG|INFO|NOTICE|WARN|WARNING|ERROR|CRITICAL|FATAL)\s*\|\s*([\w.-]+)\s*\|"
    r"\s*([\w.-]+)\s*\|\s*([\w.-]+)\s*\|\s*(.*)$"
)


def parse_kv(text: str) -> dict:
    out = {}
    for key, value in KV_RE.findall(text):
        if value.startswith('"') and value.endswith('"'):
            value = value[1:-1].replace('\\"', '"')
        out[key] = value
    return out


def parse_line_fields(rest: str) -> dict:
    """Parse the body of a log line.

    Formats seen in the evidence: JSON object; ``[LEVEL] [service] [trace]
    event: k="v"``; ``| LEVEL | service | trace | event | k="v"``; plain
    ``k=v`` pairs, including ``message="<escaped k=v pairs>"`` wrappers; and
    pipe tables ``| system | resource | type | result | subject``.
    """
    rest = rest.strip()
    if rest.startswith("{"):
        try:
            return json.loads(rest)
        except json.JSONDecodeError:
            pass
    m = BRACKET_LINE_RE.match(rest)
    if m:
        return {"level": m.group(1), "service": m.group(2), "trace_id": m.group(3),
                "event": m.group(4), **parse_kv(m.group(5))}
    m = PIPE_LOG_RE.match(rest)
    if m:
        return {"level": m.group(1), "service": m.group(2), "trace_id": m.group(3),
                "event": m.group(4), **parse_kv(m.group(5))}
    if re.match(r"^\w+=", rest):
        out = parse_kv(rest)
        nested = out.pop("message", None)
        if nested and "=" in nested:
            out.update(parse_kv(nested))
        elif nested:
            out["message"] = nested
        if "requestId" in out and "trace_id" not in out:
            out["trace_id"] = out["requestId"]
        if "function" in out and "service" not in out:
            out["service"] = out["function"]
        if out:
            return out
    if rest.startswith("|") or " | " in rest:
        parts = [p.strip() for p in rest.strip("|").split("|")]
        names = ("system", "resource_id", "record_type", "result", "subject_reference")
        if len(parts) == len(names):
            return dict(zip(names, parts))
        return {"columns": parts}
    out = {}
    for key, value in KV_RE.findall(rest):
        if value.startswith('"') and value.endswith('"'):
            value = value[1:-1]
        out[key] = value
    if not out:
        out["text"] = rest
    return out


def text_records(fid: str, kind: str, text: str) -> list[EvidenceRecord]:
    records = []
    for lineno, line in enumerate(text.splitlines(), start=1):
        if not line.strip():
            continue
        m = LINE_TS_RE.match(line)
        when, rest = (m.group(1), m.group(2)) if m else (None, line)
        fields = parse_line_fields(rest)
        fields.setdefault("_raw", line.strip())
        resource = fields.get("resource_id")
        if not resource:
            found = RESOURCE_RE.findall(rest)
            resource = found[0] if len(set(found)) == 1 else None
        system = fields.get("system") or fields.get("service") or fields.get("logger")
        trace = fields.get("trace_id")
        records.append(EvidenceRecord(
            file_id=fid, kind=kind, location=f"line {lineno}",
            resource_id=resource, system_id=system, observed_at=when,
            fields=fields, group=f"{fid}:{trace}" if trace else None,
        ))
    return records


def pdf_records(fid: str, path: Path) -> list[EvidenceRecord]:
    """PDF text: each '|' table row is its own record; the rest of a page is one record.

    Business documents spread one subject over many short lines (name, email,
    support reference), so the free text of a page is kept together. Register
    tables list a different subject per row, so rows are kept apart.
    """
    try:
        from pypdf import PdfReader
    except ImportError as exc:  # pragma: no cover - depends on environment
        raise SystemExit("pypdf is required: pip install -r code/requirements.txt") from exc

    records = []
    for page_no, page in enumerate(PdfReader(str(path)).pages, start=1):
        lines = [ln.strip() for ln in (page.extract_text() or "").splitlines() if ln.strip()]
        free_text = []
        for line_no, line in enumerate(lines, start=1):
            if line.count("|") >= 2:
                found = set(RESOURCE_RE.findall(line))
                records.append(EvidenceRecord(
                    file_id=fid, kind="pdf", location=f"page {page_no} line {line_no}",
                    resource_id=found.pop() if len(found) == 1 else None, system_id=None,
                    observed_at=None, fields={"columns": [p.strip() for p in line.split("|")]},
                ))
            else:
                free_text.append(line)
        if free_text:
            text = "\n".join(free_text)
            found = set(RESOURCE_RE.findall(text))
            records.append(EvidenceRecord(
                file_id=fid, kind="pdf", location=f"page {page_no}",
                resource_id=found.pop() if len(found) == 1 else None, system_id=None,
                observed_at=None, fields={"lines": free_text},
            ))
    return records


CATALOG_ALIAS_RE = re.compile(r"\b(?:linked|confirmed) (\S+) \(([^)]+)\)", re.I)


def catalog_aliases(activity: list[dict]) -> dict[str, str]:
    """Display names the data catalog maps to resource ids.

    ``LINK_RESOURCE_IDENTITY`` rows read "Data catalog linked export_1842
    (NST-2026-0718-1842) to ...": consoles may show the alias in brackets
    (a file name, order number or customer reference) instead of the id.
    """
    aliases = {}
    for row in activity:
        if row["event_type"] != "LINK_RESOURCE_IDENTITY":
            continue
        m = CATALOG_ALIAS_RE.search(row["details"])
        if m and m.group(1) == row["resource_id"] and m.group(2) != row["resource_id"]:
            aliases[m.group(2).strip()] = row["resource_id"]
    return aliases


def image_resource_id(item: dict, aliases: dict[str, str]) -> str | None:
    """The record's resource id: as transcribed, else the one id or catalog alias shown on it."""
    rid = item.get("resource_id")
    if isinstance(rid, str) and RESOURCE_RE.fullmatch(rid.strip()):
        return rid.strip()
    values = [v.strip() for v in iter_strings(item)]
    found = {m for v in values for m in RESOURCE_RE.findall(v)}
    if len(found) == 1:
        return found.pop()
    mapped = {aliases[v] for v in values if v in aliases}
    return mapped.pop() if len(mapped) == 1 else None


def image_records(fid: str, transcribed: list[dict], aliases: dict[str, str]) -> list[EvidenceRecord]:
    """Records Gemini transcribed from a screenshot (see vision.py), one per resource."""
    from vision import image_fields

    records = []
    for n, item in enumerate(transcribed, start=1):
        rid = image_resource_id(item, aliases)
        records.append(EvidenceRecord(
            file_id=fid, kind="image", location=f"record {n}", resource_id=rid, system_id=None,
            observed_at=None, fields=image_fields(item),
        ))
    return records


# ---------------------------------------------------------------------------
# Dataset: everything loaded once, plus the search
# ---------------------------------------------------------------------------


class Dataset:
    SEARCHABLE_SUFFIXES = {".sql", ".json", ".txt", ".log", ".pdf"}
    IMAGE_SUFFIXES = {".png", ".jpg", ".jpeg", ".webp"}

    def __init__(self, dataset_dir: Path = DATASET_DIR, images=None):
        """``images`` is a vision.ImageExtractor; screenshots are skipped without one."""
        self.dataset_dir = dataset_dir
        self.images = images
        self.tables = load_csvs(dataset_dir)
        self.resolver = IdentityResolver(self.tables["user_identities"], self.tables["user_audit_log"])
        self.files = self._load_manifest()
        self.databases = load_sql_databases(self.files)
        self.records: list[EvidenceRecord] = []
        self.skipped_files: list[str] = []
        self.catalog_aliases = catalog_aliases(self.tables["system_activity"])
        self._load_evidence()
        self._resolve_document_aliases()
        self.prefix_systems = self._build_prefix_systems()
        self._build_indexes()

    # -- loading -----------------------------------------------------------

    def _load_manifest(self) -> dict[str, EvidenceFile]:
        files = {}
        for row in self.tables["evidence_files"]:
            # Manifest paths use "/"; Path joins them correctly on Windows.
            path = self.dataset_dir.joinpath(*row["file_path"].split("/"))
            files[row["file_id"]] = EvidenceFile(
                file_id=row["file_id"], path=path, origin=row["origin"],
                captured_at=row["captured_at"], captured_dt=parse_time(row["captured_at"]),
            )
        return files

    def _load_evidence(self):
        for fid, ef in self.files.items():
            suffix = ef.path.suffix.lower()
            if suffix in self.IMAGE_SUFFIXES and self.images is not None:
                records = image_records(fid, self.images.extract(fid, ef.path), self.catalog_aliases)
                if records:
                    self.records.extend(records)
                    continue
            if suffix not in self.SEARCHABLE_SUFFIXES:
                self.skipped_files.append(fid)
                continue
            if suffix == ".sql":
                self.records.extend(sql_records(fid, self.databases[fid]))
            elif suffix == ".pdf":
                self.records.extend(pdf_records(fid, ef.path))
            elif suffix == ".json":
                payload = json.loads(ef.path.read_text(encoding="utf-8"))
                self.records.extend(json_records(fid, payload))
            else:
                text = ef.path.read_text(encoding="utf-8", errors="replace")
                self.records.extend(text_records(fid, suffix.lstrip("."), text))

    def _resolve_document_aliases(self):
        """A PDF block that names no resource id but shows exactly one catalog alias
        (a statement or file name) is about that resource. Customer references
        (user_N) are skipped: a document mentioning a user is not that user's tombstone."""
        patterns = {
            alias: re.compile(r"(?<![\w-])" + re.escape(alias) + r"(?![\w-])")
            for alias in self.catalog_aliases if not re.fullmatch(r"user_\d+", alias)
        }
        for rec in self.records:
            if rec.kind != "pdf" or rec.resource_id:
                continue
            text = "\n".join(iter_strings(rec.fields))
            found = {self.catalog_aliases[a] for a, pat in patterns.items() if pat.search(text)}
            if len(found) == 1:
                rec.resource_id = found.pop()

    def _build_prefix_systems(self) -> dict[str, str]:
        """Owning system per resource prefix.

        The fixed map wins: activity targets are not always the owner (support
        index updates target search_service, for example). Activity only fills
        in prefixes the fixed map does not know.
        """
        counts: dict[str, Counter] = defaultdict(Counter)
        for row in self.tables["system_activity"]:
            counts[resource_prefix(row["resource_id"])][row["target_system"]] += 1
        mapping = dict(DEFAULT_PREFIX_SYSTEMS)
        for prefix, counter in counts.items():
            mapping.setdefault(prefix, counter.most_common(1)[0][0])
        return mapping

    def _build_indexes(self):
        self.token_index: dict[tuple[str, str], list[int]] = defaultdict(list)
        self.groups: dict[str, list[int]] = defaultdict(list)
        for i, rec in enumerate(self.records):
            rec.tokens = extract_tokens(rec.fields)
            if rec.resource_id:
                rec.tokens.add(("res", rec.resource_id))
            for tok in rec.tokens:
                self.token_index[tok].append(i)
            if rec.group:
                self.groups[rec.group].append(i)

        self.activity = self.tables["system_activity"]
        self.activity_tokens = [extract_tokens(row["details"]) | {("res", row["resource_id"])}
                                for row in self.activity]

    # -- search ------------------------------------------------------------

    def system_for(self, resource_id: str) -> str:
        return self.prefix_systems.get(resource_prefix(resource_id), "unknown")

    def file_citable(self, file_id: str, audit_dt: datetime) -> bool:
        """A file may be cited as evidence_id only if captured at or before audit_at."""
        captured = self.files[file_id].captured_dt
        return captured is not None and captured <= audit_dt

    def record_visible(self, rec: EvidenceRecord, audit_dt: datetime) -> bool:
        """Whether a record may be used to discover facts at audit_at.

        Log and text lines carry their own timestamps, so a line written at or
        before audit_at counts even when its file was exported later. The same
        holds for a JSON record inside a snapshot whose own captured_at is at
        or before audit_at. A JSON record stamped after audit_at is never
        used, even in an earlier file. Everything else falls back to the
        file's captured_at.
        """
        if rec.kind in ("log", "txt"):
            line_dt = parse_time(rec.observed_at)
            if line_dt is not None:
                return line_dt <= audit_dt
        if rec.kind == "json":
            own_dt = parse_time(rec.observed_at)
            if own_dt is not None and own_dt > audit_dt:
                return False
            snapshot_dt = parse_time(rec.snapshot_at)
            if snapshot_dt is not None:
                return snapshot_dt <= audit_dt
        return self.file_citable(rec.file_id, audit_dt)

    def find_appearances(self, identity: Identity, audit_at: str, max_rounds: int = 3):
        """Every evidence record and activity row linked to ``identity`` at ``audit_at``.

        Round 1 matches the person's identifiers directly. Later rounds follow
        links through resource ids (for example a 404 lookup that names only
        ``account_123``, or a search document whose source_resource_id is the
        account). Log lines sharing a trace_id with a match are pulled in too.
        Log lines stamped after ``audit_at``, other evidence captured after
        ``audit_at`` and activity after ``audit_at`` are ignored. Each
        appearance says whether its file may be cited (``citable``).
        """
        audit_dt = parse_time(audit_at)
        seed_tokens = identity.tokens()
        matched: dict[int, str] = {}  # record index -> reason
        activity_matched: dict[int, str] = {}
        known_resources: set[str] = set()

        def visible(i: int) -> bool:
            return self.record_visible(self.records[i], audit_dt)

        def match_tokens(tokens: set, reason_prefix: str):
            new_resources = set()
            hits: dict[int, list[str]] = defaultdict(list)
            for tok in sorted(tokens):
                for i in self.token_index.get(tok, ()):
                    if i not in matched and visible(i):
                        hits[i].append(f"{tok[0]}={tok[1]}")
            for i, reasons in hits.items():
                matched[i] = reason_prefix + ", ".join(reasons)
                rid = self.records[i].resource_id
                if rid:
                    new_resources.add(rid)
            # Pull in every line of a matched log trace.
            for i in list(matched):
                group = self.records[i].group
                if not group:
                    continue
                for j in self.groups[group]:
                    if j not in matched and visible(j):
                        matched[j] = f"trace {group.split(':', 1)[1]} ({matched[i]})"
                        rid = self.records[j].resource_id
                        if rid:
                            new_resources.add(rid)
            return new_resources

        def match_activity(tokens: set, reason_prefix: str):
            new_resources = set()
            for i, row in enumerate(self.activity):
                if i in activity_matched:
                    continue
                event_dt = parse_time(row["event_time"])
                if event_dt is None or event_dt > audit_dt:
                    continue
                hit = self.activity_tokens[i] & tokens
                if hit:
                    activity_matched[i] = reason_prefix + ", ".join(f"{k}={v}" for k, v in sorted(hit))
                    new_resources.add(row["resource_id"])
            return new_resources

        frontier = match_tokens(seed_tokens, "")
        frontier |= match_activity(seed_tokens, "")
        for _ in range(max_rounds):
            frontier -= known_resources
            if not frontier:
                break
            known_resources |= frontier
            res_tokens = {("res", r) for r in frontier}
            frontier = match_tokens(res_tokens, "via ") | match_activity(res_tokens, "via ")

        appearances = []
        for i, reason in matched.items():
            rec = self.records[i]
            if not rec.resource_id:
                continue
            appearances.append(Appearance(
                file_id=rec.file_id,
                captured_at=self.files[rec.file_id].captured_at,
                location=rec.location,
                resource_id=rec.resource_id,
                system_id=self.system_for(rec.resource_id),
                evidence_system=rec.system_id,
                observed_at=rec.observed_at,
                matched_on=reason,
                fields=rec.fields,
                citable=self.file_citable(rec.file_id, audit_dt),
            ))
        appearances.sort(key=lambda a: (a.system_id, a.resource_id, a.captured_at, a.file_id, a.location))

        activity = []
        for i, reason in activity_matched.items():
            row = self.activity[i]
            activity.append(ActivityMatch(
                activity_id=row["activity_id"], event_time=row["event_time"],
                event_type=row["event_type"], target_system=row["target_system"],
                resource_id=row["resource_id"], reported_result=row["reported_result"],
                details=row["details"], matched_on=reason,
            ))
        activity.sort(key=lambda a: (a.event_time, a.activity_id))
        return appearances, activity


# ---------------------------------------------------------------------------
# Account linking
# ---------------------------------------------------------------------------


@dataclass
class AccountLink:
    case_id: str
    user_id: str | None
    resource_id: str | None
    method: str  # evidence | activity_window | ambiguous | none | unresolved_identity
    candidates: list[str]
    activity_id: str | None = None


class AccountLinker:
    """Link every request to the user's account_service resource id.

    Rule, applied to all requests together so they can rule each other out:

    1. Evidence: an account record visible at audit_at that carries the
       user's identifiers (directly or through a log trace). One account wins.
    2. Activity: otherwise, the deletion worker's DELETE_RECORD on
       account_service between max(requested_at, reported_at - WINDOW_BEFORE)
       and reported_at + WINDOW_AFTER (and not after audit_at). Candidates are
       removed when evidence ties them to a different user (evidence from any
       capture time may only exclude, never assign), or when another request
       has already claimed them.
    3. Elimination: a request with exactly one candidate claims it, which
       removes it from every other request; repeat until stable. While
       candidates remain, prefer those that appear in evidence visible at the
       request's audit_at.
    Anything still tied is reported as ambiguous rather than guessed.

    Pass ``eligible_case_ids`` (requests that authorise deletion). Non-deletion
    requests have no account delete of their own and would otherwise claim
    someone else's.
    """

    WINDOW_BEFORE_MIN = 120
    WINDOW_AFTER_MIN = 15
    FAR_FUTURE = "2999-01-01T00:00:00Z"

    def __init__(self, ds: Dataset, request_tables=("sample_deletion_requests", "deletion_requests"),
                 eligible_case_ids: set[str] | None = None, extra_requests=()):
        from datetime import timedelta

        self.ds = ds
        rows = [r for name in request_tables for r in ds.tables[name]] + list(extra_requests)
        self.requests = {
            r["case_id"]: r for r in rows
            if eligible_case_ids is None or r["case_id"] in eligible_case_ids
        }
        self.account_deletes = [
            row for row in ds.tables["system_activity"]
            if row["event_type"] == "DELETE_RECORD" and row["target_system"] == "account_service"
            and row["source_system"] == "deletion_worker"
        ]
        self.records_by_resource: dict[str, list[EvidenceRecord]] = defaultdict(list)
        for rec in ds.records:
            if rec.resource_id:
                self.records_by_resource[rec.resource_id].append(rec)

        self.users: dict[str, str | None] = {}
        self.visible_resources: dict[str, set[str]] = {}  # case -> resources tied by identifiers
        evidence_accounts: dict[str, set[str]] = {}
        owners: dict[str, set[str]] = defaultdict(set)
        for cid, req in self.requests.items():
            ident = ds.resolver.resolve(req["email"])
            self.users[cid] = ident.user_id if ident else None
            if ident is None:
                continue
            visible, _ = ds.find_appearances(ident, req["audit_at"])
            self.visible_resources[cid] = {a.resource_id for a in visible}
            evidence_accounts[cid] = {a.resource_id for a in visible if a.system_id == "account_service"}
            anytime, _ = ds.find_appearances(ident, self.FAR_FUTURE)
            for a in anytime:
                owners[a.resource_id].add(ident.user_id)
        self.owners = owners  # resource id -> users whose identifiers the evidence ties to it

        self.links: dict[str, AccountLink] = {}
        candidates: dict[str, list[dict]] = {}
        for cid, req in self.requests.items():
            user = self.users[cid]
            if user is None:
                self.links[cid] = AccountLink(cid, None, None, "unresolved_identity", [])
                continue
            ev = evidence_accounts[cid]
            if len(ev) == 1:
                self.links[cid] = AccountLink(cid, user, next(iter(ev)), "evidence", sorted(ev))
                continue
            requested = parse_time(req["requested_at"])
            reported = parse_time(req["reported_at"])
            audit = parse_time(req["audit_at"])
            lo = max(requested, reported - timedelta(minutes=self.WINDOW_BEFORE_MIN))
            hi = min(reported + timedelta(minutes=self.WINDOW_AFTER_MIN), audit)
            window = [row for row in self.account_deletes if lo <= parse_time(row["event_time"]) <= hi]
            if ev:  # several evidence accounts: the activity window picks among them
                window = [row for row in window if row["resource_id"] in ev] or window
            candidates[cid] = [
                row for row in window
                if not owners.get(row["resource_id"]) or user in owners[row["resource_id"]]
            ]

        taken = {link.resource_id for link in self.links.values() if link.resource_id}
        changed = True
        while changed:
            changed = False
            for cid in sorted(candidates):
                rows = [row for row in candidates[cid] if row["resource_id"] not in taken]
                seen = [row for row in rows if self._in_visible_evidence(row["resource_id"], self.requests[cid]["audit_at"])]
                rows = seen or rows
                candidates[cid] = rows
                if len(rows) == 1:
                    row = rows[0]
                    self.links[cid] = AccountLink(cid, self.users[cid], row["resource_id"], "activity_window",
                                                  [row["resource_id"]], row["activity_id"])
                    taken.add(row["resource_id"])
                    del candidates[cid]
                    changed = True
        for cid, rows in candidates.items():
            ids = [row["resource_id"] for row in rows]
            self.links[cid] = AccountLink(cid, self.users[cid], None, "ambiguous" if ids else "none", ids)

    def _in_visible_evidence(self, resource_id: str, audit_at: str) -> bool:
        audit_dt = parse_time(audit_at)
        return any(self.ds.record_visible(rec, audit_dt) for rec in self.records_by_resource.get(resource_id, ()))

    def link(self, case_id: str) -> AccountLink:
        return self.links[case_id]


# ---------------------------------------------------------------------------
# Manual check against sample answers
# ---------------------------------------------------------------------------


def _short(fields: dict, limit: int = 110) -> str:
    keep = {k: v for k, v in fields.items() if k not in ("_raw",) and v not in (None, "")}
    text = json.dumps(keep, ensure_ascii=False, default=str)
    return text if len(text) <= limit else text[: limit - 3] + "..."


def demo(case_ids: list[str]):
    ds = Dataset()
    linker = AccountLinker(ds)
    requests = {r["case_id"]: r for r in ds.tables["sample_deletion_requests"]}
    expected = {r["case_id"]: r for r in ds.tables["sample_output"]}
    print(f"Loaded {len(ds.records)} evidence records from {len(ds.files) - len(ds.skipped_files)} files; "
          f"skipped {len(ds.skipped_files)} image files. email_hash is sha256(lower(email)): "
          f"{ds.resolver.hash_is_sha256}")

    for cid in case_ids:
        req = requests[cid]
        print("\n" + "=" * 100)
        print(f"{cid}  email={req['email']}  audit_at={req['audit_at']}  reported={req['reported_status']}")
        ident = ds.resolver.resolve(req["email"])
        if ident is None:
            print("  identity: UNRESOLVED")
            continue
        print(f"  identity: {ident.user_id} (matched {ident.matched_via} email)")
        print(f"    emails={sorted(ident.emails)} phones={sorted(ident.phones)} names={sorted(ident.names)}")
        print(f"    hashes={len(ident.email_hashes)} (computed for old emails: {len(ident.computed_hashes)}) "
              f"audit_ids={ident.audit_ids}")

        appearances, activity = ds.find_appearances(ident, req["audit_at"])
        link = linker.link(cid)
        print(f"  account link: {link.resource_id} via {link.method} "
              f"(candidates={link.candidates}{', activity ' + link.activity_id if link.activity_id else ''})")
        print(f"\n  FOUND in evidence ({len(appearances)}):")
        for a in appearances:
            cite = "citable" if a.citable else "NOT citable (captured after audit_at)"
            print(f"    {a.system_id:<20} {a.resource_id:<16} {a.file_id:<12} {a.location:<42} "
                  f"[{a.matched_on}] {cite}")
            print(f"        captured={a.captured_at} observed={a.observed_at} {_short(a.fields)}")
        print(f"\n  FOUND in system_activity ({len(activity)}):")
        for act in activity:
            print(f"    {act.activity_id:<14} {act.event_time} {act.event_type:<16} {act.resource_id:<16} "
                  f"{act.reported_result:<8} [{act.matched_on}]")

        print(f"\n  EXPECTED ({expected[cid]['deletion_status']}):")
        found_pairs = {(a.system_id, a.resource_id) for a in appearances}
        if link.resource_id:
            found_pairs.add(("account_service", link.resource_id))
        found_files = defaultdict(set)
        for a in appearances:
            if a.citable:
                found_files[(a.system_id, a.resource_id)].add(a.file_id)
        for step in json.loads(expected[cid]["steps_json"]):
            key = (step["system_id"], step["resource_id"])
            mark = "found" if key in found_pairs else "NOT FOUND"
            cite = step["evidence_id"]
            cite_note = ""
            if cite:
                cite_note = (" (cited file found, citable)" if cite in found_files.get(key, ())
                             else " (cited file NOT matched)")
            print(f"    {step['step_no']:>2} {step['phase']:<12} {step['operation']:<24} {step['system_id']:<18} "
                  f"{step['resource_id']:<14} ev={cite or '-':<12} -> {mark}{cite_note}")


if __name__ == "__main__":
    demo(sys.argv[1:] or ["case_05", "case_07"])
