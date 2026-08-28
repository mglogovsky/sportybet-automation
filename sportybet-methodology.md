# SportyBet — full methodology, A to Z

## 1. The goal
Exploit the gap between **bet placement** and **bet settlement** on sportybet.com (NG): fire a full-balance sports bet, but control *whether it settles* — hold the balance hostage with a second product (Turbo Mines) so the bet either starves (free loop) or settles on command (redeem).

## 2. Recon — capturing the truth
- **AdsPower profile** (`k1frnp5d`) with a real logged-in session; driven over CDP via `adspower.py` (Playwright `connect_over_cdp`).
- **`intercept_sportybet.py`** — full-network tap (requests, responses, WS) to JSONL.
- **HAR captures** of real UI betting (`ask.har`, `nechapem again.har`, HAR1/HAR2) — real user bets, so we see the *intended* flow.
- **`decode_har_orders.py`** — offline HAR decryptor: pulls the live cipher key from AdsPower storage and decrypts captured order request/response bodies.

## 3. Protocol reversing
- **Cipher**: order/cashout bodies are AES-encrypted; key lives in `localStorage`/`sessionStorage` (`ng_transId` / `CIPHER_AES_KEY`), **1h TTL**, rotates silently (bizCode `-2` = our decrypt fails → re-read key; `19414` = server rejected it).
- **Transport**: `window.originFetch` (not `window.fetch` — that one is Faro-instrumented and WAF-challenges eval calls). Fire-and-forget into `window.__sportyBetResult`, poll for the promise to land.
- **Endpoints decoded**: `orders/order`, `pocket/v1/finAccs/...userBal/NGN` (balance, ×10000 units), `factsCenter/Outcomes` (plain-JSON market status), `realSportsGame/cashAbleBets` → `cashAbleBet?integrity=full` (quote) → `cashOut` (AES POST; `32000` = price moved → re-quote).

## 4. The core discovery — what "delay" really is
Two layers, initially conflated, then separated by HAR decoding:
- **Client-side**: the UI's ~13s countdown before `orders/order` even fires. Scripted fires bypass it entirely.
- **Server-side**: verdict timing = **market status at submit** (the unified model, proven across soccer/tennis captures + ours):
  - `marketStatus 0` (OPEN) → **instant verdict ~0.3s** (accepts *and* rejects — balance check trips first at 92ms).
  - `marketStatus 1/2` (suspended) → server **parks the order** until re-open/re-price → 6–15.5s. **That parked interval is the exploitable window.**
- Status is pushed live on the SPA's own odds socket: `alive-*.sportybet.com`, Engine.IO `42["data",{topic, body(base64)}]`; `…^odds` carries `marketStatus`, `…^betStatus`/`…^status` carry `eventStatus`.
- **Settle-time balance check** (the exploit's hinge): the server checks `balance ≥ stake` when the order *enters* processing, and again effectively at settle — so a debit *during* the parked window starves the bet into `4200 "balance is not enough"`.

## 5. The exploit loop (balance-starving hold)
1. **Bet first** at full balance (fire-time check demands it — hold-first died in 0.3s, proven live).
2. **Mines second**: headless Turbo Mines round (JWT grabbed once, game tab closed; `games/create` + `bets/place` via plain `requests`) locks `mines_stake` inside the window.
3. Outcomes: mine hit → 4200 anyway; safe cell → **ENTER redeems** (cashout mines → bet settles accepted, ~1% fee) or do nothing → **4200 starve**, recover the mines cashout, loop.
4. **Redeem margin** (₦5): stake = balance − margin so the redeem path covers the first-cell fee.
5. Hardening learned live: warm-up bet consumes the always-instant first order; `--pre-fire-gap` 8s after betslip switches; wallet-settle re-sync after every cycle (4200's `data.balance` lags); circuit breaker pauses after 3 identical non-starve failures; `--precheck` defaulted off (₦10 probes are unsound both ways).

## 6. Real-time status → the 3-button UI
- **`WsMarketTracker`** parses `^odds` frames for the armed `marketId|specifier` (REST `Outcomes` fallback, 10s freshness), publishes to the UI at 1 Hz.
- **Pre-fire gate**: never fire into an OPEN market — wait up to `--market-wait` (30s) for a suspension, else skip the cycle.
- **UI** (`sportybet_hold_ui.py` + `hold_ui.html`, port 8790): persistent header badge (`⚡ INSTA READY` / `INSTA off · suspended`), and on the armed screen:
  - **⚡ INSTA BET** — full balance, plain bet, enabled when OPEN (deliberate instant book);
  - **🎯 FIRE HOLD** — the loop above, lit when suspended;
  - **⬇ CASH OUT & SETTLE** (while holding) / **💸 CASH OUT BET** (after a booking, via the reversed cashout API).

## 7. Safety nets
- **Auto-cashout** on any naked accept (bet booked without the hold): find on `cashAbleBets` → quote → `cashOut`, recover ~99%.
- STOP = finish round + cash out mines, never abort mid-flight; Ctrl+C settles in-flight verdicts; rearm/stop flags drained at every gate and wait.

## 8. Validation culture
Every claim tied to a capture: HAR-decoded verdict timings, live probes (₦10 warm-ups, real full-balance fires on Austria Wien/Partizan/San Antonio), bizCode semantics from the app bundle (`4200` balance, `4210` gift, `4510` odds, `19004` lock, `19999` dead market), unit tests for the WS parser against the exact wire shape, `py_compile` + `node --check` before every restart.

---

**In one sentence**: we turned the server's own market-suspension parking into a timing oracle — fire full-balance into a parked market, hold the balance with Mines, and let the settle-time balance check decide the bet's fate, with the reversed cashout API as the undo button.
