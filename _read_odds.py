"""Read current Winner-market odds for an open SportyBet match page (read-only)."""
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
    js = """
    () => {
      const out = {url: location.href};
      // Odds buttons are typically anchors/buttons with a numeric price.
      const cands = [];
      document.querySelectorAll('a,button,div,span').forEach(el => {
        const t = (el.innerText||'').trim();
        if (/^\\d+\\.\\d{2}$/.test(t) && el.getBoundingClientRect().width > 0) {
          const r = el.getBoundingClientRect();
          cands.push({t, x: Math.round(r.x), y: Math.round(r.y), tag: el.tagName});
        }
      });
      // keep only those in the left odds column (x < 400)
      out.odds = cands.filter(c => c.x < 450).sort((a,b)=>a.y-b.y);
      return out;
    }
    """
    out = page.evaluate(js)
    print(json.dumps(out, ensure_ascii=False, indent=2))
