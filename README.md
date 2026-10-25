# Ghost Data Still Exists: deletion-audit agent

HackerRank Orchestrate, October 2026. The full challenge contract is in [`problem_statement.md`](./problem_statement.md).

For every request in `dataset/deletion_requests.csv`, the agent works in four stages:

1. **Is this a deletion request?** It decides whether the request text, in any language, actually authorises deletion. Access, export, correction and marketing opt-out requests are `NOT_APPLICABLE` with `[]`.
2. **Who is the person?** It reconstructs the subject's current and historical identifiers: current and old emails from the audit log, their SHA-256 email hashes, phone numbers, name and `user_id`.
3. **Where is their data?** It searches every evidence file, plus the global activity stream, for records tied to those identifiers. It includes records that are only reachable through resource IDs, log traces, the data catalog or the deletion worker's batch.
4. **What is the state at `audit_at`?** It reconstructs each record's state from evidence only, never from workflow claims, and applies `deletion_rules.csv`. The verdict is `COMPLETE` or `INCOMPLETE`, followed by ordered findings, remediation steps and verification checks that cite participant-visible evidence.

The result is `output.csv` at the repository root: 518 rows of `case_id,deletion_status,steps_json`.

---

## Architecture

```text
dataset/*.csv ──► llm.py ─────────── Gemini: does the text authorise deletion?  (cached; rule fallback)
                     │
dataset/evidence ─► loaders.py ───── parse SQL (in-memory SQLite), JSON, logs, text, PDF (pypdf)
     *.png ──────► vision.py ────── Gemini: transcribe each screenshot into per-resource records (cached)
                     │
                  loaders.py ───── IdentityResolver · find_appearances · AccountLinker
                     │
                  audit.py ─────── observe → state at audit_at → deletion_rules policy → ordered steps
                     │
                  main.py ──────── output.csv  +  code/evaluation/usage.json
```

| File | Role | Deterministic? |
|---|---|---|
| `code/main.py` | Entry point: classify, load evidence, link accounts, audit, write CSV and usage | yes |
| `code/llm.py` | `GeminiClient` (REST, pacing, retry and backoff, token counting) and `DeletionClassifier` (Gemini, plus a multilingual negation-aware rule fallback) | cached Gemini answers; temperature 0 |
| `code/vision.py` | `ImageExtractor`: Gemini transcribes screenshots into a fixed JSON schema; `image_fields` maps it onto audit field names | cached; temperature 0 |
| `code/loaders.py` | Dataset loading and parsing, identity resolution, evidence search with time rules, data-catalog aliases, account linking | yes |
| `code/audit.py` | Observations → resource state → policy category → findings, remediation, verification; analytics batch linking; citations | yes |
| `code/evaluation/main.py` | Scorer against `dataset/sample_output.csv` | yes |
| `code/evaluation/validate_output.py` | Contract checks on `output.csv` | yes |

### What the LLM does, and why only that

The model is used for the two tasks where its language and vision understanding beats hand-written rules:

1. **Deletion intent.**
   - Requests come in many languages and phrasings, for example "do not delete my account, just unsubscribe me" or "I no longer use this account, please erase it".
   - Gemini classifies them in batches of 25 and returns JSON.
   - A rule classifier with multilingual deletion stems and negation scoping is the fallback when no key is set or a call fails. On every request text in the dataset, Gemini and the rules agree.
2. **Screenshots.**
   - 11 evidence files are PNG admin-console screenshots (exports, search, connectors, backups, analytics, support and so on).
   - Gemini transcribes each one once into records with fixed fields: `resource_id`, email, name, phone, `customer_reference`, status, `enabled`, `restore_allowed`, `download_available`.
   - From that point on they are ordinary evidence records.

Everything that decides the answer is plain Python. That covers identity matching, what was visible at `audit_at`, which file may be cited, which policy rule applies, and step order and serialization. Keeping these in code makes the output:

- **Reproducible:** byte-identical across runs.
- **Auditable:** every step traces to a rule and an evidence record.
- **Contract-safe:** the agent never cites a file captured after `audit_at` and never invents an operation.

Both model caches mean a re-run makes no calls.

### Key evidence rules

- **Visibility:**
  - A log or text line counts from its own timestamp.
  - A JSON record inside a snapshot bundle counts from that snapshot's own `captured_at`.
  - A record stamped after `audit_at` is never used.
  - Everything else counts from the file's `captured_at`.
- **Citation:** a file may be an `evidence_id` only if its manifest `captured_at` is at or before `audit_at`.
- **State window:** a record's state comes from evidence at or after `requested_at`, because earlier captures show the data before the deletion ran. Discovery may still use older evidence.
- **Linking:**
  - Direct identifiers come first.
  - Then resource IDs and log `trace_id` groups.
  - Data-catalog rows (`LINK_RESOURCE_IDENTITY`) resolve aliases such as file names and statement numbers.
  - The account is the deletion worker's `DELETE_RECORD` in the request's processing window, preferring the delete whose result matches the request's reported status, with one-to-one elimination across all requests.
  - Anonymized analytics records go to the request whose processing batch ran nearest in time.
- **Policy:** `deletion_rules.csv` by category:
  - Delete records.
  - Redact support messages, email deliveries and account tombstones; a tombstone keeps only the stable reference.
  - Anonymize analytics.
  - Retain audit records and legal holds.
  - Disable data flows.
  - Block backup restores.
- **Ordering:** findings, then remediation, then verification. Within each phase, steps follow the operation order (disable flows and block restores before changing records), then `system_id`, then `resource_id`.

---

## Setup

You need Python 3.10 or newer. The only third-party package is `pypdf` (see `code/requirements.txt`).

**Windows (PowerShell):**

```powershell
cd hackerrank-orchestrate-october26
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r code\requirements.txt
```

**macOS / Linux:**

```bash
cd hackerrank-orchestrate-october26
python3 -m venv .venv
source .venv/bin/activate
pip install -r code/requirements.txt
```

### Gemini API key

Create your own Gemini API key in Google AI Studio (<https://aistudio.google.com/apikey>). No key is included with this project. Then create a file named `.env` in the repository root, next to this README, and replace the placeholder with your key:

```text
GEMINI_API_KEY=your-key-here
```

You can set the `GEMINI_API_KEY` environment variable instead.

- `.env` is listed in `.gitignore`. The key is read only from the environment or `.env` and is never printed or logged.
- Optional settings:
  - `GEMINI_MODEL` (default `gemini-3.5-flash`).
  - `GEMINI_PRICE_INPUT_PER_M` and `GEMINI_PRICE_OUTPUT_PER_M`, to have runs report an estimated cost.
- Without a key the solver still runs. The rule classifier answers uncached texts, and uncached screenshots are skipped.

---

## Run

From the repository root (use `python3` on macOS/Linux if `python` is Python 2):

```bash
# Full evaluation set -> output.csv (518 rows) and code/evaluation/usage.json
python code/main.py

# Check output.csv against the submission contract
python code/evaluation/validate_output.py

# Public samples, scored against dataset/sample_output.csv
python code/main.py --requests dataset/sample_deletion_requests.csv --out sample_preds.csv
python code/evaluation/main.py --pred sample_preds.csv --show-diffs

# Custom evaluation cases
python code/main.py --requests code/evaluation/custom_cases.csv --out code/evaluation/custom_preds.csv --usage-out code/evaluation/custom_usage.json
python code/evaluation/main.py --gold code/evaluation/custom_expected.csv --pred code/evaluation/custom_preds.csv --show-diffs
```

Useful options for `code/main.py`:

| Option | Effect |
|---|---|
| `--no-llm` | Rule classifier only; screenshots are used only from cache |
| `--classify-only` | Classify request texts to fill the cache, then exit without loading evidence (low memory) |
| `--llm-only [--llm-interval 5] [--llm-attempts 6]` | Gemini answers only: paced calls, backoff on 429/5xx, no rule fallback; exits with code 2 if any text stays unanswered |
| `--guess-ambiguous-account` | Always emit a best-guess account when several account deletes fit the window (off by default) |

A full run takes about 15 seconds when the caches are warm.

---

## Viewing results

After a run, build a browsable page from `output.csv`:

```bash
python code/ui/build_viewer_v2.py
```

This writes `audit_viewer_v2.html` at the repository root. It is a single self-contained file (inline CSS, JavaScript and SVG, system fonts, built with the standard library only, no server or network); open it by double-clicking.

- **Overview:** summary tiles, a status donut, a reported-vs-actual breakdown that highlights the "ghost data" cases (reported COMPLETE, audited INCOMPLETE), and a ranking of which systems leak data most often. It also shows the sample score when `sample_preds.csv` and `dataset/sample_output.csv` exist. Clicking a tile, segment or system opens the matching cases.
- **Cases:** search by case_id or email, filter by status or system, and move through rows with the arrow keys. The detail panel shows the request, its timestamps and a Find → Fix → Check timeline with operation, system, resource and evidence id for each step.
- **Links:** `#case_36` opens that case directly, and `?theme=light` or `?theme=dark` forces a theme. Otherwise the page follows the system theme, and the Theme button switches it.

The file is regenerated on each build and is listed in `.gitignore`.

---

## Results

Full details: [`code/evaluation/evaluation_report.md`](./code/evaluation/evaluation_report.md) and [`code/evaluation/usage_report.md`](./code/evaluation/usage_report.md).

**Samples (32 cases):**

| Metric | Score |
|---|---|
| Status accuracy | 32/32 |
| Step precision / recall / F1 | 0.989 / 0.964 / 0.976 |
| Sequence accuracy | 24/32 |

**Full set (518 cases):**

| Status | Rows |
|---|---|
| COMPLETE | 157 |
| INCOMPLETE | 351 |
| NOT_APPLICABLE | 10 |

The validator reports 0 contract errors.

**Model usage:**

- Final run: 0 calls, all cache hits.
- Building the caches: 31 Gemini calls, about 51.7k tokens, about $0.16 at list price.

---

## Repository layout

```text
README.md                      this file
output.csv                     submission predictions (generated)
log.txt                        agent chat transcript (AGENTS.md format)
code/
  main.py  llm.py  vision.py  loaders.py  audit.py  requirements.txt
  .llm_cache.json  .image_cache.json      Gemini answer caches (no secrets)
  evaluation/
    main.py                    scorer
    validate_output.py         output contract checks
    evaluation_report.md       metrics, custom cases, failures and fixes
    usage_report.md            model calls, tokens and cost
    notes.md                   round-by-round tuning log
    custom_cases.csv  custom_expected.csv  custom_preds.csv
    usage.json  usage_ledger.jsonl
  ui/
    build_viewer_v2.py         builds audit_viewer_v2.html (results viewer)
dataset/                       participant data (not packaged)
```

## Packaging and submission

Package `code.zip` from the repository root. It must not contain `dataset/`, `.env`, virtual environments or caches of compiled code.

**Windows (PowerShell):**

```powershell
Get-ChildItem code -Recurse -Directory -Filter __pycache__ | Remove-Item -Recurse -Force
Compress-Archive -Path README.md, code -DestinationPath code.zip -Force
```

**macOS / Linux:**

```bash
zip -r code.zip README.md code -x "*/__pycache__/*" "*/.venv/*" "*/.DS_Store"
```

The two cache files inside `code/` are included on purpose. They let the packaged solution reproduce `output.csv` exactly without any API call.

Submit `code.zip`, `output.csv` and `log.txt` (as `chat_transcript`) at
https://www.hackerrank.com/contests/hackerrank-orchestrate-october26/challenges/ghost-data-still-exists
