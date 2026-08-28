"""Wait for the user to place a bet (mints a fresh cipher key), then reuse that
key to place our own duplicate bet via window.originFetch.

The key is lazy-minted on the first money-flow call after the cache expires, so
we wait for `ng_transId` (in localStorage or sessionStorage) to appear, read the
fresh key + transId, build the same payload the SPA sends, and place it.
"""
import base64
import json
import sys
import time
import urllib.parse

from Crypto.Cipher import AES
from Crypto.Util.Padding import pad, unpad
from playwright.sync_api import sync_playwright

import os
import sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from adspower import AdsPowerClient


def find_key(page):
    """Return (transId, key_bytes) from ng_transId in either storage, or None."""
    for store, getter in (("localStorage", "localStorage"),
                          ("sessionStorage", "sessionStorage")):
        raw = page.evaluate(f"() => {getter}.getItem('ng_transId')")
        if not raw:
            continue
        try:
            data = json.loads(raw)
        except Exception:
            continue
        if data.get("key") and data.get("transId") and data.get("date", 0) > time.time() * 1000:
            key = base64.b64decode(urllib.parse.unquote(data["key"]))
            return data["transId"], key, store
    return None


def aes_encrypt_b64(plaintext: str, key: bytes) -> str:
    iv = __import__("uuid").uuid4().bytes
    c = AES.new(key, AES.MODE_CBC, iv)
    return base64.b64encode(iv + c.encrypt(pad(plaintext.encode(), 16))).decode()


def aes_decrypt_b64(body: str, key: bytes) -> str:
    raw = base64.b64decode(body)
    iv, ct = raw[:16], raw[16:]
    return unpad(AES.new(key, AES.MODE_CBC, iv).decrypt(ct), 16).decode()


def build_payload(slip, stake_units):
    oi = slip["outcomeInfo"]
    spec = slip.get("marketInfo", {}).get("specifier") or ""
    mid = slip["marketInfo"]["id"]
    oid = oi["id"]
    sel_id = f"uof:1/{slip['sportId']}/{mid}/{oid}"
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


def main():
    adspower = "k1frnp5d"
    event_id = "sr:match:111111114305012"
    stake = 1000.0
    stake_units = int(round(stake * 10000))

    c = AdsPowerClient()
    with sync_playwright() as pw:
        b = pw.chromium.connect_over_cdp(c.active(adspower).cdp_url())
        page = next((p for p in b.contexts[0].pages
                     if not p.is_closed() and "sportybet.com" in (p.url or "")), None)
        if page is None:
            sys.exit("no sportybet tab")
        print("URL", page.url, flush=True)

        # Wait for the key to appear (user places a bet manually → lazy mint).
        print("waiting for you to place a bet (mints fresh cipher key)...", flush=True)
        found = None
        for _ in range(90):
            found = find_key(page)
            if found:
                break
            page.wait_for_timeout(2000)
        if found is None:
            sys.exit("no fresh cipher key appeared within 3min")
        transid, key, store = found
        print(f"got fresh key from {store} (transid {transid})", flush=True)

        raw = page.evaluate("() => localStorage.getItem('betslips')")
        slip = next((s for s in json.loads(raw or "[]") if s.get("eventId") == event_id), None)
        if slip is None:
            sys.exit(f"selection {event_id} not on live slip")
        payload = build_payload(slip, stake_units)
        plaintext = json.dumps(payload, separators=(",", ":"))
        body = aes_encrypt_b64(plaintext, key)

        js = """
        (async () => {
          const resp = await window.originFetch('/api/ng/orders/order', {
            method: 'POST',
            headers: HEADERS,
            credentials: 'include',
            body: BODY,
          });
          return { status: resp.status, body: await resp.text() };
        })()
        """.replace("BODY", json.dumps(body)).replace(
            "HEADERS", json.dumps({
                "content-type": "application/json;charset=UTF-8",
                "clientid": "web", "platform": "web", "operid": "2",
                "transid": transid,
            }))
        res = page.evaluate(js)
        print(f"HTTP {res['status']}", flush=True)
        raw_resp = res["body"]
        if res["status"] != 200:
            print(raw_resp[:500]); sys.exit(1)
        try:
            print("RESPONSE plaintext:", aes_decrypt_b64(raw_resp, key))
        except Exception as e:
            print("decrypt failed:", e); print(raw_resp[:500])


if __name__ == "__main__":
    main()
