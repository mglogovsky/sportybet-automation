#!/usr/bin/env python3
"""SportyBet balance-starving hold flow — sports bet + Turbo Mines, one loop.

This is the SportyBet equivalent of ../mines_hold_flow.py (Betnacional), adapted
to SportyBet's SYNCHRONOUS placement and verified live on 2026-08-27
(sportybet_hold_capture.jsonl / sportybet_hold_capture2.jsonl):

  * `POST /api/ng/orders/order` blocks ~9-10s (in-play window) and returns the
    FINAL verdict. The server's balance check happens at SETTLE time (end of
    the window), not at fire time — so a Mines hold started during the window
    starves the bet into `bizCode 4200 "balance is not enough"`.
  * The 4200 response carries `data.balance` (server truth, x10000 units) —
    used to re-sync the in-memory balance after every cycle.
  * First-cell instant cashout pays only ~0.99x (25-grid/1-mine): the redeem
    path costs a ~1% fee. The bet therefore stakes `balance - margin`
    (--redeem-margin, default 5 NGN): while holding, server sees
    balance - mines_stake < stake -> 4200; after redeem, server sees
    balance - fee >= stake -> accepted.

One round
---------
  0. WARM-UP (once per round): a minimal bet (--warmup-stake, default 10 NGN)
     on the armed selection consumes the session's one instant-verdict order
     (~0.5s accept), so every loop bet gets the 7-13s processing window.
  Then, one cycle:
  1. Fire the bet async — BET FIRST. The server's fire-time balance check
     requires balance >= stake at entry, so the bet must go in at (near)
     full balance. (Hold-first was tried: every cycle died at this check in
     0.3s — proven live.)
  2. Immediately create a Mines round + open one cell — MINES SECOND. The
     hold locks inside the processing window and starves the bet at settle
     time (bizCode 4200).
  3. Mine hit  -> no cashout exists; wait for the inevitable 4200; balance
     drops by --mines-stake; next cycle fires immediately.
  4. Safe cell -> hold and watch:
       ENTER        -> cash out (redeem) -> bet settles ACCEPTED -> done.
       4200 verdict -> cash out to recover -> balance -= ~fee -> next cycle.
       4510         -> odds moved; cash out, retry.
       19414        -> cipher key rejected; re-read from storage once.
  5. Ctrl+C at any point -> wait for the in-flight verdict, cash out the Mines
     round, print the final balance, exit ready for a new run.

Arming (listen mode)
--------------------
The cipher key is lazy-minted by the SPA on money-flow calls, so the script
cannot mint it itself. Instead it LISTENS: you add a selection to the betslip
and place a minimal (10 NGN) bet in the UI — that mints the key. The script
captures key + slip, shows the target, and starts the loop on Enter.

Mines transport
---------------
The game tab is needed ONCE to obtain the JWT (launch URL is behind AWS-WAF).
All rounds then run headless via plain `requests` (sportybet-mines-flow.md
§9, tested live) — no game tab required during the loop.
"""
from __future__ import annotations

import argparse
import base64
import json
import math
import os
import queue
import random
import re
import select
import sys
import threading
import time
import urllib.parse
import uuid
from collections import deque

import requests

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from adspower import AdsPowerError  # noqa: E402
from sportybet_mines import GAME_ORIGIN, SportyBetMines  # noqa: E402
from sportybet_place import aes_decrypt_b64, aes_encrypt_b64  # noqa: E402

ORDER_PATH = "/api/ng/orders/order"
BALANCE_PATH = "/api/ng/pocket/v1/finAccs/finAcc/userBal/NGN"
RESULT_VAR = "__sportyBetResult"

# SportyBet minimum sports stake (NGN) — observed 10.00 bets in realbetlist.
MIN_SPORTS_STAKE = 10.0


class MinesAuthExpired(RuntimeError):
    """A round call returned 401 — the launch JWT needs a refresh."""


# ---------------------------------------------------------------------------
# Headless Mines transport (sportybet-mines-flow.md §9 — no tab needed).
# ---------------------------------------------------------------------------
class HeadlessMines:
    def __init__(self, jwt: str, apikey: str):
        self.base = f"https://{GAME_ORIGIN}/api/"
        self.s = requests.Session()
        self.s.headers.update({
            "Content-Type": "application/json",
            "Authorization": jwt,
            "apikey": apikey,
            "subpartnerid": "SportyBet NG",
            "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                          "AppleWebKit/537.36 (KHTML, like Gecko) "
                          "Chrome/131.0.0.0 Safari/537.36",
        })

    def _api(self, path: str, body: dict) -> dict:
        r = self.s.post(self.base + path, data=json.dumps(body), timeout=15)
        if r.status_code == 401:
            raise MinesAuthExpired(f"{path} HTTP 401 — launch JWT expired")
        if r.status_code != 200:
            raise RuntimeError(f"{path} HTTP {r.status_code}: {r.text[:200]}")
        return r.json()

    def create_round(self, mines: int, desk_size: int) -> tuple[str, str]:
        seed = str(uuid.uuid4())
        d = self._api("games/create", {
            "clientSeed": seed, "nonce": 1, "size": mines,
            "deskSize": desk_size, "theme": "turbomines",
        })
        return d["roundId"], seed

    def place(self, round_id: str, index: int, seed: str, amount: float) -> dict:
        return self._api("bets/place", {
            "theme": "turbomines", "roundId": round_id, "index": index,
            "clientSeed": seed, "nonce": 1,
            "amount": int(amount), "currency": "ngn",
        })

    def cashout(self, round_id: str) -> dict:
        return self._api("bets/cashout", {"gameId": round_id})


# ---------------------------------------------------------------------------
# Enter-signal helpers (non-blocking when stdin is not a TTY).
# ---------------------------------------------------------------------------
def _enter_ready() -> bool:
    if not sys.stdin.isatty():
        return False
    try:
        ready, _, _ = select.select([sys.stdin], [], [], 0)
    except (OSError, ValueError):
        return False
    return bool(ready)


def _consume_enter() -> None:
    try:
        sys.stdin.readline()
    except EOFError:
        pass


def wait_for_enter(prompt: str) -> None:
    if prompt:
        print(prompt, flush=True)
    if sys.stdin.isatty():
        input()
    else:
        sys.stdin.readline()


# ---------------------------------------------------------------------------
# Control bridge — one control surface for the terminal AND the UI app
# (sportybet_hold_ui.py). The flow publishes its state and receives actions
# ("begin"/"redeem"/"rearm"/"retry"/"stop") through here; with no action
# queue attached it degrades to plain keyboard Enter, exactly as before.
# ---------------------------------------------------------------------------
class StopRequested(Exception):
    """Raised at gates/cycle boundaries once the operator asked to stop."""


class ControlBridge:
    def __init__(self, actions: "queue.Queue | None" = None):
        self.actions = actions
        self.snapshot: dict = {"state": "setup"}
        self.log_lines: deque = deque(maxlen=300)
        self.history: deque = deque(maxlen=40)
        self.stop_requested = False
        self.rearm_requested = False
        self._lock = threading.Lock()

    def record(self, entry: dict) -> None:
        """Append a cycle record ({cycle, vclass, secs}) shown under the UI log."""
        self.history.append(entry)
        self.publish(history=list(self.history))

    def log(self, msg: str = "") -> None:
        print(msg, flush=True)
        self.log_lines.append(str(msg))

    def publish(self, **fields) -> None:
        with self._lock:
            self.snapshot.update(fields)

    def snap(self) -> dict:
        with self._lock:
            return dict(self.snapshot)

    def _drain(self):
        """Non-blocking: consume queued UI actions. "stop" is captured as a
        flag (and published); anything else is returned as (action, payload)."""
        if self.actions is None:
            return None
        try:
            while True:
                item = self.actions.get_nowait()
                act, payload = item if isinstance(item, tuple) else (item, None)
                if act == "stop":
                    if not self.stop_requested:
                        self.stop_requested = True
                        self.publish(state="stopping")
                        self.log("STOP requested — finishing the round, "
                                 "then cashing out the mines hold")
                elif act == "rearm":
                    # Captured as a flag (like stop) so it works mid-loop,
                    # not just at gates; gates consume it via _rearm_gate().
                    if not self.rearm_requested:
                        self.rearm_requested = True
                        self.publish(rearm_pending=True)
                        self.log("CHANGE SELECTION requested — this round "
                                 "plays out, then back to the betslip")
                else:
                    return act, payload
        except queue.Empty:
            return None

    def wait_action(self, timeout: float = 0.2):
        """Block up to `timeout` for a queued action; returns (act, payload)
        or None. Keyboard Enter maps to ("enter", None)."""
        deadline = time.time() + timeout
        while True:
            if _enter_ready():
                _consume_enter()
                return ("enter", None)
            got = self._drain()
            if got is not None:
                return got
            if self.stop_requested:
                raise StopRequested()
            if time.time() >= deadline:
                return None
            time.sleep(0.1)

    def clear_actions(self) -> None:
        """Drop stale queued actions (e.g. a double-clicked button) so they
        can't leak into the next hold and fire unintended."""
        if self.actions is None:
            return
        try:
            while True:
                self.actions.get_nowait()
        except queue.Empty:
            pass

    def redeem_ready(self) -> bool:
        """Mid-hold poll: True when the operator wants to redeem (button or
        Enter). A queued "stop" is captured as a side effect."""
        got = self._drain()
        if got and got[0] == "redeem":
            _consume_enter()  # discard a stray newline so it can't double-fire
            return True
        if got is not None:
            self.log(f"(ignored mid-hold action {got[0]!r})")
        return _enter_ready()

    def _take_rearm(self) -> bool:
        """Consume a pending CHANGE SELECTION request (clears the flag)."""
        if not self.rearm_requested:
            return False
        self.rearm_requested = False
        self.publish(rearm_pending=False)
        return True

    def gate(self, prompt: str, accept=("begin",), state: str | None = None):
        """Block until an accepted action arrives (UI button or Enter, which
        maps to the first accepted action). Returns (action, payload).
        Raises StopRequested on stop."""
        if state:
            self.publish(state=state, prompt=prompt)
        self.log(prompt)
        if self.actions is None:
            wait_for_enter("")
            if self.stop_requested:
                raise StopRequested()
            return accept[0], None
        while True:
            if _enter_ready():
                _consume_enter()
                if self.stop_requested:
                    raise StopRequested()
                self._take_rearm()  # stale mid-loop click must not leak past a gate
                return accept[0], None
            got = self._drain()
            if got and got[0] in accept:
                self.clear_actions()  # anything left is stale (double-clicks)
                return got
            if "rearm" in accept and self._take_rearm():
                self.clear_actions()
                return ("rearm", None)
            if self.stop_requested:
                raise StopRequested()
            time.sleep(0.2)


# ---------------------------------------------------------------------------
# Live market status from the SPA's own odds websocket (alive-*.sportybet.com,
# Engine.IO `42["data",{topic, body(base64)}]` frames). This is what decides
# instant-vs-held: marketStatus 0 = OPEN → the server verdicts a bet INSTANTLY
# (~0.3s, no window — a fire books NAKED); 1/2 = suspended → the server PARKS
# the order until re-open (6-15s — the window the mines hold needs).
# The tracker is best-effort: with no frames (tab on another page, socket not
# streaming the armed event) everything falls back to REST market_status().
# ---------------------------------------------------------------------------
def _extract_market_statuses(decoded) -> dict:
    """{ 'marketId|specifier': status } from a decoded ^odds body, tolerating
    the shapes seen so far (dict with markets[], single-market dict, arrays)."""
    out = {}

    def take(mid, spec, st):
        try:
            out[f"{mid}|{spec or ''}"] = int(st)
        except (TypeError, ValueError):
            pass

    def from_market(m) -> None:
        if isinstance(m, dict):
            mid = m.get("id", m.get("marketId"))
            st = m.get("status", m.get("marketStatus"))
            if mid is not None and st is not None:
                take(mid, m.get("specifier") or "", st)
        elif (isinstance(m, (list, tuple)) and len(m) >= 3
                and isinstance(m[2], int) and isinstance(m[0], (str, int))):
            # positional: [marketId, specifier, marketStatus, ...]
            take(m[0], m[1] if isinstance(m[1], str) else "", m[2])

    if isinstance(decoded, dict):
        mkts = decoded.get("markets")
        if isinstance(mkts, list):
            for m in mkts:
                from_market(m)
        else:
            from_market(decoded)
    elif isinstance(decoded, list):
        for el in decoded:
            from_market(el)
    return out


class WsMarketTracker:
    """Tracks market/event status pushed on the odds websocket of the sports
    tab. watch() marks the armed selection's market; every matching frame
    publishes market_status into the bridge (the UI reads it 1 Hz)."""

    FRESH_SECS = 10.0

    def __init__(self, b: ControlBridge):
        self.b = b
        self.markets: dict = {}        # 'mid|spec' -> (status, ts)
        self.event_status: dict = {}   # eventId -> (status, ts)
        self.watched: str | None = None
        self.frames = 0

    def attach(self, page) -> None:
        try:
            page.on("websocket", self._on_ws)
        except Exception as e:
            self.b.log(f"ws tracker: attach failed ({e}) — REST fallback only")

    def watch(self, slip: dict | None) -> None:
        if not slip:
            self.watched = None
            return
        mi = slip.get("marketInfo") or {}
        mid = str(mi.get("id"))
        spec = str(slip.get("specifier") or mi.get("specifier") or "")
        if "?" in mid:
            mid, glued = mid.split("?", 1)
            spec = spec or glued
        self.watched = f"{mid}|{spec}"

    def status_for(self, slip: dict) -> int | None:
        mi = slip.get("marketInfo") or {}
        mid = str(mi.get("id"))
        spec = str(slip.get("specifier") or mi.get("specifier") or "")
        if "?" in mid:
            mid, glued = mid.split("?", 1)
            spec = spec or glued
        for key in (f"{mid}|{spec}", f"{mid}|"):
            hit = self.markets.get(key)
            if hit and time.time() - hit[1] <= self.FRESH_SECS:
                return hit[0]
        return None

    def _on_ws(self, ws) -> None:
        try:
            ws.on("framereceived", self._on_frame)
        except Exception:
            pass

    def _on_frame(self, payload) -> None:
        try:
            if isinstance(payload, bytes) or not payload.startswith('42["data"'):
                return
            msg = (json.loads(payload[2:]) or [None, {}])[1] or {}
            topic = str(msg.get("topic") or "")
            body = msg.get("body")
            if isinstance(body, str):
                decoded = json.loads(
                    base64.b64decode(body).decode("utf-8", "replace"))
            elif isinstance(body, dict):
                decoded = body
            else:
                return
            self._ingest(topic, decoded)
        except Exception:
            pass

    def _ingest(self, topic: str, decoded) -> None:
        now = time.time()
        if isinstance(decoded, dict) and "eventStatus" in decoded:
            m = re.search(r"sr:match:(\d+)", topic)
            if m:
                try:
                    self.event_status[m.group(1)] = (int(decoded["eventStatus"]), now)
                except (TypeError, ValueError):
                    pass
        if "^odds" not in topic:
            return
        found = _extract_market_statuses(decoded)
        if self.frames < 5:
            self.frames += 1
            self.b.log(f"ws odds frame: {json.dumps(decoded)[:240]}")
        for key, st in found.items():
            self.markets[key] = (st, now)
        if self.watched and self.watched in found:
            self.b.publish(market_status=found[self.watched],
                           market_status_at=now)


# ---------------------------------------------------------------------------
# Browser-side helpers (sportybet tab via the shared Playwright connection).
# ---------------------------------------------------------------------------
def find_key(page) -> tuple[bytes, str, float] | None:
    """(AES key, transId, expires_at_epoch) from ng_transId / CIPHER_AES_KEY
    in either storage, or None when no fresh key exists."""
    for store in ("localStorage", "sessionStorage"):
        for name in ("ng_transId", "CIPHER_AES_KEY"):
            try:
                raw = page.evaluate(f"() => {store}.getItem('{name}')")
            except Exception:
                continue
            if not raw:
                continue
            try:
                d = json.loads(raw)
            except Exception:
                continue
            if d.get("key") and d.get("transId") and d.get("date", 0) > time.time() * 1000:
                try:
                    key = base64.b64decode(urllib.parse.unquote(d["key"]))
                except Exception:
                    continue
                if len(key) == 16:
                    return key, d["transId"], float(d["date"]) / 1000.0
    return None


def read_selection(page, event_id: str | None = None) -> dict | None:
    """A selection from localStorage.betslips (by event id, or the first one)."""
    slips = read_all_selections(page)
    if event_id:
        return next((s for s in slips if s.get("eventId") == event_id), None)
    return slips[0] if slips else None


def read_all_selections(page) -> list[dict]:
    """Every selection in localStorage.betslips — note the array can hold
    STALE entries the betslip UI no longer shows, so position means nothing;
    never trust slips[0] blindly when there's more than one."""
    try:
        raw = page.evaluate("() => localStorage.getItem('betslips')")
        slips = json.loads(raw or "[]")
        return slips if isinstance(slips, list) else []
    except Exception:
        return []


def selection_sig(s: dict) -> tuple:
    return (s.get("eventId"),
            (s.get("marketInfo") or {}).get("id"),
            (s.get("outcomeInfo") or {}).get("id"))


def describe_selection(s: dict) -> dict:
    oi = s.get("outcomeInfo") or {}
    mi = s.get("marketInfo") or {}
    return {"eventId": s.get("eventId"), "marketId": mi.get("id"),
            "outcomeId": oi.get("id"), "market": mi.get("desc", "?"),
            "outcome": oi.get("desc", "?"), "odds": oi.get("odds"),
            "label": selection_label(s)}


def terminal_pick(count: int, b: "ControlBridge") -> int:
    """Terminal picker for a multi-selection slip: read 1..count from stdin."""
    while True:
        b.log(f"type 1-{count} and press Enter to arm:")
        try:
            line = sys.stdin.readline()
        except EOFError:
            line = ""
        line = (line or "").strip()
        if line.isdigit() and 1 <= int(line) <= count:
            return int(line) - 1


def market_suspended(page) -> bool:
    """True when the betslip panel shows a 'Suspended' badge for the current
    selection. Scoped to the slip container (match lists show 'Suspended' too)
    by requiring slip markers ('total stake'/'place bet') nearby."""
    try:
        return bool(page.evaluate("""() => {
          const els = document.querySelectorAll('div, section, aside');
          for (const p of els) {
            const t = (p.textContent || '').toLowerCase();
            if (t.length > 4000) continue;
            if (!t.includes('total stake') && !t.includes('place bet')) continue;
            if (t.includes('suspended')) return true;
          }
          return false;
        }"""))
    except Exception:
        return False


def accept_slip_changes(page) -> bool:
    """Click the betslip's 'Accept Changes' prompt if it's showing.

    When odds move (or a selection is suspended) the slip parks the change
    behind this button — until it's clicked, localStorage.betslips keeps the
    STALE odds and orders built from them get 4510'd/rejected. Seen live in
    the SPA's own analytics: `betslip__accept_change__click` with
    `unavailable_selections_count: 1`. Returns True if a prompt was clicked.
    """
    try:
        return bool(page.evaluate("""() => {
          const els = document.querySelectorAll('button, [role="button"], a, span, div');
          for (const el of els) {
            const t = (el.textContent || '').trim().toLowerCase();
            if (t === 'accept changes' || t === 'accept change') {
              const r = el.getBoundingClientRect();
              if (r.width > 0 && r.height > 0) { el.click(); return true; }
            }
          }
          return false;
        }"""))
    except Exception:
        return False


def clear_betslip(page) -> int:
    """Remove every selection currently in the betslip by clicking each item's
    own remove icon — <i class="m-icon-delete"></i> inside the slip's
    .m-list .m-item rows (selector confirmed against the live DOM 2026-08-28).
    The SPA then prunes localStorage.betslips itself, so a leftover selection
    can't silently re-arm as the next target. Stops when the slip reads empty,
    when no icons remain, or after 12 clicks. Returns clicks performed."""
    clicked = 0
    while read_all_selections(page) and clicked < 12:
        try:
            ok = page.evaluate("""() => {
              const icons = [...document.querySelectorAll(
                '#j_betslip .m-list .m-item i.m-icon-delete, '
                + '.m-betslips .m-list .m-item i.m-icon-delete')]
                .filter(el => {
                  const r = el.getBoundingClientRect();
                  return r.width > 0 && r.height > 0;
                });
              if (!icons.length) return false;
              icons[0].click();
              return true;
            }""")
        except Exception:
            break
        if not ok:
            break
        clicked += 1
        page.wait_for_timeout(400)
    return clicked


def selection_label(slip: dict) -> str:
    oi = slip.get("outcomeInfo") or {}
    mi = slip.get("marketInfo") or {}
    return (f"{slip.get('eventId')}  {mi.get('desc', '?')} / "
            f"{oi.get('desc', '?')} @ {oi.get('odds')}")


def build_payload(slip: dict, stake_units: int) -> dict:
    """The exact orders/order payload the SPA sends (verified 2026-08-27).

    Specifier handling: the betslip may store it as a separate field
    (marketInfo.specifier / slip.specifier) OR glued into marketInfo.id
    ("202?setnr=2", observed on tennis set markets). The selection id must
    always be "uof:1/<sport>/<market>/<outcome>?<specifier>" — getting this
    wrong (specifier mid-path or doubled) makes the server instant-reject
    with bizCode 19999 on every order."""
    oi = slip["outcomeInfo"]
    mi = slip.get("marketInfo") or {}
    mid = str(mi.get("id"))
    spec = str(slip.get("specifier") or mi.get("specifier") or "")
    if "?" in mid:
        mid, glued = mid.split("?", 1)
        spec = spec or glued
    sel_id = f"uof:1/{slip['sportId']}/{mid}/{oi['id']}"
    if spec:
        sel_id += "?" + spec
    return {
        "bizType": 1,
        "ticket": {"selections": [{
            "eventId": slip["eventId"], "id": sel_id, "odds": oi["odds"],
            "banker": False, "probability": float(oi.get("probability") or 0)}],
            "bets": [{"selectedSystems": [1], "stake": {"value": stake_units}}]},
        "orderType": 1, "paymentType": 0, "isBonusFactor": False,
        "subBizType": 2, "actualPayAmount": stake_units, "loadingShareCode": "",
    }


def fire_bet(page, body_b64: str, transid: str) -> None:
    """Fire orders/order WITHOUT awaiting — the ~9-10s verdict is polled off
    window.__sportyBetResult so Mines can run during the in-play window."""
    js = """
    (() => {
      window.RESULT_VAR = null;
      window.originFetch('ORDER_PATH', {
        method: 'POST',
        headers: HEADERS,
        credentials: 'include',
        body: BODY,
      }).then(async r => ({ status: r.status, body: await r.text() }))
        .then(r => { window.RESULT_VAR = r; })
        .catch(e => { window.RESULT_VAR = { status: -1, body: String(e) }; });
      return 'fired';
    })()
    """.replace("RESULT_VAR", RESULT_VAR).replace("ORDER_PATH", ORDER_PATH)
    js = js.replace("BODY", json.dumps(body_b64)).replace(
        "HEADERS", json.dumps({
            "content-type": "application/json;charset=UTF-8",
            "clientid": "web", "platform": "web", "operid": "2",
            "transid": transid,
        }))
    page.evaluate(js)


def poll_bet(page) -> dict | None:
    """The settled {status, body} once the promise lands, else None."""
    try:
        return page.evaluate(f"() => window.{RESULT_VAR}")
    except Exception:
        return None


def decode_verdict(res: dict, key: bytes) -> dict:
    if res.get("status") != 200:
        return {"bizCode": -1, "http": res.get("status"),
                "message": (res.get("body") or "")[:300]}
    try:
        return json.loads(aes_decrypt_b64(res["body"], key))
    except Exception as e:
        return {"bizCode": -2, "message": f"decrypt failed: {e}"}


def wait_verdict(page, key: bytes, timeout: float = 30.0) -> dict:
    deadline = time.time() + timeout
    while time.time() < deadline:
        res = poll_bet(page)
        if res is not None:
            return decode_verdict(res, key)
        page.wait_for_timeout(300)
    raise SystemExit("bet verdict never arrived within 30s — check the browser tab")


def classify(verdict: dict) -> str:
    code = verdict.get("bizCode")
    if code == 10000:
        return "accepted"
    if code == 4200:
        return "starved"          # "balance is not enough" — the hold worked
    if code == 4510:
        return "odds_moved"
    if code == 4801:
        return "stake_limit"
    if code == 19414:
        return "key_rejected"
    if code == 19999:
        return "unavailable"      # dead/suspended market (isAvailable: false)
    # 19004 "already being processed" can also surface under other codes with
    # the same message — the previous order still holds the processing lock.
    if code == 19004 or "already being processed" in str(verdict.get("message", "")):
        return "already_processing"
    return "failed"


def verdict_balance_ngn(verdict: dict) -> float | None:
    """Server balance carried by 4200 responses (x10000 units).

    NOTE: observed to LAG the real wallet by a full cycle (Mines debit/credit
    not yet propagated) — kept for logging only; never use it to size the next
    stake (use settle_balance)."""
    bal = (verdict.get("data") or {}).get("balance")
    try:
        return int(bal) / 10000.0
    except (TypeError, ValueError):
        return None


def read_balance(page) -> float | None:
    """Live wallet balance in NGN (plain-JSON GET via originFetch)."""
    js = """
    (async () => {
      const r = await window.originFetch('BALANCE_PATH?_t=' + Date.now(),
                                         { credentials: 'include' });
      const j = await r.json();
      return (j && j.data && j.data.avlBal != null) ? j.data.avlBal : null;
    })()
    """.replace("BALANCE_PATH", BALANCE_PATH)
    try:
        bal = page.evaluate(js)
    except Exception:
        return None
    return (int(bal) / 10000.0) if bal is not None else None


def settle_balance(page, *, timeout: float = 15.0, interval: float = 1.5) -> float | None:
    """Wait for the wallet to go quiet after a cycle, then return it.

    The 4200 verdict's `data.balance` lags the real wallet by a cycle (the
    Mines debit/credit hasn't propagated when the sports server answers), so
    post-cycle re-sync must NOT trust verdict arithmetic. We poll `userBal`
    until two consecutive reads agree — that is the authoritative balance for
    the next stake. This also paces the loop past the server's order
    processing lock (bizCode 19004).
    """
    prev = read_balance(page)
    deadline = time.time() + timeout
    while time.time() < deadline:
        page.wait_for_timeout(int(interval * 1000))
        cur = read_balance(page)
        if cur is not None and cur == prev:
            return cur
        if cur is not None:
            prev = cur
    return prev


# ---------------------------------------------------------------------------
# One cycle. `live` tracks in-flight state so Ctrl+C cleanup can settle.
# ---------------------------------------------------------------------------
def api_fetch(page, path: str, method: str = "GET",
              headers: dict | None = None, body: str | None = None) -> dict:
    """Awaited window.originFetch — returns {status, body}. (fire_bet is the
    fire-and-forget variant, used only for orders/order.)"""
    js = """
    (async () => {
      const r = await window.originFetch(PATH, {
        method: METHOD,
        headers: HEADERS,
        credentials: 'include',
        BODYLINE
      });
      return { status: r.status, body: await r.text() };
    })()
    """.replace("PATH", json.dumps(path)) \
       .replace("METHOD", json.dumps(method)) \
       .replace("HEADERS", json.dumps(headers or {})) \
       .replace("BODYLINE", f"body: {json.dumps(body)}," if body else "")
    return page.evaluate(js)


def market_status(page, slip: dict) -> int | None:
    """Live status of the slip's market via factsCenter/Outcomes (plain JSON,
    no encryption). 0 = OPEN (a bet verdicts INSTANTLY — no window), 1/2 =
    suspended (the server parks the order until re-open — the window the
    mines hold needs). None = couldn't read."""
    mi = slip.get("marketInfo") or {}
    mid = str(mi.get("id"))
    spec = str(slip.get("specifier") or mi.get("specifier") or "")
    if "?" in mid:
        mid, glued = mid.split("?", 1)
        spec = spec or glued
    body = json.dumps([{"outcomeId": str(slip["outcomeInfo"]["id"]),
                        "eventId": slip["eventId"],
                        "marketId": mid, "specifier": spec}])
    try:
        res = api_fetch(page, "/api/ng/factsCenter/Outcomes", "POST",
                        {"content-type": "application/json;charset=UTF-8",
                         "clientid": "web", "platform": "web", "operid": "2"},
                        body)
        data = json.loads(res.get("body") or "{}")
        markets = (data.get("data") or [{}])[0].get("markets") or []
        if markets:
            return int(markets[0]["status"])
    except Exception:
        pass
    return None


def sports_cashout(page, key: bytes, transid: str, order_id: str,
                   b: "ControlBridge", retries: int = 2) -> tuple[bool, str]:
    """Cash out a NAKED accepted bet to recover the stake immediately.

    The UI's ~13s countdown is client-side, so our fires bypass it — on
    no-window events the bet books before the mines hold exists. The undo
    path (decoded 2026-08-27): the bet lands on cashAbleBets within a second
    or two; quote with cashAbleBet, then POST cashOut AES-encrypted like
    orders/order. bizCode 32000 ("wrong amount") = the price moved — re-quote
    and retry. Returns (ok, detail)."""
    bet_id = None
    for _ in range(4):
        try:
            res = api_fetch(page, "/api/ng/realSportsGame/cashAbleBets"
                                  "?pageSize=5&pageNum=1")
            lst = json.loads(res.get("body") or "{}")
            for bet in (lst.get("data") or {}).get("cashAbleBets") or []:
                if bet.get("orderId") == order_id:
                    bet_id = bet.get("id")
                    break
        except Exception:
            pass
        if bet_id:
            break
        page.wait_for_timeout(800)
    if not bet_id:
        return False, f"order {order_id} never appeared on cashAbleBets"
    for attempt in range(retries + 1):
        try:
            res = api_fetch(page, f"/api/ng/realSportsGame/cashAbleBet"
                                  f"?betId={bet_id}&integrity=full")
            det = json.loads(res.get("body") or "{}")
            co = (det.get("data") or {}).get("cashOut") or {}
            amount = str(co.get("maxCashOutAmount"))
            used = str(co.get("availableStake"))
            payload = {"betId": bet_id, "usedStake": used,
                       "isPartial": False, "amount": amount}
            body = aes_encrypt_b64(json.dumps(payload, separators=(",", ":")), key)
            res2 = api_fetch(page, "/api/ng/realSportsGame/cashOut", "POST",
                             {"content-type": "application/json;charset=UTF-8",
                              "clientid": "web", "platform": "web",
                              "operid": "2", "transid": transid}, body)
            resp = json.loads(aes_decrypt_b64(res2["body"], key))
            if resp.get("bizCode") == 10000:
                return True, f"cashed out {amount} (bet {bet_id})"
            if resp.get("bizCode") == 32000 and attempt < retries:
                page.wait_for_timeout(700)
                continue
            return False, f"cashOut rejected: {resp.get('bizCode')} " \
                          f"{resp.get('message')}"
        except Exception as e:
            if attempt >= retries:
                return False, f"{type(e).__name__}: {e}"
    return False, "cashout failed"


def run_cycle(page, mines: HeadlessMines, args, slip: dict,
              key_ref: list, balance: float, live: dict,
              b: ControlBridge, cycle_no: int) -> tuple[str, float, str]:
    """Returns (state, balance, verdict_class). state is "accepted" (booked)
    or "rejected" (loop again); verdict_class feeds the circuit breaker."""
    key, transid = key_ref[0], key_ref[1]
    stake = math.floor((balance - args.redeem_margin) * 100 + 1e-6) / 100.0
    if stake < MIN_SPORTS_STAKE:
        raise SystemExit(
            f"balance NGN {balance:,.2f} too low — stake would be NGN {stake:,.2f} "
            f"(min {MIN_SPORTS_STAKE:g} + margin {args.redeem_margin:g})")
    stake_units = int(round(stake * 10000))

    # Acknowledge pending slip changes (odds moved / suspended) so the stored
    # odds are current, then re-read the selection (falls back to captured).
    accept_slip_changes(page)
    fresh = read_selection(page, slip["eventId"])
    if fresh:
        slip = fresh

    # Pre-fire cooldown — ONLY on a betslip switch (incl. the first fire of a
    # round, where last_fire_sig is unset). Same-selection loop cycles never
    # cool down: they already sit a natural ~14s apart. Instant verdicts
    # (~0.3-1s, the ones that race the mines place and book NAKED) only ever
    # arrive for orders fired within ~5s of the previous verdict; >= ~7s out
    # gets the full 7-13s window (pattern holds across all captures).
    sig = selection_sig(slip)
    if sig != live.get("last_fire_sig"):
        gap = time.time() - live.get("last_verdict_at", 0.0)
        if gap < args.pre_fire_gap:
            wait_s = args.pre_fire_gap - gap
            b.log(f"cooldown: {wait_s:.1f}s before firing (betslip switch)")
            b.publish(state="cooldown")
            page.wait_for_timeout(int(wait_s * 1000))
        live["last_fire_sig"] = sig

    # Never fire into a market the slip itself marks as suspended.
    if market_suspended(page):
        b.publish(suspended=True)
        b.log("market SUSPENDED (badge in the betslip) — not firing")
        return "rejected", balance, "unavailable"
    b.publish(suspended=False)

    # Pre-fire market-status gate: an OPEN market (status 0) verdicts a bet
    # INSTANTLY (~0.3s) — the fire would book NAKED before the mines hold
    # exists. Suspended markets (1/2) park the order until re-open = the
    # window the hold needs. Status comes from the WS tracker when fresh,
    # else REST factsCenter/Outcomes. Wait for a flip; if the market stays
    # open, skip this cycle (the circuit breaker will pause after 3).
    tracker = live.get("ws")
    if tracker is not None:
        tracker.watch(slip)
    waited = 0.0
    announced = False
    while True:
        st = tracker.status_for(slip) if tracker is not None else None
        if st is None:
            st = market_status(page, slip)
        if st is None:
            if announced:
                b.log("market status unreadable — firing as before")
            break  # unknown: proceed on the old assumptions
        if st != 0:
            if announced:
                b.log(f"market suspended after {waited:.0f}s — firing")
            break
        b._drain()  # capture STOP/rearm clicks during the wait
        if b.stop_requested:
            raise StopRequested()
        if not announced:
            announced = True
            b.publish(state="cooldown")
            b.log("market OPEN (status 0) — a fire now books INSTANTLY; "
                  "waiting for it to suspend…")
        if waited >= args.market_wait:
            b.log(f"market stayed open for {waited:.0f}s — skipping this cycle")
            return "rejected", balance, "unavailable"
        page.wait_for_timeout(1000)
        waited += 1.0

    # BET FIRST, MINES SECOND — the server's FIRE-TIME balance check requires
    # balance >= stake when the order enters processing, so the bet must fire
    # at (near) full balance; the mines hold then locks inside the processing
    # window and starves it at settle time. (Hold-first was tried and every
    # cycle died at the fire-time check in 0.3s — proven live.)
    payload = build_payload(slip, stake_units)
    body = aes_encrypt_b64(json.dumps(payload, separators=(",", ":")), key)
    sel_id = payload["ticket"]["selections"][0]["id"]
    b.log(f"\nFIRING NGN {stake:,.2f} on: {selection_label(slip)}  [{sel_id}]")
    t_fire = time.time()
    b.publish(state="arming", stake=stake, fired_at=t_fire)
    fire_bet(page, body, transid)
    live["bet_in_flight"] = True

    def _resync(tag: str) -> float:
        """Authoritative post-cycle balance: wait for the wallet to settle."""
        bal = settle_balance(page)
        b.log(f"{tag} — balance NGN {bal:,.2f}" if bal is not None
              else f"{tag} — balance read failed, keeping NGN {balance:,.2f}")
        if bal is not None:
            b.publish(balance=bal)
        return bal if bal is not None else balance

    round_id = None
    try:
        round_id, seed = mines.create_round(args.mines, args.desk_size)
        cell = args.cell if args.cell is not None else random.randint(0, args.desk_size - 1)
        placed = mines.place(round_id, cell, seed, args.mines_stake)
    except Exception as e:
        # The hold failed AFTER the bet fired (genuine mines outage / JWT —
        # the warm-up consumes the instant-verdict race, so this is rare).
        # Judge the cycle by the verdict alone.
        verdict = wait_verdict(page, key)
        live["bet_in_flight"] = False
        live["last_verdict_at"] = time.time()
        b.publish(fired_at=None)
        v = classify(verdict)
        b.record({"cycle": cycle_no, "vclass": v,
                  "secs": round(time.time() - t_fire, 1)})
        b.log(f"hold failed post-fire ({type(e).__name__}: {e}); "
              f"verdict after {time.time() - t_fire:.1f}s: {v}")
        if v == "accepted":
            d = verdict.get("data") or {}
            live["last_order"] = d.get("orderId")
            b.log(f"⚠ NAKED ACCEPT — bet booked WITHOUT the mines hold: "
                  f"order {d.get('orderId')}, stake {d.get('totalStake')}, "
                  f"potential {d.get('potentialWinnings')}")
            ok, info = sports_cashout(page, key, transid, str(d.get("orderId")), b)
            b.log(f"auto-cashout: {info}" if ok
                  else f"auto-cashout FAILED: {info} — review it in the site's Cashout tab")
            b.publish(state="naked", booked={"order": d.get("orderId"),
                                             "stake": d.get("totalStake"),
                                             "potential": d.get("potentialWinnings"),
                                             "cashed": info if ok else None})
            return "accepted", balance, v
        if isinstance(e, MinesAuthExpired):
            raise  # outer loop re-launches the mines session
        return "rejected", _resync("hold failed post-fire"), v

    live["round_id"] = round_id

    if placed.get("status") == 0:
        b.publish(state="holding", cell=cell, holding=False)
        b.log(f"MINE HIT at cell {cell} — no cashout; waiting for the starved verdict")
        verdict = wait_verdict(page, key)
        live["bet_in_flight"] = False
        live["round_id"] = None
        live["last_verdict_at"] = time.time()
        b.publish(fired_at=None)
        v = classify(verdict)
        b.record({"cycle": cycle_no, "vclass": v,
                  "secs": round(time.time() - t_fire, 1)})
        b.log(f"bet: {v} ({verdict.get('message')}) "
              f"after {time.time() - t_fire:.1f}s — mine cost NGN {args.mines_stake:g}")
        return "rejected", _resync("mine hit"), v

    live["armed"] = True
    b.clear_actions()  # pre-hold clicks are stale — only fresh redeems count
    b.publish(state="holding", cell=cell, holding=True)
    b.log(f"holding NGN {args.mines_stake:g} (cell {cell} safe) — "
          f"ENTER cashes out & settles the bet; otherwise it starves")

    while True:
        if not b.stop_requested and b.redeem_ready():
            co = mines.cashout(round_id)
            live["armed"] = False
            live["round_id"] = None
            b.publish(state="settling", holding=False)
            b.log(f"REDEEMED -> payout {co.get('payout')} "
                  f"(coeff {co.get('coefficient')}) — waiting for the verdict")
            verdict = wait_verdict(page, key)
            live["bet_in_flight"] = False
            live["last_verdict_at"] = time.time()
            b.publish(fired_at=None)
            v = classify(verdict)
            b.record({"cycle": cycle_no, "vclass": v,
                      "secs": round(time.time() - t_fire, 1)})
            b.log(f"verdict after {time.time() - t_fire:.1f}s: {v}")
            if v == "accepted":
                d = verdict.get("data") or {}
                live["last_order"] = d.get("orderId")
                b.log(f"BET ACCEPTED — order {d.get('orderId')}, "
                      f"stake {d.get('totalStake')}, "
                      f"potential winnings {d.get('potentialWinnings')}")
                b.publish(state="booked", booked={"order": d.get("orderId"),
                                                  "stake": d.get("totalStake"),
                                                  "potential": d.get("potentialWinnings")})
                return "accepted", balance, v
            b.log(f"redeemed but bet came back {v} ({verdict.get('message')})")
            return "rejected", _resync("redeem path failed"), v

        res = poll_bet(page)
        if res is None:
            page.wait_for_timeout(int(args.poll_interval * 1000))
            continue

        verdict = decode_verdict(res, key)
        live["bet_in_flight"] = False
        live["last_verdict_at"] = time.time()
        b.publish(fired_at=None)
        v = classify(verdict)
        b.record({"cycle": cycle_no, "vclass": v,
                  "secs": round(time.time() - t_fire, 1)})
        b.log(f"verdict after {time.time() - t_fire:.1f}s: {v}")

        # Every non-accepted verdict recovers the hold before looping.
        co = mines.cashout(round_id)
        live["armed"] = False
        live["round_id"] = None
        b.publish(holding=False)

        if v == "starved":
            b.log(f"bet starved (4200) — recovered {co.get('payout')}; "
                  f"waiting for the wallet to settle")
            return "rejected", _resync("starved"), v

        if v == "accepted":
            d = verdict.get("data") or {}
            live["last_order"] = d.get("orderId")
            b.log(f"⚠ NAKED ACCEPT — no server window on this event: order "
                  f"{d.get('orderId')}, stake {d.get('totalStake')}, "
                  f"potential {d.get('potentialWinnings')}")
            ok, info = sports_cashout(page, key, transid, str(d.get("orderId")), b)
            b.log(f"auto-cashout: {info}" if ok
                  else f"auto-cashout FAILED: {info} — review it in the site's Cashout tab")
            b.publish(state="naked", booked={"order": d.get("orderId"),
                                             "stake": d.get("totalStake"),
                                             "potential": d.get("potentialWinnings"),
                                             "cashed": info if ok else None})
            return "accepted", balance, v

        if v == "already_processing":
            b.log("previous order still processing (19004) — recovered the "
                  f"hold (payout {co.get('payout')}), pacing before retry")
            return "rejected", _resync("19004"), v

        if v == "odds_moved":
            b.log(f"odds moved (4510) — recovered {co.get('payout')}, retrying")
            return "rejected", _resync("odds moved"), v

        if v == "unavailable":
            b.log(f"market unavailable (19999) — recovered {co.get('payout')}")
            return "rejected", _resync("unavailable"), v

        if v == "key_rejected" or (v == "failed" and verdict.get("bizCode") == -2):
            # bizCode -2 = WE couldn't decrypt the response: the SPA re-minted
            # the cipher key (1h TTL) so the answer came back under the NEW
            # key (or as plain JSON) while we still hold the stale one.
            new = find_key(page)
            if not new:
                raise SystemExit(
                    f"cipher key unusable ({v}) and no fresh key in storage — "
                    "place a minimal bet manually, then re-run")
            key_ref[:] = list(new)
            b.log(f"cipher key unusable ({v}) — refreshed from storage, retrying")
            return "rejected", _resync("key refreshed"), "key_rejected"

        if v == "stake_limit":
            raise SystemExit("4801 stake over the market limit — pick another market")
        b.log(f"bet failed: {verdict}")
        return "rejected", _resync("failed"), v


def insta_bet(page, args, slip: dict, key_ref: list, balance: float,
              live: dict, b: ControlBridge) -> bool:
    """One plain order at FULL balance — no warm-up, no mines, no loop. The
    deliberate move for an OPEN market: the verdict lands in ~0.3s and the
    bet simply books. Returns True when accepted."""
    key, transid = key_ref[0], key_ref[1]
    balance = read_balance(page) or balance  # the wallet may have moved since arming
    stake = math.floor(balance * 100 + 1e-6) / 100.0
    if stake < MIN_SPORTS_STAKE:
        b.log(f"insta: balance NGN {balance:,.2f} below the "
              f"{MIN_SPORTS_STAKE:g} min stake")
        return False
    accept_slip_changes(page)
    fresh = read_selection(page, slip["eventId"])
    if fresh:
        slip = fresh
    payload = build_payload(slip, int(round(stake * 10000)))
    body = aes_encrypt_b64(json.dumps(payload, separators=(",", ":")), key)
    b.log(f"INSTA BET: firing full balance NGN {stake:,.2f} on "
          f"{selection_label(slip)}")
    b.publish(state="insta", stake=stake, fired_at=time.time())
    fire_bet(page, body, transid)
    live["bet_in_flight"] = True
    verdict = wait_verdict(page, key)
    live["bet_in_flight"] = False
    live["last_verdict_at"] = time.time()
    b.publish(fired_at=None)
    v = classify(verdict)
    b.log(f"insta verdict: {v} ({verdict.get('message')})")
    bal = settle_balance(page)
    if bal is not None:
        b.publish(balance=bal)
    if v == "accepted":
        d = verdict.get("data") or {}
        live["last_order"] = d.get("orderId")
        b.log(f"INSTA BET ACCEPTED — order {d.get('orderId')}, stake "
              f"{d.get('totalStake')}, potential {d.get('potentialWinnings')}")
        b.publish(state="booked",
                  booked={"order": d.get("orderId"),
                          "stake": d.get("totalStake"),
                          "potential": d.get("potentialWinnings"),
                          "insta": True})
        return True
    # NOTE: no state republish on failure — the caller loops back to ARM,
    # which republishes within ~1s. Flashing "ready" here would expose a
    # gate-less INSTA button whose clicks would queue up and fire at the
    # NEXT ready gate (unintended full-balance bet).
    return False


def booked_gate(page, key_ref: list, live: dict, b: ControlBridge) -> None:
    """The booked/naked screen's gate: RE-ARM moves on (Enter in the terminal).
    (Naked accepts still auto-cash-out in the accept paths — only the manual
    button was removed.)"""
    b.gate("RE-ARM the next target (Ctrl+C to quit)", ("rearm",))


def warmup_bet(page, args, slip: dict, key_ref: list, balance: float,
               live: dict, b: "ControlBridge") -> float:
    """Fire one MINIMAL bet on the armed selection to consume the session's
    instant-verdict first order (~0.5s accept). Every later order gets the
    7-13s processing window the mines hold needs — without this, the first
    loop bet can settle before the mines place lands and book NAKED (proven
    live). Returns the settled balance afterwards."""
    key, transid = key_ref[0], key_ref[1]
    stake = max(MIN_SPORTS_STAKE, float(args.warmup_stake))
    if balance - stake < MIN_SPORTS_STAKE:
        b.log(f"warm-up skipped — balance NGN {balance:,.2f} too low for a "
              f"{stake:g} warm-up")
        return balance
    accept_slip_changes(page)
    fresh = read_selection(page, slip["eventId"])
    if fresh:
        slip = fresh
    payload = build_payload(slip, int(round(stake * 10000)))
    body = aes_encrypt_b64(json.dumps(payload, separators=(",", ":")), key)
    b.log(f"warm-up: firing minimal NGN {stake:,.2f} on "
          f"{selection_label(slip)} to consume the instant verdict")
    b.publish(state="warmup", stake=stake, fired_at=time.time())
    t0 = time.time()
    fire_bet(page, body, transid)
    live["bet_in_flight"] = True
    verdict = wait_verdict(page, key)
    live["bet_in_flight"] = False
    live["last_verdict_at"] = time.time()
    b.publish(fired_at=None)
    v = classify(verdict)
    d = verdict.get("data") or {}
    if v == "accepted":
        b.log(f"warm-up booked ({d.get('orderId')}, stake {d.get('totalStake')}) "
              f"after {time.time() - t0:.1f}s — loop bets now get the full window")
    else:
        b.log(f"warm-up verdict: {v} ({verdict.get('message')}) — continuing")
    bal = settle_balance(page)
    if bal is not None:
        b.publish(balance=bal)
        return bal
    return balance


def probe_window(page, args, slip: dict, key_ref: list, balance: float,
                 live: dict, b: "ControlBridge") -> tuple[float, float, str]:
    """Fire one minimal bet on the armed selection and TIME the verdict.

    The first order on a selection is ALWAYS instant (the warm-up consumed
    it), so this second minimal bet is the tell for whether the EVENT itself
    carries the anti-courtsiding window: an instant verdict means no window —
    a full-balance fire would book NAKED before the mines hold lands (proven
    live 2026-08-27: obscure match = instant, Austria Wien v Braga = 9.4s).
    Returns (balance, verdict_seconds, verdict_class)."""
    key, transid = key_ref[0], key_ref[1]
    stake = max(MIN_SPORTS_STAKE, float(args.warmup_stake))
    accept_slip_changes(page)
    fresh = read_selection(page, slip["eventId"])
    if fresh:
        slip = fresh
    payload = build_payload(slip, int(round(stake * 10000)))
    body = aes_encrypt_b64(json.dumps(payload, separators=(",", ":")), key)
    b.log(f"pre-flight: probing the window with minimal NGN {stake:,.2f} on "
          f"{selection_label(slip)}")
    b.publish(state="warmup", stake=stake, fired_at=time.time())
    t0 = time.time()
    fire_bet(page, body, transid)
    live["bet_in_flight"] = True
    verdict = wait_verdict(page, key)
    secs = time.time() - t0
    live["bet_in_flight"] = False
    live["last_verdict_at"] = time.time()
    b.publish(fired_at=None)
    v = classify(verdict)
    d = verdict.get("data") or {}
    b.log(f"pre-flight verdict after {secs:.1f}s: {v} ({verdict.get('message')})"
          + (f" — order {d.get('orderId')}" if v == "accepted" else ""))
    bal = settle_balance(page)
    if bal is not None:
        b.publish(balance=bal)
        balance = bal
    return balance, secs, v


# ---------------------------------------------------------------------------
# Top level.
# ---------------------------------------------------------------------------
def run(args, bridge: "ControlBridge | None" = None) -> int:
    b = bridge or ControlBridge()
    if args.mines_stake < 20:
        raise SystemExit("--mines-stake must be >= 20 (game minBet)")

    b.publish(state="setup", stop_requested=False)
    # NOTE: SportyBetMines.__enter__ already calls connect() — calling it again
    # would start a second sync_playwright while the first one's loop is
    # pumping, tripping "Sync API inside the asyncio loop".
    with SportyBetMines(args.adspower) as mines_drv:
        page = mines_drv._sporty_page

        # Live market status off the SPA's own odds socket (decides
        # instant-vs-held); REST market_status() is the fallback.
        tracker = WsMarketTracker(b)
        tracker.attach(page)

        # LOGIN FIRST: the Mines launch URL needs an authenticated session
        # (AWS WAF), and arming reads the live slip — both need the operator
        # logged in. Terminal mode: this is just an Enter gate.
        b.gate("Log in to SportyBet in the AdsPower browser window this app "
               "just opened (NOT your normal Chrome), then press CONTINUE",
               ("continue",), state="login")

        # Grab the Mines JWT, then CLOSE the game tab: rounds run headless
        # (sportybet-mines-flow.md §9), so the tab is pure background noise.
        def grab_headless() -> HeadlessMines:
            prof = mines_drv.launch()
            tab = getattr(prof, "game_tab", None)
            if tab is not None:
                try:
                    tab.close()
                except Exception:
                    pass
                prof.game_tab = None
            return HeadlessMines(prof.jwt, prof.id)

        mines = grab_headless()
        b.publish(mines_ok=True)
        b.log("mines session ready — JWT captured, game tab closed "
              "(rounds run headless)")

        live = {"armed": False, "round_id": None, "bet_in_flight": False,
                "last_verdict_at": 0.0, "last_fire_sig": None, "t_fire": None,
                "last_order": None, "ws": tracker}
        key_ref: list | None = None
        round_no = 0
        try:
            while True:  # session rounds: ARM -> hold loop -> booked -> re-arm
                round_no += 1
                b.publish(round_no=round_no, booked=None)
                if round_no > 1:
                    # Re-arm: clear last round's slip so a leftover selection
                    # can't silently re-arm as the new target.
                    try:
                        n = clear_betslip(page)
                        if n:
                            b.log(f"cleared {n} leftover selection(s) from "
                                  "the betslip")
                    except Exception as e:  # noqa: BLE001 — never block arming
                        b.log(f"betslip clear failed ({type(e).__name__}: {e}) "
                              "— continuing")

                # -- ARM ---------------------------------------------------
                # Smart key handling: a still-valid key needs no minimal bet.
                key_pair = find_key(page)
                if key_pair:
                    mins = max(0.0, (key_pair[2] - time.time()) / 60)
                    b.log(f"\ncipher key valid (transid {key_pair[1]}, expires in "
                          f"{mins:.0f} min) — no minimal bet needed")
                else:
                    b.log("\nno valid cipher key — place a minimal (10 NGN) bet "
                          "in the UI to mint one")
                b.publish(state="arm", need_bet=key_pair is None, target=None,
                          market_status=None, market_status_at=None)
                tracker.watch(None)
                b.clear_actions()  # stale clicks (e.g. INSTA) must not leak into the next gate
                b.log("listening for a betslip selection IN THE ADSPOWER "
                      "WINDOW (Ctrl+C to quit)")

                # Live slip tracking: follow the CURRENT slip content (the
                # operator may add/remove/replace selections while listening).
                # A single selection auto-arms after ~2s stable; with several,
                # the operator picks one explicitly (picker cards in the UI,
                # numbered prompt in the terminal) — the array can hold stale
                # entries, so slips[0] is never trusted blindly.
                slip = None
                last_list_sig = None
                stable_since = None
                pick_published = False
                deadline = time.time() + args.arm_timeout
                while time.time() < deadline:
                    if b.stop_requested:
                        raise StopRequested()
                    if key_pair is None:
                        key_pair = find_key(page)
                        if key_pair:
                            b.publish(need_bet=False,
                                      key_expires_at=key_pair[2])
                    accept_slip_changes(page)
                    b.publish(suspended=market_suspended(page))
                    choices = read_all_selections(page)
                    if args.event_id:
                        filtered = [c for c in choices
                                    if c.get("eventId") == args.event_id]
                        if filtered:
                            choices = filtered
                    list_sig = tuple(selection_sig(c) for c in choices)
                    if list_sig != last_list_sig:
                        last_list_sig = list_sig
                        stable_since = time.time() if list_sig else None
                        pick_published = False
                        if len(choices) == 1:
                            b.publish(target=describe_selection(choices[0]),
                                      choices=None)
                            b.log(f"target: {selection_label(choices[0])}")
                        elif len(choices) > 1:
                            b.publish(target=None,
                                      choices=[describe_selection(c)
                                               for c in choices])
                            b.log(f"{len(choices)} selections on the slip — "
                                  "pick one")
                        else:
                            b.publish(target=None, choices=None)
                            b.log("selection removed — listening...")
                    if not (key_pair and choices and stable_since is not None
                            and time.time() - stable_since >= 2.0):
                        # Wait ~1s between slip reads, but keep draining the
                        # action queue so STOP / CHANGE SELECTION respond
                        # while listening (wait_action raises StopRequested).
                        got = b.wait_action(timeout=1.0)
                        if got and got[0] not in ("enter",):
                            b.log(f"(ignored while listening: {got[0]})")
                        continue
                    if len(choices) == 1:
                        slip = choices[0]
                        break
                    # multi-selection: explicit pick required
                    if args.betslip_index is not None:
                        if 0 <= args.betslip_index < len(choices):
                            slip = choices[args.betslip_index]
                            break
                        raise SystemExit(
                            f"--betslip-index {args.betslip_index} out of range "
                            f"({len(choices)} selections)")
                    if b.actions is None:
                        if not pick_published:
                            for i, c in enumerate(choices):
                                b.log(f"  [{i+1}] {selection_label(c)}")
                            pick_published = True
                        slip = choices[terminal_pick(len(choices), b)]
                        break
                    got = b.wait_action(timeout=1.0)
                    if got and got[0] == "pick":
                        payload = got[1] or {}
                        psig = (payload.get("eventId"),
                                str(payload.get("marketId")),
                                str(payload.get("outcomeId")))
                        match = next(
                            (c for c in choices
                             if tuple(map(str, selection_sig(c))) == psig), None)
                        if match is not None:
                            slip = match
                            break
                        b.log("stale pick (slip changed) — pick again")
                    elif got and got[0] not in ("enter",):
                        b.log(f"(ignored while listening: {got[0]})")
                if key_pair is None:
                    raise SystemExit("no cipher key appeared — place a minimal bet first")
                if slip is None:
                    raise SystemExit(
                        f"no slip selection captured"
                        f"{f' for {args.event_id}' if args.event_id else ''}")
                key_ref = list(key_pair)
                b.publish(key_expires_at=key_ref[2], choices=None,
                          target=describe_selection(slip))
                b.log(f"armed on: {selection_label(slip)}")
                tracker.watch(slip)
                ms = tracker.status_for(slip)
                if ms is None:
                    ms = market_status(page, slip)
                b.publish(market_status=ms, market_status_at=time.time())
                if ms is not None:
                    b.log(f"market status: {ms} "
                          f"({'OPEN — bets verdict instantly' if ms == 0 else 'suspended — the hold window'})")

                # Balance: round 1 honours --start-balance; later rounds always
                # re-read the settled wallet (the booked bet changed it).
                if round_no == 1 and args.start_balance is not None:
                    balance = args.start_balance
                else:
                    balance = settle_balance(page) or read_balance(page)
                    if not balance or balance <= 0:
                        raise SystemExit("could not read the wallet balance — "
                                         "pass --start-balance")
                b.publish(balance=balance,
                          stake_preview=max(0.0, balance - args.redeem_margin))
                b.log(f"balance: NGN {balance:,.2f}  (stake per cycle: balance - "
                      f"{args.redeem_margin:g} margin)")

                if not args.yes:
                    got = b.gate("Press Enter to START the hold loop, "
                                 "INSTA BET for an immediate full-balance bet "
                                 "(or RE-ARM another selection)",
                                 ("begin", "insta", "rearm"), state="ready")
                    if got[0] == "rearm":
                        b.log("re-arming — pick another selection")
                        continue
                    if got[0] == "insta":
                        if insta_bet(page, args, slip, key_ref, balance,
                                     live, b):
                            booked_gate(page, key_ref, live, b)
                        continue

                # -- WARM-UP: fire a minimal bet on EVERY selection currently
                # on the slip (not just the armed one) — instant verdicts can
                # be per-selection, so any entry the operator might arm later
                # must have its instant order consumed too. Each later loop
                # bet then gets the 7-13s window (an instant verdict would
                # otherwise race the mines place and can book a naked
                # full-balance bet — proven live).
                if args.warmup_stake and args.warmup_stake > 0:
                    warm_slips = read_all_selections(page) or [slip]
                    if args.event_id:
                        filtered = [s for s in warm_slips
                                    if s.get("eventId") == args.event_id]
                        if filtered:
                            warm_slips = filtered
                    seen = set()
                    for ws in warm_slips:
                        sig = selection_sig(ws)
                        if sig in seen:
                            continue
                        seen.add(sig)
                        balance = warmup_bet(page, args, ws, key_ref,
                                             balance, live, b)
                    b.publish(balance=balance,
                              stake_preview=max(0.0, balance - args.redeem_margin))

                # -- PRE-FLIGHT: prove the EVENT carries the in-play window.
                # The warm-up consumed the always-instant first order, so a
                # second minimal bet is the tell: instant verdict = no window
                # = the full-balance fire would book NAKED. Pause for the
                # operator instead of bleeding into a naked accept.
                no_window = False
                auto = 0
                while args.precheck and args.warmup_stake and args.warmup_stake > 0:
                    balance, p_secs, p_v = probe_window(
                        page, args, slip, key_ref, balance, live, b)
                    if p_secs >= args.precheck_secs:
                        b.log(f"pre-flight OK — window confirmed "
                              f"({p_secs:.1f}s, {p_v})")
                        break
                    if p_v == "already_processing" and auto < 3:
                        # The warm-up's order still holds the server lock —
                        # this says nothing about the window. Re-probe.
                        auto += 1
                        b.log("pre-flight hit the order lock (19004) — "
                              "waiting 5s and re-probing")
                        page.wait_for_timeout(5000)
                        continue
                    if p_v == "accepted":
                        pclass = "no-window"
                        why = ("this event books bets INSTANTLY — no in-play "
                               "window, the loop would go NAKED")
                    else:
                        pclass = p_v
                        why = f"pre-flight probe failed: {p_v}"
                    b.publish(pause={"vclass": pclass, "count": 1})
                    got = b.gate(
                        f"PRE-FLIGHT: {why} (verdict in {p_secs:.1f}s). "
                        "RETRY the probe or CHANGE SELECTION (Ctrl+C to quit)",
                        ("retry", "rearm"), state="paused")
                    b.publish(pause=None)
                    auto = 0
                    if got[0] == "rearm":
                        b.log("re-arming — pick another selection")
                        no_window = True
                        break
                if no_window:
                    continue

                # -- HOLD LOOP ----------------------------------------------
                ttl_warned = False
                cycles = 0
                booked = False
                rearm_now = False
                fail_sig = None
                fail_count = 0
                while True:
                    if args.max_cycles and cycles >= args.max_cycles:
                        b.log(f"reached --max-cycles {args.max_cycles} without "
                              "booking")
                        return 1
                    if balance < args.min_stake:
                        b.log(f"balance NGN {balance:,.2f} below --min-stake "
                              f"{args.min_stake:g} — stopping")
                        return 1
                    if not ttl_warned and key_ref[2] - time.time() < 600:
                        b.log("WARNING: cipher key expires in <10 min — place a "
                              "minimal bet soon to refresh it")
                        ttl_warned = True
                    cycles += 1
                    b.publish(cycle=cycles)
                    try:
                        state, balance, vclass = run_cycle(
                            page, mines, args, slip, key_ref, balance, live, b,
                            cycles)
                    except MinesAuthExpired:
                        b.log("mines JWT expired — re-launching")
                        mines = grab_headless()
                        continue
                    if state == "accepted":
                        booked = True
                        b._take_rearm()  # a booked bet wins over a pending rearm
                        break
                    if b.stop_requested:
                        raise StopRequested()
                    if b._take_rearm():
                        b.log("back to the betslip — pick a new selection")
                        rearm_now = True
                        break
                    # Circuit breaker: starved is the designed outcome (healthy
                    # cycle) — anything else repeating N times means a dead
                    # market or a stuck server; pause instead of bleeding fees.
                    if vclass == "starved":
                        fail_sig, fail_count = None, 0
                    else:
                        fail_count = fail_count + 1 if vclass == fail_sig else 1
                        fail_sig = vclass
                        if args.fail_pause and fail_count >= args.fail_pause:
                            b.publish(pause={"vclass": vclass, "count": fail_count})
                            got = b.gate(
                                f"{fail_count} consecutive '{vclass}' failures — "
                                "the market may be dead/suspended. RETRY or "
                                "CHANGE SELECTION (Ctrl+C to quit)",
                                ("retry", "rearm"), state="paused")
                            b.publish(pause=None)
                            fail_sig, fail_count = None, 0
                            if got[0] == "rearm":
                                b.log("re-arming — pick another selection")
                                rearm_now = True
                                break
                    if args.cycle_delay > 0:
                        page.wait_for_timeout(int(args.cycle_delay * 1000))

                if rearm_now:
                    continue
                b.log("flow complete — bet booked.")
                booked_gate(page, key_ref, live, b)
        except (KeyboardInterrupt, StopRequested):
            b.log("\nstopping — finishing cleanly...")
            if live["bet_in_flight"] and key_ref:
                try:
                    verdict = wait_verdict(page, key_ref[0], timeout=15)
                    b.log(f"in-flight bet verdict: {classify(verdict)} "
                          f"({verdict.get('message')})")
                except SystemExit:
                    pass
            if live["armed"] and live["round_id"]:
                try:
                    co = mines.cashout(live["round_id"])
                    b.log(f"mines hold cashed out -> payout {co.get('payout')}")
                except Exception as e:
                    b.log(f"mines cashout failed (round may have settled): {e}")
            final = read_balance(page)
            if final is not None:
                b.publish(balance=final)
                b.log(f"final balance: NGN {final:,.2f} — ready for a new run")
            return 1


def build_parser() -> argparse.ArgumentParser:
    """The CLI parser (also used by sportybet_hold_ui.py to build default args)."""
    p = argparse.ArgumentParser(
        description="SportyBet balance-starving hold: full-balance bet + Mines "
                    "hold. ENTER redeems (bet settles), otherwise the bet starves "
                    "and the loop repeats. Ctrl+C stops cleanly.")
    p.add_argument("--adspower", default=None, help="AdsPower profile id/name")
    p.add_argument("--event-id", default=None,
                   help="Only arm on this event id (default: first slip selection)")
    p.add_argument("--start-balance", type=float, default=None,
                   help="In-memory balance in NGN for round 1 (default: live "
                        "wallet balance; later rounds always re-read the wallet)")
    p.add_argument("--redeem-margin", type=float, default=5.0,
                   help="NGN shaved off the stake so the redeem path covers the "
                        "mines cashout fee (default 5)")
    p.add_argument("--mines-stake", type=float, default=100.0,
                   help="Mines stake per cycle in NGN (default 100, game min 20)")
    p.add_argument("--mines", type=int, default=1, help="Mine count (default 1)")
    p.add_argument("--desk-size", type=int, default=25,
                   help="Grid cells: 25 / 49 / 81 (default 25)")
    p.add_argument("--cell", type=int, default=None,
                   help="Grid index to open (default: random)")
    p.add_argument("--min-stake", type=float, default=20.0,
                   help="Stop when the balance drops below this (default 20)")
    p.add_argument("--max-cycles", type=int, default=0,
                   help="Stop after N failed cycles (0 = loop forever)")
    p.add_argument("--fail-pause", type=int, default=3,
                   help="Pause for Enter after N consecutive non-starved "
                        "failures of the same kind (default 3, 0 = never pause)")
    p.add_argument("--poll-interval", type=float, default=0.3,
                   help="Bet-verdict poll interval in seconds (default 0.3)")
    p.add_argument("--cycle-delay", type=float, default=2.0,
                   help="Extra pause between cycles in seconds (default 2) — on "
                        "top of the wallet settle wait, to stay clear of the "
                        "server's order processing lock (19004)")
    p.add_argument("--arm-timeout", type=float, default=600.0,
                   help="Seconds to wait for key + slip while arming (default 600)")
    p.add_argument("--warmup-stake", type=float, default=10.0,
                   help="Minimal bet (NGN) fired once per round to consume the "
                        "instant first-order verdict (default 10, 0 = skip)")
    p.add_argument("--precheck", type=int, default=0,
                   help="DISABLED by default: the window probe is unsound — "
                        "minimal bets skip the in-play window even on windowed "
                        "events (false NO WINDOW), and order-lock queue waits "
                        "mimic a window (false OK). 1 = enable anyway")
    p.add_argument("--precheck-secs", type=float, default=4.0,
                   help="Window-probe threshold in seconds (default 4) — "
                        "instant verdicts are ~0.3-1s, windowed ones 6-13s")
    p.add_argument("--pre-fire-gap", type=float, default=8.0,
                   help="Seconds between the last verdict and firing after a "
                        "betslip switch (default 8) — orders fired <~5s after a "
                        "verdict get INSTANT verdicts that race the mines hold. "
                        "Same-selection loop cycles never cool down.")
    p.add_argument("--market-wait", type=float, default=30.0,
                   help="Seconds to wait for an OPEN market (status 0 — bets "
                        "there verdict instantly, no window) to suspend before "
                        "skipping the cycle (default 30)")
    p.add_argument("--betslip-index", type=int, default=None,
                   help="Pre-select the Nth slip selection when the slip has "
                        "several (0-based; skips the picker)")
    p.add_argument("--yes", action="store_true",
                   help="Skip the 'Press Enter to START' confirmation")
    return p


def default_args(**overrides) -> argparse.Namespace:
    """Parser defaults as a Namespace, with keyword overrides — how
    sportybet_hold_ui.py builds session args from the settings form."""
    args = build_parser().parse_args([])
    for k, v in overrides.items():
        setattr(args, k, v)
    return args


def main() -> None:
    args = build_parser().parse_args()

    if not args.adspower:
        f = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                         ".adspower_profile")
        if os.path.exists(f):
            with open(f) as _f:
                cfg = json.load(_f)
            args.adspower = cfg.get("user_id") or cfg.get("name")
    if not args.adspower:
        raise SystemExit("pass --adspower (or set .adspower_profile)")

    try:
        sys.exit(run(args))
    except AdsPowerError as exc:
        raise SystemExit(f"AdsPower: {exc}") from exc
    except KeyboardInterrupt:
        print("\nStopped.", flush=True)


if __name__ == "__main__":
    main()
