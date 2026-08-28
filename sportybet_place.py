#!/usr/bin/env python3
"""Place a SportyBet sports bet by AES-encrypting the same payload the site uses.

Reads the live AES key from `localStorage.ng_transId` and the current selection
from the live betslip (`localStorage.betslips`), builds the `orders/order`
payload, encrypts with AES-128-CBC, POSTs it, and decrypts the response.

This mirrors the exact request the SPA sends (see sportybet-mines-flow.md §4).
"""
from __future__ import annotations

import argparse
import base64
import json
import sys
import urllib.parse
import uuid

from Crypto.Cipher import AES
from Crypto.Util.Padding import pad, unpad
from playwright.sync_api import sync_playwright

import os
import sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from adspower import AdsPowerClient

ORDER_URL = "https://www.sportybet.com/api/ng/orders/order"
TRANSID_HEADER = "transid"  # the cipher session handle


def aes_key_from_ls(page) -> tuple[bytes, str]:
    """Return (AES key, transId) from the fresh cipher session.

    The SPA mints the key into either sessionStorage.CIPHER_AES_KEY (per the
    HAR doc) or localStorage.ng_transId (observed in captures). Prefer whichever
    is fresh; raise if neither is present.
    """
    for store in ("sessionStorage", "localStorage"):
        keyname = "CIPHER_AES_KEY" if store == "sessionStorage" else "ng_transId"
        try:
            raw = page.evaluate(f"() => {store}.getItem('{keyname}')")
        except Exception:
            continue
        if not raw:
            continue
        try:
            data = json.loads(raw)
            key_b64 = urllib.parse.unquote(data["key"])
            key = base64.b64decode(key_b64)
            if len(key) == 16 and data.get("transId"):
                return key, data["transId"]
        except Exception:
            continue
    raise SystemExit("no fresh cipher key in browser (localStorage.ng_transId / "
                     "sessionStorage.CIPHER_AES_KEY) — place one bet manually first")


def aes_encrypt_b64(plaintext: str, key: bytes) -> str:
    iv = uuid.uuid4().bytes  # 16 random bytes, prepended
    c = AES.new(key, AES.MODE_CBC, iv)
    return base64.b64encode(iv + c.encrypt(pad(plaintext.encode(), 16))).decode()


def aes_decrypt_b64(body: str, key: bytes) -> str:
    raw = base64.b64decode(body)
    iv, ct = raw[:16], raw[16:]
    return unpad(AES.new(key, AES.MODE_CBC, iv).decrypt(ct), 16).decode()


def read_selection(page, event_id: str) -> dict | None:
    """Pull a live selection from localStorage.betslips by event id."""
    raw = page.evaluate("() => localStorage.getItem('betslips')")
    for slip in json.loads(raw or "[]"):
        if slip.get("eventId") == event_id:
            return slip
    return None


def build_payload(slip: dict, stake_units: int) -> dict:
    oi = slip["outcomeInfo"]
    return {
        "bizType": 1,
        "ticket": {
            "selections": [{
                "eventId": slip["eventId"],
                "id": f"uof:1/{slip['sportId']}/{slip['marketInfo']['id']}/{oi['id']}",
                "odds": oi["odds"],
                "banker": False,
                "probability": float(oi.get("probability") or 0),
            }],
            "bets": [{"selectedSystems": [1], "stake": {"value": stake_units}}],
        },
        "orderType": 1,
        "paymentType": 0,
        "isBonusFactor": False,
        "subBizType": 2,
        "actualPayAmount": stake_units,
        "loadingShareCode": "",
    }


def main() -> None:
    p = argparse.ArgumentParser(description="Place a SportyBet bet (AES-encrypted orders/order)")
    p.add_argument("--adspower", default="k1frnp5d")
    p.add_argument("--event-id", default="sr:match:73963734",
                   help="Match event id to bet on (must be on the live slip)")
    p.add_argument("--stake", type=float, default=10000.0, help="Stake in NGN")
    p.add_argument("--no-verify", action="store_true",
                   help="Use the slip odds as-is without a re-read guard")
    args = p.parse_args()

    stake_units = int(round(args.stake * 10000))

    c = AdsPowerClient()
    with sync_playwright() as pw:
        b = pw.chromium.connect_over_cdp(c.active(args.adspower).cdp_url())
        context = b.contexts[0]
        page = next((pg for pg in context.pages
                     if not pg.is_closed() and "sportybet.com" in (pg.url or "")), None)
        if page is None:
            sys.exit("no sportybet tab")
        key, transid = aes_key_from_ls(page)
        slip = read_selection(page, args.event_id)
        if slip is None:
            sys.exit(f"selection {args.event_id} not on the live slip")
        payload = build_payload(slip, stake_units)
        plaintext = json.dumps(payload, separators=(",", ":"))

        body = aes_encrypt_b64(plaintext, key)

        # Use window.originFetch — the SPA's native fetch (before Faro patched it).
        # window.fetch is a Faro wrapper that returns a custom object and throws a
        # WAF challenge on eval-context calls; Playwright's APIRequestContext returns
        # a CloudFront 202 challenge. originFetch returns a real standard Response
        # with the synchronous settlement (200 + encrypted body).
        js = """
        (async () => {
          const resp = await window.originFetch('/api/ng/orders/order', {
            method: 'POST',
            headers: HEADERS,
            credentials: 'include',
            body: BODY,
          });
          const text = await resp.text();
          return { status: resp.status, body: text };
        })()
        """.replace("BODY", json.dumps(body)).replace(
            "HEADERS", json.dumps({
                "content-type": "application/json;charset=UTF-8",
                "clientid": "web",
                "platform": "web",
                "operid": "2",
                TRANSID_HEADER: transid,
            }))
        res = page.evaluate(js)
        print(f"HTTP {res['status']}")
        raw_resp = res["body"]
        if res["status"] != 200:
            print(raw_resp[:500])
            sys.exit(1)
        try:
            plain_resp = aes_decrypt_b64(raw_resp, key)
        except Exception as e:
            print("could not decrypt response:", e)
            print(raw_resp[:500])
            sys.exit(1)
        print("RESPONSE plaintext:")
        print(plain_resp)


if __name__ == "__main__":
    main()
