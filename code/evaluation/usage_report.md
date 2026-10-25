# Usage report: Ghost Data Still Exists

## Models

| Provider | Model | Used for | Access |
|---|---|---|---|
| Google Gemini (Generative Language REST API, `generateContent`) | `gemini-3.5-flash` | 1. Deletion-intent classification of `request_text` (batches of 25 texts, JSON mode, temperature 0, minimal thinking) | `GEMINI_API_KEY` from the environment or an uncommitted `.env` |
| (same) | `gemini-3.5-flash` | 2. Transcribing the 11 evidence screenshots into structured records (one image per call, JSON mode, temperature 0) | (same) |

No other model or provider is used. Everything else is deterministic Python: identity resolution, evidence search, timing rules, policy, step planning and serialization.

Both uses are cached:

| Use | Cache file | Key |
|---|---|---|
| Classification | `code/.llm_cache.json` | prompt version + model + text |
| Screenshot transcription | `code/.image_cache.json` | prompt version + model + image bytes |

A cache entry is only ever a Gemini answer, never a rule-fallback guess.

## Price assumed

Google's standard paid-tier list price for `gemini-3.5-flash`:

| Token type | USD per 1M tokens |
|---|---|
| Input (image input is billed as input tokens) | $1.50 |
| Output (includes thinking tokens) | $9.00 |

Source: Google AI for Developers, Gemini Developer API pricing (<https://ai.google.dev/gemini-api/docs/pricing>), and the Gemini 3.6 release notes (<https://ai.google.dev/gemini-api/docs/generate-content/whats-new-gemini-3.6>), which give 3.5 Flash output as $9.00 per 1M. Checked 2026-10-25.

The key ran into HTTP 429 quota limits that are typical of the free tier. If the project is on the free tier, the actual charge is $0; the figures below are what the same usage costs at paid list price.

To have the program compute cost itself, set `GEMINI_PRICE_INPUT_PER_M` and `GEMINI_PRICE_OUTPUT_PER_M`. No price is assumed in code.

## A. Final full-dataset run that produced `output.csv`

Command: `python code/main.py` (518 requests in `dataset/deletion_requests.csv`). Machine-readable copy: `code/evaluation/usage.json`.

| Model / task | Calls | Cache hits | Input tokens | Output tokens | Total tokens | Est. cost |
|---|---|---|---|---|---|---|
| gemini-3.5-flash: classification | 0 | 398 distinct texts | 0 | 0 | 0 | $0.00 |
| gemini-3.5-flash: screenshots | 0 | 11 images | 0 | 0 | 0 | $0.00 |
| **Overall** | **0** | 409 | **0** | **0** | **0** | **$0.00** |

- Average per request: 0 tokens, $0.00.
- Rule fallbacks: 0. All 398 texts the run classifies were answered by Gemini, and all 11 screenshots by Gemini transcriptions.
- Why 398 texts, not 375: the 518 requests have 375 distinct texts. The run also classifies the 32 sample requests, because the account linker uses every authorised deletion request to rule out accounts claimed by other requests.

**Why this run made no calls:** every text and screenshot it needed had already been answered by Gemini during development (section B) and was served from the cache. This is intended: a re-run gives byte-identical output (verified by SHA-256) and costs nothing. The real model cost of producing this output is therefore the cumulative cost in section B.

## B. Cumulative calls that built the caches

Per-run numbers come from each run's console output and `usage.json`. Since round 8 they are also appended automatically to `code/evaluation/usage_ledger.jsonl` for every run that makes a call.

### Classification (gemini-3.5-flash)

| Run | Calls | Input | Output |
|---|---|---|---|
| First classification of the sample + evaluation texts | 13 | 17,952 | 3,785 |
| Second pass: 2 batches answered; 25 texts hit HTTP 503/429 and fell back to rules | 2 | 2,729 | 586 |
| `--classify-only --llm-only` retry of those 25 texts (paced, backoff) | 3 | 1,789 | 284 |
| Custom evaluation cases (6 new texts) | 1 | 468 | 69 |
| One-text check after refactoring the HTTP client | 1 | not recorded (about 400) | not recorded (<20) |
| **Subtotal** | **20** | **22,938** + about 400 | **4,724** + under 20 |

### Screenshot transcription (gemini-3.5-flash)

| Run | Calls | Input | Output |
|---|---|---|---|
| Test transcription of one screenshot | 1 | 1,605 | 712 |
| Remaining 10 screenshots (first sample run with vision) | 10 | 15,922 | 5,806 |
| **Subtotal** | **11** | **17,527** | **6,518** |

### Totals and cost

| Model / task | Calls | Input tokens | Output tokens | Total tokens | Est. cost |
|---|---|---|---|---|---|
| gemini-3.5-flash: classification | 20 | 22,938 | 4,724 | 27,662 | $0.0769 |
| gemini-3.5-flash: screenshots | 11 | 17,527 | 6,518 | 24,045 | $0.0850 |
| **Overall (recorded)** | **31** | **40,465** | **11,242** | **51,707** | **$0.162** |

- **Average per request** (cumulative tokens and cost spread over the 518 evaluation requests): **99.8 tokens** and **$0.00031**.
- The same calls also covered the 32 sample and 7 custom requests. Over all 557 requests the average is 92.8 tokens and $0.00029.

### Not counted above

These calls returned no tokens and were not billed:

- HTTP 404 responses while `gemini-2.5-flash` was unavailable to this key.
- HTTP 503 and 429 responses during the second pass.
- A single 12-token latency probe of `gemini-3.8-flash`, which was not adopted.

## C. Cost of a clean run with empty caches (estimate)

If the caches are deleted, `python code/main.py` would make about:

| Task | Calls | Input tokens | Output tokens | Est. cost |
|---|---|---|---|---|
| Classification: 398 texts, batches of 25 | 16 | ~22,600 | ~4,650 | ~$0.076 |
| Screenshots: 11 images | 11 | ~17,500 | ~6,400 | ~$0.084 |
| **Total** | **27** | ~40,100 | ~11,050 | **~$0.16** |

- Per request: about 99 tokens and $0.0003.
- Token counts are extrapolated from the observed per-text and per-image averages above.
- Without a key, the solver still runs: the rule classifier answers every text, and uncached screenshots are skipped.
