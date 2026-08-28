"""AdsPower local-API client for the ``epicbet-api`` folder.

This folder is deliberately independent of ``app/`` (see README), so
this is a small standalone client rather than an import of
``app/sites/epicbet/adspower.py``. It covers only what the trigger
path here needs — find a profile, start it, learn its debug port, stop
it — not the profile CRUD the production adapter does.

Why AdsPower rather than the native Chrome this folder used to launch
------------------------------------------------------------------
AdsPower spoofs the TLS + HTTP/2 fingerprint (JA3/JA4, SETTINGS frame
order) that no JS-level patch can reach, and carries a per-profile
proxy + pre-warmed cookie jar. It is also how multiple EpicBet
accounts stay isolated from each other. The vanilla Chrome this server
used to spawn had none of that.

Two things about AdsPower that WILL bite you if you forget them
---------------------------------------------------------------
1. **The debug port changes on every start.** It is assigned per
   launch, not per profile. Any code that remembers "the EpicBet port
   is 9400" is wrong the moment the profile is restarted — read
   ``debug_port`` (or ``ws_puppeteer``) out of the ``start`` response
   every time, which is exactly what ``ui_server.py`` does now.

2. **The rate limit is ~1 request/second and it is per-APPLICATION,
   not per-process.** Two tools talking to AdsPower at once trip it
   even though neither is looping. ``_call`` therefore paces every
   request and retries a "too many request" refusal instead of
   surfacing it — otherwise a perfectly good start fails because some
   other window happened to poll at the same moment.
"""
from __future__ import annotations

import threading
import time
from dataclasses import dataclass
from typing import Optional

import requests

DEFAULT_API_BASE = "http://127.0.0.1:50325"
DEFAULT_TIMEOUT_SEC = 20.0

# See note 2 in the module docstring. 1.1s rather than 1.0s so clock
# jitter can't put two calls inside the same AdsPower-side second.
MIN_REQUEST_SPACING_SEC = 1.1
RATE_LIMIT_RETRIES = 3
RATE_LIMIT_BACKOFF_SEC = 1.5

# A profile that has never been opened (or has been idle for a while)
# takes 10-25s on its first start: AdsPower materialises the Chromium
# build, applies the proxy, and restores the cookie jar. Later starts
# are 1-3s.
START_POLL_TIMEOUT_SEC = 40.0
START_POLL_INTERVAL_SEC = 0.75


class AdsPowerError(RuntimeError):
    """Any AdsPower failure: transport, HTTP status, or a non-zero
    ``code`` in an otherwise-200 response."""

    def __init__(self, message: str, *, code: Optional[int] = None) -> None:
        super().__init__(message)
        self.code = code


@dataclass
class AdsProfile:
    user_id: str
    serial: str
    name: str
    group: str = ""
    remark: str = ""

    def label(self) -> str:
        """What the operator picks from. Unnamed profiles fall back to the
        user_id — most profiles in a real AdsPower install have no name,
        and a list of bare "#38, #39, #40" is not a choice anyone can make."""
        bits = []
        if self.serial:
            bits.append(f"#{self.serial}")
        bits.append(self.name if self.name else f"({self.user_id})")
        return " ".join(bits).strip() or self.user_id


@dataclass
class AdsBrowser:
    """A running profile. ``debug_port`` and ``ws_puppeteer`` are both
    fresh per start — never cache them across a stop."""
    user_id: str
    debug_port: Optional[int]
    ws_puppeteer: str
    webdriver: str = ""

    @property
    def http_debug_url(self) -> str:
        return f"http://127.0.0.1:{self.debug_port}" if self.debug_port else ""

    def cdp_url(self) -> str:
        """What to hand Playwright's ``connect_over_cdp``. Prefer the ws
        URL AdsPower hands back — it skips the HTTP discovery roundtrip
        and is what the production worker uses."""
        return self.ws_puppeteer or self.http_debug_url


class AdsPowerClient:
    """Sync client. Sync on purpose: ``start`` blocks for seconds and
    every caller here already runs it off the request thread."""

    def __init__(
        self,
        api_base: str = DEFAULT_API_BASE,
        api_token: Optional[str] = None,
        timeout: float = DEFAULT_TIMEOUT_SEC,
    ) -> None:
        self.api_base = api_base.rstrip("/")
        self.api_token = api_token or None
        self.timeout = timeout
        self._session = requests.Session()
        if self.api_token:
            self._session.headers["Authorization"] = f"Bearer {self.api_token}"
        self._last_call_at = 0.0
        self._pace_lock = threading.Lock()

    # -- Health ------------------------------------------------------------

    def status(self) -> dict:
        """Probe the local API so the operator gets "AdsPower isn't
        running" instead of a 25s timeout on ``start``."""
        return self._call("/status")

    # -- Profiles ----------------------------------------------------------

    def list_profiles(self, page_size: int = 100) -> list[AdsProfile]:
        data = self._call(
            "/api/v1/user/list", params={"page": 1, "page_size": page_size},
        )
        out: list[AdsProfile] = []
        for row in (data.get("data") or {}).get("list", []) or []:
            out.append(AdsProfile(
                user_id=str(row.get("user_id") or ""),
                serial=str(row.get("serial_number") or ""),
                name=str(row.get("name") or ""),
                group=str(row.get("group_name") or ""),
                remark=str(row.get("remark") or ""),
            ))
        return out

    def resolve(self, ref: str) -> AdsProfile:
        """Turn whatever the operator typed into one profile.

        Matched most-specific first — user_id, serial number, exact
        name, then case-insensitive partial name. A partial that hits
        more than one profile raises with the candidates listed rather
        than picking one: starting the wrong profile means betting from
        the wrong account.
        """
        ref = (ref or "").strip()
        if not ref:
            raise AdsPowerError("no AdsPower profile given")
        profiles = self.list_profiles()
        if not profiles:
            raise AdsPowerError(
                "AdsPower reports no profiles — create one in the AdsPower "
                "app first, then log into EpicBet inside it once."
            )
        for p in profiles:
            if p.user_id == ref:
                return p
        for p in profiles:
            if p.serial == ref:
                return p
        for p in profiles:
            if p.name == ref:
                return p
        if len(ref) >= 3:
            hits = [p for p in profiles if ref.lower() in p.name.lower()]
            if len(hits) == 1:
                return hits[0]
            if len(hits) > 1:
                names = ", ".join(h.label() for h in hits)
                raise AdsPowerError(
                    f"{ref!r} matches {len(hits)} AdsPower profiles ({names}) "
                    "— use the exact name, the serial, or the user_id"
                )
        known = ", ".join(p.label() for p in profiles[:10])
        raise AdsPowerError(f"no AdsPower profile matches {ref!r}. Known: {known}")

    # -- Lifecycle ---------------------------------------------------------

    def active(self, user_id: str) -> Optional[AdsBrowser]:
        """The already-running browser for this profile, or None.

        Never raises: AdsPower words "not started" differently across
        versions and it always means the same thing here.
        """
        try:
            data = self._call("/api/v1/browser/active", params={"user_id": user_id})
        except AdsPowerError:
            return None
        payload = data.get("data") or {}
        if str(payload.get("status") or "").lower() not in ("active", "running", "started"):
            return None
        return self._browser_from(user_id, payload)

    def start(self, user_id: str, *, open_tabs: int = 1, ip_tab: int = 0) -> AdsBrowser:
        """Start the profile and return its fresh debug endpoint.

        Idempotent — AdsPower hands back the running instance if the
        profile is already open. ``ip_tab=0`` suppresses the IP-check
        tab so the window opens on the profile's own start page.
        """
        # Cold starts materialise the Chromium build + proxy + cookie jar
        # (10-25s, see module docstring) — well past the 20s client default.
        data = self._call("/api/v1/browser/start", params={
            "user_id": user_id, "open_tabs": open_tabs, "ip_tab": ip_tab,
        }, timeout=60.0)
        browser = self._browser_from(user_id, data.get("data") or {})
        if browser is not None:
            return browser

        # Success with no debugger URL — rare, but the protocol allows
        # it on a cold start. Poll `active` until it has one.
        deadline = time.time() + START_POLL_TIMEOUT_SEC
        while time.time() < deadline:
            time.sleep(START_POLL_INTERVAL_SEC)
            browser = self.active(user_id)
            if browser is not None:
                return browser
        raise AdsPowerError(
            f"AdsPower started profile {user_id} but never returned a debugger "
            f"URL within {START_POLL_TIMEOUT_SEC:.0f}s — open the profile from "
            "the AdsPower window to see what it is waiting on"
        )

    def stop(self, user_id: str) -> dict:
        """Close the profile's browser. Best-effort — a stop that fails
        must not block the caller's own cleanup."""
        try:
            self._call("/api/v1/browser/stop", params={"user_id": user_id})
            return {"ok": True, "user_id": user_id}
        except AdsPowerError as e:
            return {"ok": False, "user_id": user_id, "error": str(e)}

    # -- Internals ---------------------------------------------------------

    @staticmethod
    def _browser_from(user_id: str, payload: dict) -> Optional[AdsBrowser]:
        ws = payload.get("ws") or {}
        ws_url = ws.get("puppeteer") if isinstance(ws, dict) else ""
        port_raw = payload.get("debug_port")
        try:
            debug_port = int(port_raw) if port_raw not in (None, "") else None
        except (TypeError, ValueError):
            debug_port = None
        if not ws_url and not debug_port:
            return None
        return AdsBrowser(
            user_id=user_id,
            debug_port=debug_port,
            ws_puppeteer=str(ws_url or ""),
            webdriver=str(payload.get("webdriver") or ""),
        )

    def _pace(self) -> None:
        with self._pace_lock:
            wait = MIN_REQUEST_SPACING_SEC - (time.time() - self._last_call_at)
            if wait > 0:
                time.sleep(wait)
            self._last_call_at = time.time()

    def _call(self, path: str, params: Optional[dict] = None,
              timeout: Optional[float] = None) -> dict:
        """Single chokepoint so pacing, the rate-limit retry, and error
        wording stay identical for every endpoint. ``timeout`` overrides the
        client default for slow calls (browser start can take 10-25s)."""
        url = f"{self.api_base}{path}"
        for attempt in range(RATE_LIMIT_RETRIES + 1):
            self._pace()
            try:
                r = self._session.get(url, params=params or None,
                                      timeout=timeout or self.timeout)
            except requests.RequestException as e:
                raise AdsPowerError(
                    f"AdsPower call to {path} failed — is the AdsPower desktop "
                    f"app running and listening on {self.api_base}? ({e})"
                ) from e
            if r.status_code >= 400:
                raise AdsPowerError(
                    f"AdsPower {path} returned HTTP {r.status_code}: {r.text[:200]}"
                )
            try:
                data = r.json()
            except ValueError as e:
                raise AdsPowerError(
                    f"AdsPower {path} returned non-JSON: {r.text[:200]}"
                ) from e
            code = data.get("code")
            if code == 0:
                return data
            msg = str(data.get("msg") or data.get("message") or data)[:300]
            if "too many request" in msg.lower() and attempt < RATE_LIMIT_RETRIES:
                time.sleep(RATE_LIMIT_BACKOFF_SEC)
                continue
            raise AdsPowerError(f"AdsPower {path}: {msg} (code {code})", code=code)
        raise AdsPowerError(f"AdsPower {path}: exhausted rate-limit retries")
