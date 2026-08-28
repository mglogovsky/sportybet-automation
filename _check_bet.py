"""Check whether the SportyBet bet was placed: balance + bet list."""
import json

import os
import sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from adspower import AdsPowerClient
from playwright.sync_api import sync_playwright

c = AdsPowerClient()
with sync_playwright() as pw:
    b = pw.chromium.connect_over_cdp(c.active("k1frnp5c").cdp_url())
    page = next((p for p in b.contexts[0].pages if not p.is_closed() and "sportybet.com" in (p.url or "")), None)
    print("URL:", page.url)
    js = """
    () => {
      const lines = (document.body.innerText||'').split('\\n').map(s=>s.trim()).filter(Boolean);
      return { tail: lines.slice(-30) };
    }
    """
    res = page.evaluate(js)
    print(json.dumps(res, ensure_ascii=False, indent=2))
