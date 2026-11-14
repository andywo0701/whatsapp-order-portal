"""Wake the scanning bot with one authenticated POST. URL and key stay in the environment."""

from __future__ import annotations

import json
import os
import re
import threading
import time
import urllib.request
from urllib.parse import urlsplit

# Per signed-in staff member. Enough to retry, not enough to hammer the bot.
SCAN_WINDOW = 60
SCAN_MAX = 3
SCAN_TIMEOUT = 8

_HITS: dict[str, list[float]] = {}
_LOCK = threading.Lock()
_ID = re.compile(r"^[A-Za-z0-9_-]{6,64}$")


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """Refuse redirects so the bearer key is not forwarded to another host."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: ANN001
        return None


def reset_scan_limits() -> None:
    with _LOCK:
        _HITS.clear()


def scan_is_limited(bucket: str, now: float | None = None) -> bool:
    moment = time.time() if now is None else now
    with _LOCK:
        hits = [stamp for stamp in _HITS.get(bucket, []) if moment - stamp < SCAN_WINDOW]
        _HITS[bucket] = hits
        return len(hits) >= SCAN_MAX


def mark_scan(bucket: str, now: float | None = None) -> None:
    moment = time.time() if now is None else now
    with _LOCK:
        _HITS.setdefault(bucket, []).append(moment)


def post_scan(payload: dict) -> str:
    """Return sent, unconfigured, or failed. Never raises and never logs the secret."""
    url, key = _credentials()
    if not url or not key:
        return "unconfigured"
    body = json.dumps(_safe_payload(payload), ensure_ascii=False).encode("utf-8")
    request = urllib.request.Request(
        url,
        data=body,
        method="POST",
        headers={
            "Authorization": f"Bearer {key}",
            "Content-Type": "application/json; charset=utf-8",
            "Accept": "application/json",
            "User-Agent": "whatsapp-order-portal",
        },
    )
    try:
        response = _send(request, SCAN_TIMEOUT)
        if response is None:
            return "failed"
        with response:
            code = getattr(response, "status", None)
            if code is None:
                code = response.getcode()
            response.read(2048)
    except Exception:
        return "failed"
    if isinstance(code, int) and 200 <= code < 300:
        return "sent"
    return "failed"


def _safe_payload(payload: dict) -> dict[str, str]:
    requested = payload.get("requested_by") if isinstance(payload, dict) else ""
    safe = {
        "action": "scan",
        "source": "whatsapp-order-portal",
        "requested_by": requested.strip()[:40] if isinstance(requested, str) else "",
    }
    submission = payload.get("submission_id") if isinstance(payload, dict) else ""
    if isinstance(submission, str) and _ID.fullmatch(submission):
        safe["submission_id"] = submission
    return safe


def _credentials() -> tuple[str, str]:
    url = os.environ.get("SCAN_WEBHOOK_URL", "").strip()
    key = os.environ.get("SCAN_WEBHOOK_KEY", "").strip()
    if not url or not key or len(url) > 500 or len(key) > 500:
        return "", ""
    if any(char in url or char in key for char in ("\n", "\r", "\x00")):
        return "", ""
    parsed = urlsplit(url)
    if parsed.scheme != "https" or not parsed.hostname:
        return "", ""
    if parsed.username or parsed.password:
        return "", ""
    return url, key


def _send(request: urllib.request.Request, timeout: float):
    opener = urllib.request.build_opener(_NoRedirect)
    return opener.open(request, timeout=timeout)
