#!/usr/bin/env python3
"""Throwaway licensing test — no real license server exists yet.

Spins up a local mock implementing POST /api/license/check, then exercises:
  1. client.check: OK / EXPIRED / REVOKED / UNKNOWN_KEY / UNREACHABLE,
     unrecognized-200-verdict -> UNREACHABLE, bearer header only when the
     token is non-empty (v2 decision 1)
  2. gate: startup ladder -> fail CLOSED on unreachable; locked-state polling
     unlocks when the server starts saying OK
  3. real sportybet_hold_ui.py HTTP endpoints (--no-window, test port):
     needs_key at boot -> activate_key (lowercase input normalized) ->
     deactivate -> activate with EXPIRED -> locked -> recheck_license action
     after the server recovers -> unlocked
  4. store key normalization; config resolution; UI port precedence

Run:  python3 test_licensing.py

The UI subprocess needs a python with the app's runtime deps installed from
requirements.txt (playwright/requests/pycryptodome). Override with
TEST_UI_PYTHON; default is the local project venv.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import threading
import time
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

HERE = Path(__file__).parent
GOOD_KEY = "SBET-GOOD-KEY"
BAD_KEY = "SBET-NOPE-NOPE"

UI_PYTHON = os.environ.get(
    "TEST_UI_PYTHON",
    "/Users/martinglogovsky/Documents/GitHub/unified-listener-and-cashout/.venv/bin/python")

FAILURES = []


def check(name, cond, detail=""):
    print(f"  {'PASS' if cond else 'FAIL'}  {name}" + (f" — {detail}" if detail and not cond else ""))
    if cond is False:
        FAILURES.append(name)


# ---------------------------------------------------------------- mock server
class MockLicense(BaseHTTPRequestHandler):
    verdict = "OK"           # class-level, flipped by tests
    raw_body = None          # when set, returned verbatim (protocol breakage)
    seen_auth = []

    def do_POST(self):
        if not self.path.startswith("/api/license/check"):
            self.send_error(404)
            return
        MockLicense.seen_auth.append(self.headers.get("Authorization"))
        n = int(self.headers.get("content-length") or 0)
        body = json.loads(self.rfile.read(n) or b"{}")
        key = body.get("key")
        if MockLicense.raw_body is not None:
            payload = MockLicense.raw_body
        elif key == BAD_KEY:
            payload = json.dumps({"verdict": "UNKNOWN_KEY"}).encode()
        elif MockLicense.verdict == "OK":
            payload = json.dumps({"verdict": "OK",
                                  "expiresAt": int(time.time()) + 20 * 86400,
                                  "secondsLeft": 20 * 86400}).encode()
        else:
            payload = json.dumps({"verdict": MockLicense.verdict}).encode()
        self.send_response(200)
        self.send_header("content-type", "application/json")
        self.send_header("content-length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def log_message(self, *a):
        pass


def http_json(url, body=None):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data,
                                 headers={"content-type": "application/json"})
    with urllib.request.urlopen(req, timeout=5) as r:
        return json.loads(r.read())


def wait_state(port, pred, timeout=10.0, what=""):
    t0 = time.time()
    while time.time() - t0 < timeout:
        try:
            st = http_json(f"http://127.0.0.1:{port}/api/state")
            lic = st.get("license") or {}
            if pred(lic):
                return lic
        except Exception:
            pass
        time.sleep(0.2)
    raise AssertionError(f"timeout waiting for {what}")


# ------------------------------------------------------------------ the tests
def main():
    tmp = Path(tempfile.mkdtemp(prefix="sportypilot-test-"))
    store_file = tmp / "license.json"

    mock = ThreadingHTTPServer(("127.0.0.1", 0), MockLicense)
    mock_port = mock.server_address[1]
    threading.Thread(target=mock.serve_forever, daemon=True).start()
    mock_url = f"http://127.0.0.1:{mock_port}"
    os.environ["LICENSE_SERVER_URL"] = mock_url  # read by build_config at import
    os.environ["LICENSE_API_TOKEN"] = ""         # v2 decision 1: empty default
    os.environ["SPORTYPILOT_LICENSE_STORE"] = str(store_file)
    sys.path.insert(0, str(HERE))

    import build_config
    from licensing import client, store, gate

    print("== client.check ==")
    build_config.SERVER_BASE_URL = mock_url
    build_config.LICENSE_API_TOKEN = ""

    v = client.check(GOOD_KEY)
    check("OK verdict returns seconds/expires", v[0] == "OK" and v[1] > 0 and v[2] > 0, str(v))
    check("no Authorization header when token empty",
          MockLicense.seen_auth and MockLicense.seen_auth[-1] is None,
          str(MockLicense.seen_auth[-1]))
    build_config.LICENSE_API_TOKEN = "test-token"
    client.check(GOOD_KEY)
    check("bearer header sent when token set",
          MockLicense.seen_auth[-1] == "Bearer test-token", str(MockLicense.seen_auth[-1]))
    build_config.LICENSE_API_TOKEN = ""

    v = client.check(BAD_KEY)
    check("unknown key -> UNKNOWN_KEY", v[0] == "UNKNOWN_KEY" and v[1] is None, str(v))
    MockLicense.verdict = "EXPIRED"
    check("EXPIRED verdict", client.check(GOOD_KEY)[0] == "EXPIRED")
    MockLicense.verdict = "REVOKED"
    check("REVOKED verdict", client.check(GOOD_KEY)[0] == "REVOKED")
    MockLicense.verdict = "OK"

    MockLicense.raw_body = json.dumps({"verdict": "SOMETHING_NEW"}).encode()
    check("unrecognized 200 verdict -> UNREACHABLE (never definitive)",
          client.check(GOOD_KEY)[0] == "UNREACHABLE")
    MockLicense.raw_body = b"not json at all"
    check("garbage body -> UNREACHABLE", client.check(GOOD_KEY)[0] == "UNREACHABLE")
    MockLicense.raw_body = None

    build_config.SERVER_BASE_URL = "http://127.0.0.1:1"  # dead
    check("dead server -> UNREACHABLE", client.check(GOOD_KEY)[0] == "UNREACHABLE")
    build_config.SERVER_BASE_URL = mock_url

    print("== store ==")
    check("no key file -> load None", store.load() is None)
    store.save("  sbet-good-key  ")
    check("save normalizes (strip+uppercase)", store.load() == "SBET-GOOD-KEY",
          store.load() or "")
    mode = oct(store_file.stat().st_mode & 0o777)
    check("key file perms 0600", mode == "0o600", mode)
    store.delete()
    check("delete -> load None", store.load() is None)

    print("== gate: startup ladder fails CLOSED ==")
    gate.STARTUP_LADDER = (0, 0.1, 0.1)   # compress the 0s/15s/45s ladder
    store.save(GOOD_KEY)
    build_config.SERVER_BASE_URL = "http://127.0.0.1:1"
    locked = []
    g = gate.LicenseGate(on_lock=lambda verdict: locked.append(verdict))
    g.start()
    t0 = time.time()
    while g.status == "checking" and time.time() - t0 < 10:
        time.sleep(0.05)
    check("startup unreachable -> locked:UNREACHABLE after ladder",
          g.status == "locked:UNREACHABLE", g.status)
    check("on_lock fired", locked == ["UNREACHABLE"], str(locked))
    build_config.SERVER_BASE_URL = mock_url
    store.delete()

    print("== gate: no key file -> needs_key ==")
    g2 = gate.LicenseGate()
    g2.start()
    t0 = time.time()
    while g2.status == "checking" and time.time() - t0 < 5:
        time.sleep(0.1)
    check("needs_key", g2.status == "needs_key", g2.status)

    print("== gate: activate while unreachable keeps prior state ==")
    build_config.SERVER_BASE_URL = "http://127.0.0.1:1"
    v = g2.activate(GOOD_KEY)
    check("activate returns UNREACHABLE", v == "UNREACHABLE", v)
    time.sleep(0.2)
    check("still needs_key (fail-closed, no lock)", g2.status == "needs_key", g2.status)
    build_config.SERVER_BASE_URL = mock_url
    v = g2.activate(GOOD_KEY)
    check("activate OK after server back", v == "OK" and g2.status == "OK", v)
    store.delete()

    print("== gate: locked-state polling unlocks on OK ==")
    gate.LOCKED_POLL = 0.3
    store.save(GOOD_KEY)
    MockLicense.verdict = "EXPIRED"
    g3 = gate.LicenseGate()
    g3.start()
    t0 = time.time()
    while not g3.status.startswith("locked") and time.time() - t0 < 5:
        time.sleep(0.05)
    check("definitive startup verdict -> locked:EXPIRED (short-circuits ladder)",
          g3.status == "locked:EXPIRED", g3.status)
    MockLicense.verdict = "OK"   # admin extends — no restart, no re-activate
    t0 = time.time()
    while g3.status != "OK" and time.time() - t0 < 5:
        time.sleep(0.05)
    check("locked poll picks up OK -> unlocked", g3.status == "OK", g3.status)
    check("expiry populated after unlock", bool(g3.expires_at), str(g3.expires_at))
    store.delete()

    print("== config ==")
    from licensing import config as user_config
    cfg_file = tmp / "config.json"
    check("config lives next to license.json", user_config.config_path() == cfg_file,
          str(user_config.config_path()))
    user_config.update(ui_port=9999)
    check("ui_port: config file", user_config.ui_port(8790) == 9999)
    os.environ["SPORTYPILOT_PORT"] = "8888"
    check("ui_port: env beats config", user_config.ui_port(8790) == 8888)
    del os.environ["SPORTYPILOT_PORT"]
    check("api base default", user_config.adspower_api_base().startswith("http://127.0.0.1:"))
    user_config.update(adspower_api_base="http://127.0.0.1:59999")
    check("api base from config", user_config.adspower_api_base() == "http://127.0.0.1:59999")
    mode = oct(cfg_file.stat().st_mode & 0o777)
    check("config file perms 0600", mode == "0o600", mode)
    user_config.save({})  # reset before subprocess tests

    print("== real HTTP endpoints (sportybet_hold_ui.py) ==")
    ui_port = 8879
    env = dict(os.environ)
    env["LICENSE_SERVER_URL"] = mock_url
    # Run the UI with a python that has the runtime deps (requirements.txt);
    # adspower.py is vendored in this repo.
    ui_python = UI_PYTHON if os.path.exists(UI_PYTHON) else sys.executable
    proc = subprocess.Popen(
        [ui_python, str(HERE / "sportybet_hold_ui.py"),
         "--port", str(ui_port), "--no-window"],
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, env=env)
    base = f"http://127.0.0.1:{ui_port}"
    try:
        lic = wait_state(ui_port, lambda l: l.get("status") == "needs_key",
                         what="needs_key at boot")
        check("boots to needs_key (no key file)", True)
        check("state license has verdict + checking fields",
              "verdict" in lic and "checking" in lic, str(sorted(lic)))

        st = http_json(base + "/api/state")
        cfg = st.get("config") or {}
        check("state exposes config block", cfg.get("ui_port") == ui_port and
              cfg.get("config_path", "").endswith("config.json"), str(cfg))

        r = http_json(base + "/api/action", {"action": "save_config",
                                             "adspower_api_base": "http://127.0.0.1:59998"})
        check("save_config ok", r.get("ok") is True, str(r))
        saved = json.loads((tmp / "config.json").read_text())
        check("config file written", saved.get("adspower_api_base") == "http://127.0.0.1:59998",
              str(saved))
        r = http_json(base + "/api/action", {"action": "save_config", "ui_port": "bogus"})
        check("save_config bad port rejected", r.get("ok") is False, str(r))
        r = http_json(base + "/api/action", {"action": "save_config", "adspower_api_base": ""})
        check("save_config clears key", r.get("ok") is True, str(r))

        r = http_json(base + "/api/action", {"action": "start", "profile": "x"})
        check("start refused when not licensed", r.get("ok") is False and
              "license locked" in (r.get("error") or ""), str(r))

        # lowercase input is normalized before the server sees it
        r = http_json(base + "/api/action",
                      {"action": "activate_key", "key": "  sbet-good-key  "})
        check("activate_key OK with lowercase/whitespace input", r.get("ok") is True, str(r))
        lic = wait_state(ui_port, lambda l: l.get("status") == "OK", what="OK after activate")
        check("state license OK with expiry",
              lic.get("expires_at") and lic.get("seconds_left"), str(lic))
        check("key file written normalized", store_file.read_text().find("SBET-GOOD-KEY") >= 0)

        r = http_json(base + "/api/action", {"action": "start", "profile": ""})
        check("start no longer license-blocked",
              "license locked" not in (r.get("error") or ""), str(r))

        r = http_json(base + "/api/action", {"action": "deactivate"})
        check("deactivate ok", r.get("ok") is True, str(r))
        wait_state(ui_port, lambda l: l.get("status") == "needs_key",
                   what="needs_key after deactivate")
        check("key file deleted", not store_file.exists())

        # EXPIRED at a recheck with the key still stored -> locked; then the
        # server recovers (admin extends) and the lock overlay's Re-check
        # button unlocks without re-entering the key.
        r = http_json(base + "/api/action", {"action": "activate_key", "key": GOOD_KEY})
        check("re-activate OK (key stored again)", r.get("ok") is True, str(r))
        wait_state(ui_port, lambda l: l.get("status") == "OK", what="OK before expiry")
        MockLicense.verdict = "EXPIRED"
        r = http_json(base + "/api/action", {"action": "recheck_license"})
        check("recheck_license accepted", r.get("ok") is True, str(r))
        lic = wait_state(ui_port, lambda l: l.get("status") == "locked:EXPIRED",
                         what="locked:EXPIRED")
        check("state locked:EXPIRED with verdict field", lic.get("verdict") == "EXPIRED", str(lic))
        r = http_json(base + "/api/action", {"action": "start", "profile": "x"})
        check("start refused while locked", r.get("ok") is False and
              "license locked" in (r.get("error") or ""), str(r))

        MockLicense.verdict = "OK"
        r = http_json(base + "/api/action", {"action": "recheck_license"})
        check("second recheck accepted", r.get("ok") is True, str(r))
        lic = wait_state(ui_port, lambda l: l.get("status") == "OK",
                         what="OK after recheck")
        check("recheck unlocked the app", True)
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
        out = proc.stdout.read().decode(errors="replace") if proc.stdout else ""
        if FAILURES:
            print("---- app output ----\n" + out[-3000:])

    print("== port precedence: SPORTYPILOT_PORT env (no CLI flag) ==")
    env2 = dict(env)
    env2["SPORTYPILOT_PORT"] = "8881"
    proc2 = subprocess.Popen(
        [ui_python, str(HERE / "sportybet_hold_ui.py"), "--no-window"],
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, env=env2)
    try:
        st = None
        t0 = time.time()
        while time.time() - t0 < 10:
            try:
                st = http_json("http://127.0.0.1:8881/api/state")
                break
            except Exception:
                time.sleep(0.3)
        check("bound to SPORTYPILOT_PORT 8881 without --port",
              st is not None and (st.get("config") or {}).get("ui_port") == 8881,
              str(st and st.get("config")))
    finally:
        proc2.terminate()
        try:
            proc2.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc2.kill()

    print()
    if FAILURES:
        print(f"FAILED: {len(FAILURES)} check(s): {FAILURES}")
        sys.exit(1)
    print(f"ALL CHECKS PASSED ({'see counts above'})")


if __name__ == "__main__":
    main()
