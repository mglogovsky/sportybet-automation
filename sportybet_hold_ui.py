#!/usr/bin/env python3
"""SportyBet Hold UI — a small local app window driving sportybet_hold_flow.

Opens http://127.0.0.1:8790 in a Chrome --app window (no tab bar/omnibox).
The page picks an AdsPower profile and presses START; the flow then runs on a
single worker thread (the sole owner of Playwright — see the asyncio note in
sportybet_hold_flow.py) and publishes its state through a ControlBridge, which
is also how the buttons (begin / redeem / rearm / retry / stop) reach it.

Endpoints
---------
  GET  /               the page (hold_ui.html)
  GET  /api/state      1 Hz snapshot: state, balance, target, timer, log tail
  GET  /api/profiles   AdsPower profiles for the IDLE picker
  POST /api/action     {"action": start|begin|redeem|rearm|retry|stop, ...}

STOP means "finish the current round, cash out the mines hold, back to IDLE" —
never an abort. Quitting this app (Ctrl+C in the terminal) asks the worker to
do the same and always leaves the AdsPower browser running.
"""
from __future__ import annotations

import argparse
import json
import os
import queue
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from adspower import AdsPowerClient, AdsPowerError  # noqa: E402
from sportybet_hold_flow import ControlBridge, default_args  # noqa: E402
from sportybet_hold_flow import run as flow_run  # noqa: E402

HERE = Path(__file__).parent
DEFAULT_PORT = 8790
WINDOW_SIZE = (430, 780)

DEFAULT_SETTINGS = {
    "mines_stake": 100.0, "mines": 1, "desk_size": 25,
    "redeem_margin": 5.0, "min_stake": 20.0, "cycle_delay": 2.0,
}


class App:
    def __init__(self) -> None:
        self.ads = AdsPowerClient()
        self.bridge = ControlBridge(actions=queue.Queue())
        self.bridge.publish(state="idle")
        self.worker: threading.Thread | None = None
        self.error: str | None = None
        self.settings: dict = dict(DEFAULT_SETTINGS)
        self._start_lock = threading.Lock()

    def start_session(self, body: dict) -> dict:
        with self._start_lock:
            if self.worker is not None and self.worker.is_alive():
                return {"ok": False, "error": "session already running"}
            profile = str(body.get("profile") or "").strip()
            if not profile:
                return {"ok": False, "error": "pick a profile first"}
            for k in DEFAULT_SETTINGS:
                if body.get(k) is not None:
                    try:
                        self.settings[k] = float(body[k]) if k != "mines" and k != "desk_size" \
                            else int(body[k])
                    except (TypeError, ValueError):
                        pass
            # Fresh bridge per session: clean queue, clean log.
            self.bridge = ControlBridge(actions=queue.Queue())
            self.error = None
            args = default_args(adspower=profile, **self.settings)
            self.worker = threading.Thread(target=self._work, args=(args,),
                                           name="hold-flow", daemon=True)
            self.worker.start()
            return {"ok": True}

    def _work(self, args) -> None:
        try:
            rc = flow_run(args, self.bridge)
            self.bridge.log(f"session ended (rc={rc})")
            self.bridge.publish(state="idle")
        except SystemExit as e:
            self.error = str(e) or "session aborted"
            self.bridge.log(f"stopped: {self.error}")
            self.bridge.publish(state="error")
        except Exception as e:  # noqa: BLE001 — surface anything to the page
            self.error = f"{type(e).__name__}: {e}"
            self.bridge.log(f"ERROR: {self.error}")
            self.bridge.publish(state="error")
        finally:
            self.bridge.stop_requested = False

    def default_profile(self) -> str:
        """The operator's usual profile (from .adspower_profile) — pre-selected
        in the IDLE picker so nobody arms on the wrong browser."""
        try:
            cfg = json.loads((HERE / ".adspower_profile").read_text())
            return str(cfg.get("user_id") or cfg.get("name") or "")
        except Exception:
            return ""

    def state(self) -> dict:
        b = self.bridge
        snap = b.snap()
        snap.update({
            "running": self.worker is not None and self.worker.is_alive(),
            "error": self.error,
            "log": list(b.log_lines)[-60:],
            "stop_requested": b.stop_requested,
            "settings": self.settings,
            "mines_stake": self.settings.get("mines_stake"),
            "default_profile": self.default_profile(),
            "now": time.time(),
        })
        return snap


APP = App()


def open_window(url: str) -> bool:
    """Chrome --app mode: a small window with no tab strip or omnibox."""
    try:
        subprocess.Popen(
            ["open", "-na", "Google Chrome", "--args",
             f"--app={url}",
             f"--window-size={WINDOW_SIZE[0]},{WINDOW_SIZE[1]}",
             "--window-position=100,60"],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        return True
    except Exception:
        return False


class Handler(BaseHTTPRequestHandler):
    server_version = "SportyBetHoldUI/1.0"

    # -- helpers -----------------------------------------------------------
    def _send_json(self, obj: dict, code: int = 200) -> None:
        body = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("content-type", "application/json")
        self.send_header("content-length", str(len(body)))
        self.send_header("cache-control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args) -> None:  # quiet
        pass

    # -- routes ------------------------------------------------------------
    def do_GET(self) -> None:
        path = urlparse(self.path).path
        if path in ("/", "/index.html", "/hold_ui.html"):
            try:
                body = (HERE / "hold_ui.html").read_bytes()
            except OSError:
                self.send_error(404, "hold_ui.html not found")
                return
            self.send_response(200)
            self.send_header("content-type", "text/html; charset=utf-8")
            self.send_header("content-length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        if path == "/api/state":
            self._send_json(APP.state())
            return
        if path == "/api/profiles":
            try:
                profiles = [{"user_id": p.user_id, "label": p.label()}
                            for p in APP.ads.list_profiles()]
                self._send_json({"ok": True, "profiles": profiles})
            except AdsPowerError as e:
                self._send_json({"ok": False, "error": str(e)})
            return
        self.send_error(404)

    def do_POST(self) -> None:
        path = urlparse(self.path).path
        if path != "/api/action":
            self.send_error(404)
            return
        try:
            n = int(self.headers.get("content-length") or 0)
            body = json.loads(self.rfile.read(n) or b"{}")
        except Exception:
            self._send_json({"ok": False, "error": "bad JSON body"}, code=400)
            return
        act = body.get("action")
        if act == "start":
            self._send_json(APP.start_session(body))
            return
        if act in ("begin", "continue", "redeem", "rearm", "retry", "pick",
                   "insta", "cashout_bet", "stop"):
            if APP.worker is None or not APP.worker.is_alive():
                self._send_json({"ok": False, "error": "no session running"})
                return
            APP.bridge.actions.put((act, body))
            self._send_json({"ok": True})
            return
        self._send_json({"ok": False, "error": f"unknown action {act!r}"}, code=400)


def main() -> None:
    p = argparse.ArgumentParser(description="SportyBet Hold UI")
    p.add_argument("--port", type=int, default=DEFAULT_PORT)
    p.add_argument("--no-window", action="store_true",
                   help="don't open the Chrome --app window (print the URL only)")
    args = p.parse_args()

    srv = ThreadingHTTPServer(("127.0.0.1", args.port), Handler)
    url = f"http://127.0.0.1:{args.port}"
    if not args.no_window and not open_window(url):
        print(f"(could not open Chrome app-mode — open {url} yourself)")
    print(f"SportyBet Hold UI on {url} — Ctrl+C to quit", flush=True)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        print("\nshutting down — asking the round to finish...", flush=True)
        try:
            APP.bridge.actions.put("stop")
        except Exception:
            pass
        w = APP.worker
        if w is not None and w.is_alive():
            w.join(timeout=20)
        srv.server_close()
        print("bye — AdsPower browser left running", flush=True)


if __name__ == "__main__":
    main()
