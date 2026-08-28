"""Inspect live SportyBet betslip + cipher-key storage (no bet placed)."""
import json

import os
import sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from adspower import AdsPowerClient
from playwright.sync_api import sync_playwright

c = AdsPowerClient()
with sync_playwright() as pw:
    b = pw.chromium.connect_over_cdp(c.active("k1frnp5c").cdp_url())
    ctx = b.contexts[0]
    page = next((p for p in ctx.pages if not p.is_closed() and "sportybet.com" in (p.url or "")), None)
    if page is None:
        print("no sportybet tab"); raise SystemExit
    print("URL", page.url)
    js = """
    () => {
      const out = {url: location.href};
      try {
        out.ls = {};
        for (let i=0;i<localStorage.length;i++) {
          const k = localStorage.key(i);
          if (/cipher|transid|ng_trans|slip|stake|selection/i.test(k))
            out.ls[k] = (localStorage.getItem(k)||'').slice(0,300);
        }
      } catch(e){ out.lsErr = String(e); }
      try {
        out.ss = {};
        for (let i=0;i<sessionStorage.length;i++) {
          const k = sessionStorage.key(i);
          if (/cipher|transid|slip|stake|selection/i.test(k))
            out.ss[k] = (sessionStorage.getItem(k)||'').slice(0,300);
        }
      } catch(e){ out.ssErr = String(e); }
      const lines = (document.body.innerText||'').split('\\n').map(s=>s.trim()).filter(Boolean);
      out.bodyTail = lines.slice(-60);
      return out;
    }
    """
    res = page.evaluate(js)
    print(json.dumps(res, ensure_ascii=False, indent=2)[:6000])
