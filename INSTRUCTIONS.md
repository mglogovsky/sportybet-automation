# FeedWire - Sporty Bet — Setup & Usage

The app is a small native window (no browser needed) that drives your
SportyBet session through an AdsPower profile. This guide covers installation
and daily use on **macOS** and **Windows**.

---

## What you need before installing (both platforms)

1. **AdsPower** installed and running, with its local API on the default port
   (`http://127.0.0.1:50325`). The app drives the AdsPower browser — it does
   not include one.
2. A SportyBet account, logged in inside your AdsPower profile.
3. A **license key** (`SBET-XXXX-XXXX-XXXX`) from your provider.

---

## macOS

### Install

1. Unzip `FeedWire-SportyBet-macos.zip`.
2. Drag **FeedWire - Sporty Bet.app** into `/Applications` (this also makes it
   appear in Launchpad).
3. **First launch:** macOS Gatekeeper will warn because the app is not
   notarized. Right-click the app → **Open** → **Open** in the dialog.
   You only need to do this once; afterwards it opens normally.

### Uninstall

Delete the app from `/Applications`. Your license key and settings live in
`~/Library/Application Support/SportyPilot/` — delete that folder too if you
want a full reset.

---

## Windows

### Install

1. Run **FeedWire-SportyBet-Setup.exe**. The installer automatically installs
   the **Microsoft Edge WebView2 Runtime** and other components the app needs
   (internet required on first install; you may see one admin prompt).
2. **First launch:** Windows SmartScreen may warn ("Windows protected your
   PC") because the app is unsigned. Click **More info** → **Run anyway**.
3. Launch from the Start Menu or the desktop shortcut.

> Only if you were given the raw `.zip` instead: unzip it to a permanent
> local folder first (e.g. `C:\Apps\FeedWire - Sporty Bet` — **not** inside
> OneDrive, and never run the exe from inside the zip), then make sure the
> [WebView2 Runtime](https://developer.microsoft.com/microsoft-edge/webview2/)
> is installed before launching.

### Uninstall

Settings → Apps → **FeedWire - Sporty Bet** → Uninstall. Your license key and
settings live in `%APPDATA%\SportyPilot\` — delete that too for a full reset.

---

## First run (both platforms)

1. Start **AdsPower** first.
2. Open the app. A small window appears titled *FeedWire - Sporty Bet*.
3. On the very first run it asks for your **license key** — paste it and press
   **ACTIVATE**. The key is checked against the license server
   (`https://feed-wire.pro`) and stored locally; you won't be asked again
   unless the key expires or is revoked.
4. Pick your **AdsPower profile** from the dropdown and press **▶ START**.

The header shows your license status: 🟢 days left, 🟡 under 3 days,
🔒 locked. If it locks, the app stops gracefully (never mid-round) and shows
a RE-CHECK button.

## Daily use

1. Press **▶ START** with your profile selected. The app's browser window
   opens SportyBet.
2. If asked, **log in** in the AdsPower window, then press CONTINUE.
3. **Add a selection** to your betslip in the AdsPower window.
4. If the red **⚠ ACTION NEEDED — PLACE A ₦10 BET** banner shows: place any
   ₦10 bet on any match in the AdsPower window (this refreshes your tokens).
   The app detects it and **clears the betslip automatically** — then just
   add your real selection.
5. Once armed, the card shows your selection and balance:
   - **⚡ INSTABET** — fires a full-balance bet that verdicts instantly
     (enabled only while the market is OPEN).
   - **↺ CHANGE SELECTION** — back to the betslip step.
6. **■ STOP** (top bar) finishes the current round, cashes out the mines
   hold, and returns to idle. Closing the app window does the same graceful
   shutdown — never a hard abort.

## Troubleshooting

| Symptom | Fix |
|---|---|
| Profile dropdown says "AdsPower not reachable" | Start AdsPower; check ⚙ SETTINGS → ADSPOWER API BASE is `http://127.0.0.1:50325` |
| "Can't reach the license server" | Check your internet; press RETRY. The app needs to reach `feed-wire.pro` |
| "License expired or revoked" | Contact your provider for a new key (🔒 screen → CHANGE KEY) |
| Window didn't open / port busy | Change the port in ⚙ SETTINGS (UI PORT), restart the app |

## Settings file (advanced)

`~/Library/Application Support/SportyPilot/config.json` (macOS) /
`%APPDATA%\SportyPilot\config.json` (Windows):
`adspower_api_base`, `adspower_api_token`, `ui_port`. Editable from the app's
⚙ SETTINGS panel or by hand (port changes need a restart).

---

## Building from source (developers)

PyInstaller does **not** cross-compile — build each artifact on its own OS.

```bash
pip install -r requirements.txt   # playwright, requests, pycryptodome, pywebview

# macOS (on a Mac) → dist/FeedWire - Sporty Bet.app + FeedWire-SportyBet-macos.zip
PYTHON=$(which python3) packaging/build_macos.sh

# Windows (on Windows) → dist\FeedWire - Sporty Bet\ + FeedWire-SportyBet-windows.zip
powershell -ExecutionPolicy Bypass -File packaging\build_windows.ps1
```

- macOS builds are ad-hoc signed (beta). Real distribution needs a Developer
  ID certificate + notarization, or users must right-click → Open.
- Windows builds are unsigned (SmartScreen warning). Use an EV code-signing
  certificate for production.
- Icons: `packaging/FeedWire.icns` (macOS) / `FeedWire.ico` (Windows),
  regenerate from `packaging/icon-src.png` (see `packaging/README.md`).
- The license server URL is baked at build time (`build_config.py`, default
  `https://feed-wire.pro`; override with the `LICENSE_SERVER_URL` env var for
  testing — note that a double-clicked app does **not** inherit shell env vars).
