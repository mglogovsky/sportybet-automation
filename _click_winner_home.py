"""Click the main-market 'Winner / Home' odds on the open match page, add to slip."""
import json
import time

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
      const out = {clicked: false};
      const lines = (document.body.innerText||'').split('\\n').map(s=>s.trim()).filter(Boolean);
      const winnerIdx = lines.indexOf('Winner');
      // Find all 'Home' buttons and pick the one whose row is the first 'Home'
      // after the Winner header (main market), i.e. smallest y among Home rows
      // located just below the Winner header.
      let header = null;
      [...document.querySelectorAll('div,span,h1,h2,h3')].forEach(el => {
        const t=(el.innerText||'').trim();
        if(t==='Winner' && el.getBoundingClientRect().height>0) header=el;
      });
      const hdrY = header ? header.getBoundingClientRect().y : 0;
      let target=null, best=1e9;
      [...document.querySelectorAll('button,a,div,span')].forEach(el => {
        const t=(el.innerText||'').trim();
        if(t!=='Home') return;
        const r=el.getBoundingClientRect();
        if(r.width===0||r.height===0) return;
        const d=r.y-hdrY;
        if(d>0 && d<best){best=d;target=el;}
      });
      if(target){ target.click(); out.clicked=true; out.y=Math.round(target.getBoundingClientRect().y); }
      return out;
    }
    """
    print("click:", json.dumps(page.evaluate(js), ensure_ascii=False))
    time.sleep(2)
    raw = page.evaluate("() => localStorage.getItem('betslips')")
    slips = json.loads(raw or "[]")
    print("slip count:", len(slips))
    for s in slips:
        print(json.dumps({
            "eventId": s.get("eventId"),
            "market": s.get("marketInfo", {}).get("id"),
            "desc": s.get("marketInfo", {}).get("desc"),
            "outcome": s.get("outcomeInfo", {}).get("desc"),
            "odds": s.get("outcomeInfo", {}).get("odds"),
            "prob": s.get("outcomeInfo", {}).get("probability"),
        }, ensure_ascii=False))
