# SportyBet (NG) automation

Everything SportyBet-specific lives here. It sits inside `epicbet-api/` and depends on
`../adspower.py` (AdsPower CDP client) — every script inserts the parent dir on `sys.path`
so it runs from this subfolder.

Two automation surfaces:
1. **Sports bet placement** — `POST /api/ng/orders/order`, AES-128-CBC encrypted.
2. **Turbo Mines (Hub88)** — plain HTTP POSTs to `turbomines.turbogfast.xyz`.

The shared goal is the *balance-starving hold*: place the sports bet for the full balance,
lock part of it in a Mines round, then cash out to settle — exactly like the Betnacional
`mines_hold_flow` (in `../`).

---

## Layout

| File | Role |
|---|---|
| `sportybet_hold_flow.py` | **The balance-starving hold loop** — arms by listening for your minimal UI bet (mints the key), then fires full-balance bets into SUSPENDED markets (the parked-order window) while holding a Mines round. ENTER redeems (bet settles accepted); otherwise the bet starves (4200) and loops. Ctrl+C settles cleanly. Mines rounds run headless via `requests`. A `WsMarketTracker` reads the live market status off the odds socket (REST fallback) and the pre-fire gate waits for a suspension (`--market-wait`) so a fire never books naked; if one still does, the bet is auto-cashed-out. UI extras: ⚡ INSTA BET (full-balance plain bet while the market is OPEN) and 💸 CASH OUT BET. Circuit breaker pauses after 3 consecutive non-starved failures; after a booking it offers to re-arm the next target in the same session. |
| `sportybet_hold_ui.py` + `hold_ui.html` | **The local app** (port 8790): profile picker, live header badge (`⚡ INSTA READY` / `INSTA off · suspended`), INSTA BET / FIRE HOLD buttons, redeem + cashout actions, cycle history. |
| `sportybet_place.py` | **Place a sports bet** via `window.originFetch`, AES-encrypting the payload with the live `ng_transId` key. This is the working path. |
| `sportybet_mines.py` | Launch Turbo Mines + drive rounds (`games/create`, `bets/place`, `bets/cashout`). |
| `intercept_sportybet.py` | Capture all SportyBet HTTP/WS traffic from an AdsPower profile to a JSONL file (the "observe a real bet" interceptor). |
| `sportybet-har-analysis.md` | Deep analysis of the bet-placement architecture, encryption scheme, and 22-endpoint cipher scope. |
| `sportybet-methodology.md` | The full A-to-Z methodology: recon → protocol reversing → the market-status delay model → the hold loop → the 3-button UI. |
| `session-handoff-2026-08-28.md` | Session handoff: what's built, what's verified, next steps, money-safety notes. |
| `sportybet-mines-flow.md` | Live-capture analysis of the Mines launch chain + round transport + the "no tab needed for rounds" finding. |
| `README.md` | This file — the practical, current how-to. |
| `_wait_key_place.py` | Wait for the user to place a bet (which lazy-mints a fresh cipher key), then reuse it to place a duplicate. |
| `_place_10000_ui.py` | UI-driven placement: set stake + click Book Bet. |
| `_click_winner_home.py`, `_read_odds.py`, `_check_bet.py`, `_probe_sporty_slip.py`, `_probe_fetch.py` | One-shot probes used to reverse-engineer the flow. |
| `sportybet_capture*.jsonl`, `sportybet_bet_capture.jsonl`, `sportybet_dupe_capture.jsonl`, `sportybet_place_capture.jsonl` | Captured HTTP/WS traffic (data). |

---

## The bet-placement procedure (current, working)

### Phase 0 — the cipher key

The key lives in the **`ng_transId`** storage key (per-country name: `${country}_transId`, i.e.
`ng_transId` for Nigeria). It is **lazy-minted**: cleared/expired cache fires nothing by
itself; a fresh key is minted on the **next money-flow call** (place a bet, edit a bet,
open cashout, deposit/withdraw/transfer). Reloading the page does **not** re-mint it.

```js
const c = JSON.parse(sessionStorage.CIPHER_AES_KEY || "null"); // key name is ng_transId
if (c?.key && c?.transId && c.date > Date.now()) {
  key = Base64.parse(decodeURIComponent(c.key));               // reuse
} else {
  key = WordArray.random(16);                                  // mint
  wrapped = wrap(cipher.encrypt("password=" + encodeURIComponent(base64(key))));
  // POST /base/cipher {body: wrapped}  →  { bizCode:10000, data:{ transId } }
  storage.CIPHER_AES_KEY = { transId, key: encodeURIComponent(base64(key)), date: now+1h };
}
```

`transId` is the **request header** `transid`; the key never crosses the wire (client-minted).

### Phase 1 — read the slip + key (in the browser)

- **Selection** → `localStorage.betslips` (array; each has `eventId`, `sportId`,
  `marketInfo.id/desc`, `outcomeInfo.id/odds/probability`, and optional `specifier`).
- **Key** → `ng_transId` in `localStorage` **or** `sessionStorage` (location is
  country/session dependent — read whichever is present).

### Phase 2 — build + encrypt + send

Payload (money is **integer ×10000**: `10000000` = NGN 1000.00):

```json
{"bizType":1,"ticket":{"selections":[{
   "eventId":"sr:match:...",
   "id":"uof:1/<sportId>/<marketId>/<outcomeId>",   // + "?specifier=..." if the market has one
   "odds":"<live odds string>","banker":false,"probability":<float>}],
  "bets":[{"selectedSystems":[1],"stake":{"value":<stakeUnits>}}]},
 "orderType":1,"paymentType":0,"isBonusFactor":false,
 "subBizType":2,"actualPayAmount":<stakeUnits>,"loadingShareCode":""}
```

Specifier gotcha (proven 2026-08-27): the betslip may store the specifier **glued into
`marketInfo.id`** (`"202?setnr=2"` on tennis set markets) instead of a separate field. The id
must be rebuilt as `uof:1/<sport>/<market>/<outcome>?<specifier>` — a specifier left mid-path
(or doubled) makes the server instant-reject every order with `19999`. Non-specifier markets
(1X2/Winner, id 1/186) never hit this, which is why it stayed hidden until tennis.

Encrypt with **AES-128-CBC / PKCS7**, random 16-byte IV prepended, whole thing base64:

```
body = base64( iv ‖ AES_CBC_encrypt(plaintext, key, iv, Pkcs7) )
```

Send **via `window.originFetch`** (this is the crucial detail):

```js
const resp = await window.originFetch('/api/ng/orders/order', {
  method: 'POST',
  headers: { "content-type":"application/json;charset=UTF-8",
             "clientid":"web", "platform":"web", "operid":"2", "transid":<transId> },
  credentials: 'include',
  body: <encrypted>,
});
```

**Why `originFetch` and not alternatives** (all tested live):

| Caller | Result |
|---|---|
| `window.originFetch` | ✅ Real standard `Response`, `200` + encrypted JSON. **Use this.** |
| `window.fetch` | ❌ Faro-instrumented wrapper — returns a custom object and throws a WAF challenge (`Page.evaluate: SyntaxError: Sorry, something went wrong`) on eval-context calls. |
| `context.request` (Playwright APIRequestContext) | ❌ CloudFront returns a `202` challenge with empty body (treated as a bot), even for a simple balance GET. |
| in-page `fetch` with a **full URL** | ❌ SPA wrapper doubles the path (`/api/ng//www.sportybet.com/api/ng/orders/order`) + duplicates `clientid` (`"web, web"`) → `404`. |
| in-page `fetch` with a **relative path** | ❌ Resolves against the current SPA route and returns a non-standard object (`Page.evaluate: Object`). |

### Phase 3 — read the result

The response is also encrypted. Decrypt with the same key (IV = first 16 bytes):

```
plain = AES_CBC_decrypt(base64_decode(resp.body)[16:], key, iv=first16, Pkcs7)
```

Verdicts (`bizCode`):
- `10000` → accepted (response carries `orderId`, `totalStake`, `potentialWinnings`).
- `4200` → *"balance is not enough"* — **the starved-bet verdict** (proven 2026-08-27).
  The server's balance check happens at **settle time** (end of the in-play window), so
  a Mines hold placed during the window starves the bet. The response carries
  `data.balance` (server truth, ×10000) — use it to re-sync.
- `4801` → *"Stakes exceeds the limit"* — stake over the market's max (low-odds markets often cap well below 1000).
- `4510` → *"Odds not acceptable, please try again"* — the odds moved during the in-play window (`selection changed`).
- `19004` → *"Your request is already being processed"* — the previous order still holds the
  server's processing lock. The hold flow paces cycles (wallet settle + `--cycle-delay`) to stay clear of it.
- `19999` → *"Sorry, something went wrong"* with `isAvailable:false` — the market is
  dead/suspended. Instant (~0.3s), not a starve; the hold flow pauses after 3 consecutive repeats.
- `19414` → `UNABLE_TO_DECRYPT_BY_CIPHER` — drop the cached key, re-mint (Phase 0), retry.

Placement is **synchronous**; whether the verdict is instant or held depends on
the **market status at submit time** (HAR + odds-socket decoded 2026-08-28):

- **Market OPEN (`marketStatus 0`)** → the server answers immediately (~0.3s,
  even the rejects). There is no window — a scripted fire (which bypasses the
  client-side ~13s UI countdown) books **NAKED** before a mines hold could
  exist.
- **Market SUSPENDED (`marketStatus 1/2`)** → the server **parks the order**
  until the market re-opens/re-prices, then verdicts (observed 6.3–15.5s —
  e.g. 1X2 @8.75 on Austria Wien v Braga, and tennis mid-point). That parked
  interval **is** the window the mines hold needs.

Status is read live from the SPA's own odds socket (`alive-*.sportybet.com`,
Engine.IO `42["data",…]` frames, topic `…^odds` carries `marketStatus`; the
`…^betStatus`/`…^status` topics carry the event-level `eventStatus`). The hold
flow tracks it in `WsMarketTracker` (REST `factsCenter/Outcomes` as fallback),
**waits for a suspension before every fire** (`--market-wait`, default 30s),
and the UI's armed screen shows the state and offers **⚡ INSTA BET** (full
balance, plain bet — enabled while OPEN) vs **🎯 FIRE HOLD** (the loop). If a
fire still books naked (race), the flow **auto-cashes-out** the accepted bet
via `realSportsGame/cashAbleBets` → `cashAbleBet` → `cashOut` (AES-encrypted
like orders; bizCode 32000 = price moved → re-quote) — also available as a
manual **💸 CASH OUT BET** button on the booked/naked screens. Two ordering
rules the hold flow is built on (both proven live 2026-08-27):

- **BET FIRST, MINES SECOND.** The server's **fire-time balance check** requires
  `balance ≥ stake` when the order enters processing — arming the Mines hold first makes
  every cycle die at this check in 0.3s. The bet must enter at (near) full balance; the
  Mines hold then locks inside the window and starves it at settle time.
- **The session's first order settles instantly (~0.5s)** — it would race the Mines place
  and can book a naked full-balance bet (observed: instant accept → wallet to ₦5 → Mines
  place `422`). The flow therefore fires a **minimal warm-up bet** (`--warmup-stake`,
  default 10) once per round to consume that instant order; later orders get the full delay.

Also: when odds move, the slip parks the change behind an **"Accept Changes"** prompt —
until clicked, `localStorage.betslips` keeps stale odds. The flow clicks it automatically
while arming and before every cycle (seen in the SPA's own analytics as
`betslip__accept_change__click`).

Balance is re-read from the server after a bet; `realSportsGame/cashAbleBets/count` increments to
confirm the ticket.

**The redeem margin (proven 2026-08-27).** A first-cell instant Mines cashout pays only
~0.99× (25-grid/1-mine; 0.97× on 49, 0.96× on 81) — a ~1% fee. A *full*-balance bet +
redeem therefore still starves by exactly the fee. The hold flow stakes
`balance − margin` (default 5 NGN): while holding, the server sees `balance −
mines_stake < stake` → 4200; after redeeming, it sees `balance − fee ≥ stake` → accepted.

---

## Usage

```bash
cd epicbet-api/sportybet

# The hold loop: arm it, then add a selection + place a minimal (10 NGN) bet
# in the UI — the script captures key + slip and loops on Enter.
python3 sportybet_hold_flow.py --adspower k1frnp5d

# Place a bet from the current betslip selection (reads live key + odds).
python3 sportybet_place.py --adspower k1frnp5d --event-id sr:match:... --stake 1000

# Wait for a manual bet (mints fresh key), then place a duplicate with that key.
python3 _wait_key_place.py

# Drive a Turbo Mines round (create/place/cashout).
python3 sportybet_mines.py --adspower k1frnp5d --stake 50 --mines 1 --cashout

# Capture real SportyBet traffic to a JSONL while the operator plays.
python3 intercept_sportybet.py --adspower k1frnp5d --out cap.jsonl
```

`--adspower` is the AdsPower profile id/name (here `k1frnp5d` = #39, the operator's live NG
SportyBet account — its storage persists across restarts. **Do NOT use `k1frnp5c` = #38: it
clears storage on close and only ever holds stale slip ghosts.**)
`sportybet_place.py --event-id` must match a selection currently on the live slip.

---

## Mines (Hub88 Turbo Mines) — the round API

Host `turbomines.turbogfast.xyz/api`, auth `Authorization: <JWT>` + `apikey: <id>` +
`subpartnerid: SportyBet NG`. Plain JSON, no WebSocket, no canvas.

| Action | Endpoint | Body |
|---|---|---|
| Launch | `common/profile` | `{token, cid, gameId, visitorId, subPartnerId}` → `{token:<JWT>, id, balance}` |
| Start round | `games/create` | `{clientSeed, nonce:1, size:<mines>, deskSize:25, theme}` → `{roundId}` |
| Open cell | `bets/place` | first call adds `{amount, currency}`; later `{roundId, index}` |
| Cash out | `bets/cashout` | `{gameId}` |

- The **game tab is only needed to obtain the JWT** (the launch URL is behind an AWS-WAF
  challenge that only real navigation passes). **Rounds run headless via `requests`** with
  the tab closed — see `sportybet-mines-flow.md` §9.
- `index = row*5+col`, `deskSize=25`. Money is **whole NGN units** (`amount`), not ×10000.

---

## Sports cashout (decoded 2026-08-27, not scripted)

Fully captured live (`sportybet_hold_capture2.jsonl`):

1. `GET /api/ng/realSportsGame/cashAbleBet?betId=<betId>&integrity=full` — **plain JSON**
   (no encryption). The nested `data.cashOut` carries `betId`, `coefficient`,
   **`maxCashOutAmount`** (the offered price), `availableStake`, `isSupportPartial`.
2. `POST /api/ng/realSportsGame/cashOut` — AES-encrypted like `orders/order`:
   `{"betId": "...", "usedStake": "<availableStake>", "isPartial": false,
     "amount": "<maxCashOutAmount>"}` → `{"bizCode":10000,"data":{}}`.
3. Bet status without decryption: `GET /api/ng/orders/order/v2/realbetlist?isSettled=10&pageSize=5&pageNo=1`
   → per-bet `winningStatus`, `isSettled`, per-selection `status`/`eventStatus`.

---

## Key facts

| Thing | Value |
|---|---|
| Bet endpoint | `POST /api/ng/orders/order` (synchronous, ~13.4s in-play delay) |
| Encryption | AES-128-CBC / PKCS7, random IV prepended, base64; both request and response |
| Cipher key | `ng_transId` (localStorage **or** sessionStorage), lazy-minted, 1h TTL |
| `transid` header | the cipher session handle from `ng_transId` |
| Money | integer ×10000 for sports bets; whole NGN units for Mines `amount` |
| Mines game | Hub88 `tbg_turbomines`, host `turbomines.turbogfast.xyz`, plain HTTPS, no WS |
| Mines auth | JWT from `common/profile`; rounds are headless HTTP |

See `sportybet-har-analysis.md` for the full architecture/encryption analysis and
`sportybet-mines-flow.md` for the Mines launch + round details.
