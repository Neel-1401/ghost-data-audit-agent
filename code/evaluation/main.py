"""
Scorer for the "Ghost Data Still Exists" challenge.

Compares a predictions CSV against a gold CSV (by default dataset/sample_output.csv)
and reports:
  - case-level status accuracy (+ confusion matrix)
  - step-level precision / recall / F1 (steps matched as a multiset per case)
  - field-level accuracy (each step field compared position by position)
  - sequence accuracy (whole steps_json array identical, order included)
  - exact case accuracy (status AND steps identical)
  - format errors (invalid JSON, bad step numbering, wrong ordering, bad evidence ids ...)

Usage (from the repo root):
  python code/evaluation/main.py                         # scores output.csv vs sample_output.csv
  python code/evaluation/main.py --pred my_preds.csv
  python code/evaluation/main.py --show-diffs            # print what went wrong per case
  python code/evaluation/main.py --markdown report.md    # also write a markdown summary
"""

import argparse
import csv
import json
import sys
from collections import Counter
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_GOLD = REPO_ROOT / "dataset" / "sample_output.csv"
DEFAULT_PRED = REPO_ROOT / "output.csv"

STATUSES = ["COMPLETE", "INCOMPLETE", "NOT_APPLICABLE"]
PHASES = ["FINDING", "REMEDIATION", "VERIFICATION"]

# operation -> (phase, required state), in the order given by the problem statement
OPERATIONS = {
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
OP_ORDER = {op: i for i, op in enumerate(OPERATIONS)}

STEP_FIELDS = ["step_no", "phase", "system_id", "resource_id", "operation", "state", "evidence_id"]
# Fields that identify a step for multiset matching (step_no is positional, so excluded)
MATCH_FIELDS = ["phase", "operation", "system_id", "resource_id", "state", "evidence_id"]


# --------------------------------------------------------------------------- loading

def load_csv(path):
    with open(path, newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        return reader.fieldnames, list(reader)


def parse_steps(raw):
    """Return (steps, error). steps is a list of dicts or None if unparsable."""
    try:
        steps = json.loads(raw)
    except (json.JSONDecodeError, TypeError) as e:
        return None, f"invalid JSON: {e}"
    if not isinstance(steps, list):
        return None, "steps_json is not a JSON array"
    for i, s in enumerate(steps):
        if not isinstance(s, dict):
            return None, f"step {i + 1} is not an object"
    return steps, None


def load_valid_evidence_ids():
    """file_id / activity_id / audit_id values that a step may cite."""
    ids = set()
    sources = [
        ("evidence_files.csv", "file_id"),
        ("system_activity.csv", "activity_id"),
        ("user_audit_log.csv", "audit_id"),
    ]
    for fname, col in sources:
        p = REPO_ROOT / "dataset" / fname
        if p.exists():
            _, rows = load_csv(p)
            ids.update(r[col] for r in rows)
    return ids


# --------------------------------------------------------------------------- validation

def validate_case(status, steps, valid_evidence):
    """Check one predicted row against the output contract. Returns a list of error strings."""
    errs = []
    if status not in STATUSES:
        errs.append(f"unknown deletion_status '{status}'")
    if steps is None:
        return errs

    if status == "NOT_APPLICABLE" and steps:
        errs.append("NOT_APPLICABLE must have an empty steps array")
    if status in ("COMPLETE", "INCOMPLETE") and not steps:
        errs.append(f"{status} has no steps")

    for i, s in enumerate(steps, start=1):
        missing = [k for k in STEP_FIELDS if k not in s]
        if missing:
            errs.append(f"step {i}: missing fields {missing}")
            continue
        if s["step_no"] != i:
            errs.append(f"step {i}: step_no is {s['step_no']}, expected {i}")
        op = s["operation"]
        if op not in OPERATIONS:
            errs.append(f"step {i}: unknown operation '{op}'")
            continue
        phase, state = OPERATIONS[op]
        if s["phase"] != phase:
            errs.append(f"step {i}: {op} must be phase {phase}, got {s['phase']}")
        if s["state"] != state:
            errs.append(f"step {i}: {op} must have state {state}, got {s['state']}")
        if status == "COMPLETE" and phase != "VERIFICATION":
            errs.append(f"step {i}: COMPLETE cases may only contain VERIFICATION steps")
        ev = s["evidence_id"]
        if status == "COMPLETE" or phase == "FINDING":
            if not ev:
                errs.append(f"step {i}: {phase} step must cite evidence")
            elif valid_evidence and ev not in valid_evidence:
                errs.append(f"step {i}: unknown evidence_id '{ev}'")
        elif status == "INCOMPLETE" and ev != "":
            errs.append(f"step {i}: INCOMPLETE {phase} step must have empty evidence_id")

    # ordering: phase, then operation order, then system_id, then resource_id
    def sort_key(s):
        return (OP_ORDER.get(s.get("operation"), 99), str(s.get("system_id")), str(s.get("resource_id")))

    if all(k in s for s in steps for k in ("operation", "system_id", "resource_id")):
        if [sort_key(s) for s in steps] != sorted(sort_key(s) for s in steps):
            errs.append("steps are not in the required order (phase > operation > system_id > resource_id)")
    return errs


# --------------------------------------------------------------------------- scoring

def step_tuple(s, fields=MATCH_FIELDS):
    return tuple(str(s.get(k, "")) for k in fields)


def score(gold_rows, pred_rows, valid_evidence):
    pred_by_id = {}
    duplicate_ids = []
    for r in pred_rows:
        if r["case_id"] in pred_by_id:
            duplicate_ids.append(r["case_id"])
        pred_by_id[r["case_id"]] = r

    res = {
        "n_cases": len(gold_rows),
        "missing_cases": [],
        "duplicate_cases": duplicate_ids,
        "status_correct": 0,
        "confusion": Counter(),            # (gold, pred) -> count
        "tp": 0, "fp": 0, "fn": 0,         # step multiset matching, all fields
        "tp_core": 0, "fp_core": 0, "fn_core": 0,  # ignoring evidence_id
        "field_correct": Counter(), "field_total": 0,
        "sequence_correct": 0,
        "exact_correct": 0,
        "format_errors": {},               # case_id -> [errors]
        "diffs": {},                       # case_id -> details for --show-diffs
    }
    core_fields = [f for f in MATCH_FIELDS if f != "evidence_id"]

    for g in gold_rows:
        cid = g["case_id"]
        gold_steps, _ = parse_steps(g["steps_json"])
        p = pred_by_id.get(cid)
        if p is None:
            res["missing_cases"].append(cid)
            res["confusion"][(g["deletion_status"], "<missing>")] += 1
            res["fn"] += len(gold_steps)
            res["fn_core"] += len(gold_steps)
            res["field_total"] += len(gold_steps)
            continue

        pred_steps, perr = parse_steps(p.get("steps_json", ""))
        errs = [perr] if perr else []
        errs += validate_case(p.get("deletion_status", ""), pred_steps, valid_evidence)
        if errs:
            res["format_errors"][cid] = errs
        pred_steps = pred_steps or []

        # status
        gs, ps = g["deletion_status"], p.get("deletion_status", "")
        res["confusion"][(gs, ps)] += 1
        status_ok = gs == ps
        res["status_correct"] += status_ok

        # step multiset matching
        for fields, prefix in ((MATCH_FIELDS, ""), (core_fields, "_core")):
            gc = Counter(step_tuple(s, fields) for s in gold_steps)
            pc = Counter(step_tuple(s, fields) for s in pred_steps)
            tp = sum((gc & pc).values())
            res["tp" + prefix] += tp
            res["fp" + prefix] += sum(pc.values()) - tp
            res["fn" + prefix] += sum(gc.values()) - tp

        # field-level, position by position (gold length defines the denominator)
        n = max(len(gold_steps), len(pred_steps))
        res["field_total"] += n
        for i in range(n):
            gstep = gold_steps[i] if i < len(gold_steps) else {}
            pstep = pred_steps[i] if i < len(pred_steps) else {}
            for f in STEP_FIELDS:
                if gstep and pstep and str(gstep.get(f)) == str(pstep.get(f)):
                    res["field_correct"][f] += 1

        # sequence + exact
        seq_ok = [step_tuple(s, STEP_FIELDS) for s in gold_steps] == [step_tuple(s, STEP_FIELDS) for s in pred_steps]
        res["sequence_correct"] += seq_ok
        res["exact_correct"] += seq_ok and status_ok

        if not (seq_ok and status_ok):
            gset = Counter(step_tuple(s) for s in gold_steps)
            pset = Counter(step_tuple(s) for s in pred_steps)
            res["diffs"][cid] = {
                "gold_status": gs,
                "pred_status": ps,
                "missing_steps": list((gset - pset).elements()),
                "extra_steps": list((pset - gset).elements()),
                "order_only": gset == pset and not seq_ok,
            }
    return res


def prf(tp, fp, fn):
    p = tp / (tp + fp) if tp + fp else 0.0
    r = tp / (tp + fn) if tp + fn else 0.0
    f = 2 * p * r / (p + r) if p + r else 0.0
    return p, r, f


def pct(a, b):
    return f"{100 * a / b:.1f}%" if b else "n/a"


# --------------------------------------------------------------------------- reporting

def build_report(res, gold_path, pred_path):
    n = res["n_cases"]
    lines = []
    add = lines.append
    add(f"# Evaluation report\n")
    add(f"- Gold: `{gold_path}`")
    add(f"- Predictions: `{pred_path}`")
    add(f"- Cases scored: {n}")
    if res["missing_cases"]:
        add(f"- **Missing predictions:** {len(res['missing_cases'])} ({', '.join(res['missing_cases'][:10])}...)")
    if res["duplicate_cases"]:
        add(f"- **Duplicate case_ids in predictions:** {res['duplicate_cases']}")
    add("")

    add("## Headline metrics\n")
    add("| Metric | Score |")
    add("|---|---|")
    add(f"| Status accuracy | {res['status_correct']}/{n} ({pct(res['status_correct'], n)}) |")
    p, r, f = prf(res["tp"], res["fp"], res["fn"])
    add(f"| Step precision / recall / F1 (all fields) | {p:.3f} / {r:.3f} / {f:.3f} |")
    p, r, f = prf(res["tp_core"], res["fp_core"], res["fn_core"])
    add(f"| Step precision / recall / F1 (ignoring evidence_id) | {p:.3f} / {r:.3f} / {f:.3f} |")
    add(f"| Sequence accuracy (steps identical incl. order) | {res['sequence_correct']}/{n} ({pct(res['sequence_correct'], n)}) |")
    add(f"| Exact case accuracy (status + steps) | {res['exact_correct']}/{n} ({pct(res['exact_correct'], n)}) |")
    add(f"| Cases with format errors | {len(res['format_errors'])} |")
    add("")

    add("## Field-level accuracy (position by position)\n")
    add("| Field | Accuracy |")
    add("|---|---|")
    for fld in STEP_FIELDS:
        add(f"| {fld} | {pct(res['field_correct'][fld], res['field_total'])} |")
    add("")

    add("## Status confusion matrix (rows = gold, columns = predicted)\n")
    cols = STATUSES + (["<missing>"] if res["missing_cases"] else [])
    add("| gold \\ pred | " + " | ".join(cols) + " |")
    add("|---|" + "---|" * len(cols))
    for gs in STATUSES:
        add(f"| {gs} | " + " | ".join(str(res["confusion"][(gs, c)]) for c in cols) + " |")
    add("")
    return "\n".join(lines)


def print_diffs(res, limit):
    shown = 0
    for cid, d in res["diffs"].items():
        if shown >= limit:
            print(f"... {len(res['diffs']) - limit} more cases with differences")
            break
        shown += 1
        print(f"\n--- {cid}: gold={d['gold_status']} pred={d['pred_status']}"
              + ("  (same steps, WRONG ORDER)" if d["order_only"] else ""))
        for s in d["missing_steps"]:
            print("   MISSING ", " | ".join(s))
        for s in d["extra_steps"]:
            print("   EXTRA   ", " | ".join(s))
        for e in res["format_errors"].get(cid, []):
            print("   FORMAT  ", e)


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--gold", default=str(DEFAULT_GOLD), help="gold CSV (default: dataset/sample_output.csv)")
    ap.add_argument("--pred", default=str(DEFAULT_PRED), help="predictions CSV (default: output.csv)")
    ap.add_argument("--show-diffs", action="store_true", help="print missing/extra steps per wrong case")
    ap.add_argument("--diff-limit", type=int, default=20)
    ap.add_argument("--markdown", help="also write the report to this markdown file")
    args = ap.parse_args()

    if not Path(args.pred).exists():
        sys.exit(f"Predictions file not found: {args.pred}")

    gold_header, gold_rows = load_csv(args.gold)
    pred_header, pred_rows = load_csv(args.pred)
    if pred_header != ["case_id", "deletion_status", "steps_json"]:
        print(f"WARNING: prediction header is {pred_header}, expected case_id,deletion_status,steps_json")

    res = score(gold_rows, pred_rows, load_valid_evidence_ids())
    report = build_report(res, args.gold, args.pred)
    print(report)

    if res["format_errors"]:
        print(f"Format errors in {len(res['format_errors'])} case(s); first few:")
        for cid, errs in list(res["format_errors"].items())[:5]:
            print(f"  {cid}: {errs[0]}" + (f" (+{len(errs) - 1} more)" if len(errs) > 1 else ""))

    if args.show_diffs:
        print_diffs(res, args.diff_limit)

    if args.markdown:
        Path(args.markdown).write_text(report, encoding="utf-8")
        print(f"\nWrote {args.markdown}")


if __name__ == "__main__":
    main()