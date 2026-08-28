"""Test which fetch call shape returns real JSON on the SPA page (no bet)."""
import json
import os
import sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from adspower import AdsPowerClient
from playwright.sync_api import sync_playwright

c = AdsPowerClient()
with sync_playwright() as pw:
    b = pw.chromium.connect_over_cdp(c.active("k1frnp5c").cdp_url())
    page = next((p for p in b.contexts[0].pages
                 if not p.is_closed() and "sportybet.com" in (p.url or "")), None)
    print("URL", page.url)

    def try_fetch(label, js):
        try:
            r = page.evaluate(js)
            print(label, "=>", json.dumps(r, indent=2)[:300])
        except Exception as e:
            print(label, "ERR", str(e)[:200])

    # 1. SPA wrapper, absolute path
    try_fetch("SPA abs-path", """
    (async () => {
      const r = await fetch('/api/ng/pocket/v1/finAccs/finAcc/userBal/NGN',
                            {method:'GET', credentials:'include'});
      return {status: r.status, type: typeof r, hasText: typeof r.text, body: await r.text()};
    })()
    """)

    # 2. Native originFetch, absolute path
    try_fetch("native abs-path", """
    (async () => {
      const r = await window.originFetch('/api/ng/pocket/v1/finAccs/finAcc/userBal/NGN',
                            {method:'GET', credentials:'include'});
      return {status: r.status, type: typeof r, hasText: typeof r.text, body: await r.text()};
    })()
    """)
