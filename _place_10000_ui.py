"""Set stake to 10000 in the SportyBet slip and click Book Bet (UI-driven)."""
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
    print("URL", page.url)

    # Set stake input to 10000 (visible slip input, placeholder 'min. 10').
    set_res = page.evaluate("""
    () => {
      const inp = [...document.querySelectorAll('input')].find(i =>
        i.getBoundingClientRect().width > 0 && i.getAttribute('placeholder') === 'min. 10');
      if (!inp) return 'no stake input';
      const setter = Object.getOwnPropertyDescriptor(HTMLInputElement.prototype,'value').set;
      setter.call(inp, '10000');
      inp.dispatchEvent(new Event('input', {bubbles:true}));
      inp.dispatchEvent(new Event('change', {bubbles:true}));
      return 'stake set to ' + inp.value;
    }
    """)
    print("stake:", set_res)
    time.sleep(1)

    click_res = page.evaluate("""
    () => {
      const el = [...document.querySelectorAll('button,div,span,a')].find(b =>
        /^Book Bet$/i.test((b.innerText||'').trim()) && b.getBoundingClientRect().width > 0);
      if (!el) return 'no book bet button';
      el.click();
      return 'clicked ' + el.tagName + ' ' + (el.textContent||'').trim().slice(0,20);
    }
    """)
    print("click:", click_res)

    # In-play placement takes ~13s. Wait, then report slip cleared (bet placed).
    print("waiting 20s for settlement...", flush=True)
    time.sleep(20)
    raw = page.evaluate("() => localStorage.getItem('betslips')")
    import json
    slips = json.loads(raw or "[]")
    print("slip count after:", len(slips))
    print("done — check interceptor capture for the orders/order response")
