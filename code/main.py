"""Ghost Data Still Exists: deletion-audit solver.

Usage (from the repo root):
    python code/main.py                                   # dataset/deletion_requests.csv -> output.csv
    python code/main.py --requests dataset/sample_deletion_requests.csv --out sample_preds.csv
    python code/main.py --no-llm                          # rule-based classifier only

Steps per request:
  1. Classify whether the request authorises deletion (Gemini, cached; rules as fallback).
     Non-deletion requests are NOT_APPLICABLE with an empty step list.
     Screenshot evidence is transcribed by Gemini (cached) into records.
  2. Resolve current and historical identifiers and link the user's account.
  3. Reconstruct each linked record's state at audit_at from the evidence,
     apply deletion_rules.csv, and build the ordered steps.
"""

from __future__ import annotations

import argparse
import csv
import json
import re
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from audit import Auditor  # noqa: E402
from llm import DeletionClassifier  # noqa: E402
from loaders import REPO_ROOT, AccountLinker, Dataset  # noqa: E402
from vision import ImageExtractor  # noqa: E402

LINK_TABLES = ("sample_deletion_requests", "deletion_requests")
LEDGER_PATH = REPO_ROOT / "code" / "evaluation" / "usage_ledger.jsonl"


def case_sort_key(case_id: str):
    match = re.search(r"(\d+)$", case_id)
    return (case_id[: match.start()] if match else case_id, int(match.group(1)) if match else -1)


def resolve_path(value: str) -> Path:
    path = Path(value)
    return path if path.is_absolute() else REPO_ROOT / path


def read_requests(path: Path) -> list[dict]:
    with path.open(encoding="utf-8", newline="") as handle:
        return list(csv.DictReader(handle))


def append_ledger(label: str, *usages) -> None:
    """Record every run that made model calls, so cumulative cost survives cache hits."""
    entries = [u.as_dict() for u in usages if u.calls]
    if not entries:
        return
    line = {"at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), "label": label,
            "usage": [{k: e[k] for k in ("provider", "model", "calls", "input_tokens", "output_tokens")}
                      for e in entries]}
    with LEDGER_PATH.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(line) + "\n")


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--requests", default="dataset/deletion_requests.csv", help="requests CSV")
    parser.add_argument("--out", default="output.csv", help="output CSV path")
    parser.add_argument("--no-llm", action="store_true", help="use the rule-based classifier only")
    parser.add_argument("--llm-only", action="store_true",
                        help="Gemini answers only: pace calls, back off on rate limits, never fall back to "
                             "rules; exit with code 2 if any text stays unanswered")
    parser.add_argument("--llm-interval", type=float, default=5.0,
                        help="seconds between Gemini calls with --llm-only (default 5)")
    parser.add_argument("--llm-attempts", type=int, default=6,
                        help="attempts per Gemini batch with --llm-only (default 6)")
    parser.add_argument("--classify-only", action="store_true",
                        help="only classify request texts (fills the cache); loads no evidence, writes no output")
    parser.add_argument("--guess-ambiguous-account", action="store_true",
                        help="emit a best-guess account when the linker finds several candidates")
    parser.add_argument("--usage-out", default="code/evaluation/usage.json",
                        help="where to write model usage for this run")
    args = parser.parse_args(argv)

    started = time.time()
    requests_path, out_path = resolve_path(args.requests), resolve_path(args.out)
    requests = read_requests(requests_path)

    # 1. Classify every request text the linker considers (target file plus
    #    the other request files, so they can rule each other's accounts out).
    #    Only the request CSVs are read here; evidence loads afterwards.
    link_rows = {r["case_id"]: r for name in LINK_TABLES for r in read_requests(REPO_ROOT / "dataset" / f"{name}.csv")}
    for row in requests:
        link_rows.setdefault(row["case_id"], row)
    if args.llm_only and args.no_llm:
        parser.error("--llm-only and --no-llm cannot be combined")
    if args.llm_only:
        classifier = DeletionClassifier(llm_only=True, min_interval=args.llm_interval,
                                        max_attempts=args.llm_attempts, batch_size=10)
    else:
        classifier = DeletionClassifier(use_llm=not args.no_llm)
    verdicts = classifier.classify_many([r["request_text"] for r in link_rows.values()])
    sources = classifier.source_counts()
    print(f"Classified {len(set(t.strip() for t in verdicts)) + sources['unanswered']} distinct texts: "
          f"Gemini {sources['gemini']} ({sources['gemini_fresh']} new, {sources['gemini_cached']} cached), "
          f"rules {sources['rules']}, unanswered {sources['unanswered']}")
    if classifier.unanswered:
        print(f"--llm-only: {len(classifier.unanswered)} texts still unanswered after retries "
              f"(last error: {classifier.usage.errors[-1] if classifier.usage.errors else 'none'}). "
              "No output written; re-run when the quota resets.")
        return 2
    if args.classify_only:
        append_ledger("classify-only", classifier.usage)
        print(f"Model usage: {classifier.usage.calls} calls, {classifier.usage.input_tokens} in / "
              f"{classifier.usage.output_tokens} out tokens")
        return 0
    eligible = {cid for cid, r in link_rows.items() if verdicts[r["request_text"]]}
    images = ImageExtractor(use_llm=not args.no_llm)
    ds = Dataset(images=images)

    # 2. Link accounts across all eligible requests at once.
    #    Rows from a requests file outside dataset/ join the dataset requests.
    known = {r["case_id"] for name in LINK_TABLES for r in ds.tables[name]}
    linker = AccountLinker(ds, request_tables=LINK_TABLES, eligible_case_ids=eligible,
                           extra_requests=[r for r in requests if r["case_id"] not in known])
    auditor = Auditor(ds, linker, owners=linker.owners, include_ambiguous_account=args.guess_ambiguous_account)

    # 3. Audit.
    rows, counts = [], {}
    for request in sorted(requests, key=lambda r: case_sort_key(r["case_id"])):
        cid = request["case_id"]
        if cid not in eligible:
            status, steps = "NOT_APPLICABLE", []
        else:
            identity = ds.resolver.resolve(request["email"])
            if identity is None:
                status, steps = "NOT_APPLICABLE", []  # no such subject in the identity data
            else:
                result = auditor.audit(request, identity)
                status, steps = result.status, result.steps
        counts[status] = counts.get(status, 0) + 1
        rows.append({"case_id": cid, "deletion_status": status,
                     "steps_json": json.dumps(steps, ensure_ascii=False, separators=(",", ":"))})

    with out_path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=["case_id", "deletion_status", "steps_json"])
        writer.writeheader()
        writer.writerows(rows)

    usage = classifier.usage.as_dict()
    usage["text_sources"] = sources
    usage["images"] = images.usage.as_dict()
    usage["images"]["sources"] = {k: sum(1 for v in images.sources.values() if v == k)
                                  for k in ("llm", "cache", "skipped")}
    usage["images"]["errors"] = images.errors
    usage.update({"requests_file":str(requests_path.relative_to(REPO_ROOT)) if requests_path.is_relative_to(REPO_ROOT)
                  else str(requests_path), "requests": len(requests), "classified_texts": len(set(verdicts)),
                  "seconds": round(time.time() - started, 1)})
    resolve_path(args.usage_out).write_text(json.dumps(usage, indent=2), encoding="utf-8")
    append_ledger(f"run {usage['requests_file']}", classifier.usage, images.usage)

    print(f"Wrote {len(rows)} rows to {out_path} {counts}")
    print(f"Classifier: {usage['calls']} model calls, {usage['input_tokens']} in / {usage['output_tokens']} out tokens, "
          f"{usage['cache_hits']} cached, {usage['rule_fallbacks']} rule fallbacks")
    print(f"Images: {images.usage.calls} model calls, {images.usage.input_tokens} in / "
          f"{images.usage.output_tokens} out tokens, sources {usage['images']['sources']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
