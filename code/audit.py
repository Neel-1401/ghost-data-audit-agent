"""Deletion audit for one request: record states at audit_at, policy, steps.

Pipeline for an authorised deletion request:

1. Collect the subject's resources: everything the evidence links to the
   person's identifiers (``Dataset.find_appearances``) plus the account found
   by ``AccountLinker``.
2. Turn every visible evidence record about each resource into an
   ``Observation`` (present? exposes direct identifiers? process active?).
   Reported workflow statuses and activity results are never used as state.
3. Reduce observations to the resource's state at audit_at (latest wins).
4. Apply deletion_rules.csv by data category to decide findings, remediation
   and the final state to verify.
5. Emit steps in the contract order and pick evidence that may be cited.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from datetime import datetime

from loaders import (
    AccountLink, Appearance, Dataset, EvidenceRecord, Identity, parse_time, resource_prefix,
)

OPERATION_ORDER = [
    "REPORT_REMAINING_RECORD", "REPORT_EXPOSED_PII", "REPORT_ACTIVE_DATA_FLOW", "REPORT_RESTORE_RISK",
    "DISABLE_DATA_FLOW", "BLOCK_RESTORE", "DELETE_RECORD", "REDACT_RECORD", "ANONYMIZE_RECORD",
    "VERIFY_RECORD_ABSENT", "VERIFY_RECORD_PRESENT", "VERIFY_RECORD_REDACTED", "VERIFY_RECORD_ANONYMIZED",
    "VERIFY_DATA_FLOW_DISABLED", "VERIFY_RESTORE_BLOCKED",
]
OP_RANK = {op: i for i, op in enumerate(OPERATION_ORDER)}
OP_PHASE_STATE = {
    "REPORT_REMAINING_RECORD": ("FINDING", "PRESENT"),
    "REPORT_EXPOSED_PII": ("FINDING", "PRESENT"),
    "REPORT_ACTIVE_DATA_FLOW": ("FINDING", "ACTIVE"),
    "REPORT_RESTORE_RISK": ("FINDING", "ACTIVE"),
    "DISABLE_DATA_FLOW": ("REMEDIATION", "DISABLED"),
    "BLOCK_RESTORE": ("REMEDIATION", "BLOCKED"),
    "DELETE_RECORD": ("REMEDIATION", "ABSENT"),
    "REDACT_RECORD": ("REMEDIATION", "REDACTED"),
    "ANONYMIZE_RECORD": ("REMEDIATION", "ANONYMIZED"),
    "VERIFY_RECORD_ABSENT": ("VERIFICATION", "ABSENT"),
    "VERIFY_RECORD_PRESENT": ("VERIFICATION", "PRESENT"),
    "VERIFY_RECORD_REDACTED": ("VERIFICATION", "REDACTED"),
    "VERIFY_RECORD_ANONYMIZED": ("VERIFICATION", "ANONYMIZED"),
    "VERIFY_DATA_FLOW_DISABLED": ("VERIFICATION", "DISABLED"),
    "VERIFY_RESTORE_BLOCKED": ("VERIFICATION", "BLOCKED"),
}

NULLS = {None, "", "null", "none", "NULL", "None", "-", "—"}
# Fields whose value directly identifies a person. Stable internal references
# (customer_reference, user_id, subject_reference) are listed separately.
DIRECT_ID_FIELDS = {
    "email", "phone", "full_name", "name", "display_name", "requester_name", "requester_email",
    "requester_phone", "owner_email", "owner_phone", "customer_email", "email_hash", "matched_value",
}
STABLE_REF_FIELDS = {"customer_reference", "user_id", "subject_reference", "subject_ref"}
NON_OBSERVATION_EVENTS = re.compile(r"_started$|^connection_acquired$|^dependency_|^thumbnail_failed$|^export_started$")
TRUE_VALUES = {"true", "1", "yes", "enabled", "on"}
FALSE_VALUES = {"false", "0", "no", "disabled", "off"}

# Policy: resource prefix -> data category (deletion_rules.csv). Support and
# analytics records are refined per record in ``category_for``.
PREFIX_CATEGORY = {
    "account": "USER_PROFILE",
    "search": "SEARCH_INDEX_ENTRY",
    "cache": "CACHE_ENTRY",
    "export": "USER_EXPORT",
    "marketing": "MARKETING_PROFILE",
    "email_delivery": "EMAIL_DELIVERY_LOG",
    "audit_record": "SECURITY_AUDIT_LOG",
    "legal_hold": "LEGAL_HOLD_RECORD",
    "backup": "BACKUP_ARCHIVE",
    "data_flow": "DATA_FLOW",
    "tombstone": "ACCOUNT_TOMBSTONE",
    "object": "SUPPORT_ATTACHMENT",
    "support": "SUPPORT_RECORD",
    "analytics": "ANALYTICS_PROFILE",
}
DELETE_CATEGORIES = {
    "USER_PROFILE", "SECONDARY_IDENTIFIER", "SEARCH_INDEX_ENTRY", "CACHE_ENTRY", "SUPPORT_ATTACHMENT",
    "USER_EXPORT", "MARKETING_PROFILE",
}
REDACT_CATEGORIES = {"SUPPORT_MESSAGE", "EMAIL_DELIVERY_LOG", "ACCOUNT_TOMBSTONE"}
ANONYMIZE_CATEGORIES = {"ANALYTICS_PROFILE", "PRODUCT_TELEMETRY"}
RETAIN_CATEGORIES = {"SECURITY_AUDIT_LOG", "LEGAL_HOLD_RECORD"}


def is_null(value) -> bool:
    return value is None or (isinstance(value, str) and value.strip() in NULLS)


def as_bool(value) -> bool | None:
    if isinstance(value, bool):
        return value
    if value is None:
        return None
    text = str(value).strip().lower()
    if text in TRUE_VALUES:
        return True
    if text in FALSE_VALUES:
        return False
    return None


def flatten(fields: dict, resource_id: str | None) -> dict:
    """Merge nested record payloads (record_json, profile, search hits) into one dict."""
    flat = {}
    for key, value in fields.items():
        if key in ("record_json", "profile") and isinstance(value, dict):
            flat.update(value)
            flat[f"_has_{key}"] = True
        elif key == "profile" and value is None:
            flat["_profile_null"] = True
        elif key == "hits" and isinstance(value, dict):
            docs = value.get("documents") or []
            match = [d for d in docs if isinstance(d, dict) and d.get("resource_id") == resource_id]
            flat["total_hits"] = value.get("total", len(docs))
            if match:
                flat.update(match[0])
        else:
            flat.setdefault(key, value)
    return flat


# ---------------------------------------------------------------------------
# Observations
# ---------------------------------------------------------------------------


@dataclass
class Observation:
    resource_id: str
    file_id: str
    location: str
    time: datetime
    citable: bool
    present: bool | None
    exposed: bool | None = None  # direct identifiers visible in the record content
    stable_ref: bool | None = None
    active: bool | None = None  # data flow enabled / restore allowed
    record_type: str | None = None
    message_like: bool = False
    attachment_like: bool = False
    hash_keyed: bool = False
    retention_until: str | None = None


def observe(rec: EvidenceRecord, captured_at: datetime, citable: bool) -> Observation | None:
    rid = rec.resource_id
    prefix = resource_prefix(rid)
    f = flatten(rec.fields, rid)
    event = str(f.get("event") or "")
    if event and NON_OBSERVATION_EVENTS.search(event):
        return None

    when = parse_time(rec.observed_at) or captured_at
    obs = Observation(resource_id=rid, file_id=rec.file_id, location=rec.location, time=when,
                      citable=citable, present=None)
    record_type = f.get("record_type")
    result = str(f.get("result") or "").strip().upper()
    is_lookup_row = bool(record_type) and bool(result)  # handover/ledger rows describe a lookup
    if is_lookup_row:
        obs.record_type = str(record_type).upper()

    # -- presence --------------------------------------------------------
    present = None
    if result in ("NOT_FOUND", "MISS", "NO_HIT", "ABSENT", "DELETED"):
        present = False
    elif result in ("FOUND", "IDENTIFIABLE", "ACTIVE", "BLOCKED", "DISABLED", "ANONYMIZED", "REDACTED",
                    "HIT", "RETAINED", "PRESENT"):
        present = True
    for key in ("status_code", "http_status"):
        code = str(f.get(key) or "")
        if code.startswith("404") or code.startswith("410"):
            present = False
        elif code.startswith("2") and present is None:
            present = True
    rows = f.get("rows")
    if rows is not None and str(rows).isdigit():
        present = int(rows) > 0 if present is None or int(rows) == 0 else present
    hits = f.get("total_hits")
    if hits is not None and str(hits).isdigit() and int(hits) == 0:
        present = False
    if as_bool(f.get("archive_present")) is False:
        present = False
    available = as_bool(f.get("download_available", f.get("available")))
    if prefix == "export" and available is False:
        present = False
    if f.get("_profile_null") and present is None:
        present = False
    if present is None and rec.kind in ("sql", "json", "pdf", "image"):
        present = True  # a stored record, document or console listing is itself proof of presence
    if present is None and event in ("message_pii_scan", "attachment_pii_scan", "analytics_identity_scan",
                                     "analytics_profile_read", "message_read", "deletion_marker_check",
                                     "contact_query_completed", "contact_lookup_completed",
                                     "connector_heartbeat", "connector_config_loaded",
                                     "restore_candidate_evaluated", "restore_authorization_completed",
                                     "export_download_check_completed", "retained_order_loaded"):
        present = True
    obs.present = present

    # -- identifiers -----------------------------------------------------
    if result == "IDENTIFIABLE":
        obs.exposed = True
    elif result in ("ANONYMIZED", "REDACTED"):
        obs.exposed = False
    elif not is_lookup_row:
        id_fields = [k for k in f if k in DIRECT_ID_FIELDS]
        if id_fields:
            obs.exposed = any(not is_null(f[k]) for k in id_fields)
        direct = f.get("direct_identifiers")
        if direct is not None:
            obs.exposed = str(direct).strip().lower() not in NULLS | {"0", "false", "none"}
        if rec.kind == "pdf":
            obs.exposed = True
        ref_fields = [k for k in f if k in STABLE_REF_FIELDS]
        if ref_fields:
            obs.stable_ref = any(not is_null(f[k]) for k in ref_fields)

    # -- processes ---------------------------------------------------------
    if prefix in ("data_flow", "backup"):
        if result == "ACTIVE":
            obs.active = True
        elif result in ("DISABLED", "BLOCKED"):
            obs.active = False
        for key in ("enabled", "restore_allowed"):
            val = as_bool(f.get(key))
            if val is not None:
                obs.active = val
        eligible, block = as_bool(f.get("eligible")), as_bool(f.get("deletion_block"))
        if eligible is not None or block is not None:
            obs.active = bool(eligible) and not bool(block)
        decision = str(f.get("decision") or "").lower()
        if decision:
            obs.active = decision in ("allow", "allowed", "approved", "permitted", "restore_allowed")

    # -- record kind hints -------------------------------------------------
    keys = set(f)
    obs.message_like = bool(keys & {"message_excerpt", "message_text", "requester_email", "requester_name",
                                    "ticket_id", "channel"}) or "message" in event or "ticket" in event
    obs.attachment_like = (rec.kind == "pdf" or bool(keys & {"file_name", "uploaded_by", "file", "storage_class"})
                           or "attachment" in event)
    corr = str(f.get("correlation_key") or "")
    obs.hash_keyed = prefix == "analytics" and event == "contact_lookup_completed" and bool(
        re.fullmatch(r"[0-9a-f]{64}", corr.lower()))
    if not is_null(f.get("retention_until")):
        obs.retention_until = str(f["retention_until"])
    return obs


# ---------------------------------------------------------------------------
# Resource state and policy
# ---------------------------------------------------------------------------


@dataclass
class ResourceState:
    resource_id: str
    system_id: str
    observations: list[Observation]
    present: bool | None = None
    exposed: bool | None = None
    stable_ref: bool | None = None
    active: bool | None = None
    category: str = ""
    evidence: dict[str, str] = field(default_factory=dict)  # state label -> cited file


def latest(observations: list[Observation], attr: str):
    """Value of ``attr`` from the most recent observation that reports it."""
    best = None
    for obs in sorted(observations, key=lambda o: (o.time, getattr(o, attr) is True)):
        value = getattr(obs, attr)
        if value is not None:
            best = value
    return best


def category_for(state: ResourceState) -> str:
    prefix = resource_prefix(state.resource_id)
    base = PREFIX_CATEGORY.get(prefix, "")
    types = {o.record_type for o in state.observations if o.record_type}
    if prefix == "support":
        if "SUPPORT_ATTACHMENT" in types:
            return "SUPPORT_ATTACHMENT"
        if "SUPPORT_MESSAGE" in types:
            return "SUPPORT_MESSAGE"
        if any(o.attachment_like for o in state.observations):
            return "SUPPORT_ATTACHMENT"
        if any(o.message_like for o in state.observations):
            return "SUPPORT_MESSAGE"
        return "SUPPORT_ATTACHMENT"  # contact/index copies that belong only to the subject
    if prefix == "analytics" and any(o.hash_keyed for o in state.observations):
        return "SECONDARY_IDENTIFIER"
    return base


def build_state(ds: Dataset, resource_id: str, audit_dt: datetime, records: list[EvidenceRecord],
                since: datetime | None = None) -> ResourceState:
    observations = []
    for rec in records:
        if not ds.record_visible(rec, audit_dt):
            continue
        ef = ds.files[rec.file_id]
        obs = observe(rec, ef.captured_dt, ds.file_citable(rec.file_id, audit_dt))
        if obs is not None and (since is None or obs.time >= since):
            observations.append(obs)
    state = ResourceState(resource_id=resource_id, system_id=ds.system_for(resource_id), observations=observations)
    state.present = latest(observations, "present")
    state.exposed = latest(observations, "exposed")
    state.stable_ref = latest(observations, "stable_ref")
    state.active = latest(observations, "active")
    state.category = category_for(state)
    return state


def cite(state: ResourceState, attr: str, value) -> str:
    """Latest citable observation supporting ``attr == value``; else latest citable at all."""
    support = [o for o in state.observations if o.citable and getattr(o, attr) == value]
    pool = support or [o for o in state.observations if o.citable]
    if not pool:
        return ""
    return max(pool, key=lambda o: (o.time, o.file_id)).file_id


@dataclass
class Decision:
    findings: list[tuple[str, str]] = field(default_factory=list)  # (operation, evidence)
    remediation: list[str] = field(default_factory=list)
    verification: list[tuple[str, str]] = field(default_factory=list)  # (operation, evidence)


def decide(state: ResourceState) -> Decision | None:
    """Policy for one resource. None when the resource needs no step."""
    d = Decision()
    cat = state.category
    if cat in DELETE_CATEGORIES:
        if state.present:
            d.findings.append(("REPORT_REMAINING_RECORD", cite(state, "present", True)))
            d.remediation.append("DELETE_RECORD")
        d.verification.append(("VERIFY_RECORD_ABSENT", cite(state, "present", False)))
    elif cat in REDACT_CATEGORIES:
        if state.present is False:
            d.verification.append(("VERIFY_RECORD_ABSENT", cite(state, "present", False)))
            return d
        if state.exposed:
            d.findings.append(("REPORT_EXPOSED_PII", cite(state, "exposed", True)))
            d.remediation.append("REDACT_RECORD")
        d.verification.append(("VERIFY_RECORD_REDACTED", cite(state, "exposed", False)))
    elif cat in ANONYMIZE_CATEGORIES:
        if state.present is False:
            d.verification.append(("VERIFY_RECORD_ABSENT", cite(state, "present", False)))
            return d
        if state.exposed or (state.exposed is None and state.stable_ref):
            d.findings.append(("REPORT_EXPOSED_PII", cite(state, "exposed", True)))
            d.remediation.append("ANONYMIZE_RECORD")
        d.verification.append(("VERIFY_RECORD_ANONYMIZED", cite(state, "exposed", False)))
    elif cat in RETAIN_CATEGORIES:
        if state.present:
            d.verification.append(("VERIFY_RECORD_PRESENT", cite(state, "present", True)))
        else:
            return None
    elif cat == "DATA_FLOW":
        if state.active:
            d.findings.append(("REPORT_ACTIVE_DATA_FLOW", cite(state, "active", True)))
            d.remediation.append("DISABLE_DATA_FLOW")
        d.verification.append(("VERIFY_DATA_FLOW_DISABLED", cite(state, "active", False)))
    elif cat == "BACKUP_ARCHIVE":
        if state.active:
            d.findings.append(("REPORT_RESTORE_RISK", cite(state, "active", True)))
            d.remediation.append("BLOCK_RESTORE")
        d.verification.append(("VERIFY_RESTORE_BLOCKED", cite(state, "active", False)))
    else:
        return None
    return d


# ---------------------------------------------------------------------------
# Linking records that carry no identifiers
# ---------------------------------------------------------------------------


class ProcessingLinker:
    """Link anonymized records to a request through the deletion worker's activity.

    Anonymization removes every identifier, so an anonymized analytics record
    can only be tied to its subject by the processing that touched it. For each
    deletion request with no identity-linked record of that kind, candidates are
    deletion_worker rows on that kind of resource between
    max(requested_at, reported_at - WINDOW) and min(reported_at + WINDOW,
    audit_at) whose resource has evidence visible at audit_at and is not tied to
    another person or claimed by another request.

    The worker processes one subject's records together, so each candidate row
    goes to the request whose own processed records ("anchors": worker rows on
    resources already tied to that request by identifiers) are nearest in
    time. Assignment is one-to-one, closest pairs first, and a pair farther
    than MAX_ANCHOR_GAP_MIN from every anchor is not linked.
    """

    WINDOW_MIN = 150
    MAX_ANCHOR_GAP_MIN = 60
    PREFIXES = ("analytics",)

    def __init__(self, ds: Dataset, account_linker, records_by_resource: dict[str, list[EvidenceRecord]]):
        from datetime import timedelta

        self.ds = ds
        requests = account_linker.requests
        users = account_linker.users
        visible = account_linker.visible_resources
        owners = account_linker.owners
        claimed_by_identity = {rid: cid for cid, rids in visible.items() for rid in rids}

        worker_rows = [row for row in ds.tables["system_activity"] if row["source_system"] == "deletion_worker"]
        self.links: dict[str, dict[str, str]] = {}  # case -> prefix -> resource
        self.ambiguous: dict[str, dict[str, list[str]]] = {}

        anchors: dict[str, list[datetime]] = {}
        for cid, req in requests.items():
            tied = set(visible.get(cid, ()))
            account = account_linker.links.get(cid)
            if account and account.resource_id:
                tied.add(account.resource_id)
            audit = parse_time(req["audit_at"])
            anchors[cid] = [parse_time(row["event_time"]) for row in worker_rows
                            if row["resource_id"] in tied and parse_time(row["event_time"]) <= audit]

        def gap_minutes(cid: str, when: datetime) -> float | None:
            if not anchors.get(cid):
                return None
            return min(abs((when - a).total_seconds()) for a in anchors[cid]) / 60

        for prefix in self.PREFIXES:
            candidates: dict[str, list[tuple[str, datetime]]] = {}
            for cid, req in requests.items():
                user = users.get(cid)
                if user is None or any(resource_prefix(r) == prefix for r in visible.get(cid, ())):
                    continue
                reported, audit = parse_time(req["reported_at"]), parse_time(req["audit_at"])
                lo = max(parse_time(req["requested_at"]), reported - timedelta(minutes=self.WINDOW_MIN))
                hi = min(reported + timedelta(minutes=self.WINDOW_MIN), audit)
                found = []
                for row in worker_rows:
                    rid = row["resource_id"]
                    when = parse_time(row["event_time"])
                    if resource_prefix(rid) != prefix or any(rid == f[0] for f in found):
                        continue
                    if not lo <= when <= hi:
                        continue
                    if owners.get(rid) and user not in owners[rid]:
                        continue
                    if claimed_by_identity.get(rid, cid) != cid:
                        continue
                    if not any(ds.record_visible(rec, audit) for rec in records_by_resource.get(rid, ())):
                        continue
                    found.append((rid, when))
                if found:
                    candidates[cid] = found

            pairs = []
            for cid, rows in candidates.items():
                for rid, when in rows:
                    gap = gap_minutes(cid, when)
                    if gap is not None and gap <= self.MAX_ANCHOR_GAP_MIN:
                        pairs.append((gap, cid, rid))
            used_cases, used_rows = set(), set()
            for gap, cid, rid in sorted(pairs):
                if cid in used_cases or rid in used_rows:
                    continue
                self.links.setdefault(cid, {})[prefix] = rid
                used_cases.add(cid)
                used_rows.add(rid)
            # Requests without anchors: a single unclaimed candidate is theirs.
            changed = True
            while changed:
                changed = False
                for cid in sorted(candidates):
                    if cid in used_cases or anchors.get(cid):
                        continue
                    left = [rid for rid, _ in candidates[cid] if rid not in used_rows]
                    if len(left) == 1:
                        self.links.setdefault(cid, {})[prefix] = left[0]
                        used_cases.add(cid)
                        used_rows.add(left[0])
                        changed = True
            for cid, rows in candidates.items():
                if cid not in used_cases:
                    left = [rid for rid, _ in rows if rid not in used_rows]
                    if left:
                        self.ambiguous.setdefault(cid, {})[prefix] = left

    def linked(self, case_id: str) -> dict[str, str]:
        return self.links.get(case_id, {})


# ---------------------------------------------------------------------------
# Case assembly
# ---------------------------------------------------------------------------


@dataclass
class CaseResult:
    case_id: str
    status: str
    steps: list[dict]
    notes: list[str] = field(default_factory=list)


class Auditor:
    def __init__(self, ds: Dataset, linker, owners: dict[str, set[str]] | None = None,
                 include_ambiguous_account: bool = False):
        self.ds = ds
        self.linker = linker
        self.owners = owners or {}
        self.include_ambiguous_account = include_ambiguous_account
        self.records_by_resource: dict[str, list[EvidenceRecord]] = {}
        for rec in ds.records:
            if rec.resource_id:
                self.records_by_resource.setdefault(rec.resource_id, []).append(rec)
        self.processing = ProcessingLinker(ds, linker, self.records_by_resource)
        self._store_snapshots: list[str] | None = None

    def _account_store_snapshots(self) -> list[str]:
        """Files holding a full snapshot of the account profile store.

        A SQL capture with primary/replica profile tables, or a snapshot bundle
        with an account_service collection. Inventory captures and service
        exports list selected records only, so omission there proves nothing.
        """
        if self._store_snapshots is not None:
            return self._store_snapshots
        found = []
        for fid, ef in self.ds.files.items():
            if fid in self.ds.databases:
                tables = [r[0] for r in self.ds.databases[fid].execute("SELECT name FROM sqlite_master WHERE type='table'")]
                if any(re.fullmatch(r"customer_profiles_(primary|\w*replica\w*)", t) for t in tables):
                    found.append(fid)
            elif ef.path.suffix.lower() == ".json":
                try:
                    payload = json.loads(ef.path.read_text(encoding="utf-8"))
                except (OSError, ValueError):
                    continue
                snapshots = payload.get("snapshots", []) if isinstance(payload, dict) else []
                if any(isinstance(s, dict) and s.get("data_store") == "account_service" for s in snapshots):
                    found.append(fid)
        self._store_snapshots = found
        return found

    def failed_activity(self, resource_id: str, audit_dt: datetime) -> str:
        """Latest failed or partial treatment of a resource at or before audit_at.

        Cited for a finding whose state is only visible in a file captured after
        audit_at (which may not be cited): the failed run corroborates it.
        """
        rows = [row for row in self.ds.activity
                if row["resource_id"] == resource_id and row["reported_result"] in ("FAILED", "PARTIAL")
                and parse_time(row["event_time"]) <= audit_dt]
        return max(rows, key=lambda r: (r["event_time"], r["activity_id"]))["activity_id"] if rows else ""

    def account_fallback_file(self, audit_dt: datetime) -> str:
        """Latest citable full account-store snapshot (proves absence by omission)."""
        best = None
        for fid in self._account_store_snapshots():
            ef = self.ds.files[fid]
            if self.ds.file_citable(fid, audit_dt) and (best is None or ef.captured_dt > self.ds.files[best].captured_dt):
                best = fid
        return best or ""

    def audit(self, request: dict, identity: Identity) -> CaseResult:
        """Audit one request. Ambiguous accounts are left out unless that leaves no step at all."""
        result = self._audit(request, identity, guess_account=self.include_ambiguous_account)
        if not result.steps and not self.include_ambiguous_account:
            # No other evidence: a best-guess account (the earliest candidate delete)
            # beats an unsupported empty answer.
            guessed = self._audit(request, identity, guess_account=True)
            if guessed.steps:
                return guessed
        return result

    def _audit(self, request: dict, identity: Identity, guess_account: bool) -> CaseResult:
        cid = request["case_id"]
        audit_dt = parse_time(request["audit_at"])
        notes = []
        appearances, _ = self.ds.find_appearances(identity, request["audit_at"])

        resources: set[str] = set()
        for a in appearances:
            if a.system_id == "account_service":
                continue  # the account comes from the linker
            owners = self.owners.get(a.resource_id)
            if owners and identity.user_id not in owners:
                continue  # evidence ties it to someone else
            resources.add(a.resource_id)

        link: AccountLink = self.linker.link(cid)
        account = link.resource_id
        if account is None and link.method == "ambiguous" and guess_account and link.candidates:
            account = link.candidates[0]
            notes.append(f"ambiguous account, best guess {account}")
        if account:
            resources.add(account)
        resources.update(self.processing.linked(cid).values())

        # State comes from evidence at or after requested_at: an earlier capture shows
        # the data before the deletion ran, so it can neither prove nor refute the result.
        since = parse_time(request["requested_at"])
        states = [build_state(self.ds, rid, audit_dt, self.records_by_resource.get(rid, []), since)
                  for rid in sorted(resources)]
        findings, remediation, verification = [], [], []
        for state in states:
            if not state.observations and resource_prefix(state.resource_id) != "account":
                continue  # no visible evidence about its state
            decision = decide(state)
            if decision is None:
                continue
            for op, ev in decision.findings:
                findings.append((op, state, ev or self.failed_activity(state.resource_id, audit_dt)))
            for op in decision.remediation:
                remediation.append((op, state, ""))
            for op, ev in decision.verification:
                if resource_prefix(state.resource_id) == "account" and not ev:
                    ev = self.account_fallback_file(audit_dt)
                verification.append((op, state, ev))

        status = "INCOMPLETE" if findings else "COMPLETE"
        steps_raw = findings + remediation + verification
        if status == "INCOMPLETE":
            steps_raw = [(op, st, ev if OP_PHASE_STATE[op][0] == "FINDING" else "") for op, st, ev in steps_raw]
        steps_raw.sort(key=lambda t: (OP_RANK[t[0]], t[1].system_id, t[1].resource_id))
        steps = []
        for n, (op, st, ev) in enumerate(steps_raw, start=1):
            phase, state_label = OP_PHASE_STATE[op]
            steps.append({
                "step_no": n, "phase": phase, "system_id": st.system_id, "resource_id": st.resource_id,
                "operation": op, "state": state_label, "evidence_id": ev,
            })
        return CaseResult(cid, status, steps, notes)
