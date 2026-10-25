# Evaluation report: Ghost Data Still Exists

All numbers come from the final code that produced the submitted `output.csv`.

- Scorer: `code/evaluation/main.py` (unchanged).
- Format validator: `code/evaluation/validate_output.py`.

## 1. Public sample set (32 cases)

Commands (from the repo root):

```text
python code/main.py --requests dataset/sample_deletion_requests.csv --out sample_preds.csv
python code/evaluation/main.py --pred sample_preds.csv --show-diffs
```

### Headline metrics

| Metric | Score |
|---|---|
| Case-level deletion-status accuracy | **32/32 (100.0%)** |
| Step precision / recall / F1 (all fields) | 0.989 / 0.938 / **0.963** |
| Step precision / recall / F1 (ignoring evidence_id) | 0.989 / 0.938 / 0.963 |
| Sequence accuracy (steps identical, including order) | **21/32 (65.6%)** |
| Exact case accuracy (status + steps) | 21/32 (65.6%) |
| Cases with format errors | 0 |

A step counts as matched only when all seven fields are equal. The scorer reports these step metrics as precision, recall and F1.

### Field-level accuracy

Fields are compared position by position, step *n* of the prediction against step *n* of the gold answer.

| Field | Accuracy |
|---|---|
| step_no | 93.8% |
| phase | 93.8% |
| system_id | 85.1% |
| resource_id | 84.5% |
| operation | 87.6% |
| state | 87.6% |
| evidence_id | 93.3% |

The position-by-position fields score lower than step F1 because one missing step shifts every later step out of position.

### Status confusion matrix (rows = gold, columns = predicted)

| gold \ pred | COMPLETE | INCOMPLETE | NOT_APPLICABLE |
|---|---|---|---|
| COMPLETE | 10 | 0 | 0 |
| INCOMPLETE | 0 | 22 | 0 |
| NOT_APPLICABLE | 0 | 0 | 0 |

### What the 11 inexact cases have in common

- Every finding and remediation step that the solver emits is correct.
- All differences are in verification steps:
  - 12 gold steps are missing.
  - 2 predicted steps are extra.
- No status is wrong.
- The causes are listed under "Known unreachable or ambiguous items" below.

## 2. Full evaluation set (518 cases)

`python code/main.py` (no arguments) reads `dataset/deletion_requests.csv` and writes `output.csv`. Then `python code/evaluation/validate_output.py` checks:

| Check | Result |
|---|---|
| Header is `case_id,deletion_status,steps_json` | pass |
| Exactly 518 rows, one per request, sorted by case_id | pass |
| `steps_json` is valid JSON | pass |
| Each step has exactly the 7 contract fields | pass |
| Operations and states are allowed and phase-consistent | pass |
| `step_no` is consecutive from 1 | pass |
| Phases are ordered FINDING → REMEDIATION → VERIFICATION | pass |
| `NOT_APPLICABLE` rows have `[]` | pass |
| COMPLETE and INCOMPLETE rows have at least one step | pass |
| Every finding cites evidence; every COMPLETE verification cites evidence | pass |
| INCOMPLETE remediation and verification steps have an empty evidence_id | pass |
| Every evidence_id is a real `file_id`, `activity_id` or `audit_id` | pass |
| No cited file or row is dated after that case's `audit_at` | pass |

Validator summary: 0 errors.

| Item | Count |
|---|---|
| Steps | 2,900 |
| Citations | 1,124 |

| Status | Rows |
|---|---|
| COMPLETE | 158 |
| INCOMPLETE | 350 |
| NOT_APPLICABLE | 10 |
| **Total** | **518** |

Two runs produce byte-identical `output.csv` files, with the same SHA-256 hash.

## 3. Additional evaluation cases (created by me)

Files:

- `code/evaluation/custom_cases.csv`: the 7 requests, in the same format as `deletion_requests.csv`.
- `code/evaluation/custom_expected.csv`: the expected answers.

Commands:

```text
python code/main.py --requests code/evaluation/custom_cases.csv --out code/evaluation/custom_preds.csv --usage-out code/evaluation/custom_usage.json
python code/evaluation/main.py --gold code/evaluation/custom_expected.csv --pred code/evaluation/custom_preds.csv --show-diffs
```

How the expected answers were set:

- **Non-deletion cases:** the expected answer is `NOT_APPLICABLE` with `[]`. These requests reuse a real subject and real timestamps, so the evidence would support a deletion audit if the text were misread.
- **Deletion cases:** each reuses the subject and timestamps of a public sample whose answer is published in `sample_output.csv`. The identity, cutoff and evidence are therefore the same, and the correct answer is that sample's answer. Only the request email or language changes, which is exactly what the case tests.

### Inputs

All identities are the fictional subjects from `dataset/`.

| Case | email | requested_at | reported_status | reported_at | audit_at | request_text |
|---|---|---|---|---|---|---|
| custom_01 | leila.haddad@yahoo.com | 2026-08-27T14:17:00Z | COMPLETE | 2026-08-28T09:03:00Z | 2026-09-08T13:40:00Z | Hallo, bitte schicken Sie mir eine vollständige Kopie aller personenbezogenen Daten, die Sie zu meinem Konto gespeichert haben. Ich möchte mein Konto behalten. |
| custom_02 | jonas.berg@outlook.com | 2026-08-29T06:51:00Z | PARTIALLY_COMPLETE | 2026-08-30T17:12:00Z | 2026-09-10T09:15:00Z | Hola, necesito exportar todos mis pedidos y datos de perfil en un archivo CSV para llevarlos a otro servicio. No quiero cerrar la cuenta. |
| custom_03 | priya.raman@gmail.com | 2026-08-31T11:26:00Z | COMPLETE | 2026-09-01T19:30:00Z | 2026-09-12T08:00:00Z | Je ne veux plus recevoir vos e-mails promotionnels. Merci de me désinscrire de la newsletter, mais ne supprimez surtout pas mon compte. |
| custom_04 | gabriel.costa@proton.me | 2026-08-29T20:03:00Z | PARTIALLY_COMPLETE | 2026-08-31T13:44:00Z | 2026-09-14T16:10:00Z | Please don't delete or close anything. I only need you to correct the phone number on my profile, the old one is wrong. |
| custom_05 | minjun.kim90@gmail.com (old email) | 2026-08-24T12:00:00Z | COMPLETE | 2026-08-25T12:00:00Z | 2026-09-15T12:00:00Z | I changed the email on this account before deleting it. Please check that nothing tied to the old address was left behind, even in systems that do not display the email. |
| custom_06 | L.FONTAINE46@YAHOO.COM | 2026-09-13T11:00:00Z | COMPLETE | 2026-09-15T11:00:00Z | 2026-09-26T11:00:00Z | Non voglio solo disiscrivermi dalla newsletter: voglio che il mio account e tutti i miei dati personali vengano cancellati definitivamente. |
| custom_07 | novak.tomasz@icloud.com | 2026-09-09T12:00:00Z | COMPLETE | 2026-09-12T12:00:00Z | 2026-10-01T12:00:00Z | कृपया इस ईमेल पते से जुड़ा मेरा खाता और उससे जुड़ी सभी व्यक्तिगत जानकारी स्थायी रूप से हटा दें। |

### Expected vs actual

| Case | What it tests | Expected | Actual | Result |
|---|---|---|---|---|
| custom_01 | German **access** request ("send me a copy… I want to keep my account") | NOT_APPLICABLE, `[]` | NOT_APPLICABLE, `[]` | exact |
| custom_02 | Spanish **export / portability** request ("export my orders to CSV… I don't want to close the account") | NOT_APPLICABLE, `[]` | NOT_APPLICABLE, `[]` | exact |
| custom_03 | French **marketing opt-out** with a negated deletion ("ne supprimez surtout pas mon compte") | NOT_APPLICABLE, `[]` | NOT_APPLICABLE, `[]` | exact |
| custom_04 | English **correction** request that mentions deletion only in negation ("don't delete or close anything") | NOT_APPLICABLE, `[]` | NOT_APPLICABLE, `[]` | exact |
| custom_05 | Deletion using an **old email** from `user_audit_log.csv` (`minjun.kim90@…`, replaced by the current address); subject of case_12 | INCOMPLETE, 5 steps (case_12 answer) | INCOMPLETE, 5 steps | exact |
| custom_06 | **Tricky deletion:** Italian, phrased with an opt-out negation ("I don't just want to unsubscribe: delete my account"), email in UPPER CASE; subject of case_03 | COMPLETE, 3 steps (case_03 answer) | COMPLETE, 2 steps | 1 verification missing |
| custom_07 | **Tricky deletion:** Hindi request (a script the classifier never saw in the samples); subject of case_11 | INCOMPLETE, 14 steps (case_11 answer) | INCOMPLETE, 14 steps | exact |

| Metric | Score |
|---|---|
| Status accuracy | 7/7 (100%) |
| Step precision / recall / F1 | 1.000 / 0.955 / 0.977 |
| Sequence and exact accuracy | 6/7 (85.7%) |

**custom_06 miss:** the missing step is `VERIFY_RECORD_ANONYMIZED analytics_9406`.

- An anonymized analytics record carries no identifier. It can only be linked through the deletion worker's batch.
- custom_06 is a second request for the case_03 subject with identical timestamps. Its competitor for the batch row is therefore a request that behaves the same.
- The one-to-one batch assignment gives the row to case_03.
- This is the shared-batch limitation described below. With a single request per subject, as in the dataset, the step is found: case_03 itself is exact.

## 4. Failures found and improvements made

Every fix is a general rule; no case ID, resource ID or answer is hard-coded. The full log with causes is in `code/evaluation/notes.md`.

| Round | Status | Step P / R / F1 | Exact | Change |
|---|---|---|---|---|
| 1 | 29/32 | 0.830 / 0.756 / 0.791 | 13/32 | First full solver run |
| 2 | 32/32 | 1.000 / 0.896 / 0.945 | 16/32 | Pipe-log parser stopped swallowing handover tables |
| 3 | 32/32 | 0.989 / 0.907 / 0.946 | 15/32 | Anonymized analytics linked through deletion-worker activity |
| 4 | 32/32 | 0.989 / 0.912 / 0.949 | 18/32 | Worker rows assigned to the request whose anchors are nearest in time |
| 5 | 32/32 | 0.989 / 0.917 / 0.952 | 18/32 | Requests with no anchors may take a single unclaimed candidate |
| 6 | 32/32 | 0.989 / 0.917 / 0.952 | 18/32 | Account absence cites the latest full account-store snapshot |
| 7 | 32/32 | 0.989 / 0.938 / 0.963 | 21/32 | Each nested snapshot's own captured_at decides visibility |
| 8 | 32/32 | 0.989 / 0.938 / 0.963 | 21/32 | Screenshots read by Gemini, catalog aliases, requested_at state window, zero-step and citation fallbacks (fixes 12 invalid rows in the 518 run) |

### Main failures and fixes

1. **Handover tables parsed as logs (round 1 → 2).**
   - Failure: a pipe-delimited log regex also matched shift-handover tables. Record types became "trace IDs", so every resource of one type linked to every case.
   - Fix: a pipe log line must start with a real log level.
2. **Anonymized analytics unreachable (rounds 2 → 5).**
   - Cause: anonymization removes every identifier.
   - Fix: these records are linked through the deletion worker's batch around `reported_at`. Rows go one-to-one to the request whose already-linked records ran nearest in time. A request with no anchors may take a candidate only if exactly one unclaimed candidate is left.
3. **Wrong proof of account absence (round 5 → 6).**
   - Fix: absence by omission is cited from the latest full account-store snapshot (primary or replica profile tables, or an `account_service` collection). Partial exports are not used.
4. **Snapshot bundles hid early snapshots (round 6 → 7).**
   - Cause: a bundle captured after `audit_at` holds snapshots captured before it.
   - Fix: those nested snapshots may now be used for discovery, while the file is still never cited. This recovered 4 retained audit records.
5. **Screenshots ignored; 12 invalid rows in the first full run (round 8).**
   - Failure: the first 518 run had 11 COMPLETE rows with no steps and 1 finding with no evidence, because the solver skipped the 11 PNG screenshots.
   - Gemini now transcribes each screenshot once into per-resource records, which are cached and handled by the same deterministic rules as other evidence.
   - `LINK_RESOURCE_IDENTITY` activity rows act as the data catalog. They resolve aliases shown on screen or in PDFs (file names, statement numbers) to resource IDs.
   - Two general rules came from the sample answers:
     - **Gold never cites evidence captured before `requested_at` (0 of 75 citations).** Resource state is now built only from evidence at or after `requested_at`, because earlier captures show the data before the deletion ran. Without this rule, screenshots from an earlier request added 3 wrong steps to the samples. With it, samples are back to 21/32 with nothing lost.
     - **Last-resort fallbacks.** A case with no other step uses the best-guess account. A finding whose state is visible only in a file captured after `audit_at` cites the failed or partial deletion activity for that resource instead.
6. **Deletion classifier (before scoring).**
   - Failure: the rule fallback treated "no longer use this account" as a negation.
   - Fix: a negation now counts only if it governs a deletion word.
   - Gemini now classifies all request texts, with 0 rule fallbacks. It agrees with the rule classifier on every text.
7. **Gemini quota errors.** HTTP 503 and 429 left 25 texts on rules. An `--llm-only` retry with pacing and backoff, plus a low-memory `--classify-only` mode, filled them in.

### Known unreachable or ambiguous items (remaining sample misses)

1. **Retained audit records that exist in no participant file:** `audit_record_6645` (case_18) and `audit_record_5569` (case_20). Nothing can link them.
2. **Account not identifiable (6 cases).**
   - `account_004` (case_04) appears in no evidence or activity file.
   - In case_18, case_20, case_22, case_23 and case_32, several account deletes fall inside the request window, and nothing in the evidence tells them apart.
   - Leaving the account out scored better than guessing. Guessing gave F1 0.947 and 19/32 exact; leaving it out gives 0.952 and 18/32, measured at round 6. The default is therefore to leave it out unless the case would otherwise have no steps.
3. **Shared-batch analytics (case_19, case_21, case_25, case_26).** Several requests share a `reported_at`, and their worker rows run at identical offsets. No participant signal says which anonymized record belongs to which request. case_21 also has no anchor of its own.
