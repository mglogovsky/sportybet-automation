# Mockup — How the Current App Changes

Baseline: the current app is a **single-operator local tool** —
`sportybet_hold_ui.py` (local HTTP server on :8790) serving `hold_ui.html`
in a Chrome `--app` window, driving `sportybet_hold_flow.py` (Playwright →
AdsPower profile → SportyBet). No licensing, no remote signals, no multi-user.

Target: a **licensed, signal-driven product** you sell to many users.

This document maps each part of today's app to what it becomes, with screen
mockups.

---

## 1. The one-paragraph delta

Today the operator manually arms a selection and presses buttons (BEGIN /
FIRE HOLD / INSTA / CASH OUT). In the product version, the app boots into a
**license gate**, then a **live signal feed replaces the operator**: goals,
corners and cards arrive over WSS from your AWS backend, the strategy engine
evaluates them, and the existing execution core (hold flow, insta bet,
cashout) fires automatically under wallet limits. The local UI becomes a
monitoring + control dashboard instead of the thing that drives every step.

---

## 2. What stays, what changes, what's new

| Current piece | Fate | Notes |
|---|---|---|
| `sportybet_hold_flow.py` (80 KB execution engine) | **STAYS — becomes `executor/`** | The hold/insta/cashout logic, bizCode handling, `WsMarketTracker` is exactly the "executor" module from ARCHITECTURE.md. Wrap it, don't rewrite it. |
| `WsMarketTracker` (odds socket listener) | **STAYS + promoted** | Becomes the local market-status feed *and* a safety cross-check against server signals (server says GOAL, local socket should confirm). |
| `hold_ui.html` + `sportybet_hold_ui.py` (localhost:8790 server) | **CHANGES — UI shell kept, screens replaced** | Same Chrome `--app` pattern and 1 Hz `/api/state` polling works fine. Screens redesigned per §4. |
| `ControlBridge` (action queue + state publish) | **STAYS — extended** | New actions: `activate_license`, `pause_strategy`, `signal` (server→engine injection). |
| AdsPower profile picker | **CHANGES** | Moved to Settings; each license can pin a default profile. Still how SportyBet is driven. |
| Manual buttons (BEGIN/REDEEM/REARM/RETRY) | **DEMOTED** | Become an "Advanced / manual override" tray. Auto mode is the default. |
| Settings form (stakes, margins, delays) | **CHANGES** | Split into "Wallet limits" (user) and "Strategy" (server-pushed bundles, user toggles). |
| License module | **NEW** | Activation screen, local hashed token cache, heartbeat, offline grace. |
| Signal subscriber | **NEW** | WSS/MQTT client to AWS IoT Core, signature verify, dedup, replay. |
| Strategy engine | **NEW** | JSON rule packs from server: signal type → conditions → action (insta/hold/skip) + stake rule. |
| Telemetry | **NEW** | Batched bet outcomes + errors → `/v1/telemetry`. |
| AWS backend | **NEW** | Everything in ARCHITECTURE.md §3. |

---

## 3. New process map

```
BEFORE (today):
  operator opens UI → picks profile → START → manually arms selection
    → watches screen → presses FIRE HOLD / INSTA / CASH OUT

AFTER (product):
  app boot → license gate (activate once, then silent)
    → profile auto-loaded, session starts
    → WSS connects to AWS: signals stream in
    → per signal: strategy engine decides → executor fires (hold/insta/cashout)
    → operator watches dashboard, can pause/override/cash-out anytime
```

---

## 4. Screen mockups

### 4.1 NEW — License activation (first run / expired / revoked)

```
┌─────────────────────────────────────────┐
│  ⚽ SPORTYPILOT                    v0.9.0 │
│─────────────────────────────────────────│
│                                         │
│         Activate your license           │
│                                         │
│   ┌─────────────────────────────────┐   │
│   │ SBET-____-____-____             │   │
│   └─────────────────────────────────┘   │
│                                         │
│   [ Activate ]                          │
│                                         │
│   Key is bound to this computer on      │
│   first activation.                     │
│                                         │
│   ✕ Invalid or revoked key              │  ← error state
│   ⠿ Contacting server…                  │  ← loading state
└─────────────────────────────────────────┘
```

On success: token stored as `HMAC(JWT, machine_fingerprint)` (hashed, machine-bound).
On later boots this screen is **skipped entirely** unless the token is expired AND
the server is unreachable past grace, or the key was revoked.

### 4.2 CHANGED — Main dashboard (today's "ready/armed" screen, evolved)

```
┌─────────────────────────────────────────┐
│ ● LIVE   PRO plan · expires in 23d      │  ← license pill (green/amber/red)
│ Wallet ₦ 42,350   Today: +₦ 8,100       │  ← balance + session P/L
│─────────────────────────────────────────│
│ 📡 SIGNALS                    ▮▮▮▮ good │
│  34' ⚽ GOAL   Home 1–0  · Utd–City     │
│  31' 🚩 CORNER Away #7   · Utd–City    │
│  28' 🟨 CARD   Home      · Utd–City    │
│  ─── history scrolls ───                │
│─────────────────────────────────────────│
│ 🤖 AUTO-PILOT            [ ON  | OFF ]  │
│  Strategy: Goal-Rush v3 ✓ (server)      │
│  Next: armed on Utd–City, Over 2.5      │
│─────────────────────────────────────────│
│ LAST BET                                │
│  Over 2.5 @ 1.85 · ₦2,000 · ⏳ parked   │  ← statuses: parked/booked/cashed
│─────────────────────────────────────────│
│ [ ⏸ PAUSE ]  [ 💸 CASH OUT ]  [ ⚙ ]    │
│ ▸ Advanced (manual hold/insta/rearm)    │  ← today's buttons, collapsed
└─────────────────────────────────────────┘
```

What changed vs `hold_ui.html` today:
- Header gains **license pill + signal health** (WSS latency/loss).
- The countdown/timer-centric "ready/armed" middle is replaced by a **signal feed** —
  signals are now the star; the bet flow is a consequence, not the focus.
- Buttons: INSTA/FIRE HOLD/BEGIN collapse into an "Advanced" tray. Top-level actions
  are only **PAUSE** (kill-switch, finishes current round like today's STOP),
  **CASH OUT**, **Settings**.
- `⚡ INSTA READY` badge becomes "Next: armed on …" line driven by strategy state.

### 4.3 NEW — Strategy / wallet settings

```
┌─────────────────────────────────────────┐
│ ⚙ Settings                              │
│─────────────────────────────────────────│
│ Wallet limits (hard stops, always on)   │
│  Max stake/bet      [ ₦ 2,000 ]         │
│  Daily loss stop    [ ₦ 10,000 ]        │
│  Stop after N losses[ 3 ]               │
│─────────────────────────────────────────│
│ Strategies (synced from server ✓ v3)    │
│  ☑ Goal-Rush   GOAL → Over 2.5 (<20')   │
│  ☑ Corner-Press CORNER ≥8 → corners O   │
│  ☐ Card-Hunter  2+ cards → bookings O   │
│  Stake: 5% of wallet, cap ₦2,000        │
│─────────────────────────────────────────│
│ Browser profile: [k1frnp5d ▾]           │  ← today's IDLE picker, moved here
│─────────────────────────────────────────│
│ License SBET-…-9F2K · PRO · [Manage]    │
└─────────────────────────────────────────┘
```

Rule packs are **server-signed JSON** — you ship strategy updates to all users
without an app release; users only toggle and size stakes.

### 4.4 NEW — License states (edge cases)

```
 OFFLINE, token valid:        REVOKED:
 ┌───────────────────────┐    ┌───────────────────────┐
 │ 🟡 OFFLINE MODE        │    │ ⛔ LICENSE REVOKED     │
 │ Signals paused. Bets   │    │ This key was disabled │
 │ disabled until server  │    │ by the provider.      │
 │ reachable. Grace: 2d   │    │ [ Contact support ]   │
 │ [ Retry connection ]   │    └───────────────────────┘
 └───────────────────────┘
```

License logic mirrors ARCHITECTURE.md §4: activate once online → hashed local
cache → heartbeat re-check → revocation ladder (suspend / expiry / token_version /
signal cut).

---

## 5. Module-level change map (file view)

```
TODAY                                   PRODUCT
─────────────────────────────────────────────────────────────
sportybet_hold_ui.py            →      app/server.py          (same pattern, +license middleware)
hold_ui.html                    →      app/ui/index.html      (screens in §4)
sportybet_hold_flow.py          →      engine/executor.py     (wrapped, same core)
  └ ControlBridge               →      engine/bridge.py       (+signal injection, +pause)
  └ WsMarketTracker             →      engine/market.py       (+server-signal cross-check)
sportybet_place.py / _*.py      →      engine/actions/        (kept as executor primitives)
—                               →      licensing/activate.py  (NEW: activate + HMAC cache + heartbeat)
—                               →      signals/subscriber.py  (NEW: WSS/MQTT + verify + dedup + replay)
—                               →      strategy/engine.py     (NEW: JSON rule packs → decisions)
—                               →      telemetry/client.py    (NEW: batched outcome upload)
—                               →      AWS backend            (NEW: API GW + Lambda + DDB + IoT Core)
```

---

## 6. Migration order (smallest-risk path)

1. **License gate in front of today's app** — app won't start without activation;
   everything else untouched. (This is the sellable wrapper.)
2. **Signal subscriber in shadow mode** — feed renders in the dashboard, logs
   decisions, fires nothing. Validate latency + correctness against `WsMarketTracker`.
3. **Auto-pilot behind a flag** — strategy engine drives the existing executor,
   dry-run first, then real stakes with tight wallet caps.
4. **Strategy bundles + telemetry + admin console** — the multi-user machinery.

The executor you already have (hold window, insta, cashout, bizCodes) is the
hardest part to rebuild and it's done — the product work is mostly *around* it,
not inside it.
