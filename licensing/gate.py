"""LicenseGate — background thread enforcing the license state machine.

States (self.status):
  "needs_key"        no key file — the UI shows key entry
  "checking"         a server check is in flight
  "OK"              licensed; expires_at/seconds_left are fresh
  "offline-retry"    a running-state check was unreachable — retry in 15 min
  "locked:<verdict>" dead (EXPIRED | REVOKED | UNKNOWN_KEY | UNREACHABLE)

Rules (PLAN-licensing.md v2, A1/A3):
  - startup, no key file            -> needs_key
  - startup, key present            -> up to 3 checks over ~60s (0s/15s/45s)
                                       before locked:UNREACHABLE (absorbs the
                                       relay restart on server deploys); any
                                       definitive verdict short-circuits
  - running                         -> re-check every 12h; ALSO at expires_at
                                       whenever seconds_left < 12h; and
                                       immediately after a machine wake
                                       (monotonic gap > ~2 min between ticks)
  - definitive non-OK while running -> graceful stop + locked:<verdict>
  - UNREACHABLE while running       -> offline-retry, one retry in 15 min,
                                       then locked:UNREACHABLE (+ graceful stop)
  - locked (any cause)              -> keep polling every 15 min; a poll that
                                       comes back OK clears the lock and
                                       re-enables the UI (admin extend picks
                                       up with no restart)

The gate never touches the flow directly: on lock it fires on_lock, which the
UI server wires to the existing graceful 'stop' action (finish the round,
cash out, idle — never abort mid-flight).
"""
from __future__ import annotations

import threading
import time

from . import client, store

RECHECK_INTERVAL = 12 * 3600           # 12 h
UNREACHABLE_RETRY = 15 * 60            # 15 min
LOCKED_POLL = 15 * 60                  # 15 min — renewal pickup while locked
STARTUP_LADDER = (0, 15, 45)           # ~60s of startup retries
WAKE_SLOP = 120                        # gap beyond the scheduled timeout that
                                       # means "the machine slept" — re-check


class LicenseGate(threading.Thread):
    def __init__(self, on_change=None, on_lock=None) -> None:
        super().__init__(name="license-gate", daemon=True)
        self.on_change = on_change or (lambda status, expires_at, seconds_left: None)
        self.on_lock = on_lock or (lambda verdict: None)
        self.status = "checking"
        self.expires_at: int | None = None
        self.seconds_left: int | None = None
        self._wake = threading.Event()

    # -- callbacks ---------------------------------------------------------
    def _set(self, status: str, expires_at=None, seconds_left=None) -> None:
        self.status = status
        self.expires_at = expires_at
        self.seconds_left = seconds_left
        try:
            self.on_change(status, expires_at, seconds_left)
        except Exception:
            pass

    def _lock(self, verdict: str) -> None:
        self._set(f"locked:{verdict}")
        try:
            self.on_lock(verdict)
        except Exception:
            pass

    # -- actions (called from the UI server) --------------------------------
    def activate(self, key: str) -> str:
        """Validate a freshly entered key with the server. On OK the key is
        saved locally and the gate unlocks. Returns the verdict."""
        prev = (self.status, self.expires_at, self.seconds_left)
        self._set("checking")
        verdict, seconds_left, expires_at = client.check(key)
        if verdict == "OK":
            store.save(key)
            self._set("OK", expires_at, seconds_left)
            self._wake.set()  # restart the 12h recheck clock
        elif verdict == "UNREACHABLE":
            # Don't lock a previously-working app over a failed re-entry —
            # restore the prior state (from needs_key this still grants
            # nothing, so fail-closed is preserved).
            self._set(*prev)
        else:
            self._lock(verdict)
        return verdict

    def deactivate(self) -> None:
        store.delete()
        self._set("needs_key")
        self._wake.set()

    def recheck(self) -> None:
        """UI 'Re-check'/'Retry' button: nudge the loop so it checks now.
        Only meaningful when there is a stored key to check."""
        self._wake.set()

    # -- thread -------------------------------------------------------------
    def _startup_ladder(self, key: str) -> None:
        """Key present at boot: up to 3 checks over ~60s (0s/15s/45s) before
        declaring locked:UNREACHABLE. Any definitive verdict short-circuits."""
        self._set("checking")
        for delay in STARTUP_LADDER:
            if delay:
                self._wake.wait(delay)
                self._wake.clear()
                # An activate/deactivate during the wait owns the state now.
                if store.load() != key or self.status not in ("checking", "offline-retry"):
                    return
            verdict, seconds_left, expires_at = client.check(key)
            if verdict == "OK":
                self._set("OK", expires_at, seconds_left)
                return
            if verdict != "UNREACHABLE":
                self._lock(verdict)
                return
        # All three tries unreachable — fail closed.
        self._lock("UNREACHABLE")

    def _next_timeout(self) -> float | None:
        if self.status == "OK":
            timeout = RECHECK_INTERVAL
            if self.expires_at is not None:
                to_expiry = self.expires_at - time.time()
                if to_expiry < RECHECK_INTERVAL:
                    # Bound post-expiry overrun to minutes, not 12h.
                    timeout = max(1.0, to_expiry)
            return timeout
        if self.status.startswith("locked:"):
            return LOCKED_POLL
        return None  # needs_key / checking: sleep until woken

    def run(self) -> None:
        key = store.load()
        if not key:
            self._set("needs_key")
        else:
            self._startup_ladder(key)

        while True:
            timeout = self._next_timeout()
            self._wake.wait(timeout)
            self._wake.clear()
            # Wake-from-sleep lands here late (the monotonic gap exceeds the
            # scheduled timeout + WAKE_SLOP) and takes the same check path
            # below — that IS the "check immediately on wake" behavior.
            if self.status in ("needs_key", "checking"):
                continue  # nothing to do until activate/deactivate wakes us
            key = store.load()
            if not key:
                self._set("needs_key")
                continue
            # locked polls every 15 min; OK checks at 12h / expires_at / wake
            # (a wake from machine sleep just lands here early — same path).
            verdict, seconds_left, expires_at = client.check(key)
            if verdict == "OK":
                self._set("OK", expires_at, seconds_left)  # also clears a lock
            elif verdict == "UNREACHABLE":
                if self.status.startswith("locked:"):
                    continue  # stays locked, network text
                self._set("offline-retry", self.expires_at, self.seconds_left)
                self._wake.wait(UNREACHABLE_RETRY)
                self._wake.clear()
                last_tick = time.monotonic()
                # A wake (activate/deactivate) may have changed everything
                # while we waited — only retry if we're still mid-retry on
                # the same key.
                if self.status != "offline-retry" or store.load() != key:
                    continue
                verdict, seconds_left, expires_at = client.check(key)
                if verdict == "OK":
                    self._set("OK", expires_at, seconds_left)
                elif verdict == "UNREACHABLE":
                    self._lock("UNREACHABLE")
                else:
                    self._lock(verdict)
            else:
                # Definitive non-OK — locked (and while running, on_lock
                # enqueues the graceful stop).
                self._lock(verdict)
