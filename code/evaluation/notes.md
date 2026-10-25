# Failures found and fixes made

Running log for the sample-set tuning loop (`python code/main.py --requests dataset/sample_deletion_requests.csv --out sample_preds.csv`, then `python code/evaluation/main.py --pred sample_preds.csv --show-diffs`). Every fix is a general rule; no case IDs, resource IDs or answers are hard-coded.

| Round | Status acc. | Step P / R / F1 | Sequence acc. | Change |
|---|---|---|---|---|
| 1 | 29/32 | 0.830 / 0.756 / 0.791 | 13/32 | First full solver run |
| 2 | 32/32 | 1.000 / 0.896 / 0.945 | 16/32 | Pipe-log parser no longer swallows handover tables |
| 3 | 32/32 | 0.989 / 0.907 / 0.946 | 15/32 | Link anonymized analytics through deletion-worker activity (unique-candidate elimination) |
| 4 | 32/32 | 0.989 / 0.912 / 0.949 | 18/32 | Assign worker rows to the request whose own processed records are nearest in time |
| 5 | 32/32 | 0.989 / 0.917 / 0.952 | 18/32 | Requests with no anchors may take a single unclaimed candidate |
| 6 | 32/32 | 0.989 / 0.917 / 0.952 | 18/32 | Account absence cites the latest full account-store snapshot (unchanged without guessing; see below) |
| 7 | 32/32 | 0.989 / 0.938 / 0.963 | 21/32 | Snapshot bundles: each nested snapshot's own captured_at decides visibility |
| 8 | 32/32 | 0.989 / 0.938 / 0.963 | 21/32 | Screenshots via Gemini, catalog aliases, requested_at state window, fallbacks (fixes 12 invalid rows in the 518 run) |

## Round 1 → 2

**Failure:** case_11, case_13 and case_14 came out COMPLETE and listed resources belonging to other people from the shift handover file (`evidence_03`).

**Cause:** the new pipe-delimited log parser (`| LEVEL | service | trace | event | k=v`) also matched handover rows (`| system | resource | type | result | subject`). It treated `USER_PROFILE`, `SEARCH_INDEX_ENTRY` and the other record types as trace IDs, so all handover rows of one type became a single "trace" and every resource in it linked to every case.

**Fix (general):** a pipe log line must start with a real log level (`DEBUG`, `INFO`, `WARN`, ...). Handover tables fall through to the table parser.

## Round 2 → 3

**Failure:** anonymized analytics records missing in 8 cases (for example case_21, case_25 to case_28).

**Cause:** anonymization removes every identifier, so identity matching cannot reach them. The only link is the deletion worker's `ANONYMIZE_RECORD` row, which runs during the subject's processing batch about 30 minutes before `reported_at`.

**Fix (general):** `ProcessingLinker` collects `deletion_worker` rows on `analytics_*` within ±150 min of `reported_at` (and not after `audit_at`), drops resources tied to another person by evidence or claimed by another request through identifiers, and requires visible evidence of the record's state. Only analytics records qualify: redacted support records, tombstones, backups and data flows keep a stable `customer_reference`, so identity matching already finds those.

**Side effect:** unique-candidate elimination let whichever request sorted first take a shared candidate, adding wrong records to case_09 and case_13. Net sequence went down one.

## Round 3 → 4

**Fix (general):** the worker processes one subject's records together. Each candidate row goes to the request whose anchors are nearest in time; anchors are the worker rows on resources already tied to that request by identifiers or by its linked account. The matching is one-to-one, closest pairs first, capped at 60 minutes. This fixed case_03, case_09, case_13, case_27 and case_28.

**Remaining:** when several requests share a `reported_at`, their policy rows run at identical offsets (−29, −22, −15 and −8 min), so anchor distance cannot separate them (case_25 and case_26; case_19 receives an extra record). No signal in the participant data resolves this.

## Round 4 → 5

**Failure:** case_04 and case_21 lost their analytics record because they have no anchors (their account is not linked).

**Fix (general):** after anchor matching, a request without anchors takes a candidate only if exactly one unclaimed candidate remains. This fixed case_04's analytics step.

## Round 5 → 6

**Failure:** a COMPLETE case whose account has no observation of its own cited the wrong file (case_31: `evidence_54` instead of `evidence_51`).

**Fix (general):** absence by omission is proven only by a full snapshot of the account profile store: a SQL capture with primary or replica profile tables, or a snapshot bundle with an `account_service` collection. Inventory captures and service exports list selected records only. The latest citable such file is cited. This matches all four affected cases (case_01, case_02, case_04 and case_31).

## Round 6 → 7: nested snapshot timestamps

**Failure:** retained security audit records missing in case_23, case_24, case_29 and case_30 (`audit_at` 2026-09-15T12:00).

**Cause:** they appear only in `evidence_02`, a snapshot bundle whose file-level `captured_at` is 2026-09-17T12:00. Each entry in `snapshots` carries its own `captured_at` (from 09-09 to 09-17), but visibility was decided by the file timestamp alone.

**Fix (general), same rule as log lines:**

- A JSON record inside a snapshot whose own `captured_at` is at or before `audit_at` can be used to discover records and state.
- A file is cited only when its file-level `captured_at` in `evidence_files.csv` is at or before `audit_at`. Citation is unchanged.
- Tightening: a JSON record whose own `observed_at` or `recorded_at` is after `audit_at` is never used, even inside a file captured earlier. Two files hold records stamped after their own capture time (`evidence_36`, `evidence_13`).

**Survey of all JSON evidence:** nested snapshot `captured_at` appears in all 12 `customer_data_snapshot_bundle_*` files. Only `evidence_02` holds snapshots captured before a cutoff inside a file captured after it. Service exports use per-record `observed_at` and ledgers use per-record `recorded_at`; neither loosens visibility.

**Result:** all four audit records are found. Compared case by case with round 6, exactly four cases changed, each by one added `VERIFY_RECORD_PRESENT` step that matches the gold answer. Nothing was removed or made worse, no status changed, and `evidence_02` is never cited (all four steps are INCOMPLETE verifications with empty `evidence_id`).

## Classifier: `--llm-only` retry mode

**Problem:** in the first full classification, Gemini returned HTTP 503 (high demand) and then HTTP 429 (quota), so 25 distinct request texts fell back to the rule classifier.

**Change:** `python code/main.py ... --llm-only [--llm-interval 5]`

- There is no rule fallback. Texts already answered by Gemini come from the cache; only the missing ones are requested.
- Calls are paced at least `--llm-interval` seconds apart (default 5) in batches of 10.
- On HTTP 429 or 5xx it backs off from 15 s, doubling up to 120 s, for at most 6 attempts per batch, and honours the provider's `retryDelay` when one is given.
- The cache is saved after every batch, so progress survives a later failure.
- If any text is still unanswered, it prints how many and exits with code 2 without writing output.

Every run now reports how many distinct texts came from Gemini (new calls or cached Gemini answers) and how many from rules; the counts are also stored as `text_sources` in `usage.json`.

**Low-memory retry:** a first background retry was stopped by Claude Code because the machine ran low on memory. `--classify-only` now reads only the request CSVs, classifies them and exits without loading any evidence; the evidence `Dataset` loads only after classification on normal runs. `--llm-attempts` bounds the worst-case wait for a foreground run.

**Result** (`python code/main.py --requests dataset/sample_deletion_requests.csv --classify-only --llm-only --llm-interval 5 --llm-attempts 4`): the 25 missing texts were answered in 3 calls (1,789 input / 284 output tokens). **All 398 distinct request texts are now classified by Gemini, 0 by rules.** Gemini marks exactly the 10 non-deletion requests (case_533 to case_542) and agrees with the rule classifier on every text. Re-scoring the 32 samples gives the same round-7 result: status 32/32, step F1 0.963, 21/32 exact.

## Round 8: first full run, screenshots and the requested_at window

**Failure:** the first full 518-case run passed every format check except two:

- 11 rows were COMPLETE with no steps (for example case_33, case_35, case_39, case_416).
- case_126 had a finding with no evidence_id.

**Cause:** the evidence for most of these subjects exists only in the 11 PNG screenshots, which were skipped. Each screenshot has a companion `LINK_RESOURCE_IDENTITY` row in `system_activity.csv`, the data catalog, mapping its resource (and any on-screen alias) to an email and user_id.

**Fixes (general):**

1. `code/vision.py`: Gemini transcribes each screenshot once into per-resource records (identifiers, status, enabled, restore_allowed, download_available). These are cached in `code/.image_cache.json` and mapped onto the field names the audit rules already read. Free-form labels may not set state fields.
2. Catalog aliases: "linked export_1842 (NST-2026-0718-1842)" maps the bracketed alias to the resource. Screenshot records and PDF blocks without a resource id take the one id or alias they show. Customer references (user_N) are not used as aliases for PDFs.
3. **State window:** gold never cites a file captured before `requested_at` (0 of 75 citations). Resource state is now built only from observations at or after `requested_at`, since an earlier capture shows the data before the deletion ran. Discovery is unchanged. Without this rule, screenshots made for an earlier request of the same person added `object_19` to case_07 and `tombstone_17` to case_17 (19/32). With it, the samples are back to 21/32, identical to round 7.
4. **Zero-step fallback:** when a case would otherwise have no step at all, the best-guess ambiguous account (earliest candidate delete) is used instead of an unsupported empty COMPLETE. No sample changes.
5. **Citation fallback:** a finding whose state is visible only in a file captured after `audit_at` cites the latest FAILED or PARTIAL deletion activity on that resource at or before `audit_at`.

**Result:**

- 518 run: 0 validator errors. Status counts: COMPLETE 158, INCOMPLETE 350, NOT_APPLICABLE 10.
- Samples: unchanged at 32/32, F1 0.963, 21/32.
- 11 image calls in total (one test call, then 10).

## Ambiguous-account experiment

| Mode | Step P / R / F1 | Sequence |
|---|---|---|
| Leave ambiguous accounts out (default) | 0.989 / 0.917 / **0.952** | 18/32 |
| Best guess = earliest candidate delete (`--guess-ambiguous-account`) | 0.968 / 0.927 / 0.947 | **19/32** |

Guessing is right in case_18 and case_31 and wrong in case_20, case_22, case_23 and case_32. It wins one exact case but lowers step F1, so the default leaves the account step out when the linker cannot decide.

## Earlier fixes, made before the scorer existed

- Log formats: added parsing for `[LEVEL] [service] [trace] event: k="v"`, `| LEVEL | service | trace | event | k="v"` and `requestId=... function=... message="<escaped k=v>"`. Previously these lines had no event and no trace, so lookups could not be tied to their results.
- Prefix → system map: support records were labelled `search_service` because support-index updates in the activity stream target the search service. The fixed prefix map now wins over activity-derived guesses.
- Deletion classifier fallback: bare "no" counted as a negation, so "I no longer use this account and would like it permanently deleted" was classified as non-deletion. A negation now counts only when it governs a deletion word.
- Account linker: non-deletion requests have no account delete of their own and claimed other users' accounts by time window. Only requests classified as deletions take part in linking.

## Known gaps (round 7)

1. **Two retained security audit records appear in no participant file:** `audit_record_6645` (case_18) and `audit_record_5569` (case_20). The four found only in `evidence_02` were recovered in round 7.
2. **Account not linked (6 cases).** case_04's `account_004` appears nowhere except the sample answers. In case_18, case_20, case_22, case_23 and case_32, several account deletes fall inside the window and nothing in the evidence separates them.
3. **Shared-batch analytics (case_19, case_25, case_26).** See round 3 → 4.

Every remaining difference is a missing step or a guessed resource. No status is wrong, and no finding or remediation step is wrong.
