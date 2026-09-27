"""
Signed uplink to the MagicKeys Detect backend (Supabase edge function "ingest").

Wire format (must match supabase/functions/_shared/hmac.ts):
  POST <MKD_INGEST_URL>
  Content-Type: application/json
  X-MK-Device:    <device_id>
  X-MK-Timestamp: <unix seconds, integer>
  X-MK-Signature: hex(HMAC_SHA256(device_secret, "<timestamp>.<raw body bytes>"))

Body: {"deviceId": str, "events": [...], "heartbeat": {...} | null}

The backend rejects timestamps more than 300 s from its clock, so the Pi must
keep NTP time (the Mildura build also fits a DS3231 RTC).
"""

from __future__ import annotations

import hashlib
import hmac
import json
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Any

USER_AGENT = "magickeys-detect-agent/1.0"


def sign(secret: str, timestamp: int, body: bytes) -> str:
    msg = str(timestamp).encode("ascii") + b"." + body
    return hmac.new(secret.encode("utf-8"), msg, hashlib.sha256).hexdigest()


@dataclass
class SendResult:
    ok: bool
    status: int          # HTTP status, 0 if no response
    accepted: list[int]  # seqs the backend confirmed stored (new or duplicate)
    error: str | None


class Uplink:
    def __init__(self, url: str, device_id: str, secret: str, timeout_s: float = 15.0):
        self._url = url
        self._device_id = device_id
        self._secret = secret
        self._timeout = timeout_s

    def send(self, events: list[dict[str, Any]], heartbeat: dict[str, Any] | None,
             now: float | None = None) -> SendResult:
        payload = {"deviceId": self._device_id, "events": events, "heartbeat": heartbeat}
        body = json.dumps(payload, separators=(",", ":")).encode("utf-8")
        ts = int(now if now is not None else time.time())
        req = urllib.request.Request(
            self._url,
            data=body,
            method="POST",
            headers={
                "Content-Type": "application/json",
                "User-Agent": USER_AGENT,
                "X-MK-Device": self._device_id,
                "X-MK-Timestamp": str(ts),
                "X-MK-Signature": sign(self._secret, ts, body),
            },
        )
        try:
            with urllib.request.urlopen(req, timeout=self._timeout) as resp:
                status = resp.status
                raw = resp.read()
        except urllib.error.HTTPError as exc:
            detail = exc.read()[:300].decode("utf-8", "replace")
            return SendResult(False, exc.code, [], f"HTTP {exc.code}: {detail}")
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            return SendResult(False, 0, [], f"network: {exc}")

        try:
            data = json.loads(raw)
            accepted = [int(s) for s in data.get("accepted", [])]
        except (ValueError, TypeError, AttributeError) as exc:
            return SendResult(False, status, [], f"bad response body: {exc}")

        sent = {int(e["seq"]) for e in events}
        missing = sent - set(accepted)
        if missing:
            # Backend must confirm every seq it was sent; anything else is a fault.
            return SendResult(False, status, accepted, f"backend did not confirm seqs {sorted(missing)}")
        return SendResult(True, status, accepted, None)


class Backoff:
    """Exponential backoff, 2 s doubling to a 300 s ceiling. Reset on success."""

    def __init__(self, base: float = 2.0, ceiling: float = 300.0):
        self._base = base
        self._ceiling = ceiling
        self._fails = 0

    def fail(self) -> float:
        self._fails += 1
        return min(self._ceiling, self._base * (2 ** (self._fails - 1)))

    def reset(self) -> None:
        self._fails = 0

    @property
    def failures(self) -> int:
        return self._fails
