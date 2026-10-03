"""EVREN (evren-llmapi.ssyz.org.tr): the provider rules the bridge applies with --profile evren.

- Daily and 5-minute window numbers come on every response (X-Evren-* and X-Ratelimit-* headers) and from
  GET /v1/quota. Cached input counts 3% against the daily quota ("Modeller ve API", 2026-10-02).
- A daily-limit 429 (code daily_token_limit_exceeded) is refused before any cost, so the bridge parks the key until
  error.evren.resets_at (an hour when that is missing) and retries the request on another key.
- A per-minute 429 often comes without Retry-After; the bridge adds 60 seconds.
- When EVREN cannot reach its model, a stream gets HTTP 200 and one synthetic chunk (no id, empty delta,
  finish_reason "error") instead of a 503. Real model chunks always carry an id.
"""
import datetime
import json
import time

NAME = "EVREN"
DEFAULT_URL = "https://evren-llmapi.ssyz.org.tr"
QUOTA_PATH = "/v1/quota"
RETRY_AFTER = "60"
CACHE_WEIGHT = 0.03
TPM_LIMIT = 500_000  # documented per-minute limit, shown for scale
WINDOW_LIMIT = 2_500_000  # tokens per 5-minute window
DAILY_LIMIT = 10_000_000
# Shown first in the panel, one colour each; green, yellow, orange and red otherwise mean good, fair, weak and error.
MODELS = {"mimo-v2.6-pro": "#d7af87", "deepseek-v4.1-flash": "#5fafff", "glm-5.3": "#d787ff",
          "qwen3.8-flash-next": "#5fd7d7"}
SHORT_NAMES = {"deepseek-v4.1-flash": "deepseek-v4.1", "qwen3.8-flash-next": "qwen3.8-flash"}
QUOTA_FIELDS = ("used_tokens", "cap", "window_minutes", "reset_at", "usage_ratio", "level",
                "daily_used_tokens", "daily_limit_tokens", "daily_remaining_tokens", "daily_reset_at")


def as_dict(value):
    return value if isinstance(value, dict) else {}


def timestamp(iso):
    return datetime.datetime.fromisoformat(iso.replace("Z", "+00:00")).timestamp()


def quota(data):
    """The fields the bridge shows, from a GET /v1/quota body."""
    body = json.loads(data)
    fields = {name: body.get(name) for name in QUOTA_FIELDS}
    fields["daily_reset"] = timestamp(fields["daily_reset_at"]) if fields["daily_reset_at"] else None
    fields["error"] = None
    return fields


def quota_from_headers(resp):
    """Daily and 5-minute window numbers as sent on every response; empty when the headers are missing."""
    fields = {}
    try:
        cap = int(resp.getheader("X-Evren-Daily-Limit-Tokens"))
        left = int(resp.getheader("X-Evren-Daily-Remaining-Tokens"))
        fields.update(daily_limit_tokens=cap, daily_remaining_tokens=left, daily_used_tokens=cap - left,
                      daily_reset=float(resp.getheader("X-Evren-Daily-Reset")), error=None)
    except (TypeError, ValueError):
        pass
    try:
        cap = int(resp.getheader("X-Ratelimit-Limit-Tokens"))
        fields.update(cap=cap, used_tokens=cap - int(resp.getheader("X-Ratelimit-Remaining-Tokens")))
    except (TypeError, ValueError):
        pass
    return fields


def park_until(body):
    """When this 429 body is the daily limit, the time to park the key until; otherwise None."""
    try:
        err = as_dict(as_dict(json.loads(body)).get("error"))
    except ValueError:
        return None
    if err.get("code") != "daily_token_limit_exceeded":
        return None
    try:
        return timestamp(as_dict(err.get("evren")).get("resets_at"))
    except (AttributeError, ValueError):
        return time.time() + 3600


def masked_error(line):
    """True for the synthetic chunk that stands for "could not reach the model"."""
    text = line.decode("utf-8", "replace").strip()
    if not text.startswith("data:"):
        return False
    try:
        chunk = json.loads(text[5:])
        choice = chunk["choices"][0]
    except (ValueError, KeyError, IndexError, TypeError):
        return False
    if not isinstance(chunk, dict) or not isinstance(choice, dict):
        return False
    return "id" not in chunk and choice.get("finish_reason") == "error" and not choice.get("delta")


def quota_cost(record):
    """Tokens a request took from the daily quota, cached input weighted as EVREN does; an estimate."""
    prompt, cached, out = (record.get(k) or 0 for k in ("prompt", "cached", "out"))
    return prompt - cached + cached * CACHE_WEIGHT + out
