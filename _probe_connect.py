import asyncio, sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

def lp(tag):
    try:
        l = asyncio.get_running_loop()
        print(f"{tag}: RUNNING {l!r}", flush=True)
    except RuntimeError:
        print(f"{tag}: none", flush=True)

lp("top")
from adspower import AdsPowerClient
lp("after adspower import")
c = AdsPowerClient()
p = c.resolve("k1frnp5c")
lp("after resolve")
meta = c.active(p.user_id) or c.start(p.user_id)
lp("after active/start")
from playwright.sync_api import sync_playwright
lp("after playwright import")
pw = sync_playwright().start()
print("start OK", flush=True)
pw.stop()
