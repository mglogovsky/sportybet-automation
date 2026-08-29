# FeedWire - Sporty Bet — packaging

Builds the app (`sportybet_hold_ui.py` + `sportybet_hold_flow.py`
+ `hold_ui.html` + `licensing/`) into a distributable **FeedWire - Sporty Bet**
app via PyInstaller. The UI runs in a **native pywebview window** (WKWebView
on macOS, WebView2 on Windows) — no Chrome or external browser needed.

End-user setup lives in [INSTRUCTIONS.md](../INSTRUCTIONS.md); this file is
for whoever builds the artifacts.

## Prerequisites (build machine)

- Python 3.10+ with the runtime deps installed
  (`pip install -r requirements.txt` — playwright, requests, pycryptodome,
  pywebview). The macOS script auto-installs PyInstaller.
- `adspower.py` — **vendored in this repo** (repo root), so no external source
  checkout is needed at build or run time. It only depends on `requests`.

## Prerequisites (end user machine)

- **AdsPower installed and running** with a local API on its default port —
  the app drives the AdsPower browser over CDP; no browser is bundled.
- A valid license key (`SBET-XXXX-XXXX-XXXX`), entered on first run.

## Build

```sh
# macOS (on a Mac)
PYTHON=$(which python3) packaging/build_macos.sh
#   → "dist/FeedWire - Sporty Bet.app" + FeedWire-SportyBet-macos.zip

# Windows (on Windows)
powershell -ExecutionPolicy Bypass -File packaging\build_windows.ps1
#   → "dist\FeedWire - Sporty Bet\" + FeedWire-SportyBet-windows.zip
```

PyInstaller does **not** cross-compile: build each artifact on its own OS.

> `build_macos.sh` needs a Python with the runtime deps. Pass it explicitly
> via `PYTHON=...` (the built-in default is a machine-specific dev path).

## Icons

- `packaging/FeedWire.icns` — macOS bundle icon (set on `BUNDLE` in the spec).
- `packaging/FeedWire.ico` — Windows exe icon (set on `EXE` in the spec).
- Source image: `packaging/icon-src.png`. Regenerate after changing it:

```sh
# macOS .icns
mkdir -p packaging/FeedWire.iconset
for s in 16 32 128 256 512; do
  sips -z $s $s packaging/icon-src.png --out packaging/FeedWire.iconset/icon_${s}x${s}.png
  sips -z $((s*2)) $((s*2)) packaging/icon-src.png --out packaging/FeedWire.iconset/icon_${s}x${s}@2x.png
done
iconutil -c icns packaging/FeedWire.iconset -o packaging/FeedWire.icns

# Windows .ico (Pillow)
python3 -c "from PIL import Image; \
img = Image.open('packaging/icon-src.png').convert('RGBA'); \
img.save('packaging/FeedWire.ico', sizes=[(16,16),(24,24),(32,32),(48,48),(64,64),(128,128),(256,256)])"
```

## Signing notes

- **macOS**: the script ad-hoc signs the bundle (`codesign --sign -`). On
  other Macs Gatekeeper will still warn — beta users right-click → Open.
  Real distribution needs a Developer ID cert + notarization.
- **Windows**: unsigned builds trigger SmartScreen. Self-signed is OK for
  beta; use an EV cert for production (see comment in the .ps1).

## Notes

- User config lives next to the license key in
  `%APPDATA%/SportyPilot/config.json` (Windows) /
  `~/Library/Application Support/SportyPilot/config.json` (macOS) /
  `~/.config/sportypilot/config.json` (Linux). Keys: `adspower_api_base`,
  `adspower_api_token`, `ui_port`. Editable from the app's idle-screen
  ⚙ SETTINGS panel (`save_config` action) or by hand.
- Resolution order — UI port: `--port` flag → env `SPORTYPILOT_PORT` →
  config `ui_port` → 8790. AdsPower API base/token: env `ADSPOWER_API_BASE` /
  `ADSPOWER_API_TOKEN` → config keys → `http://127.0.0.1:50325`.
- The license key is stored per-user at
  `%APPDATA%/SportyPilot/license.json` (Windows) /
  `~/Library/Application Support/SportyPilot/license.json` (macOS) —
  it is machine-local config, not baked into the build.
- License server URL: baked from `build_config.py` at build time
  (default `https://feed-wire.pro`; `LICENSE_SERVER_URL` env override is for
  terminal-launched testing only — Finder-launched apps don't inherit env).
- Auto-update is out of scope: distribute the zip/.app manually.
