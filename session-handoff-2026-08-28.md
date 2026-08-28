# Session handoff — SportyBet hold flow (2026-08-28)

## Where we are
The hold flow is now **market-status driven**: it knows live whether a bet would
verdict instantly (market OPEN, status 0) or get parked (suspended, status 1/2 =
the mines-hold window), and the UI exposes both modes as buttons. All code is
written, compiled, reviewed twice, and the server is running the new build.

## Environment
- Dir: `epicbet-api/sportybet` (repo root: `unified-listener-and-cashout`).
- UI server: `python3 sportybet_hold_ui.py --no-window` on **http://127.0.0.1:8790**,
  log `/tmp/sportybet_hold_ui.log`. **Currently running** (started 2026-08-28,
  state `idle`). The Chrome app window recovers on its own after restarts (1 Hz poll).
- AdsPower profile: `k1frnp5d` (`.adspower_profile`); CDP via `../adspower.py`.
- HAR decoder: `/var/folders/jr/vclmcj6n72z_8xqzg6y41flc0000gn/T/opencode/decode_har_orders.py`.

## The model (what decides instant vs held)
- UI's ~13s countdown is **client-side** — scripted fires (`window.originFetch`)
  bypass it.
- Server verdict = **market status at submit**: `marketStatus 0` (open) → instant
  (~0.3s, accepts and rejects); `1/2` (suspended) → order parked until re-open
  (6–15.5s) = **the window**. Proven across HAR1/HAR2 + soccer/tennis captures.
- Status sources: odds socket `alive-*.sportybet.com` (`42["data",…]` frames,
  `…^odds` → marketStatus, `…^betStatus`/`…^status` → eventStatus) via
  `WsMarketTracker`; REST fallback `factsCenter/Outcomes` (`market_status()`).
- Settle-time balance check: a debit inside the parked window starves the bet →
  `4200`. bizCodes: 10000 accept / 4200 balance / 4510 odds / 19004 lock /
  19999 dead / 19414 & -2 key / 32000 cashout-price-moved.

## Done this session
1. **`WsMarketTracker`** — WS listener on the sports tab; tracks `mid|spec` →
   (status, ts), 10s freshness; publishes `market_status` to the bridge. First 5
   odds frames are logged for shape validation (our own captures never recorded
   the socket; the parser shape comes from the external `ask.har` analysis).
2. **Pre-fire gate** in `run_cycle` — OPEN market → wait up to `--market-wait`
   (default 30s) for a suspension (stop/rearm-aware via `_drain()`), else skip
   cycle as `unavailable` (circuit breaker pauses after 3).
3. **⚡ INSTA BET** (`insta_bet`) — one plain **full-balance** order (re-reads the
   wallet first), no mines/loop; accepted → booked screen, remembers `last_order`.
4. **💸 CASH OUT BET** (`booked_gate` + `sports_cashout`) — manual cashout of the
   remembered order on booked/naked screens; repeatable. Auto-cashout on naked
   accepts was already wired into both accept paths of `run_cycle`.
5. **UI** — header badge `⚡ INSTA READY` / `INSTA off · suspended`; ready screen
   with market line + INSTA (enabled when open) / FIRE HOLD (lit when suspended);
   `insta` state + pill; `cashed` row on booked/naked; actions `insta` +
   `cashout_bet` in `sportybet_hold_ui.py`.
6. **Review fixes** — removed a gate-less `state="ready"` republish that could
   queue an unintended INSTA into the next gate; insta re-reads balance.
7. **Docs** — `README.md` delay model rewritten (market-status model);
   `sportybet-methodology.md` (full A-to-Z write-up) added.

## Verified
`py_compile` both py files; `node --check` the inline JS; WS tracker unit test
against the exact `ask.har` frame + 3 payload shapes (parse/watch/publish/
garbage); action-allowlist + signature checks; state↔consumer audit (every
button-state has an active drainer).

## Not yet done / next steps
1. **Live smoke**: arm a selection, confirm the header badge tracks a real
   suspension flip; read the `ws odds frame:` log lines to confirm the parser
   matches the live socket shape (REST fallback covers if not).
2. First real INSTA / FIRE HOLD runs with the new gate.
3. Consider exposing `--market-wait` in the UI settings form (currently default 30).
4. `intercept_sportybet.py` never captured WS frames on our tabs — if the tracker
   stays blind, check the interceptor's WS hook (separate issue from the flow).

## Money-safety notes
- INSTA BET stakes the **full wallet** — real money, instant book, by design.
- Auto-cashout recovers ~99% on naked accepts; manual 💸 is the same path.
- STOP = finish round + mines cashout, never abort mid-flight.
