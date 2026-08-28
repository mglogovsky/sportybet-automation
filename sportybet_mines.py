"""SportyBet (NG) Hub88 Turbo Mines — launch + round transport.

The game is Hub88 "Turbo Mines" (`tbg_turbomines`), hosted at
`turbomines.turbogfast.xyz`. Unlike the Betnacional/Spribe Mines (WebSocket +
iframe canvas), this is plain HTTPS POSTs — no WS, no DOM canvas clicking.

All calls run as **in-page fetches** on the AdsPower browser (real Chrome
fingerprint/session), so Cloudflare doesn't 403. The round API is plaintext JSON
auth'd by a per-game JWT.

Flow captured live (see `sportybet-mines-flow.md`):
  launch:  hub88/v1/game-launch-url  ->  launcher (fh8labs)  ->  turbogfast.xyz/?token=...
  profile: POST /api/common/profile {token,cid,gameId,visitorId,subPartnerId}
           -> { token:<JWT>, id:"cg_...", externalToken, balance }
  round:   POST /api/games/create  {clientSeed,nonce,size,deskSize:25,theme}
           -> { roundId }
  place:   POST /api/bets/place    first call carries {amount,currency}; later {roundId,index}
           -> { status:0 => mine/lost,  status:1 => safe/armed }
  cashout: POST /api/bets/cashout  {gameId}  -> { payout = amount * coefficient }

Grid is 5x5, `index = row*5+col`, `deskSize=25`. Money is NGN whole units (`amount`).
"""

from __future__ import annotations

import json
import time
import uuid
from dataclasses import dataclass, field
from urllib.parse import parse_qs, urlparse

from playwright.sync_api import Playwright, sync_playwright

import os
import sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from adspower import AdsPowerClient, AdsPowerError

GAME_CODE = "tbg_turbomines"
LAUNCH_URL_API = (
    "https://www.sportybet.com/api/ng/games/hub88/v1/game-launch-url"
    f"?gameCode={GAME_CODE}"
)
GAME_ORIGIN = "turbomines.turbogfast.xyz"  # fallbacks: rmproxy.site, turboexplorer.online


@dataclass
class MinesProfile:
    """The per-game session after launch (from /api/common/profile)."""
    jwt: str
    id: str               # apikey, e.g. cg_218058097
    external_token: str   # 32-hex, in the game tab URL
    balance: float        # float NGN
    sid: str
    game_tab: object = field(default=None, repr=False)


def _in_page_fetch(page, url, *, method="POST", headers=None, body=None) -> dict:
    """Run fetch() from the page so Cloudflare sees the real browser origin."""
    headers = headers or {}
    js = """
    (async () => {
      const resp = await fetch(URL, {
        method: METHOD,
        headers: HEADERS,
        credentials: 'include',
        body: BODY,
      });
      const text = await resp.text();
      let data; try { data = JSON.parse(text); } catch (e) { data = text; }
      return { status: resp.status, data: data };
    })()
    """
    js = (js
          .replace("URL", json.dumps(url))
          .replace("METHOD", json.dumps(method))
          .replace("HEADERS", json.dumps(headers))
          .replace("BODY", json.dumps(body)))
    return page.evaluate(js)


class SportyBetMines:
    """Launch Mines in an AdsPower profile and drive rounds over its browser."""

    def __init__(self, adspower_ref: str):
        self.adspower_ref = adspower_ref
        self._pw: Playwright | None = None
        self._browser = None
        self._context = None
        self._sporty_page = None
        self._active_seed: str | None = None
        self.profile: MinesProfile | None = None

    # -- connection --------------------------------------------------------
    def connect(self) -> None:
        client = AdsPowerClient()
        profile = client.resolve(self.adspower_ref)
        meta = client.active(profile.user_id) or client.start(profile.user_id)
        ws_url = meta.cdp_url()
        if not ws_url:
            raise AdsPowerError(f"{profile.label()} exposed no debug endpoint")
        self._pw = sync_playwright().start()
        self._browser = self._pw.chromium.connect_over_cdp(ws_url)
        self._context = self._browser.contexts[0]
        # Prefer a stable sportybet page. The live-match tab is fine, but if the
        # launch fetch is WAF-challenged we fall back to a fresh root tab.
        self._sporty_page = next(
            (p for p in self._context.pages
             if not p.is_closed() and "sportybet.com" in (p.url or "")
             and "games" in (p.url or "").lower()),
            None,
        ) or next(
            (p for p in self._context.pages
             if not p.is_closed() and "sportybet.com" in (p.url or "")),
            None,
        )
        if self._sporty_page is None:
            self._sporty_page = self._context.new_page()
            self._sporty_page.goto("https://www.sportybet.com/ng/games",
                                   wait_until="domcontentloaded", timeout=45000)

    def close(self) -> None:
        try:
            if self._browser:
                self._browser.close()
        except Exception:
            pass
        try:
            if self._pw:
                self._pw.stop()
        except Exception:
            pass
        self._pw = self._browser = self._context = None

    def __enter__(self):
        self.connect()
        return self

    def __exit__(self, *exc_info):
        self.close()
        return False

    # -- launch ------------------------------------------------------------
    def _launch_url(self) -> str:
        # Direct navigation (not an in-page fetch) so AWS WAF lets the JSON
        # through — fetch() from the SPA gets challenge-blocked.
        for _ in range(3):
            try:
                self._sporty_page.goto(LAUNCH_URL_API, wait_until="domcontentloaded",
                                       timeout=30000)
                text = self._sporty_page.evaluate("() => document.body.innerText")
            except Exception as e:
                self._sporty_page.wait_for_timeout(1500)
                continue
            try:
                data = json.loads(text)
            except Exception:
                self._sporty_page.wait_for_timeout(1500)
                continue
            url = (data.get("data") or {}).get("url")
            if not url:
                self._sporty_page.wait_for_timeout(1500)
                continue
            # Return the SPA to a real page for subsequent work.
            self._sporty_page.goto("https://www.sportybet.com/ng/games",
                                   wait_until="domcontentloaded", timeout=45000)
            return url
        raise AdsPowerError("game-launch-url failed after retries (WAF)")

    def _existing_game_tab(self):
        for p in self._context.pages:
            if not p.is_closed() and GAME_ORIGIN in (p.url or ""):
                return p
        return None

    def _open_game_tab(self, launch_url: str):
        existing = self._existing_game_tab()
        if existing is not None:
            return existing
        tab = self._context.new_page()
        tab.goto(launch_url, wait_until="domcontentloaded", timeout=60000)
        # Wait for the redirect chain to land on the game host.
        for _ in range(40):
            if GAME_ORIGIN in (tab.url or ""):
                break
            tab.wait_for_timeout(500)
        if GAME_ORIGIN not in (tab.url or ""):
            raise AdsPowerError(f"Mines launcher did not land on game host (url={tab.url})")
        return tab

    def launch(self) -> MinesProfile:
        """Launch the game and fetch the per-game JWT + balance.

        Reuses an already-open game tab when one exists (no new tab, no launcher
        navigation); otherwise navigates the launcher chain once.
        """
        tab = self._existing_game_tab()
        if tab is None:
            launch_url = self._launch_url()
            tab = self._open_game_tab(launch_url)
        token = (parse_qs(urlparse(tab.url).query).get("token") or [""])[0]
        if not token:
            raise AdsPowerError("game tab URL missing token query param")
        # Let the SPA hydrate enough to accept fetches.
        for _ in range(30):
            try:
                tab.wait_for_timeout(500)
                res = _in_page_fetch(
                    tab,
                    "https://turbomines.turbogfast.xyz/api/common/profile",
                    headers={"Content-Type": "application/json"},
                    body=json.dumps({
                        "token": token,
                        "cid": "hub88tgb",
                        "gameId": "turbomines",
                        "visitorId": str(uuid.uuid4()),
                        "subPartnerId": "SportyBet NG",
                    }),
                )
                data = res.get("data") or {}
                if res.get("status") == 200 and data.get("token"):
                    break
            except Exception:
                continue
        else:
            raise AdsPowerError("could not reach /api/common/profile on the game host")
        data = res.get("data") or {}
        self.profile = MinesProfile(
            jwt=data["token"],
            id=data.get("id", ""),
            external_token=token,
            balance=float(data.get("balance", 0) or 0),
            sid=data.get("sid", ""),
            game_tab=tab,
        )
        return self.profile

    # -- round transport ---------------------------------------------------
    def _api(self, path: str, body: dict) -> dict:
        tab = self.profile.game_tab
        url = f"https://{GAME_ORIGIN}/api/{path}"
        res = _in_page_fetch(
            tab,
            url,
            headers={
                "Content-Type": "application/json",
                "Authorization": self.profile.jwt,
                "apikey": self.profile.id,
                "subpartnerid": "SportyBet NG",
            },
            body=json.dumps(body),
        )
        if res.get("status") != 200:
            raise AdsPowerError(f"{path} HTTP {res.get('status')}: {res.get('data')}")
        return res.get("data") or {}

    def create_round(self, mines: int, *, client_seed: str | None = None) -> dict:
        seed = client_seed or str(uuid.uuid4())
        self._active_seed = seed
        return self._api("games/create", {
            "clientSeed": seed,
            "nonce": 1,
            "size": mines,
            "deskSize": 25,
            "theme": "turbomines",
        })

    def place(self, round_id: str, index: int, *, amount: float | None = None,
              nonce: int = 1) -> dict:
        body = {"theme": "turbomines", "roundId": round_id, "index": index}
        if amount is not None:
            # The stake call must reuse the exact clientSeed/nonce the round was
            # created with (captured: identical seed across create + first place).
            body.update({"clientSeed": getattr(self, "_active_seed", ""),
                         "nonce": nonce,
                         "amount": int(amount), "currency": "ngn"})
        return self._api("bets/place", body)

    def cashout(self, round_id: str) -> dict:
        return self._api("bets/cashout", {"gameId": round_id})


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
def main() -> None:
    import argparse
    import os
    import sys

    p = argparse.ArgumentParser(description="SportyBet Turbo Mines round driver")
    p.add_argument("--adspower", default=None, help="AdsPower profile id/name")
    p.add_argument("--stake", type=float, default=20.0, help="Round stake in NGN")
    p.add_argument("--mines", type=int, default=1, help="Mine count (default 1)")
    p.add_argument("--cell", type=int, default=None,
                   help="Grid index 0-24 to open (default: random)")
    p.add_argument("--cashout", action="store_true",
                   help="After a safe first cell, cash out immediately")
    p.add_argument("--hold", action="store_true",
                   help="After a safe first cell, print balance and wait for Enter to cash out")
    p.add_argument("--no-launch", action="store_true",
                   help="Reuse an already-open game tab (skip launch)")
    args = p.parse_args()

    if not args.adspower:
        f = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".adspower_profile")
        if os.path.exists(f):
            with open(f) as _f:
                cfg = json.load(_f)
            args.adspower = cfg.get("user_id") or cfg.get("name")
    if not args.adspower:
        p.error("pass --adspower")

    with SportyBetMines(args.adspower) as mines:
        prof = mines.launch()
        print(f"launched — balance NGN {prof.balance:.2f}, player {prof.id}", flush=True)

        created = mines.create_round(args.mines)
        rid = created.get("roundId")
        print(f"round {rid} (mines={args.mines})", flush=True)

        import random
        cell = args.cell if args.cell is not None else random.randint(0, 24)
        placed = mines.place(rid, cell, amount=args.stake)
        status = placed.get("status")
        print(f"opened cell {cell} -> status {status} ({placed.get('result')})", flush=True)

        if status == 0:
            print(f"MINE HIT — round lost (mines at {placed.get('mines')})", flush=True)
            sys.exit(0)
        # status == 1 -> safe, round armed
        if args.cashout:
            co = mines.cashout(rid)
            print(f"cashed out -> payout {co.get('payout')} ({co.get('result')})", flush=True)
            sys.exit(0)
        print(f"round armed — coefficient {placed.get('coefficient')}", flush=True)
        if args.hold:
            print("Press Enter to cash out...", flush=True)
            if sys.stdin.isatty():
                input()
            else:
                sys.stdin.readline()
            co = mines.cashout(rid)
            print(f"cashed out -> payout {co.get('payout')} ({co.get('result')})", flush=True)


if __name__ == "__main__":
    main()
