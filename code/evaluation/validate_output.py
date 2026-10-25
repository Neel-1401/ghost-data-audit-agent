"""Check output.csv against the submission contract (problem_statement.md / AGENTS.md section 6).

Usage (from the repo root): python code/evaluation/validate_output.py [--out output.csv] [--requests ...]
"""

from __future__ import annotations

import argparse
import csv
import json
import re
import sys
from datetime import datetime
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
DATASET = REPO_ROOT / "dataset"
FIELDS = {"step_no", "phase", "system_id", "resource_id", "operation", "state", "evidence_id"}
OPS = {
    "FINDING": {"REPORT_REMAINING_RECORD": "PRESENT", "REPORT_EXPOSED_PII": "PRESENT",
                "REPORT_ACTIVE_DATA_FLOW": "ACTIVE", "REPORT_RESTORE_RISK": "ACTIVE"},
    "REMEDIATION": {"DISABLE_DATA_FLOW": "DISABLED", "BLOCK_RESTORE": "BLOCKED", "DELETE_RECORD": "ABSENT",
                    "REDACT_RECORD": "REDACTED", "ANONYMIZE_RECORD": "ANONYMIZED"},
    "VERIFICATION": {"VERIFY_RECORD_ABSENT": "ABSENT", "VERIFY_RECORD_PRESENT": "PRESENT",
                     "VERIFY_RECORD_REDACTED": "REDACTED", "VERIFY_RECORD_ANONYMIZED": "ANONYMIZED",
                     "VERIFY_DATA_FLOW_DISABLED": "DISABLED", "VERIFY_RESTORE_BLOCKED": "BLOCKED"},
}
PHASE_ORDER = {"FINDING": 0, "REMEDIATION": 1, "VERIFICATION": 2}


def ts(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def rows(path: Path) -> list[dict]:
    with path.open(encoding="utf-8", newline="") as handle:
        return list(csv.DictReader(handle))


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default="output.csv")
    ap.add_argument("--requests", default="dataset/deletion_requests.csv")
    args = ap.parse_args()
    out_path, req_path = REPO_ROOT / args.out, REPO_ROOT / args.requests

    requests = {r["case_id"]: r for r in rows(req_path)}
    files = {r["file_id"]: ts(r["captured_at"]) for r in rows(DATASET / "evidence_files.csv")}
    times = {r["activity_id"]: ts(r["event_time"]) for r in rows(DATASET / "system_activity.csv")}
    times.update({r["audit_id"]: ts(r["event_time"]) for r in rows(DATASET / "user_audit_log.csv")})

    errors = []
    with out_path.open(encoding="utf-8", newline="") as handle:
        header = handle.readline().strip()
    if header != "case_id,deletion_status,steps_json":
        errors.append(f"header is {header!r}")
    out = rows(out_path)
    ids = [r["case_id"] for r in out]
    num = lambda c: (re.sub(r"\d+$", "", c), int(re.search(r"(\d+)$", c).group(1)))  # noqa: E731
    if len(out) != len(requests):
        errors.append(f"{len(out)} rows, expected {len(requests)}")
    if ids != sorted(ids, key=num):
        errors.append("rows are not sorted by case_id")
    if len(set(ids)) != len(ids) or set(ids) != set(requests):
        errors.append("case_id set differs from the requests file")

    counts: dict[str, int] = {}
    n_steps = n_cited = 0
    for row in out:
        cid, status = row["case_id"], row["deletion_status"]
        counts[status] = counts.get(status, 0) + 1
        try:
            steps = json.loads(row["steps_json"])
        except json.JSONDecodeError as exc:
            errors.append(f"{cid}: invalid JSON ({exc})")
            continue
        if not isinstance(steps, list):
            errors.append(f"{cid}: steps_json is not an array")
            continue
        if status not in ("COMPLETE", "INCOMPLETE", "NOT_APPLICABLE"):
            errors.append(f"{cid}: unknown status {status}")
        if status == "NOT_APPLICABLE" and steps:
            errors.append(f"{cid}: NOT_APPLICABLE with steps")
        if status != "NOT_APPLICABLE" and not steps:
            errors.append(f"{cid}: {status} with no steps")
        audit = ts(requests[cid]["audit_at"]) if cid in requests else None
        last_phase = -1
        for i, step in enumerate(steps, start=1):
            n_steps += 1
            if set(step) != FIELDS:
                errors.append(f"{cid} step {i}: fields {sorted(step)}")
                continue
            if step["step_no"] != i:
                errors.append(f"{cid} step {i}: step_no {step['step_no']}")
            phase = step["phase"]
            if OPS.get(phase, {}).get(step["operation"]) != step["state"]:
                errors.append(f"{cid} step {i}: {phase}/{step['operation']}/{step['state']} not allowed")
            if PHASE_ORDER.get(phase, -1) < last_phase:
                errors.append(f"{cid} step {i}: phase out of order")
            last_phase = PHASE_ORDER.get(phase, -1)
            ev = step["evidence_id"]
            if status == "COMPLETE" and phase != "VERIFICATION":
                errors.append(f"{cid} step {i}: COMPLETE case with a {phase} step")
            if status == "INCOMPLETE" and phase != "FINDING" and ev:
                errors.append(f"{cid} step {i}: future {phase} step cites {ev}")
            if (phase == "FINDING" or status == "COMPLETE") and not ev:
                errors.append(f"{cid} step {i}: {phase} step without evidence_id")
            if ev:
                n_cited += 1
                when = files.get(ev) or times.get(ev)
                if when is None:
                    errors.append(f"{cid} step {i}: unknown evidence_id {ev}")
                elif audit is not None and when > audit:
                    errors.append(f"{cid} step {i}: {ev} is dated after audit_at")

    print(f"rows={len(out)} statuses={dict(sorted(counts.items()))} steps={n_steps} citations={n_cited}")
    print(f"errors={len(errors)}")
    for line in errors:
        print("  " + line)
    return 1 if errors else 0


if __name__ == "__main__":
    sys.exit(main())
