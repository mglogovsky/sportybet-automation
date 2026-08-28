"""License server client (urllib only — no new dependencies).

POST {SERVER_BASE_URL}/api/license/check
  Headers: Authorization: Bearer {LICENSE_API_TOKEN}  — ONLY when non-empty
  Body:    {"key": "SBET-XXXX-XXXX-XXXX-XXXX"}
  200:     {"verdict": "OK", "expiresAt": <epoch>, "secondsLeft": <int>}
           {"verdict": "EXPIRED"|"REVOKED"|"UNKNOWN_KEY"}

check() returns (verdict, seconds_left, expires_at):
  ("OK", seconds_left, expires_at) or
  ("EXPIRED"|"REVOKED"|"UNKNOWN_KEY"|"UNREACHABLE", None, None)

Classification rule (PLAN-licensing.md v2, A1 — load-bearing):
  HTTP 200 with a parseable, KNOWN verdict -> that verdict.
  EVERYTHING else -> UNREACHABLE: timeout, DNS failure, any non-200
  (401/403/429/5xx included), unparseable body, or a 200 carrying an
  unrecognized verdict. A transport/protocol failure must never surface as
  a definitive lock — the server answers 503 when its own store is broken
  for exactly this reason.
"""
from __future__ import annotations

import json
import urllib.error
import urllib.request

import build_config

TIMEOUT = 4.0  # seconds — the app must never hang on a dead server

KNOWN_VERDICTS = ("EXPIRED", "REVOKED", "UNKNOWN_KEY")


def check(key: str) -> tuple:
    """Ask the server about a key. Any failure is UNREACHABLE — the caller
    (LicenseGate) decides what that means."""
    headers = {"Content-Type": "application/json"}
    if build_config.LICENSE_API_TOKEN:  # empty token = no header (v2 decision 1)
        headers["Authorization"] = f"Bearer {build_config.LICENSE_API_TOKEN}"
    req = urllib.request.Request(
        build_config.SERVER_BASE_URL.rstrip("/") + "/api/license/check",
        data=json.dumps({"key": key}).encode(),
        headers=headers,
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
            body = json.loads(resp.read().decode())
    except (OSError, urllib.error.URLError, ValueError):
        return ("UNREACHABLE", None, None)
    verdict = body.get("verdict")
    if verdict == "OK":
        try:
            seconds_left = int(body.get("secondsLeft"))
            expires_at = int(body.get("expiresAt"))
        except (TypeError, ValueError):
            return ("UNREACHABLE", None, None)
        return ("OK", seconds_left, expires_at)
    if verdict in KNOWN_VERDICTS:
        return (verdict, None, None)
    # A 200 with an unrecognized verdict is a protocol failure, not a
    # definitive lock.
    return ("UNREACHABLE", None, None)
