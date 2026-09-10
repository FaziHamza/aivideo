"""XtroEdge AI client - the one place that talks to a model.

Only `/messages` is used: it is stateless and takes vision content blocks, which
suits per-video judgements. The conversational `/chat/{id}` endpoints keep
server-side history, which would be wrong here - each video is judged on its own
and nothing should carry over between them.

Three things about this integration are not obvious:

  - **A browser User-Agent is required.** The gateway sits behind Cloudflare,
    which rejects urllib's default agent with `403 error code: 1010` before the
    request ever reaches the API. That failure looks exactly like a bad key and
    is not: `{"detail": ...}` in the body means the API answered, a bare
    `error code: NNNN` means Cloudflare did.
  - **Requests are the scarce resource, not tokens.** The key allows 500
    requests a day against a 1,000,000 token budget, so at 50-60 videos a day
    the request count binds long before the tokens do. Everything here batches
    hard - many images per request - and callers check `remaining_requests()`
    before spending.
  - **The key is never hardcoded and never logged.** It is read from the
    XTROEDGE_API_KEY environment variable, or from data/xtroedge_key.txt, which
    is inside the gitignored data directory.

Standard library only - no new dependency for the desktop app to ship.
"""

from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Optional

from .config import DATA_DIR

BASE_URL = "https://aiapi.xtroedge.com/api/v1"
KEY_FILE = DATA_DIR / "xtroedge_key.txt"
KEY_ENV = "XTROEDGE_API_KEY"

# Cloudflare rejects urllib's default agent - see the module docstring.
_USER_AGENT = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
               "(KHTML, like Gecko) Chrome/140.0.0.0 Safari/537.36")

# Per the integration brief: a quota 429 will fail again immediately, so only
# burst 429s are retried.
_QUOTA_PREFIXES = ("daily_requests", "monthly_requests", "daily_tokens")
_BURST_RETRIES = 3
_BURST_BACKOFF = 1.0


class XtroError(RuntimeError):
    """Anything that stopped a call from returning a usable answer."""

    def __init__(self, message: str, status: int = 0, retryable: bool = False):
        super().__init__(message)
        self.status = status
        self.retryable = retryable


class XtroQuotaError(XtroError):
    """A limit on the key was reached; retrying now cannot help."""


class XtroAuthError(XtroError):
    """No key, a rejected key, or Cloudflare refusing the client."""


def find_key() -> Optional[str]:
    """The API key, or None. Never logged, never returned in errors."""
    from_env = os.environ.get(KEY_ENV, "").strip()
    if from_env:
        return from_env
    try:
        from_file = KEY_FILE.read_text(encoding="utf-8").strip()
    except OSError:
        return None
    return from_file or None


def configured() -> bool:
    return find_key() is not None


def key_source() -> str:
    """Where the key came from, for the environment check. Never the key."""
    if os.environ.get(KEY_ENV, "").strip():
        return f"{KEY_ENV} environment variable"
    if KEY_FILE.exists() and KEY_FILE.read_text(encoding="utf-8").strip():
        return str(KEY_FILE)
    return "not configured"


def env_overrides() -> bool:
    """Whether the environment variable is set and therefore wins.

    The UI needs this: saving a key to the file while `XTROEDGE_API_KEY` is
    set looks like it worked and changes nothing, which is a confusing hour to
    spend.
    """
    return bool(os.environ.get(KEY_ENV, "").strip())


def key_hint() -> str:
    """A few characters of the stored key, enough to tell two keys apart.

    Deliberately not the key. Showing all of it in a window that gets
    screen-shared or screenshotted is how keys leak, and the operator only
    ever needs to answer "is this the one I pasted?".
    """
    key = find_key()
    if not key:
        return ""
    tail = key[-4:] if len(key) > 8 else ""
    return f"xek_...{tail}" if tail else "(set)"


def store_key(key: str) -> Path:
    """Save the key for future runs. Returns where it went.

    Written to a file under `data/` rather than anywhere in the source tree:
    `data/` is gitignored and is not part of a packaged build, so a key set on
    one machine never travels with the app. Nothing here logs the value, and
    the caller is expected not to either.
    """
    key = (key or "").strip()
    if not key:
        raise XtroAuthError("No key given.")
    if any(c.isspace() for c in key):
        raise XtroAuthError(
            "That does not look like a key - it contains spaces. Paste the "
            "value on its own, with no quotes."
        )
    KEY_FILE.parent.mkdir(parents=True, exist_ok=True)
    KEY_FILE.write_text(key + "\n", encoding="utf-8")
    try:
        # Best effort on Windows, real on POSIX: owner-only.
        KEY_FILE.chmod(0o600)
    except OSError:
        pass
    return KEY_FILE


def forget_key() -> bool:
    """Remove the stored key. True if there was one to remove."""
    try:
        KEY_FILE.unlink()
        return True
    except FileNotFoundError:
        return False
    except OSError:
        return False


# --------------------------------------------------------------------------
def _request(method: str, path: str, body: Optional[dict] = None,
             timeout: float = 180.0) -> Any:
    key = find_key()
    if not key:
        raise XtroAuthError(
            f"No XtroEdge API key. Set {KEY_ENV} or put the key in {KEY_FILE}."
        )

    payload = json.dumps(body).encode("utf-8") if body is not None else None
    headers = {
        "X-API-Key": key,
        "User-Agent": _USER_AGENT,
        "Accept": "application/json",
    }
    if payload is not None:
        headers["Content-Type"] = "application/json"

    request = urllib.request.Request(BASE_URL + path, data=payload,
                                     headers=headers, method=method)
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        raise _translate(exc) from None
    except urllib.error.URLError as exc:
        raise XtroError(f"Could not reach XtroEdge: {exc.reason}",
                        retryable=True) from None
    except json.JSONDecodeError:
        raise XtroError("XtroEdge returned a response that was not JSON.")


def _translate(exc: urllib.error.HTTPError) -> XtroError:
    """Turn an HTTP error into something with a usable message."""
    try:
        raw = exc.read().decode("utf-8", "replace")
    except Exception:
        raw = ""
    detail = raw.strip()
    try:
        parsed = json.loads(raw)
        if isinstance(parsed, dict) and parsed.get("detail"):
            detail = str(parsed["detail"])
    except (json.JSONDecodeError, TypeError):
        # A bare "error code: NNNN" body is Cloudflare, not the API.
        if "error code:" in detail:
            return XtroAuthError(
                f"Blocked before reaching XtroEdge ({detail}). This is the "
                "CDN refusing the client, not a key problem.",
                status=exc.code,
            )

    if exc.code == 429:
        if any(detail.startswith(p) for p in _QUOTA_PREFIXES):
            return XtroQuotaError(f"XtroEdge quota reached: {detail}",
                                  status=429)
        return XtroError(f"XtroEdge rate limit: {detail}", status=429,
                         retryable=True)
    if exc.code in (401, 403):
        return XtroAuthError(f"XtroEdge rejected the key: {detail}",
                             status=exc.code)
    if exc.code in (502, 504):
        return XtroError(f"XtroEdge upstream error: {detail}", status=exc.code,
                         retryable=True)
    return XtroError(f"XtroEdge error {exc.code}: {detail}", status=exc.code)


# --------------------------------------------------------------------------
_usage_cache: tuple[float, dict] | None = None


def usage(max_age: float = 0.0) -> dict:
    """Current counters and limits for this key.

    `max_age` allows a cached answer that many seconds old. The budget checks
    run before every video, and a batch is 50-60 videos - asking the API
    fresh each time would spend a meaningful slice of the very quota being
    checked. A few minutes of staleness only shifts the reading by the handful
    of requests made since, which the callers' reserve margins already cover.
    """
    global _usage_cache
    if max_age > 0 and _usage_cache is not None:
        stamp, counters = _usage_cache
        if time.time() - stamp <= max_age:
            return counters
    data = _request("GET", "/chat/usage", timeout=30)
    counters = data if isinstance(data, dict) else {}
    _usage_cache = (time.time(), counters)
    return counters


def remaining_requests(max_age: float = 0.0) -> Optional[int]:
    """Requests left today, or None when unknown or unlimited.

    Callers use this to decide whether to spend on optional model work; None
    means "no reason to hold back".
    """
    try:
        counters = usage(max_age)
    except XtroError:
        return None
    if counters.get("unlimited"):
        return None
    limit = counters.get("daily_request_limit")
    used = counters.get("requests_today")
    if not isinstance(limit, int) or not isinstance(used, int) or limit < 0:
        return None
    return max(0, limit - used)


def models() -> list[dict]:
    data = _request("GET", "/chat/models", timeout=30)
    if isinstance(data, dict) and isinstance(data.get("data"), list):
        return data["data"]
    return []


def message(content: list[dict], system: str = "", model: str = "",
            max_tokens: int = 2000, timeout: float = 180.0) -> str:
    """One stateless `/messages` call. Returns the reply's text.

    Burst rate limits are retried; quota limits and auth failures are not.

    Verdicts are not perfectly repeatable: the gateway rejects `temperature`
    as deprecated for its models, so sampling cannot be pinned. Tests that
    read model verdicts must use thresholds, not exact expected sets.
    """
    body: dict[str, Any] = {
        "model": model or "XtroEdge Pro v3",
        "max_tokens": max_tokens,
        "messages": [{"role": "user", "content": content}],
    }
    if system:
        body["system"] = system

    last: Optional[XtroError] = None
    for attempt in range(_BURST_RETRIES):
        try:
            response = _request("POST", "/messages", body, timeout=timeout)
            return _text_of(response)
        except XtroQuotaError:
            raise
        except XtroAuthError:
            raise
        except XtroError as exc:
            if not exc.retryable:
                raise
            last = exc
            if attempt < _BURST_RETRIES - 1:
                time.sleep(_BURST_BACKOFF * (attempt + 1))
    raise last or XtroError("XtroEdge call failed.")


def _text_of(response: Any) -> str:
    """Concatenated text blocks from an Anthropic-shaped Messages response."""
    if not isinstance(response, dict):
        return ""
    if response.get("stop_reason") == "refusal":
        raise XtroError("The model declined this request.")
    blocks = response.get("content")
    if isinstance(blocks, str):
        return blocks
    if not isinstance(blocks, list):
        return ""
    parts = [b.get("text", "") for b in blocks
             if isinstance(b, dict) and b.get("type") == "text"]
    return "".join(parts).strip()


# --------------------------------------------------------------------------
def image_block(jpeg: bytes) -> dict:
    """A vision content block for a JPEG."""
    import base64

    return {
        "type": "image",
        "source": {
            "type": "base64",
            "media_type": "image/jpeg",
            "data": base64.standard_b64encode(jpeg).decode("ascii"),
        },
    }


def parse_json(text: str) -> Any:
    """Best-effort JSON out of a model reply.

    The gateway passes the provider's response through verbatim and does not
    document structured-output support, so the reply is plain text that may
    arrive wrapped in prose or a markdown fence. Rather than depend on a
    feature that may not be there, take the first JSON value in the text.
    """
    if not text:
        return None
    candidate = text.strip()
    if candidate.startswith("```"):
        candidate = candidate.split("\n", 1)[-1]
        if "```" in candidate:
            candidate = candidate.rsplit("```", 1)[0]
        candidate = candidate.strip()

    try:
        return json.loads(candidate)
    except json.JSONDecodeError:
        pass

    for opener, closer in (("{", "}"), ("[", "]")):
        start = candidate.find(opener)
        end = candidate.rfind(closer)
        if start != -1 and end > start:
            try:
                return json.loads(candidate[start:end + 1])
            except json.JSONDecodeError:
                continue
    return None
