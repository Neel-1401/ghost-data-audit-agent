"""Build audit_viewer_v2.html: a "privacy forensics console" for output.csv.

Usage (from the repo root):
    python code/ui/build_viewer_v2.py

Reads output.csv, dataset/deletion_requests.csv and, when present,
sample_preds.csv + dataset/sample_output.csv (for the sample score). Writes
one self-contained page (inline CSS, JS and SVG; system fonts; no network)
that opens by double-clicking. Standard library only.

Design (chosen with the ui-ux-pro-max skill): Swiss/minimal dense dashboard,
"vault navy + secure green" palette with red for leaks and amber for ghost
data, system sans + monospace stacks; donut for the status split, stacked
bars for reported-vs-actual, sorted horizontal bars for the per-system
ranking, and a vertical Find -> Fix -> Check timeline per case.
"""

from __future__ import annotations

import csv
import json
import re
from collections import Counter
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
OUTPUT = REPO_ROOT / "output.csv"
REQUESTS = REPO_ROOT / "dataset" / "deletion_requests.csv"
SAMPLE_PREDS = REPO_ROOT / "sample_preds.csv"
SAMPLE_GOLD = REPO_ROOT / "dataset" / "sample_output.csv"
STEP_FIELDS = ("phase", "system_id", "resource_id", "operation", "state", "evidence_id")


def read_rows(path: Path) -> list[dict]:
    with path.open(encoding="utf-8", newline="") as handle:
        return list(csv.DictReader(handle))


def case_key(case_id: str):
    match = re.search(r"(\d+)$", case_id)
    return (case_id[: match.start()] if match else case_id, int(match.group(1)) if match else -1)


def sample_score() -> dict | None:
    """Status accuracy, step precision/recall/F1 and exact cases of sample_preds.csv vs gold."""
    if not (SAMPLE_PREDS.exists() and SAMPLE_GOLD.exists()):
        return None
    gold = {r["case_id"]: r for r in read_rows(SAMPLE_GOLD)}
    pred = {r["case_id"]: r for r in read_rows(SAMPLE_PREDS)}
    status_ok = exact = matched = n_pred = n_gold = 0
    for cid, g in gold.items():
        p = pred.get(cid, {"deletion_status": "", "steps_json": "[]"})
        g_steps = [tuple(s.get(f) for f in ("step_no",) + STEP_FIELDS) for s in json.loads(g["steps_json"])]
        try:
            p_steps = [tuple(s.get(f) for f in ("step_no",) + STEP_FIELDS) for s in json.loads(p["steps_json"])]
        except json.JSONDecodeError:
            p_steps = []
        g_set, p_set = Counter(s[1:] for s in g_steps), Counter(s[1:] for s in p_steps)
        matched += sum((g_set & p_set).values())
        n_gold += len(g_steps)
        n_pred += len(p_steps)
        same_status = g["deletion_status"] == p["deletion_status"]
        status_ok += same_status
        exact += same_status and g_steps == p_steps
    precision = matched / n_pred if n_pred else 0.0
    recall = matched / n_gold if n_gold else 0.0
    f1 = 2 * precision * recall / (precision + recall) if precision + recall else 0.0
    return {"cases": len(gold), "status": status_ok, "exact": exact,
            "precision": round(precision, 3), "recall": round(recall, 3), "f1": round(f1, 3)}


def build_data() -> dict:
    requests = {r["case_id"]: r for r in read_rows(REQUESTS)}
    cases = []
    for row in sorted(read_rows(OUTPUT), key=lambda r: case_key(r["case_id"])):
        req = requests.get(row["case_id"], {})
        steps = json.loads(row["steps_json"])
        cases.append({
            "id": row["case_id"],
            "email": req.get("email", ""),
            "reported": req.get("reported_status", ""),
            "status": row["deletion_status"],
            "requested_at": req.get("requested_at", ""),
            "reported_at": req.get("reported_at", ""),
            "audit_at": req.get("audit_at", ""),
            "text": req.get("request_text", ""),
            "steps": [[s.get(f, "") for f in STEP_FIELDS] for s in steps],
        })
    return {"cases": cases, "score": sample_score()}


VIEWER = REPO_ROOT / "audit_viewer_v2.html"

PAGE = r"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Privacy Forensics Console</title>
<style>
:root { color-scheme: light; --font: ui-sans-serif, system-ui, -apple-system, "Segoe UI", Roboto, "Helvetica Neue", Arial, sans-serif;
  --mono: ui-monospace, "Cascadia Code", "SF Mono", Menlo, Consolas, "Liberation Mono", monospace;
  --bg: #f1f4f8; --surface: #ffffff; --surface-2: #f7f9fc; --text: #0f172a; --muted: #4a5a70; --faint: #6b7a90;
  --border: #d8dfe8; --primary: #1e3a5f; --primary-soft: #e3ebf5; --ring: #1e3a5f;
  --ok: #047857; --ok-soft: #dcf3ea; --leak: #b91c1c; --leak-soft: #fbe4e4; --na: #475569; --na-soft: #e6eaf0;
  --ghost: #92400e; --ghost-soft: #fdf0d5; --ghost-edge: #d97706; --fix: #1d4ed8; --fix-soft: #e0e9fc;
  --bar-track: #e8edf3; --shadow: 0 1px 2px rgba(15, 23, 42, .06); }
:root[data-theme="dark"] { color-scheme: dark;
  --bg: #0b1222; --surface: #121a2c; --surface-2: #192134; --text: #e8edf5; --muted: #a3b0c2; --faint: #8796ab;
  --border: rgba(255, 255, 255, .09); --primary: #8fb3e3; --primary-soft: #1d2b45; --ring: #8fb3e3;
  --ok: #34d399; --ok-soft: #10302a; --leak: #f87171; --leak-soft: #3a1c22; --na: #a3b0c2; --na-soft: #222b3d;
  --ghost: #fbbf24; --ghost-soft: #33280f; --ghost-edge: #f59e0b; --fix: #93b4fb; --fix-soft: #1c2a4a;
  --bar-track: #1f2a3f; --shadow: none; }
@media (prefers-color-scheme: dark) { :root:not([data-theme]) { color-scheme: dark;
  --bg: #0b1222; --surface: #121a2c; --surface-2: #192134; --text: #e8edf5; --muted: #a3b0c2; --faint: #8796ab;
  --border: rgba(255, 255, 255, .09); --primary: #8fb3e3; --primary-soft: #1d2b45; --ring: #8fb3e3;
  --ok: #34d399; --ok-soft: #10302a; --leak: #f87171; --leak-soft: #3a1c22; --na: #a3b0c2; --na-soft: #222b3d;
  --ghost: #fbbf24; --ghost-soft: #33280f; --ghost-edge: #f59e0b; --fix: #93b4fb; --fix-soft: #1c2a4a;
  --bar-track: #1f2a3f; --shadow: none; } }
* { box-sizing: border-box; }
html, body { margin: 0; height: 100%; }
body { background: var(--bg); color: var(--text); font: 14px/1.5 var(--font); display: flex; flex-direction: column; overflow: hidden; }
svg.i { width: 16px; height: 16px; fill: none; stroke: currentColor; stroke-width: 2; stroke-linecap: round; stroke-linejoin: round; flex: none; }
button { font: inherit; color: inherit; cursor: pointer; }
:focus-visible { outline: 2px solid var(--ring); outline-offset: 2px; }
.mono { font-family: var(--mono); font-size: 12.5px; }
@media (prefers-reduced-motion: no-preference) { button, .seg, .sys-row, tbody tr { transition: background-color .18s, border-color .18s, opacity .18s; } }

/* Header */
header { display: flex; align-items: center; gap: 18px; padding: 0 20px; height: 54px; background: var(--surface); border-bottom: 1px solid var(--border); flex: none; }
.brand { display: flex; align-items: center; gap: 10px; font-weight: 700; letter-spacing: .01em; white-space: nowrap; }
.brand .logo { width: 30px; height: 30px; border-radius: 8px; background: var(--primary); color: var(--surface); display: grid; place-items: center; }
.brand small { display: block; font-weight: 500; color: var(--muted); font-size: 11.5px; letter-spacing: .04em; text-transform: uppercase; }
nav.tabs { display: flex; gap: 4px; height: 100%; }
nav.tabs button { background: none; border: 0; border-bottom: 2px solid transparent; padding: 0 14px; color: var(--muted); font-weight: 600; min-height: 44px; }
nav.tabs button[aria-selected="true"] { color: var(--text); border-bottom-color: var(--primary); }
.hdr-right { margin-left: auto; display: flex; align-items: center; gap: 12px; }
.score-pill { display: flex; gap: 8px; align-items: center; font-size: 12.5px; color: var(--muted); border: 1px solid var(--border); border-radius: 999px; padding: 4px 12px; white-space: nowrap; }
.score-pill b { color: var(--text); font-variant-numeric: tabular-nums; }
.icon-btn { display: inline-flex; align-items: center; gap: 6px; background: var(--surface-2); border: 1px solid var(--border); border-radius: 8px; padding: 0 12px; min-height: 36px; }

main { flex: 1; min-height: 0; }
.view { height: 100%; display: none; }
.view.active { display: flex; flex-direction: column; }

/* Overview */
#overview { padding: 16px 20px; gap: 14px; overflow: auto; }
.kpis { display: grid; grid-template-columns: repeat(5, minmax(0, 1fr)); gap: 12px; flex: none; }
.kpi { background: var(--surface); border: 1px solid var(--border); border-radius: 10px; padding: 12px 14px; box-shadow: var(--shadow); text-align: left; display: flex; flex-direction: column; gap: 2px; min-height: 76px; }
button.kpi:hover { border-color: var(--primary); }
.kpi .lbl { display: flex; align-items: center; gap: 6px; color: var(--muted); font-size: 12px; font-weight: 600; text-transform: uppercase; letter-spacing: .05em; }
.kpi .val { font-size: 26px; font-weight: 700; font-variant-numeric: tabular-nums; line-height: 1.2; }
.kpi .sub { color: var(--faint); font-size: 12px; }
.kpi.ghost { border-color: var(--ghost-edge); background: linear-gradient(0deg, var(--ghost-soft), var(--ghost-soft)) , var(--surface); }
.kpi.ghost .lbl, .kpi.ghost .val { color: var(--ghost); }
.charts { display: grid; grid-template-columns: minmax(250px, .8fr) minmax(330px, 1.2fr) minmax(330px, 1.2fr); gap: 12px; flex: 1; min-height: 0; }
.card { background: var(--surface); border: 1px solid var(--border); border-radius: 10px; padding: 14px 16px; box-shadow: var(--shadow); display: flex; flex-direction: column; min-height: 0; }
.card h2 { margin: 0; font-size: 14px; font-weight: 700; }
.card .hint { margin: 2px 0 10px; color: var(--muted); font-size: 12.5px; }
.card .body { flex: 1; min-height: 0; overflow: auto; }

.donut-wrap { display: flex; flex-direction: column; align-items: center; gap: 12px; }
.donut { width: 168px; height: 168px; }
.donut text { fill: var(--text); font-family: var(--font); }
.legend { width: 100%; border-collapse: collapse; font-size: 13px; }
.legend td { padding: 5px 4px; border-top: 1px solid var(--border); }
.legend td.n { text-align: right; font-variant-numeric: tabular-nums; font-weight: 600; }
.legend td.p { text-align: right; color: var(--muted); font-variant-numeric: tabular-nums; width: 56px; }
.legend button { background: none; border: 0; padding: 0; display: inline-flex; align-items: center; gap: 8px; min-height: 28px; }
.legend button:hover { text-decoration: underline; }
.sw { width: 10px; height: 10px; border-radius: 3px; display: inline-block; }

.rva-row { display: grid; grid-template-columns: 136px 1fr 44px; gap: 10px; align-items: center; margin-bottom: 12px; }
.rva-row .name { font-size: 12.5px; font-weight: 600; }
.rva-row .name span { display: block; color: var(--faint); font-weight: 500; font-size: 11.5px; }
.rva-row .tot { text-align: right; color: var(--muted); font-variant-numeric: tabular-nums; font-size: 12.5px; }
.stack { display: flex; height: 30px; border-radius: 6px; overflow: hidden; background: var(--bar-track); }
.seg { border: 0; padding: 0 6px; min-width: 0; font-size: 12px; font-weight: 700; color: #fff; display: flex; align-items: center; justify-content: center; overflow: hidden; white-space: nowrap; }
.seg:hover { opacity: .85; }
.seg.ok { background: var(--ok); } .seg.leak { background: var(--leak); } .seg.na { background: var(--na); }
:root[data-theme="dark"] .seg, :root:not([data-theme]) .seg { color: #0b1222; }
:root[data-theme="light"] .seg { color: #fff; }
.seg.ghostseg { background-image: repeating-linear-gradient(135deg, rgba(255,255,255,.22) 0 6px, transparent 6px 12px); box-shadow: inset 0 0 0 2px var(--ghost-edge); }
.callout { display: flex; gap: 10px; align-items: flex-start; background: var(--ghost-soft); border: 1px solid var(--ghost-edge); border-radius: 8px; padding: 10px 12px; color: var(--text); font-size: 13px; margin-top: 4px; }
.callout svg { color: var(--ghost); margin-top: 2px; }
.callout b { color: var(--ghost); }
.callout button { margin-left: auto; background: var(--surface); border: 1px solid var(--ghost-edge); color: var(--ghost); border-radius: 6px; padding: 4px 10px; font-weight: 600; white-space: nowrap; min-height: 32px; }
.keyrow { display: flex; flex-wrap: wrap; gap: 14px; font-size: 12px; color: var(--muted); margin: 2px 0 12px; }
.keyrow span { display: inline-flex; align-items: center; gap: 6px; }

.sys-row { display: grid; grid-template-columns: 150px 1fr 74px; gap: 10px; align-items: center; width: 100%; background: none; border: 0; border-radius: 6px; padding: 4px 6px; text-align: left; min-height: 36px; }
.sys-row:hover { background: var(--surface-2); }
.sys-row .nm { font-family: var(--mono); font-size: 12px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.sys-row .track { height: 14px; background: var(--bar-track); border-radius: 4px; overflow: hidden; display: flex; }
.sys-row .fill { background: var(--leak); height: 100%; }
.sys-row .fill.risk { background: var(--ghost-edge); }
.sys-row .v { text-align: right; font-variant-numeric: tabular-nums; font-size: 12.5px; }
.sys-row .v b { font-weight: 700; }

/* Cases */
#cases { flex-direction: row; }
.list { width: 50%; min-width: 440px; display: flex; flex-direction: column; border-right: 1px solid var(--border); background: var(--surface); }
.toolbar { padding: 12px 14px 10px; border-bottom: 1px solid var(--border); display: flex; flex-direction: column; gap: 8px; }
.searchrow { display: flex; gap: 8px; }
.searchrow .searchbox { flex: 1; }
.searchbox { display: flex; align-items: center; gap: 8px; border: 1px solid var(--border); border-radius: 8px; padding: 0 10px; background: var(--surface-2); color: var(--muted); }
.searchbox input { flex: 1; border: 0; background: transparent; color: var(--text); font: inherit; min-height: 36px; outline: none; }
.searchbox:focus-within { outline: 2px solid var(--ring); outline-offset: 1px; }
kbd { font-family: var(--mono); font-size: 11px; border: 1px solid var(--border); border-bottom-width: 2px; border-radius: 4px; padding: 0 5px; color: var(--muted); }
.filters { display: flex; flex-wrap: wrap; gap: 6px; align-items: center; }
.chip { border: 1px solid var(--border); background: var(--surface); border-radius: 999px; padding: 0 12px; min-height: 32px; font-size: 13px; font-weight: 600; color: var(--muted); display: inline-flex; align-items: center; gap: 6px; }
.chip[aria-pressed="true"] { background: var(--primary-soft); border-color: var(--primary); color: var(--text); }
.chip .ct { font-variant-numeric: tabular-nums; color: var(--faint); font-weight: 500; }
select { font: inherit; font-size: 13px; color: var(--text); background: var(--surface-2); border: 1px solid var(--border); border-radius: 8px; min-height: 38px; padding: 0 8px; max-width: 46%; }
.active-filter { display: none; align-items: center; gap: 8px; font-size: 12.5px; color: var(--muted); }
.active-filter.on { display: flex; }
.active-filter button { background: none; border: 1px solid var(--border); border-radius: 6px; padding: 2px 8px; font-size: 12px; }
.tbl-wrap { flex: 1; overflow: auto; }
table.cases { width: 100%; border-collapse: collapse; }
table.cases th { position: sticky; top: 0; z-index: 1; background: var(--surface); text-align: left; font-size: 11.5px; letter-spacing: .05em; text-transform: uppercase; color: var(--muted); padding: 8px 14px; border-bottom: 1px solid var(--border); }
table.cases td { padding: 0 14px; height: 38px; border-bottom: 1px solid var(--border); white-space: nowrap; font-size: 13px; }
table.cases td.num { text-align: right; font-variant-numeric: tabular-nums; color: var(--muted); }
table.cases tbody tr { cursor: pointer; }
table.cases tbody tr:hover { background: var(--surface-2); }
table.cases tbody tr.ghost { background: var(--ghost-soft); box-shadow: inset 3px 0 0 var(--ghost-edge); }
table.cases tbody tr.sel { background: var(--primary-soft); box-shadow: inset 3px 0 0 var(--primary); }
.badge { display: inline-flex; align-items: center; gap: 5px; border-radius: 6px; padding: 1px 8px; font-size: 11.5px; font-weight: 700; letter-spacing: .02em; }
.b-COMPLETE { color: var(--ok); background: var(--ok-soft); }
.b-INCOMPLETE { color: var(--leak); background: var(--leak-soft); }
.b-NOT_APPLICABLE, .b-PARTIALLY_COMPLETE, .b-NOT_STARTED { color: var(--na); background: var(--na-soft); }
.gtag { display: inline-flex; align-items: center; gap: 4px; color: var(--ghost); font-size: 11.5px; font-weight: 700; margin-left: 8px; }
.gtag svg { width: 13px; height: 13px; }
.count { padding: 6px 14px; font-size: 12px; color: var(--faint); border-top: 1px solid var(--border); display: flex; gap: 14px; }

.detail { flex: 1; overflow: auto; padding: 18px 24px 28px; }
.empty { color: var(--muted); text-align: center; margin-top: 18vh; }
.empty svg { width: 34px; height: 34px; color: var(--faint); }
.d-head { display: flex; align-items: center; gap: 10px; flex-wrap: wrap; }
.d-head h2 { margin: 0; font-size: 20px; font-family: var(--mono); }
.ghost-banner { display: flex; gap: 8px; align-items: center; margin-top: 10px; padding: 8px 12px; border-radius: 8px; background: var(--ghost-soft); border: 1px solid var(--ghost-edge); color: var(--text); font-size: 13px; }
.ghost-banner svg { color: var(--ghost); }
.facts { display: grid; grid-template-columns: repeat(4, minmax(0, 1fr)); gap: 8px; margin: 14px 0; }
.fact { background: var(--surface); border: 1px solid var(--border); border-radius: 8px; padding: 8px 10px; min-width: 0; }
.fact .k { font-size: 11px; text-transform: uppercase; letter-spacing: .05em; color: var(--muted); font-weight: 600; }
.fact .v { font-family: var(--mono); font-size: 12px; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
.fact .v.wrap { white-space: normal; overflow-wrap: anywhere; }
.req { background: var(--surface); border: 1px solid var(--border); border-left: 3px solid var(--primary); border-radius: 8px; padding: 10px 14px; white-space: pre-wrap; font-size: 13.5px; }
.sec-title { font-size: 11.5px; text-transform: uppercase; letter-spacing: .06em; color: var(--muted); font-weight: 700; margin: 18px 0 8px; }
.timeline { position: relative; margin-left: 4px; }
.stage { position: relative; padding-left: 40px; padding-bottom: 16px; }
.stage::before { content: ""; position: absolute; left: 14px; top: 30px; bottom: 0; width: 2px; background: var(--border); }
.stage:last-child::before { display: none; }
.node { position: absolute; left: 0; top: 0; width: 30px; height: 30px; border-radius: 50%; display: grid; place-items: center; border: 2px solid currentColor; background: var(--surface); }
.stage.find .node { color: var(--leak); } .stage.fix .node { color: var(--fix); } .stage.check .node { color: var(--ok); }
.stage.idle .node { color: var(--faint); }
.stage h3 { margin: 0; font-size: 14px; display: flex; align-items: baseline; gap: 8px; padding-top: 4px; }
.stage h3 span { color: var(--muted); font-weight: 500; font-size: 12.5px; }
.items { margin-top: 8px; display: flex; flex-direction: column; gap: 6px; }
.item { display: grid; grid-template-columns: 28px minmax(0, 1.3fr) minmax(0, 1fr) minmax(0, 1fr) auto; gap: 10px; align-items: center; background: var(--surface); border: 1px solid var(--border); border-radius: 8px; padding: 7px 10px; font-size: 13px; }
.item .no { color: var(--faint); font-family: var(--mono); font-size: 11.5px; }
.item .op { font-weight: 600; font-size: 12.5px; overflow-wrap: anywhere; }
.item .op small { display: block; color: var(--muted); font-weight: 500; font-family: var(--mono); font-size: 11px; }
.item .sys, .item .res { font-family: var(--mono); font-size: 12px; overflow-wrap: anywhere; }
.item .sys { color: var(--muted); }
.ev { font-family: var(--mono); font-size: 11.5px; border-radius: 6px; padding: 2px 8px; border: 1px solid var(--border); white-space: nowrap; }
.ev.cited { color: var(--primary); background: var(--primary-soft); border-color: transparent; }
.ev.future { color: var(--faint); font-style: italic; }
.none { color: var(--muted); font-size: 13px; margin-top: 6px; }

@media (max-width: 1100px) {
  .charts { grid-template-columns: 1fr 1fr; } .charts .card:first-child { grid-column: span 2; }
  .kpis { grid-template-columns: repeat(3, 1fr); } .facts { grid-template-columns: repeat(2, 1fr); }
}
@media (max-width: 820px) {
  body { overflow: auto; } #cases { flex-direction: column; } .list { width: 100%; min-width: 0; height: 60vh; border-right: 0; }
  .charts { grid-template-columns: 1fr; } .charts .card:first-child { grid-column: auto; } .score-pill { display: none; }
}
</style>
</head>
<body>
<svg width="0" height="0" style="position:absolute" aria-hidden="true">
  <symbol id="i-shield" viewBox="0 0 24 24"><path d="M20 13c0 5-3.5 7.5-7.66 8.95a1 1 0 0 1-.67-.01C7.5 20.5 4 18 4 13V6a1 1 0 0 1 1-1c2 0 4.5-1.2 6.24-2.72a1.17 1.17 0 0 1 1.52 0C14.51 3.81 17 5 19 5a1 1 0 0 1 1 1z"/><path d="m9 12 2 2 4-4"/></symbol>
  <symbol id="i-search" viewBox="0 0 24 24"><circle cx="11" cy="11" r="8"/><path d="m21 21-4.3-4.3"/></symbol>
  <symbol id="i-sun" viewBox="0 0 24 24"><circle cx="12" cy="12" r="4"/><path d="M12 2v2M12 20v2M4.93 4.93l1.41 1.41M17.66 17.66l1.41 1.41M2 12h2M20 12h2M6.34 17.66l-1.41 1.41M19.07 4.93l-1.41 1.41"/></symbol>
  <symbol id="i-moon" viewBox="0 0 24 24"><path d="M12 3a6 6 0 0 0 9 9 9 9 0 1 1-9-9Z"/></symbol>
  <symbol id="i-alert" viewBox="0 0 24 24"><path d="m21.73 18-8-14a2 2 0 0 0-3.48 0l-8 14A2 2 0 0 0 4 21h16a2 2 0 0 0 1.73-3"/><path d="M12 9v4M12 17h.01"/></symbol>
  <symbol id="i-wrench" viewBox="0 0 24 24"><path d="M14.7 6.3a1 1 0 0 0 0 1.4l1.6 1.6a1 1 0 0 0 1.4 0l3.77-3.77a6 6 0 0 1-7.94 7.94l-6.91 6.91a2.12 2.12 0 0 1-3-3l6.91-6.91a6 6 0 0 1 7.94-7.94l-3.76 3.76z"/></symbol>
  <symbol id="i-check" viewBox="0 0 24 24"><circle cx="12" cy="12" r="10"/><path d="m9 12 2 2 4-4"/></symbol>
  <symbol id="i-ghost" viewBox="0 0 24 24"><path d="M9 10h.01M15 10h.01M12 2a8 8 0 0 0-8 8v12l3-3 2.5 2.5L12 19l2.5 2.5L17 19l3 3V10a8 8 0 0 0-8-8z"/></symbol>
  <symbol id="i-db" viewBox="0 0 24 24"><ellipse cx="12" cy="5" rx="9" ry="3"/><path d="M3 5v14a9 3 0 0 0 18 0V5"/><path d="M3 12a9 3 0 0 0 18 0"/></symbol>
  <symbol id="i-file" viewBox="0 0 24 24"><path d="M15 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V7Z"/><path d="M14 2v4a2 2 0 0 0 2 2h4"/></symbol>
</svg>
<header>
  <div class="brand"><span class="logo"><svg class="i"><use href="#i-shield"/></svg></span>
    <div>Privacy Forensics Console<small>Deletion audit · Ghost Data Still Exists</small></div></div>
  <nav class="tabs" role="tablist" aria-label="Views">
    <button role="tab" id="tab-overview" aria-controls="overview" aria-selected="true">Overview</button>
    <button role="tab" id="tab-cases" aria-controls="cases" aria-selected="false">Cases</button>
  </nav>
  <div class="hdr-right">
    <div class="score-pill" id="scorepill" hidden></div>
    <button class="icon-btn" id="theme" aria-label="Switch colour theme"><svg class="i"><use href="#i-moon"/></svg><span>Theme</span></button>
  </div>
</header>
<main>
  <section class="view active" id="overview" role="tabpanel" aria-labelledby="tab-overview">
    <div class="kpis" id="kpis"></div>
    <div class="charts">
      <div class="card"><h2>Audit outcome</h2><p class="hint">Status we determined for every request</p><div class="body donut-wrap" id="donut"></div></div>
      <div class="card"><h2>Reported vs actual</h2><p class="hint">Workflow claim (rows) against the audited state (segments)</p><div class="body" id="rva"></div></div>
      <div class="card"><h2>Where data leaks</h2><p class="hint">Cases with a finding in each system · click to inspect</p><div class="body" id="systems"></div></div>
    </div>
  </section>
  <section class="view" id="cases" role="tabpanel" aria-labelledby="tab-cases">
    <div class="list">
      <div class="toolbar">
        <div class="searchrow"><label class="searchbox"><svg class="i"><use href="#i-search"/></svg>
          <input type="search" id="q" placeholder="Search case_id or email" aria-label="Search case_id or email"><kbd>/</kbd></label>
          <select id="sys" aria-label="Filter by system with findings"></select></div>
        <div class="filters" id="filters" role="group" aria-label="Status filter"></div>
        <div class="active-filter" id="active"></div>
      </div>
      <div class="tbl-wrap">
        <table class="cases"><thead><tr><th>Case</th><th>Reported</th><th>Audited</th><th style="text-align:right">Steps</th></tr></thead><tbody id="rows"></tbody></table>
      </div>
      <div class="count"><span id="count"></span><span><kbd>↑</kbd> <kbd>↓</kbd> move · <kbd>Enter</kbd> open</span></div>
    </div>
    <div class="detail" id="detail"><div class="empty"><svg class="i"><use href="#i-file"/></svg><p>Select a case to see its Find → Fix → Check timeline.</p></div></div>
  </section>
</main>
<script id="data" type="application/json">__DATA__</script>
<script>
(function () {
  "use strict";
  var DATA = JSON.parse(document.getElementById("data").textContent);
  var cases = DATA.cases;
  var byId = {}; cases.forEach(function (c) { byId[c.id] = c; });
  var STATUS = ["COMPLETE", "INCOMPLETE", "NOT_APPLICABLE"];
  var LABEL = {COMPLETE: "Complete", INCOMPLETE: "Incomplete", NOT_APPLICABLE: "Not applicable",
               PARTIALLY_COMPLETE: "Partially complete", NOT_STARTED: "Not started"};
  var CLS = {COMPLETE: "ok", INCOMPLETE: "leak", NOT_APPLICABLE: "na"};
  var OP_LABEL = {REPORT_REMAINING_RECORD: "Record still present", REPORT_EXPOSED_PII: "Identifiers exposed",
    REPORT_ACTIVE_DATA_FLOW: "Active data flow", REPORT_RESTORE_RISK: "Restore risk", DISABLE_DATA_FLOW: "Disable data flow",
    BLOCK_RESTORE: "Block restore", DELETE_RECORD: "Delete record", REDACT_RECORD: "Redact identifiers",
    ANONYMIZE_RECORD: "Anonymize record", VERIFY_RECORD_ABSENT: "Verify absent", VERIFY_RECORD_PRESENT: "Verify retained",
    VERIFY_RECORD_REDACTED: "Verify redacted", VERIFY_RECORD_ANONYMIZED: "Verify anonymized",
    VERIFY_DATA_FLOW_DISABLED: "Verify flow disabled", VERIFY_RESTORE_BLOCKED: "Verify restore blocked"};
  var state = {q: "", status: "ALL", reported: null, system: "", selected: null};

  function esc(s) { return String(s == null ? "" : s).replace(/[&<>"']/g, function (ch) { return {"&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;"}[ch]; }); }
  function icon(id) { return '<svg class="i" aria-hidden="true"><use href="#i-' + id + '"/></svg>'; }
  function isGhost(c) { return c.reported === "COMPLETE" && c.status === "INCOMPLETE"; }
  function badge(s) { return '<span class="badge b-' + esc(s) + '">' + esc(LABEL[s] || s || "-") + "</span>"; }
  function when(ts) { var m = /^(\d{4}-\d{2}-\d{2})T(\d{2}:\d{2})/.exec(ts || ""); return m ? m[1] + " " + m[2] + "Z" : esc(ts); }
  function pct(n, d) { return d ? Math.round(100 * n / d) + "%" : "0%"; }
  function findSystems(c) { var s = {}; c.steps.forEach(function (st) { if (st[0] === "FINDING") s[st[1]] = 1; }); return s; }

  // ---- aggregates
  var counts = {COMPLETE: 0, INCOMPLETE: 0, NOT_APPLICABLE: 0}, ghosts = 0, matrix = {};
  var sys = {};
  cases.forEach(function (c) {
    counts[c.status] = (counts[c.status] || 0) + 1;
    if (isGhost(c)) ghosts++;
    matrix[c.reported] = matrix[c.reported] || {COMPLETE: 0, INCOMPLETE: 0, NOT_APPLICABLE: 0};
    matrix[c.reported][c.status]++;
    var seen = {};
    c.steps.forEach(function (st) {
      if (st[0] !== "FINDING") return;
      var s = sys[st[1]] = sys[st[1]] || {cases: 0, findings: 0, risk: 0};
      s.findings++;
      if (st[3] === "REPORT_ACTIVE_DATA_FLOW" || st[3] === "REPORT_RESTORE_RISK") s.risk++;
      if (!seen[st[1]]) { seen[st[1]] = 1; s.cases++; }
    });
  });
  var total = cases.length, reportedComplete = (matrix.COMPLETE ? matrix.COMPLETE.COMPLETE + matrix.COMPLETE.INCOMPLETE + matrix.COMPLETE.NOT_APPLICABLE : 0);

  // ---- header score
  var sc = DATA.score;
  if (sc) {
    var pill = document.getElementById("scorepill");
    pill.hidden = false;
    pill.title = "Public sample set scored against dataset/sample_output.csv";
    pill.innerHTML = "Sample <b>" + sc.status + "/" + sc.cases + "</b> status · <b>" + sc.exact + "/" + sc.cases + "</b> exact · F1 <b>" + sc.f1.toFixed(3) + "</b>";
  }

  // ---- KPIs
  function kpi(lbl, ic, val, sub, action, extra) {
    return '<button class="kpi ' + (extra || "") + '" data-go="' + action + '"><span class="lbl">' + icon(ic) + esc(lbl) +
      '</span><span class="val">' + val + '</span><span class="sub">' + sub + "</span></button>";
  }
  document.getElementById("kpis").innerHTML =
    kpi("Requests audited", "file", total, "518 evaluation requests", "ALL") +
    kpi("Complete", "check", counts.COMPLETE, pct(counts.COMPLETE, total) + " verified clean", "COMPLETE") +
    kpi("Incomplete", "alert", counts.INCOMPLETE, pct(counts.INCOMPLETE, total) + " with remaining data or risk", "INCOMPLETE") +
    kpi("Not applicable", "shield", counts.NOT_APPLICABLE, "not a deletion request", "NOT_APPLICABLE") +
    kpi("Ghost data", "ghost", ghosts, pct(ghosts, reportedComplete) + " of requests reported complete", "GHOST", "ghost");
  document.getElementById("kpis").addEventListener("click", function (e) {
    var b = e.target.closest("[data-go]"); if (!b) return;
    var go = b.getAttribute("data-go");
    setFilters({status: go === "GHOST" ? "GHOST" : go, reported: null, system: ""});
    showView("cases");
  });

  // ---- donut (largest slice from 12 o'clock, direct % labels + legend table)
  (function () {
    var r = 60, C = 2 * Math.PI * r, off = 0, svg = "";
    var order = STATUS.slice().sort(function (a, b) { return counts[b] - counts[a]; });
    var colors = {COMPLETE: "var(--ok)", INCOMPLETE: "var(--leak)", NOT_APPLICABLE: "var(--na)"};
    order.forEach(function (s) {
      var len = C * counts[s] / total;
      svg += '<circle r="' + r + '" cx="84" cy="84" fill="none" stroke="' + colors[s] + '" stroke-width="22" stroke-dasharray="' +
        len + " " + (C - len) + '" stroke-dashoffset="' + (-off) + '" transform="rotate(-90 84 84)"><title>' + LABEL[s] + ": " + counts[s] + " (" + pct(counts[s], total) + ")</title></circle>";
      off += len;
    });
    svg += '<text x="84" y="80" text-anchor="middle" font-size="26" font-weight="700">' + total + '</text><text x="84" y="100" text-anchor="middle" font-size="11" opacity=".7">requests</text>';
    var leg = '<table class="legend"><tbody>' + order.map(function (s) {
      return '<tr><td><button data-st="' + s + '"><span class="sw" style="background:' + colors[s] + '"></span>' + LABEL[s] +
        '</button></td><td class="n">' + counts[s] + '</td><td class="p">' + pct(counts[s], total) + "</td></tr>";
    }).join("") + "</tbody></table>";
    var el = document.getElementById("donut");
    el.innerHTML = '<svg class="donut" viewBox="0 0 168 168" role="img" aria-label="Audit outcome: ' +
      order.map(function (s) { return LABEL[s] + " " + counts[s]; }).join(", ") + '">' + svg + "</svg>" + leg;
    el.addEventListener("click", function (e) {
      var b = e.target.closest("[data-st]"); if (!b) return;
      setFilters({status: b.getAttribute("data-st"), reported: null, system: ""}); showView("cases");
    });
  })();

  // ---- reported vs actual (stacked horizontal bars; ghost segment hatched + outlined)
  (function () {
    var rows = ["COMPLETE", "PARTIALLY_COMPLETE", "NOT_STARTED"].filter(function (r) { return matrix[r]; });
    Object.keys(matrix).forEach(function (r) { if (rows.indexOf(r) < 0) rows.push(r); });
    var html = '<div class="keyrow"><span><span class="sw" style="background:var(--ok)"></span>Audited complete</span>' +
      '<span><span class="sw" style="background:var(--leak)"></span>Audited incomplete</span>' +
      '<span><span class="sw" style="background:var(--na)"></span>Not applicable</span>' +
      '<span><span class="sw" style="background:var(--leak);box-shadow:inset 0 0 0 2px var(--ghost-edge)"></span>Ghost data</span></div>';
    rows.forEach(function (r) {
      var m = matrix[r], t = m.COMPLETE + m.INCOMPLETE + m.NOT_APPLICABLE;
      html += '<div class="rva-row"><div class="name">Reported ' + esc((LABEL[r] || r).toLowerCase()) + "<span>workflow claim</span></div><div class=\"stack\">";
      STATUS.forEach(function (s) {
        if (!m[s]) return;
        var ghost = r === "COMPLETE" && s === "INCOMPLETE";
        html += '<button class="seg ' + CLS[s] + (ghost ? " ghostseg" : "") + '" style="flex:' + m[s] + '" data-r="' + r + '" data-s="' + s +
          '" title="Reported ' + esc(LABEL[r] || r) + " → audited " + LABEL[s] + ": " + m[s] + '" aria-label="Reported ' + esc(LABEL[r] || r) +
          ", audited " + LABEL[s] + ", " + m[s] + ' cases">' + (m[s] / t > .07 ? m[s] : "") + "</button>";
      });
      html += '</div><div class="tot">' + t + "</div></div>";
    });
    html += '<div class="callout">' + icon("ghost") + '<div><b>' + ghosts + " ghost-data cases.</b> The workflow reported " +
      reportedComplete + " requests as complete; the evidence shows personal data or a recreation path still remains in " +
      ghosts + " of them (" + pct(ghosts, reportedComplete) + ').</div><button data-r="COMPLETE" data-s="INCOMPLETE">Review</button></div>';
    var el = document.getElementById("rva");
    el.innerHTML = html;
    el.addEventListener("click", function (e) {
      var b = e.target.closest("[data-r]"); if (!b) return;
      setFilters({status: b.getAttribute("data-s"), reported: b.getAttribute("data-r"), system: ""}); showView("cases");
    });
  })();

  // ---- per-system ranking (sorted descending, direct labels)
  var sysNames = Object.keys(sys).sort(function (a, b) { return sys[b].cases - sys[a].cases || a.localeCompare(b); });
  (function () {
    var max = sysNames.length ? sys[sysNames[0]].cases : 1;
    var html = '<div class="keyrow"><span><span class="sw" style="background:var(--leak)"></span>Remaining or exposed data</span>' +
      '<span><span class="sw" style="background:var(--ghost-edge)"></span>Active flow / restore risk</span></div>';
    sysNames.forEach(function (n) {
      var s = sys[n], w = 100 * s.cases / max, riskShare = s.findings ? s.risk / s.findings : 0;
      html += '<button class="sys-row" data-sys="' + esc(n) + '" title="' + esc(n) + ": " + s.cases + " cases, " + s.findings + ' findings">' +
        '<span class="nm">' + esc(n) + '</span><span class="track"><span class="fill" style="width:' + (w * (1 - riskShare)) +
        '%"></span><span class="fill risk" style="width:' + (w * riskShare) + '%"></span></span><span class="v"><b>' + s.cases + "</b> case" + (s.cases === 1 ? "" : "s") + "</span></button>";
    });
    var el = document.getElementById("systems");
    el.innerHTML = html || '<p class="none">No findings.</p>';
    el.addEventListener("click", function (e) {
      var b = e.target.closest("[data-sys]"); if (!b) return;
      setFilters({status: "INCOMPLETE", reported: null, system: b.getAttribute("data-sys")}); showView("cases");
    });
  })();

  // ---- tabs
  function showView(name) {
    ["overview", "cases"].forEach(function (v) {
      document.getElementById(v).classList.toggle("active", v === name);
      document.getElementById("tab-" + v).setAttribute("aria-selected", v === name);
    });
  }
  document.getElementById("tab-overview").addEventListener("click", function () { showView("overview"); setHash("overview"); });
  document.getElementById("tab-cases").addEventListener("click", function () { showView("cases"); setHash(state.selected || "cases"); });
  document.querySelector("nav.tabs").addEventListener("keydown", function (e) {
    if (e.key !== "ArrowRight" && e.key !== "ArrowLeft") return;
    var next = document.getElementById("tab-overview").getAttribute("aria-selected") === "true" ? "cases" : "overview";
    showView(next); document.getElementById("tab-" + next).focus();
  });

  // ---- filters
  var FILTERS = [["ALL", "All", total], ["GHOST", "Ghost data", ghosts], ["COMPLETE", "Complete", counts.COMPLETE],
                 ["INCOMPLETE", "Incomplete", counts.INCOMPLETE], ["NOT_APPLICABLE", "N/A", counts.NOT_APPLICABLE]];
  var fbox = document.getElementById("filters");
  fbox.innerHTML = FILTERS.map(function (f) {
    return '<button class="chip" data-f="' + f[0] + '" aria-pressed="false">' + (f[0] === "GHOST" ? icon("ghost") : "") + f[1] + ' <span class="ct">' + f[2] + "</span></button>";
  }).join("");
  document.getElementById("sys").innerHTML = '<option value="">All systems</option>' +
    sysNames.map(function (n) { return '<option value="' + esc(n) + '">' + esc(n) + " (" + sys[n].cases + ")</option>"; }).join("");
  fbox.addEventListener("click", function (e) {
    var b = e.target.closest("[data-f]"); if (!b) return;
    setFilters({status: b.getAttribute("data-f"), reported: null});
  });
  document.getElementById("sys").addEventListener("change", function (e) { setFilters({system: e.target.value}); });
  document.getElementById("q").addEventListener("input", function (e) { state.q = e.target.value.trim().toLowerCase(); render(); });
  document.getElementById("active").addEventListener("click", function (e) { if (e.target.closest("button")) setFilters({reported: null, status: "ALL", system: ""}); });

  function setFilters(f) {
    Object.keys(f).forEach(function (k) { state[k] = f[k]; });
    Array.prototype.forEach.call(fbox.querySelectorAll("[data-f]"), function (b) {
      b.setAttribute("aria-pressed", b.getAttribute("data-f") === state.status && !state.reported);
    });
    document.getElementById("sys").value = state.system;
    var act = document.getElementById("active"), parts = [];
    if (state.reported) parts.push("reported <b>" + esc(LABEL[state.reported] || state.reported) + "</b> → audited <b>" + esc(LABEL[state.status] || state.status) + "</b>");
    if (state.system) parts.push("finding in <b class=\"mono\">" + esc(state.system) + "</b>");
    act.innerHTML = parts.length ? "Showing " + parts.join(" · ") + " <button>Clear</button>" : "";
    act.classList.toggle("on", parts.length > 0);
    render();
  }

  function visible() {
    return cases.filter(function (c) {
      if (state.status === "GHOST") { if (!isGhost(c)) return false; }
      else if (state.status !== "ALL" && c.status !== state.status) return false;
      if (state.reported && c.reported !== state.reported) return false;
      if (state.system && !findSystems(c)[state.system]) return false;
      return !state.q || c.id.toLowerCase().indexOf(state.q) >= 0 || c.email.toLowerCase().indexOf(state.q) >= 0;
    });
  }

  var tbody = document.getElementById("rows");
  function render() {
    var list = visible();
    tbody.innerHTML = list.map(function (c) {
      var g = isGhost(c);
      return '<tr tabindex="0" data-id="' + esc(c.id) + '" class="' + (g ? "ghost " : "") + (c.id === state.selected ? "sel" : "") +
        '" aria-selected="' + (c.id === state.selected) + '"><td class="mono">' + esc(c.id) + (g ? '<span class="gtag" title="Reported complete, audited incomplete">' + icon("ghost") + "ghost</span>" : "") +
        "</td><td>" + badge(c.reported) + "</td><td>" + badge(c.status) + '</td><td class="num">' + c.steps.length + "</td></tr>";
    }).join("");
    document.getElementById("count").textContent = list.length + " of " + total + " cases";
  }
  function setHash(h) { try { history.replaceState(null, "", "#" + h); } catch (e) {} }
  function pick(id, focusRow) {
    state.selected = id; render(); showDetail(id); setHash(id);
    if (focusRow) { var tr = tbody.querySelector('tr[data-id="' + id + '"]'); if (tr) { tr.focus(); tr.scrollIntoView({block: "nearest"}); } }
  }
  tbody.addEventListener("click", function (e) { var tr = e.target.closest("tr"); if (tr) pick(tr.getAttribute("data-id")); });
  tbody.addEventListener("keydown", function (e) {
    var tr = e.target.closest("tr"); if (!tr) return;
    if (e.key === "Enter" || e.key === " ") { e.preventDefault(); pick(tr.getAttribute("data-id"), true); }
    else if (e.key === "ArrowDown" && tr.nextElementSibling) { e.preventDefault(); tr.nextElementSibling.focus(); }
    else if (e.key === "ArrowUp" && tr.previousElementSibling) { e.preventDefault(); tr.previousElementSibling.focus(); }
  });
  document.addEventListener("keydown", function (e) {
    if (e.key === "/" && document.activeElement.tagName !== "INPUT") { e.preventDefault(); showView("cases"); document.getElementById("q").focus(); }
  });

  // ---- detail: Find -> Fix -> Check timeline
  function stage(kind, ic, title, sub, steps, emptyText) {
    var h = '<div class="stage ' + kind + (steps.length ? "" : " idle") + '"><span class="node">' + icon(ic) + "</span><h3>" + title +
      " <span>" + sub + "</span></h3>";
    if (!steps.length) return h + '<p class="none">' + emptyText + "</p></div>";
    h += '<div class="items">';
    steps.forEach(function (st) {
      var ev = st.s[5] ? '<span class="ev cited" title="Evidence">' + esc(st.s[5]) + "</span>" : '<span class="ev future" title="Future work: no evidence yet">to do</span>';
      h += '<div class="item"><span class="no">' + st.n + '</span><span class="op">' + esc(OP_LABEL[st.s[3]] || st.s[3]) + "<small>" + esc(st.s[3]) +
        '</small></span><span class="sys">' + esc(st.s[1]) + '</span><span class="res">' + esc(st.s[2]) + "</span>" + ev + "</div>";
    });
    return h + "</div></div>";
  }
  function showDetail(id) {
    var c = byId[id]; if (!c) return;
    var phases = {FINDING: [], REMEDIATION: [], VERIFICATION: []};
    c.steps.forEach(function (s, i) { (phases[s[0]] || []).push({n: i + 1, s: s}); });
    var h = '<div class="d-head"><h2>' + esc(c.id) + "</h2>" + badge(c.status) + (isGhost(c) ? '<span class="gtag">' + icon("ghost") + "ghost data</span>" : "") + "</div>";
    if (isGhost(c)) h += '<div class="ghost-banner">' + icon("alert") + "The workflow reported this deletion as complete, but the evidence shows data or a recreation path remains.</div>";
    h += '<div class="facts"><div class="fact"><div class="k">Subject email</div><div class="v wrap">' + esc(c.email) + '</div></div>' +
      '<div class="fact"><div class="k">Requested</div><div class="v" title="' + esc(c.requested_at) + '">' + when(c.requested_at) + '</div></div>' +
      '<div class="fact"><div class="k">Reported · ' + esc(LABEL[c.reported] || c.reported) + '</div><div class="v" title="' + esc(c.reported_at) + '">' + when(c.reported_at) + '</div></div>' +
      '<div class="fact"><div class="k">Audit cutoff</div><div class="v" title="' + esc(c.audit_at) + '">' + when(c.audit_at) + "</div></div></div>";
    h += '<div class="sec-title">Request</div><div class="req">' + esc(c.text) + "</div>";
    h += '<div class="sec-title">Find → Fix → Check</div>';
    if (c.status === "NOT_APPLICABLE") {
      h += '<p class="none">No deletion was requested, so there is nothing to audit (steps: <span class="mono">[]</span>).</p>';
    } else {
      h += '<div class="timeline">' +
        stage("find", "alert", "Find", phases.FINDING.length + " finding" + (phases.FINDING.length === 1 ? "" : "s") + " backed by evidence", phases.FINDING, "No remaining data or recreation risk found at the audit cutoff.") +
        stage("fix", "wrench", "Fix", phases.REMEDIATION.length + " remediation step" + (phases.REMEDIATION.length === 1 ? "" : "s") + ", in safe order", phases.REMEDIATION, "Nothing to fix.") +
        stage("check", "check", "Check", phases.VERIFICATION.length + " verification" + (phases.VERIFICATION.length === 1 ? "" : "s") + (c.status === "COMPLETE" ? " proving the current state" : " to pass after the fix"), phases.VERIFICATION, "No checks.") +
        "</div>";
    }
    var d = document.getElementById("detail"); d.innerHTML = h; d.scrollTop = 0;
  }

  // ---- theme (per-viewer preference; storage may be unavailable)
  var root = document.documentElement;
  function currentDark() { var t = root.getAttribute("data-theme"); return t ? t === "dark" : window.matchMedia("(prefers-color-scheme: dark)").matches; }
  function syncThemeIcon() { document.querySelector("#theme use").setAttribute("href", currentDark() ? "#i-sun" : "#i-moon"); }
  try { var saved = localStorage.getItem("forensics-theme"); if (saved) root.setAttribute("data-theme", saved); } catch (e) {}
  var qs = /[?&]theme=(light|dark)/.exec(location.search); if (qs) root.setAttribute("data-theme", qs[1]);
  syncThemeIcon();
  document.getElementById("theme").addEventListener("click", function () {
    var next = currentDark() ? "light" : "dark"; root.setAttribute("data-theme", next); syncThemeIcon();
    try { localStorage.setItem("forensics-theme", next); } catch (e) {}
  });

  // ---- start: #case_36 opens that case, #cases the list, otherwise the overview
  setFilters({status: "ALL"});
  var start = decodeURIComponent(location.hash.slice(1));
  if (byId[start]) { showView("cases"); pick(start, true); }
  else if (start === "cases") showView("cases");
})();
</script>
</body>
</html>
"""


def main() -> int:
    data = build_data()
    payload = json.dumps(data, ensure_ascii=False, separators=(",", ":")).replace("</", "<\\/")
    VIEWER.write_text(PAGE.replace("__DATA__", payload), encoding="utf-8")
    counts = Counter(c["status"] for c in data["cases"])
    print(f"Wrote {VIEWER} ({len(data['cases'])} cases: {dict(sorted(counts.items()))})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
