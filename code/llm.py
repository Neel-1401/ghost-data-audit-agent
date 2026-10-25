"""Deletion-intent classifier: does a request actually authorise deletion?

Primary path: Google Gemini over its REST API (standard library only). Texts
are classified in batches, results are cached in ``code/.llm_cache.json`` by a
hash of the text, and token usage is tracked for ``usage_report.md``.

Fallback path: a negation-aware multilingual rule classifier, used when no
``GEMINI_API_KEY`` is configured, when ``--no-llm`` is requested, or when an
API call fails. Every result records which path produced it.

The API key is read from the environment or the repo-root ``.env`` file and is
never printed or logged.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
CACHE_PATH = REPO_ROOT / "code" / ".llm_cache.json"
ENV_PATH = REPO_ROOT / ".env"

# gemini-2.5-flash is closed to new API users; gemini-3.8-flash answered a
# 12-token probe in ~100 s, gemini-3.5-flash in ~3 s.
DEFAULT_MODEL = "gemini-3.5-flash"
API_URL = "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
BATCH_SIZE = 25
PROMPT_VERSION = "v1"


def _price(name: str) -> float | None:
    """USD per million tokens from the environment; None when not configured."""
    try:
        return float(os.environ[name])
    except (KeyError, ValueError):
        return None


# Cost estimate inputs for usage reports. Set these to the provider's current
# list price for the chosen model; no price is assumed when they are unset.
PRICE_PER_M_INPUT = _price("GEMINI_PRICE_INPUT_PER_M")
PRICE_PER_M_OUTPUT = _price("GEMINI_PRICE_OUTPUT_PER_M")


def thinking_config(model: str) -> dict:
    """Smallest thinking setting each model family accepts (classification needs none)."""
    if model.startswith("gemini-2"):
        return {"thinkingBudget": 0}
    if model.startswith("gemini-3.8"):
        return {"thinkingBudget": 0}  # rejects thinkingLevel=minimal
    return {"thinkingLevel": "minimal"}

SYSTEM_PROMPT = """You classify customer privacy requests for an online shop.

For each request decide whether it AUTHORISES DELETION of the customer's account or personal data.

Answer true when the person:
- asks to delete, erase, remove, close or cancel their account or personal data (in any language);
- asks to finish, retry or complete a deletion that failed or was partial;
- asks you to check or confirm that their account or data has been deleted, or to remove remaining copies.

Answer false when the person asks only for something else, for example:
- access to or a copy/export/download of their data or invoices;
- correcting or changing details (name, phone, email);
- stopping marketing or promotional messages only;
- login or password help;
- or when they explicitly say they are NOT asking for deletion or want the account kept.

Mentions of deletion that are negated ("do not delete", "not a request to erase") do not authorise deletion.

Return JSON: {"results": [{"id": <id>, "deletion": true|false}]} with one entry per request id."""


# ---------------------------------------------------------------------------
# Rule-based fallback
# ---------------------------------------------------------------------------

DELETION_STEMS = (
    r"delet|eras|remov|clos|cancel|wipe|forget|is gone|are gone|"
    r"suppr|effac|elimin|borra|exclu|apag|lösch|losch|verwijder|usuń|usun|radera|slett|"
    r"削除|退会|消去|삭제|탈퇴|删除|注销|удал"
)
DELETION_TERMS = re.compile(DELETION_STEMS, re.IGNORECASE)
# A negation counts only when it governs a deletion word: "do not close the
# account", "not asking for deletion", "without removing". "No longer use"
# is not a negated deletion.
NEGATION_TERMS = re.compile(
    r"(?:\bnot\b|n't\b|\bnever\b|\bwithout\b|\bno\b(?! longer))\W+(?:[\w-]+\W+){0,4}?(?:" + DELETION_STEMS + r")",
    re.IGNORECASE,
)
SCOPE_LIMITERS = re.compile(r"\bonly\b", re.IGNORECASE)
NON_DELETION_CUES = re.compile(
    r"\bcopy\b|download|portable|export my|what (?:personal )?information|information request|"
    r"\bcorrect\b|misspell|out of date|\bchange the\b|\breplace it\b|reset|can't access|"
    r"promotional|marketing|invoice",
    re.IGNORECASE,
)
SENTENCE_SPLIT = re.compile(r"(?<=[.!?。！？])\s*")


def rule_classify(text: str) -> bool:
    """Negation-aware fallback. True when some sentence asks for deletion un-negated."""
    sentences = [s for s in SENTENCE_SPLIT.split(text.strip()) if s.strip()]
    positive = negated = False
    for sentence in sentences:
        if not DELETION_TERMS.search(sentence):
            continue
        if NEGATION_TERMS.search(sentence) or SCOPE_LIMITERS.search(sentence):
            negated = True
        else:
            positive = True
    if positive and not (negated and NON_DELETION_CUES.search(text)):
        return True
    # Only negated deletion wording, or none at all (access, correction, marketing).
    return False


# ---------------------------------------------------------------------------
# Gemini client
# ---------------------------------------------------------------------------


def read_api_key() -> str | None:
    key = os.environ.get("GEMINI_API_KEY")
    if not key and ENV_PATH.exists():
        for line in ENV_PATH.read_text(encoding="utf-8-sig").splitlines():
            name, sep, value = line.partition("=")
            if sep and name.strip() == "GEMINI_API_KEY":
                key = value.strip().strip('"').strip("'")
    if not key or key == "PASTE_KEY_HERE":
        return None
    return key


@dataclass
class Usage:
    model: str
    calls: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    cached_hits: int = 0
    fallbacks: int = 0
    errors: list[str] = field(default_factory=list)

    @property
    def total_tokens(self) -> int:
        return self.input_tokens + self.output_tokens

    @property
    def cost_usd(self) -> float | None:
        if PRICE_PER_M_INPUT is None or PRICE_PER_M_OUTPUT is None:
            return None
        return (self.input_tokens * PRICE_PER_M_INPUT + self.output_tokens * PRICE_PER_M_OUTPUT) / 1_000_000

    def as_dict(self) -> dict:
        cost = self.cost_usd
        return {
            "provider": "Google Gemini", "model": self.model, "calls": self.calls,
            "input_tokens": self.input_tokens, "output_tokens": self.output_tokens,
            "total_tokens": self.total_tokens,
            "estimated_cost_usd": round(cost, 6) if cost is not None else None,
            "price_per_m_input": PRICE_PER_M_INPUT, "price_per_m_output": PRICE_PER_M_OUTPUT,
            "cache_hits": self.cached_hits, "rule_fallbacks": self.fallbacks, "errors": self.errors,
        }


class GeminiClient:
    """One Gemini ``generateContent`` endpoint with pacing, retries and usage counting.

    Calls are paced at least ``min_interval`` seconds apart. HTTP 429/5xx and
    network errors are retried up to ``max_attempts`` times; ``patient``
    backoff (15 s doubling to 120 s, honouring the provider's retryDelay) is
    for quota-limited retry runs, otherwise it backs off 1, 2, 4 ... seconds.
    """

    BACKOFF_START_S = 15
    BACKOFF_CAP_S = 120

    def __init__(self, model: str, api_key: str, usage: "Usage", min_interval: float = 0.0,
                 max_attempts: int = 4, patient: bool = False):
        self.model = model
        self.api_key = api_key
        self.usage = usage
        self.min_interval = min_interval
        self.max_attempts = max_attempts
        self.patient = patient
        self._last_call = 0.0

    def generate(self, system_prompt: str, parts: list[dict]) -> str:
        """Send one JSON-mode request and return the model's text."""
        body = {
            "systemInstruction": {"parts": [{"text": system_prompt}]},
            "contents": [{"role": "user", "parts": parts}],
            "generationConfig": {
                "temperature": 0,
                "responseMimeType": "application/json",
                "thinkingConfig": thinking_config(self.model),
            },
        }
        request = urllib.request.Request(
            API_URL.format(model=self.model),
            data=json.dumps(body).encode("utf-8"),
            headers={"Content-Type": "application/json", "x-goog-api-key": self.api_key},
        )
        last_error = None
        for attempt in range(self.max_attempts):
            wait = self.min_interval - (time.monotonic() - self._last_call)
            if wait > 0:
                time.sleep(wait)
            self._last_call = time.monotonic()
            retry_after = None
            try:
                with urllib.request.urlopen(request, timeout=180) as response:
                    payload = json.loads(response.read())
                break
            except urllib.error.HTTPError as exc:
                # Keep the provider's message (never contains the key) for diagnosis.
                raw = exc.read().decode("utf-8", "replace")
                detail = raw
                try:
                    error = json.loads(raw)["error"]
                    detail = error.get("message", raw)
                    for item in error.get("details", []):
                        delay = str(item.get("retryDelay", ""))
                        if delay.endswith("s"):
                            retry_after = float(delay[:-1])
                except (ValueError, KeyError, TypeError, AttributeError):
                    pass
                last_error = f"HTTP {exc.code}: {detail[:160]}"
                if exc.code not in (429, 500, 502, 503, 504):
                    raise RuntimeError(last_error) from None
            except (urllib.error.URLError, TimeoutError) as exc:
                last_error = type(exc).__name__
            if attempt + 1 < self.max_attempts:
                backoff = min(self.BACKOFF_START_S * 2 ** attempt, self.BACKOFF_CAP_S) if self.patient else 2 ** attempt
                time.sleep(max(backoff, retry_after or 0))
        else:
            raise RuntimeError(last_error or "request failed")

        self.usage.calls += 1
        meta = payload.get("usageMetadata", {})
        self.usage.input_tokens += int(meta.get("promptTokenCount", 0))
        self.usage.output_tokens += int(meta.get("candidatesTokenCount", 0)) + int(meta.get("thoughtsTokenCount", 0))
        return payload["candidates"][0]["content"]["parts"][0]["text"]


class DeletionClassifier:
    """Classify request texts; Gemini first, rules as fallback, results cached.

    Only Gemini answers are cached, so a cache hit is a Gemini result. With
    ``llm_only`` there is no rule fallback: calls are paced at least
    ``min_interval`` seconds apart, rate-limit and server errors back off
    (honouring the provider's retryDelay when given), and texts still
    unanswered are listed in ``unanswered`` instead of being guessed.
    """

    def __init__(self, use_llm: bool = True, model: str | None = None, cache_path: Path = CACHE_PATH,
                 llm_only: bool = False, min_interval: float = 0.0, max_attempts: int = 4,
                 batch_size: int = BATCH_SIZE):
        self.model = model or os.environ.get("GEMINI_MODEL", DEFAULT_MODEL)
        self.api_key = read_api_key() if use_llm or llm_only else None
        self.cache_path = cache_path
        self.cache = self._load_cache()
        self.usage = Usage(model=self.model)
        self.client = GeminiClient(self.model, self.api_key, self.usage, min_interval=min_interval,
                                   max_attempts=max_attempts, patient=llm_only) if self.api_key else None
        self.sources: dict[str, str] = {}  # text -> "llm" | "cache" | "rules"
        self.llm_only = llm_only
        self.batch_size = batch_size
        self.unanswered: list[str] = []
        if llm_only and not self.api_key:
            raise RuntimeError("--llm-only needs GEMINI_API_KEY in the environment or .env")

    # -- cache -----------------------------------------------------------

    def _key(self, text: str) -> str:
        raw = f"{PROMPT_VERSION}|{self.model}|{text.strip()}"
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()

    def _load_cache(self) -> dict:
        try:
            return json.loads(self.cache_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {}

    def _save_cache(self):
        self.cache_path.write_text(json.dumps(self.cache, indent=1, sort_keys=True), encoding="utf-8")

    # -- API ---------------------------------------------------------------

    def _call(self, batch: list[str]) -> dict[int, bool]:
        items = [{"id": i, "text": text} for i, text in enumerate(batch)]
        text = self.client.generate(SYSTEM_PROMPT, [{"text": json.dumps({"requests": items}, ensure_ascii=False)}])
        results = json.loads(text)["results"]
        return {int(r["id"]): bool(r["deletion"]) for r in results}

    # -- public --------------------------------------------------------------

    def classify_many(self, texts: list[str]) -> dict[str, bool]:
        unique = sorted({t.strip() for t in texts})
        out: dict[str, bool] = {}
        pending = []
        for text in unique:
            hit = self.cache.get(self._key(text))
            if hit is not None:
                out[text] = hit["deletion"]
                self.sources[text] = "cache"
                self.usage.cached_hits += 1
            else:
                pending.append(text)

        if pending and self.api_key:
            for start in range(0, len(pending), self.batch_size):
                batch = pending[start:start + self.batch_size]
                try:
                    answers = self._call(batch)
                except (RuntimeError, KeyError, ValueError, IndexError) as exc:
                    self.usage.errors.append(str(exc)[:120])
                    continue
                for i, text in enumerate(batch):
                    if i in answers:
                        out[text] = answers[i]
                        self.sources[text] = "llm"
                        self.cache[self._key(text)] = {"deletion": answers[i], "model": self.model}
                self._save_cache()  # keep progress if a later batch fails

        for text in unique:
            if text not in out:
                if self.llm_only:
                    self.unanswered.append(text)
                    continue
                out[text] = rule_classify(text)
                self.sources[text] = "rules"
                self.usage.fallbacks += 1
        return {t: out[t.strip()] for t in texts if t.strip() in out}

    def source_counts(self) -> dict[str, int]:
        """Distinct texts by origin: Gemini (fresh call or cached Gemini answer) vs rules."""
        fresh = sum(1 for s in self.sources.values() if s == "llm")
        cached = sum(1 for s in self.sources.values() if s == "cache")
        rules = sum(1 for s in self.sources.values() if s == "rules")
        return {"gemini": fresh + cached, "gemini_fresh": fresh, "gemini_cached": cached,
                "rules": rules, "unanswered": len(self.unanswered)}

    def classify(self, text: str) -> bool:
        return self.classify_many([text])[text]
