# FeedWire - Sporty Bet — User Guide

## What you need

- A Windows PC or Mac
- **AdsPower** installed, with a profile that is **logged in to SportyBet**
- Your **license key** (looks like `SBET-XXXX-XXXX-XXXX`) — you get it from your provider
- Internet connection

---

## First time setup (5 minutes)

1. **Install the app:**
   - *Windows:* run **FeedWire-SportyBet-Setup.exe** and click through the
     installer (it sets up everything the app needs automatically, internet
     required). Then open **FeedWire - Sporty Bet** from the Start Menu or
     the desktop shortcut.
   - *Mac:* unzip the file you received and drag **FeedWire - Sporty Bet**
     into **Applications**.
2. **Turn on the AdsPower API** (once, very important — the app talks to
   AdsPower through it):
   - Open AdsPower → **Settings** (⚙) → **Local API**
   - Switch it **ON / Allow** and keep the default port **50325**
   - If AdsPower asks, restart it after switching this on
   - **Newer AdsPower versions also show an API key on this screen** — copy it,
     then in the app open **⚙ SETTINGS**, paste it into **ADSPOWER API KEY**,
     and press **SAVE SETTINGS**. (Only needed if the app shows a red
     "Require api-key" message; older AdsPower versions don't require it.)
3. **Open AdsPower** and leave it running.
4. **Open the app** — double-click **FeedWire - Sporty Bet**.
   - *Windows:* if you see a blue "Windows protected your PC" screen → click
     **More info → Run anyway** (this appears once).
   - *Mac:* right-click the app → **Open → Open** (this appears once).
5. **Enter your license key** and press **ACTIVATE**.
   - You only do this once. The app remembers it.
   - If it says *"Can't reach the license server"* — check your internet and
     press RETRY.
6. Done — you are on the start screen.

---

## Using the bot

1. **Open AdsPower first.** (Always before the app.)
2. Open **FeedWire - Sporty Bet**.
3. Choose your **profile** from the dropdown and press **▶ START**.
4. A browser window opens with SportyBet.
   - If it asks you to log in — log in **in that browser window**, then press
     **CONTINUE** in the app.
5. In the browser window, **add your bet to the betslip** (click the odds of
   the match you want, like you normally do).
6. **If you see the big red banner "PLACE A ₦10 BET":**
   - Place any small ₦10 bet on any match in the browser window.
   - Wait a few seconds — the app sees it, **clears the betslip for you**,
     and the banner disappears.
   - Now add your real selection to the betslip again.
7. When the app shows **"Target armed"** with your match and balance —
   press **⚡ INSTABET**.
   - If the button is grey, the market is suspended — it turns on by itself
     when the market opens. Just wait.
8. The result appears in a few seconds (**Bet booked ✅**). Press
   **↺ RE-ARM NEXT TARGET** to go again from step 5.

## Stopping

- Press **■ STOP** at the top, or just **close the app window**.
- The app always finishes safely — it never leaves anything half-done.
- Your AdsPower browser stays open. Nothing is lost.

## The colored badge (top of the app)

- 🟢 **green, "Nd left"** — license OK, N days remaining
- 🟡 **yellow** — license expires soon (under 3 days), renew it
- 🔒 **locked** — license expired or revoked; contact your provider

---

## If something goes wrong

| What you see | What to do |
|---|---|
| Profile list is empty / "AdsPower not reachable" | Open AdsPower first. If it still shows, the API is off: AdsPower → **Settings → Local API** → switch ON (port 50325), restart AdsPower, then restart the app |
| Red message "Require api-key (code -1)" | Your AdsPower version requires an API key: AdsPower → **Settings → Local API** → copy the key, then in the app: **⚙ SETTINGS → ADSPOWER API KEY** → paste → **SAVE SETTINGS** |
| "Can't reach the license server" | Check internet, press RETRY |
| "License expired or revoked" | Ask your provider for a new key |
| The ⚡ INSTABET button is grey | Normal — the market is paused; it unlocks itself when the market opens |
| Anything else | Close the app and open it again. Your license and settings are saved. |

## If the installer itself won't start (Windows)

| What you see | What to do |
|---|---|
| "Unable to execute file in the temporary directory. Error 267" | Your Windows TEMP folder is broken (this affects all installers, not just this one). Open Command Prompt and run: `mkdir C:\Temp`, then `set TEMP=C:\Temp`, `set TMP=C:\Temp`, then run the Setup.exe from that same window. For a permanent fix set TEMP/TMP to `C:\Temp` in System Properties → Environment Variables and restart |
| SmartScreen: "Windows protected your PC" | Click **More info → Run anyway** (appears once; the app is unsigned) |
| "Failed to load Python DLL / python312.dll" | You ran the exe from inside a zip. Use the Setup.exe installer instead, or extract the whole zip to a normal folder first |
| App opens then crashes mentioning `Python.Runtime` / `winforms` | Install the [WebView2 Runtime](https://developer.microsoft.com/microsoft-edge/webview2/) (the Setup.exe does this automatically) |

**One rule to remember: AdsPower first, then the app.**
