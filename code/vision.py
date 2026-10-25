"""Screenshot evidence: Gemini reads each image once into structured records.

The evidence screenshots (admin consoles for exports, search, connectors,
backups, analytics and so on) list one or more resources with the identifiers
and state shown on screen. Gemini transcribes them into a fixed JSON shape;
everything after that (identity matching, visibility at audit_at, policy) is
the same deterministic code that handles logs, JSON, SQL and PDFs.

Extractions are cached in ``code/.image_cache.json`` by a hash of the image
bytes, model and prompt version, so a re-run makes no calls. Without an API
key, images that are not cached are skipped.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
from pathlib import Path

from llm import DEFAULT_MODEL, REPO_ROOT, GeminiClient, Usage, read_api_key

CACHE_PATH = REPO_ROOT / "code" / ".image_cache.json"
PROMPT_VERSION = "img-v1"
# Field names that the audit rules interpret as state; free-form labels may not set them.
RESERVED_FIELDS = {"result", "record_type", "rows", "total_hits", "decision", "eligible", "deletion_block",
                   "http_status", "status_code", "archive_present", "available", "event", "resource_id"}
MIME = {".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".webp": "image/webp"}

SYSTEM_PROMPT = """You transcribe screenshots of internal admin consoles of an online shop into JSON.

Return one record per distinct resource shown in a table row, a detail panel or the page header.
A resource has an identifier such as account_123, search_45, export_67, backup_8, data_flow_9,
analytics_10, support_11, audit_record_12, object_13 or cache_14. Merge a table row and the detail
panel for the same resource into one record. Ignore resources that are only mentioned in an
activity feed or timeline. If the page is about a customer reference without such an identifier
(for example only user_17), return one record with resource_id null.

For each record return exactly these keys:
- "resource_id": the identifier as shown, or null.
- "email", "name", "phone": the person's direct identifiers shown for this resource
  (an email shown as owner, account, selector, requester or email counts as "email").
  Use null when the screen has no such field for this resource; use "" when the field is
  shown but empty, masked, redacted or a dash.
- "customer_reference": a stable internal reference such as user_17 shown for the resource, else null.
- "status": the status text shown for the resource (for example Ready, Expired, Healthy, Paused,
  Active, Retained reference), else null.
- "enabled": for a connector or data flow, true if it is running/continuous/healthy, false if
  paused/disabled/stopped, else null.
- "restore_allowed": for a backup or archive, false if restores are blocked or on hold,
  true if it is restorable with no block shown, else null.
- "download_available": for a data export, true if it can still be downloaded, false if
  expired/revoked/deleted, else null.
- "present": false only if the screen says the resource was not found or was deleted, else true.
- "time": the most specific timestamp shown for this resource in ISO-8601 UTC, else null.
- "other": an object with any remaining labelled values for this resource (snake_case labels).

Return JSON: {"records": [ ... ]}."""


class ImageExtractor:
    def __init__(self, use_llm: bool = True, model: str | None = None, cache_path: Path = CACHE_PATH):
        self.model = model or os.environ.get("GEMINI_MODEL", DEFAULT_MODEL)
        self.cache_path = cache_path
        try:
            self.cache = json.loads(cache_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            self.cache = {}
        self.usage = Usage(model=self.model)
        api_key = read_api_key() if use_llm else None
        self.client = GeminiClient(self.model, api_key, self.usage) if api_key else None
        self.sources: dict[str, str] = {}  # file id -> "llm" | "cache" | "skipped"
        self.errors: list[str] = []

    def _key(self, data: bytes) -> str:
        return hashlib.sha256(f"{PROMPT_VERSION}|{self.model}|".encode() + data).hexdigest()

    def extract(self, file_id: str, path: Path) -> list[dict]:
        """Records transcribed from one image ([] when unavailable)."""
        data = path.read_bytes()
        key = self._key(data)
        hit = self.cache.get(key)
        if hit is not None:
            self.sources[file_id] = "cache"
            self.usage.cached_hits += 1
            return hit["records"]
        if self.client is None:
            self.sources[file_id] = "skipped"
            return []
        parts = [
            {"inlineData": {"mimeType": MIME.get(path.suffix.lower(), "image/png"),
                            "data": base64.b64encode(data).decode("ascii")}},
            {"text": "Transcribe this screenshot."},
        ]
        try:
            records = json.loads(self.client.generate(SYSTEM_PROMPT, parts))["records"]
        except (RuntimeError, KeyError, ValueError, IndexError) as exc:
            self.errors.append(f"{file_id}: {str(exc)[:120]}")
            self.sources[file_id] = "skipped"
            return []
        records = [r for r in records if isinstance(r, dict)]
        self.cache[key] = {"file": path.name, "model": self.model, "records": records}
        self.cache_path.write_text(json.dumps(self.cache, indent=1, sort_keys=True, ensure_ascii=False),
                                   encoding="utf-8")
        self.sources[file_id] = "llm"
        return records


def image_fields(record: dict) -> dict:
    """Map one transcribed record onto the field names the audit rules read."""
    fields: dict = {}
    for key in ("email", "phone", "customer_reference", "status"):
        if record.get(key) is not None:
            fields[key] = str(record[key])
    if record.get("name") is not None:
        fields["display_name"] = str(record["name"])
    for key in ("enabled", "restore_allowed", "download_available"):
        if isinstance(record.get(key), bool):
            fields[key] = record[key]
    if record.get("present") is False:
        fields["result"] = "NOT_FOUND"
    other = record.get("other")
    if isinstance(other, dict):
        for key, value in other.items():
            if str(key) in RESERVED_FIELDS:
                continue  # state is only read from the explicit keys above
            if isinstance(value, (str, int, float, bool)):
                fields.setdefault(str(key), value)
    return fields
