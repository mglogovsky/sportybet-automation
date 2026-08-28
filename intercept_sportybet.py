#!/usr/bin/env python3
"""Capture ALL SportyBet traffic (HTTP + WebSocket) from an AdsPower profile.

Attach to the AdsPower browser over CDP, log every request/response and every
WebSocket frame on the SportyBet tab(s), and WAIT while the operator plays Mines
and places a bet in the real UI. Nothing is placed by this script — it only
observes and logs, so we can reconstruct the exact Mines-launch + bet flow the
site uses.

Captures "even the in-page one": because the hooks are attached at the CONTEXT
level, in-page fetches and websockets from the SPA, the Mines game iframe, and
any sub-frame are all captured — not just top-level navigations.

Every event is appended to the out file immediately so nothing is lost if the
process is killed.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time

import os
import sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from adspower import AdsPowerClient

# Broad match so we catch the SPA, its API gateway, the Mines/Spribe iframe, the
# live-odds socket (alive-*.sportybet.com), analytics, etc. Add hosts here if a
# game host is missed.
MATCH = (
    "sportybet.com",
    "sporty.net",
    "fh8labs.com",
    "hub88",
    "turbomines",
    "turbogfast",
    "rmproxy",
    "turboexplorer",
    "spribegaming.com",
    "synot",
    "softswiss",
    "turbo.spribe",
    "alive-",
)


def main() -> None:
    p = argparse.ArgumentParser(description="Capture SportyBet bet + Mines traffic")
    p.add_argument("--adspower", default=None, help="AdsPower profile id/name")
    p.add_argument("--out", default="sportybet_capture.jsonl", help="Output JSONL file")
    args = p.parse_args()

    ref = args.adspower
    if not ref:
        f = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".adspower_profile")
        if os.path.exists(f):
            with open(f) as _f:
                cfg = json.load(_f)
            ref = cfg.get("user_id") or cfg.get("name")
    if not ref:
        print("pass an AdsPower profile", file=sys.stderr)
        sys.exit(1)

    client = AdsPowerClient()
    profile = client.resolve(ref)
    meta = client.active(profile.user_id)
    if meta is None:
        meta = client.start(profile.user_id)
    ws_url = meta.cdp_url()
    print(f"attaching to {profile.label()} ({ws_url})", file=sys.stderr)

    out_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), args.out)
    outf = open(out_path, "w")

    def emit(evt: dict) -> None:
        evt["ts"] = time.time()
        outf.write(json.dumps(evt, ensure_ascii=False, default=str) + "\n")
        outf.flush()
        print(json.dumps(evt, ensure_ascii=False, default=str), flush=True)

    import playwright.sync_api as P

    with P.sync_playwright() as pw:
        browser = pw.chromium.connect_over_cdp(ws_url)
        context = browser.contexts[0]

        def matches(u: str) -> bool:
            return any(m in u for m in MATCH)

        def on_request(req):
            if not matches(req.url):
                return
            body = None
            try:
                if req.method in ("POST", "PUT", "PATCH"):
                    body = req.post_data
            except Exception:
                pass
            emit({
                "dir": "REQ",
                "method": req.method,
                "url": req.url,
                "headers": dict(req.headers),
                "post": body,
            })

        def on_response(resp):
            if not matches(resp.url):
                return
            ct = resp.headers.get("content-type", "")
            body = None
            if "json" in ct or "text" in ct:
                try:
                    body = resp.text()
                except Exception:
                    try:
                        body = resp.body().decode("utf-8", "replace")[:4000]
                    except Exception:
                        pass
            emit({
                "dir": "RESP",
                "status": resp.status,
                "url": resp.url,
                "headers": dict(resp.headers),
                "ct": ct,
                "body": (body or "")[:4000],
            })

        def on_ws(ws):
            net = ws.url
            if not matches(net):
                return
            emit({"dir": "WS_OPEN", "url": net})

            def _dec(payload):
                if isinstance(payload, (bytes, bytearray)):
                    try:
                        return bytes(payload).decode("latin1")
                    except Exception:
                        return repr(payload)
                return payload

            def _sent(payload):
                emit({"dir": "WS_SENT", "url": net, "payload": _dec(payload)})

            def _recv(payload):
                emit({"dir": "WS_RECV", "url": net, "payload": _dec(payload)})

            ws.on("framesent", _sent)
            ws.on("framereceived", _recv)

        context.on("request", on_request)
        context.on("response", on_response)
        context.on("websocket", on_ws)

        pages = [pg for pg in context.pages if not pg.is_closed()]
        for pg in pages:
            print(f"  open tab: {pg.url}", file=sys.stderr)

        # Playwright's sync API only dispatches queued request/response/ws events
        # when a page.wait_for_timeout() runs — a plain time.sleep leaves them
        # buffered forever. Hold one live page to pump the event loop.
        pump = pages[0] if pages else None

        print(
            "\nNOW: in the SportyBet browser, open Mines, play a round, and place a "
            "bet in the UI. Ctrl+C to stop.",
            file=sys.stderr,
        )
        print(f"capturing to {out_path} ...", file=sys.stderr)
        try:
            while True:
                if pump is not None and not pump.is_closed():
                    # Pumps the Playwright sync event loop so queued events
                    # dispatch (a plain time.sleep does not, and events would
                    # sit buffered forever).
                    pump.wait_for_timeout(500)
                else:
                    time.sleep(0.5)
        except KeyboardInterrupt:
            pass
        finally:
            outf.close()
            print(f"\nwrote {out_path}", file=sys.stderr)


if __name__ == "__main__":
    main()
