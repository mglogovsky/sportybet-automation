# SportyPilot packaging

Builds the SportyBet Hold UI (`sportybet_hold_ui.py` + `sportybet_hold_flow.py`
+ `licensing/`) into a distributable **SportyPilot** app via PyInstaller.

## Prerequisites (build machine)

- Python 3.10+ with the app's runtime deps already installed
  (`requests`, `playwright` — same env you run the app from today).
- `adspower.py` — **vendored in this repo** (repo root), so no external source
  checkout is needed at build or run time. It only depends on `requests`.
- PyInstaller (the build scripts install it via pip).

## Prerequisites (end user machine)

- **AdsPower installed and running** with a local API on its default port —
  the app drives the AdsPower browser over CDP; no browser is bundled.
- **Google Chrome** (for the `--app` window).
- Environment variables (set by the admin before handoff / via a wrapper):
  - `LICENSE_SERVER_URL` — default `https://localhost:8443` (placeholder!)
  - `LICENSE_API_TOKEN` — bearer token minted on the betradar-clone server;
    may be empty (client still sends the header).
- A valid license key (`SBET-XXXX-XXXX-XXXX`), entered on first run.

## Build

```sh
# macOS (on a Mac)
packaging/build_macos.sh        # → dist/SportyPilot.app + SportyPilot-macos.zip

# Windows (on Windows)
powershell -ExecutionPolicy Bypass -File packaging\build_windows.ps1
#   → dist\SportyPilot\ + SportyPilot-windows.zip
```

PyInstaller does **not** cross-compile: build each artifact on its own OS.

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
- Auto-update is out of scope: distribute the zip/.app manually.
