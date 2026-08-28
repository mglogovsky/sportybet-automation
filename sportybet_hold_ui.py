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
  POST /api/action     {"action": start|begin|redeem|rearm|retry|stop|reset, ...}
                         (reset = clear a dead session's error, back to idle)

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

from licensing import config as user_config  # noqa: E402

from adspower import AdsPowerClient, AdsPowerError  # noqa: E402
from sportybet_hold_flow import ControlBridge, default_args  # noqa: E402
from sportybet_hold_flow import run as flow_run  # noqa: E402

from licensing.gate import LicenseGate  # noqa: E402

HERE = Path(__file__).parent
DEFAULT_PORT = 8790
# Default app-window size (CSS px / pt). 545×882 is the size from the
# owner's reference screenshot (1090×1764 px on a 2× Retina display).
WINDOW_SIZE = (545, 882)

DEFAULT_SETTINGS = {
    "mines_stake": 100.0, "mines": 1, "desk_size": 25,
    "redeem_margin": 5.0, "min_stake": 20.0, "cycle_delay": 2.0,
}


class App:
    def __init__(self, no_license: bool = False) -> None:
        self.ads = self._make_ads_client()
        self.ui_port: int = DEFAULT_PORT  # set by main() once resolved
        self.bridge = ControlBridge(actions=queue.Queue())
        self.bridge.publish(state="idle")
        self.worker: threading.Thread | None = None
        self.error: str | None = None
        self.settings: dict = dict(DEFAULT_SETTINGS)
        self._start_lock = threading.Lock()
        # License gate: app-level (works with no session running). Publishes
        # into /api/state via _license_changed; on lock it enqueues the
        # existing graceful 'stop' action if a session is running.
        #
        # --no-license is our second, unlicensed version: the gate is never
        # started and the app reports a perpetual OK (no key entry, no server
        # checks). The customer build keeps the gate exactly as before.
        if no_license:
            self.license: dict = {"status": "OK", "expires_at": None,
                                  "seconds_left": None, "verdict": None,
                                  "checking": False}
            self.license_gate: LicenseGate | None = None
        else:
            self.license = {"status": "checking", "expires_at": None,
                            "seconds_left": None}
            self.license_gate = LicenseGate(on_change=self._license_changed,
                                            on_lock=self._license_locked)
            self.license_gate.start()

    def _license_changed(self, status, expires_at, seconds_left) -> None:
        with self._start_lock:
            self.license = {"status": status, "expires_at": expires_at,
                            "seconds_left": seconds_left,
                            # v2 /api/state shape: verdict + checking alongside
                            "verdict": status.split(":", 1)[1]
                            if status.startswith("locked:") else None,
                            "checking": status in ("checking", "offline-retry")}

    def recheck_license(self) -> dict:
        """Lock overlay's Re-check / Retry button: ask the gate to check now."""
        if self.license_gate is None:
            return {"ok": False, "error": "license check disabled in this build"}
        self.license_gate.recheck()
        return {"ok": True}

    def _license_locked(self, verdict) -> None:
        # Money-safety: only the existing graceful stop — finish the round,
        # cash out the mines hold, back to idle. Never a force-kill.
        if self.worker is not None and self.worker.is_alive():
            try:
                self.bridge.actions.put("stop")
            except Exception:
                pass

    def activate_key(self, body: dict) -> dict:
        if self.license_gate is None:
            return {"ok": False, "error": "license check disabled in this build"}
        key = str(body.get("key") or "").strip().upper()
        if not key:
            return {"ok": False, "error": "enter a license key"}
        verdict = self.license_gate.activate(key)
        if verdict == "OK":
            return {"ok": True}
        return {"ok": False, "error": f"license {verdict}"}

    def deactivate(self) -> dict:
        if self.license_gate is None:
            return {"ok": False, "error": "license check disabled in this build"}
        self.license_gate.deactivate()
        return {"ok": True}

    @staticmethod
    def _make_ads_client() -> AdsPowerClient:
        return AdsPowerClient(api_base=user_config.adspower_api_base(),
                              api_token=user_config.adspower_api_token())

    def save_config(self, body: dict) -> dict:
        """Persist user config (next to the license key file). AdsPower API
        base/token apply live; ui_port needs a restart."""
        restart = False
        fields = {}
        for k in ("adspower_api_base", "adspower_api_token"):
            if body.get(k) is not None:
                fields[k] = str(body[k]).strip()
        if body.get("ui_port") is not None:
            try:
                fields["ui_port"] = int(body["ui_port"]) if str(body["ui_port"]).strip() else ""
            except (TypeError, ValueError):
                return {"ok": False, "error": "ui_port must be a number"}
            restart = True
        cfg = user_config.update(**fields)
        self.ads = self._make_ads_client()  # live-apply API base/token
        return {"ok": True, "config": cfg, "restart_required": restart}

    def config_info(self) -> dict:
        cfg = user_config.load()
        return {
            "config_path": str(user_config.config_path()),
            "adspower_api_base": user_config.adspower_api_base(),
            "ui_port": self.ui_port,
            "ui_port_saved": cfg.get("ui_port"),
        }

    def start_session(self, body: dict) -> dict:
        if self.license_gate is not None and self.license_gate.status != "OK":
            return {"ok": False,
                    "error": f"license locked: {self.license_gate.status}"}
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
            "license": self.license,
            "config": self.config_info(),
            "now": time.time(),
        })
        return snap


APP: App  # created in main() once --no-license is known


def _find_chrome_windows() -> str | None:
    """chrome.exe via the App Paths registry key, then standard install dirs."""
    try:
        import winreg
        for root in (winreg.HKEY_CURRENT_USER, winreg.HKEY_LOCAL_MACHINE):
            try:
                with winreg.OpenKey(
                        root, r"SOFTWARE\Microsoft\Windows\CurrentVersion"
                              r"\App Paths\chrome.exe") as k:
                    path, _ = winreg.QueryValueEx(k, None)
                    if path and os.path.exists(path):
                        return path
            except OSError:
                continue
    except ImportError:
        pass
    for env in ("LOCALAPPDATA", "PROGRAMFILES", "PROGRAMFILES(X86)"):
        base = os.environ.get(env)
        if not base:
            continue
        p = os.path.join(base, r"Google\Chrome\Application\chrome.exe")
        if os.path.exists(p):
            return p
    return None


def open_window(url: str) -> bool:
    """Chrome --app mode: a small window with no tab strip or omnibox."""
    if sys.platform == "win32":
        chrome = _find_chrome_windows()
        if chrome:
            try:
                subprocess.Popen(
                    [chrome, f"--app={url}",
                     f"--window-size={WINDOW_SIZE[0]},{WINDOW_SIZE[1]}",
                     "--window-position=100,60"],
                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                return True
            except Exception:
                pass
        # Last resort: the default browser (console=False means no printed
        # URL, so always try to open SOMETHING).
        try:
            os.startfile(url)  # type: ignore[attr-defined]
            return True
        except Exception:
            return False
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
        # License actions are app-level: they must work with NO session
        # running, so they bypass the "no session running" guard below.
        if act == "activate_key":
            self._send_json(APP.activate_key(body))
            return
        if act == "deactivate":
            self._send_json(APP.deactivate())
            return
        if act == "recheck_license":
            self._send_json(APP.recheck_license())
            return
        # User config is app-level too — editable with no session running.
        if act == "save_config":
            self._send_json(APP.save_config(body))
            return
        if act == "reset":
            # The error screen's way back: a dead session can be dismissed to
            # idle without restarting the server.
            if APP.worker is not None and APP.worker.is_alive():
                self._send_json({"ok": False, "error": "session running"})
                return
            APP.error = None
            APP.bridge.publish(state="idle")
            self._send_json({"ok": True})
            return
        if act in ("begin", "continue", "redeem", "rearm", "retry", "pick",
                   "insta", "stop"):
            if APP.worker is None or not APP.worker.is_alive():
                self._send_json({"ok": False, "error": "no session running"})
                return
            APP.bridge.actions.put((act, body))
            self._send_json({"ok": True})
            return
        self._send_json({"ok": False, "error": f"unknown action {act!r}"}, code=400)


def main() -> None:
    p = argparse.ArgumentParser(description="SportyBet Hold UI")
    # Port precedence: --port flag > SPORTYPILOT_PORT env > config ui_port
    # > DEFAULT_PORT.
    p.add_argument("--port", type=int, default=None)
    p.add_argument("--no-window", action="store_true",
                   help="don't open the Chrome --app window (print the URL only)")
    p.add_argument("--no-license", action="store_true",
                   help="unlicensed version: no key entry, no license-server checks")
    args = p.parse_args()

    global APP
    APP = App(no_license=args.no_license)

    port = args.port if args.port is not None else user_config.ui_port(DEFAULT_PORT)
    APP.ui_port = port
    srv = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    url = f"http://127.0.0.1:{port}"
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
