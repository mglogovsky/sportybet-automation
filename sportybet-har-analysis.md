# SportyBet (NG) — Architecture & Bet Placement: HAR Analysis

**Analysed:** 2026-08-25
**Capture:** `www.sportybet.com.har` — 2026-08-24 21:54:20.779 → 21:55:11.492 UTC, 544 entries, 35 MB, **unfiltered**
**Earlier partial capture:** same filename, Fetch/XHR-only, 21:37:39 → 21:38:45, 158 entries — used for cross-checking
**Subject:** user `13139274`, Nigeria site (`ng`), desktop Chrome 151 / macOS, live tennis

---

## 0. Note on the first pass

An earlier analysis was run against a Fetch/XHR-filtered export of this session. Three conclusions
from it were wrong and are corrected here:

| Earlier claim | Correction |
|---|---|
| "No WebSocket — HTTP polling only" | There **is** a Socket.IO WebSocket carrying live odds (§5) |
| "Cannot determine the encryption scheme" | Fully recovered from the app bundle (§4) |
| "No balance refetch after the bet" | There is one, 2.9 s after the order returns (§6) |

The lesson is generic: a Fetch/XHR filter hides WebSockets and all script bodies, which is exactly
where the interesting mechanics live.

---

## 1. Executive summary

| # | Finding |
|---|---|
| 1 | Bet placement is **one synchronous POST** — `/api/ng/orders/order`. No ticket, no polling. |
| 2 | That endpoint — **and only that endpoint** — has **AES-128-CBC application-layer encryption** on both request and response, on top of TLS. |
| 3 | The scheme is **anti-automation obfuscation, not confidentiality**: the key is minted client-side with `Math.random()`, and the same bet data flows in **plaintext** through `factsCenter/Outcomes` seconds earlier. |
| 4 | The full bet is reconstructable without the key: **NGN 100 @ 1.55**, outcome 1714 "Home (+3.5)", accepted, wallet debited 21:55:03.597. |
| 5 | The order takes **13.397 s** — and **13.414 s** in the other capture. A 17 ms spread across two runs points at a deliberate **in-play bet delay**, not server slowness. |
| 6 | Live odds arrive over a **Socket.IO WebSocket** with a `^`-delimited topic scheme, and each push carries the **implied probability** alongside the price. |
| 7 | Money is integer fixed-point **×10000** throughout. |

---

## 2. Architecture

### 2.1 Country-scoped microservice gateway

Every call is `/api/{country}/{service}/...`, behind CloudFront → nginx.

| Service | Endpoints observed |
|---|---|
| `factsCenter` | `orderedSportList`, `sportList`, `event`, `marketGroups`, `Outcomes`, `banned/events`, `flexiblebet/v2/getOddsKey` |
| `orders` | **`order`** (placement), `config/cutbet` |
| `pocket` | `wallet/assetsInfo`, `finAccs/finAcc/userBal/NGN` |
| `patron` | `account/info` |
| `promotion` | `v1/gifts/query`, `v1/sp/query`, `v2/bonus/plans/valid`, `v1/loyalty/betting/streak/status` |
| `realSportsGame` | `cashAbleBets/count` |
| `sportySim` | `v1/config/overall` |
| `games` | `lobby/v2/games/*`, `games-common/v1/notification/get` |
| `inbox` | `v1/hasAnyUnreadMessages` |
| `common` | `config/query` |

### 2.2 Response envelope

```json
{"bizCode": 10000, "message": "Success", "data": { … }}
```

`10000` = OK. Services behind one particular gateway tier **also mirror it into a `biz-code`
response header** — which turns out to leak the encrypted endpoint's status (§4.4).

### 2.3 CMS is a separate layer

`/ng/m/cms/pages/export/{key}` and `POST /ng/m/cms/pages/getPages` — ~30 calls at boot
(`component_betslip`, `page_login`, `odds_format`, `bet_builder`, `common_dates` …). UI copy and
i18n are fully decoupled from the API.

### 2.4 Auth

Cookie-based, not bearer headers:

```
accessToken=patron:id:accesstoken:<32hex><b64>
refreshToken=patron:id:refreshtoken:<32hex>
deviceId=260824084559bdid78434471
userId=V240512124805puid63894197
userCert=350   sb_country=ng   user_segment=D1
```

Custom headers on every API call: `clientid: web`, `platform: web`, `operid: 2`, and
`transid: <cipher session handle>` (§4.2).

Geo headers come back on responses: `request-country: ng`, `current-country: SK`.

### 2.5 Hosts

| Host | Role |
|---|---|
| `www.sportybet.com` | API + documents |
| `s.sporty.net` | Static assets, JS bundles, wasm (293 requests) |
| `alive-ng.sportybet.com` | **Socket.IO WebSocket** — live odds |
| `lmt.fn.sportradar.com`, `widgets.sir.sportradar.com`, `img.sportradar.com` | Sportradar Live Match Tracker widget |
| `faro-sportybet.sportydog.net` | Grafana Faro RUM |
| `rs.fullstory.com`, `edge.fullstory.com` | FullStory session replay |
| `region1.analytics.google.com`, `googletagmanager.com`, `ad.doubleclick.net` | GA4 / GTM / DoubleClick |

Note the split: **Sportradar drives the visual match tracker; SportyBet's own socket drives the
odds.** Two independent real-time paths.

---

## 3. Betslip configuration, loaded up front

Four endpoints define what the slip will accept, all at boot:

| Endpoint | Content |
|---|---|
| `orders/config/cutbet` | Partial cashout — `sliderEnabled: true`, stake fraction `0.01`–`0.99` |
| `flexiblebet/v2/getOddsKey` | **Flexi Bet** — `oddsKeys` table from 3 to 50 selections (0.95 → 0.15), `powerFactor: 1.5`, `flexibleMinOdds: "1.01"` |
| `promotion/v2/bonus/plans/valid` | Multi-bet bonus ladder, 2→N selections, `qualifyingOddsLimit: 12000` (×10000) |
| `factsCenter/banned/events` | Client-side blocklist of match IDs |

`promotion/v1/gifts/query` then polls roughly every 2–10 s to keep free-bet availability fresh.

---

## 4. Bet placement

### 4.1 The call

```http
POST /api/ng/orders/order          duration: 13.397 s
content-type: application/json;charset=UTF-8
clientid: web    platform: web    operid: 2
transid: 260824084730pdid78453428
referer: …/tennis/live/ATP/US_Open_Men_Singles/Smith,_Keegan_vs_Galarneau,_Alexis/sr:match:73997652

I4p3S6KANPrUZyjGru5ztBlbHLfXoEfkSr+nQZ5tNkAjKeC8IpiGzWP2slUigxBpzxrnMro7wFiY…
```

Response, also ciphertext, with `biz-code: 10000` in the clear.

**Synchronous.** One request, result in the response. No ticket ID, no status polling — the
opposite of Betnacional's fire-and-forget + poll model.

### 4.2 The encryption scheme

Recovered from `s.sporty.net/global/main/modules/main/desktop/common/base/base.33b8f17f4e.js`.
Deobfuscated:

**Key establishment** — cached in `sessionStorage` under `CIPHER_AES_KEY`:

```js
// cache hit if { key, transId, date } exists and date > Date.now()
key  = CryptoJS.lib.WordArray.random(16);                    // 128-bit, client-generated
body = wrap(cipher.encrypt("password=" + encodeURIComponent(base64(key))));

POST /base/cipher    Content-type: text/plain
// → { bizCode: 10000, data: { transId } }

sessionStorage.CIPHER_AES_KEY = JSON.stringify({
  transId,
  key:  encodeURIComponent(base64(key)),
  date: Date.now() + 3600000                                 // 1-hour TTL
});
```

**Request encryption** — random IV, prepended:

```js
iv = CryptoJS.lib.WordArray.random(16);
ct = CryptoJS.AES.encrypt(opt.body, key, { iv, mode: CBC, padding: Pkcs7 });
opt.body = Base64( iv.clone().concat(ct.ciphertext) );
// x-www-form-urlencoded requests get Content-Type rewritten to text/plain
```

**Response decryption** — same key, IV is the first 16 bytes:

```js
raw = atob(text);
ct  = btoa(raw.slice(16));
iv  = Base64.parse(btoa(raw.slice(0,16)));
plain = AES.decrypt(ct, key, { iv, mode: CBC, padding: Pkcs7 }).toString(Utf8);
```

**Error path:** on `bizCode === UNABLE_TO_DECRYPT_BY_CIPHER` the client removes the cached key,
raises `new Error(msg, {cause: "cipher"})`, and re-registers.

Byte analysis confirms the shape:

| | base64 | bytes | = IV + ct | entropy |
|---|---|---|---|---|
| Request | 492 | 368 | 16 + 352 (22 blocks) | 7.4 b/B |
| Response | 748 | 560 | 16 + 544 (34 blocks) | 7.6 b/B |

**`transid` is the key handle, not a per-order ID.** `260824084730pdid78453428` is byte-identical
across both captures, 17 minutes apart — consistent with the 1-hour TTL.

### 4.3 Why this is obfuscation, not security

1. **The RNG is not cryptographic.** The bundled CryptoJS `WordArray.random` is
   `4294967296 * Math.random() | 0` per word. Both the AES session key and every IV come from
   `Math.random()`.
2. **The key is client-minted** and wrapped with material shipped in the bundle. Anyone who reads
   the bundle can replay the whole handshake.
3. **The same data leaks in plaintext elsewhere** (§4.5). The selection, the odds, the stake, and
   the result are all recoverable without touching the ciphertext.

The realistic purpose is raising the cost of naive bet-bots that POST straight to
`/api/ng/orders/order`. TLS is doing the actual confidentiality work.

*Not determined:* the exact wrapping function used for `/base/cipher` (`C.c.encrypt` in module
`+E11`) — module-boundary extraction was inconclusive and it is not worth guessing. It does not
affect decryptability from a HAR: the AES key is generated in-browser and never appears on the
wire. To decrypt live, read `sessionStorage.CIPHER_AES_KEY` in the browser.

*Also not in this capture:* `/base/cipher` itself. The key was already cached from earlier in the
session, inside its TTL.

### 4.4 Reconstructing the bet without the key

Every material fact leaks in plaintext:

```
biz-code: 10000                                        ← header mirrors the encrypted body status
cashAbleBets/count   3 → 4   @ 21:55:03.679 (+42 ms after the order returned)
avlBal        98643000 → 97643000        Δ = 1000000   = NGN 100.00 stake
lastUpdatedTime: 1787608503597 = 21:55:03.597          ← server-side debit timestamp
```

### 4.5 …and the selection leaks too

`POST /api/ng/factsCenter/Outcomes` at 21:54:24.336 — 26 seconds before the order, restoring the
betslip on page load — carries the selection **in the clear**:

```json
[{"eventId":"sr:match:73997652","marketId":"203","outcomeId":"1714","specifier":"setnr=2|hcp=3.5"}]
```

Response: market `203` = "2nd set - game handicap 3.5", group "Sets", outcome `1714` =
"Home (+3.5)", odds `2.25`, probability `0.4063254271`.

So the encrypted payload guards a selection that the client announced in plaintext moments earlier.

> **Caveat.** This call fires at page load, not at bet time. It is the selection *on the slip when
> the page loaded*; the user could in principle have changed it before submitting. It matches the
> `referer` match ID, and no second `Outcomes` call was made.

### 4.6 The 13-second delay is almost certainly deliberate

| Capture | Order duration |
|---|---|
| 21:54:50.240 → 21:55:03.637 | **13.397 s** |
| 21:37:48.300 → 21:38:01.714 | **13.414 s** |

A 17 ms spread across two independent runs is not variable server load. This is the signature of a
fixed **in-play bet delay** (punter delay / anti-courtsiding), standard for live betting and
typically 5–15 s.

The socket makes the consequence visible. Odds on the selected outcome `1714` during the window:

```
21:54:27.284   2.25
21:54:36.332   1.57
21:54:39.897   1.60
21:54:45.567   1.55
21:54:50.240   ── order submitted ──
21:54:52.261   1.30
21:55:01.228   1.27
21:55:03.637   ── order accepted ──
21:55:07.686   1.52
```

The price moved 1.55 → 1.30 *inside* the acceptance window. Which price the bet was struck at is
in the ciphertext; the balance delta only fixes the stake (NGN 100), not the odds.

---

## 5. The WebSocket

```
wss://alive-ng.sportybet.com/socket.io/?EIO=3&transport=websocket
handshake: {"sid":"…","upgrades":["websocket"],"pingInterval":25000,"pingTimeout":60000}
```

Engine.IO v3. 716 frames captured in 51 seconds.

### 5.1 Register, then subscribe

```js
{"type":"reg","data":{"devType":"WEB","deviceId":"e9944416-…","requestId":1,"productCode":7}}
{"type":"sub","data":{"topic":"personal_topic","subType":"SUB","pushType":"MULTI",
                      "accountId":"V240512124805puid63894197","requestId":2,"productCode":7}}
{"type":"sub","data":{"topic":"personal_topic","pushType":"SPECIAL","requestId":3}}
{"type":"sub","data":{"topic":"live^sports","pushType":"GROUP","requestId":4}}
{"type":"sub","data":{"topic":"refresh_bonus_factor_topic","pushType":"GROUP","requestId":5}}
{"type":"sub","data":{"topic":"5^3^sr:tournament:2591^sr:match:73997652^betStatus"}}
{"type":"sub","data":{"topic":"flexibleBet^statusv2"}}
{"type":"sub","data":{"topic":"banned^events"}}
{"type":"sub","data":{"topic":"product^1^status"}}
```

Each is acked with `{"type":"resp","data":"{\"requestId\":N,\"retCode\":\"SUCCESS\"}"}`.

### 5.2 Topic scheme

```
{sportId}^{?}^{tournamentId}^{matchId}^{productId}^{marketId}^{specifier}^{odds|status}
```

e.g. `5^3^sr:tournament:2591^sr:match:73997652^1^203^setnr=2|hcp=3.5^odds`
(`5` = tennis, `203` = 2nd-set game handicap, specifier carries the line).

### 5.3 Payload format

`data.body` is base64 of a positional JSON array:

```json
["<topic>", "<productId>", "<marketStatus>", "<marketName>", "<group>", "<favourite>",
 "<marketGuide>", "<timestamp>",
 ["12#Over 12.5#10.00#1###0.0524814958",
  "13#Under 12.5#1.03#1###0.9475185042"]]
```

Outcome string: `id#name#odds#isActive###probability`.

**The implied probability is pushed to the client alongside the price** — the book's own model
output, not just the offered odds.

Other payload shapes:

```json
{"eventStatus":1,"topic":"…^betStatus"}
[{"id":"sr:sport:1","name":"Football","eventSize":6}, …]     // live^sports
```

### 5.4 Traffic split

| Class | Frames |
|---|---|
| `ret` odds updates | 633 |
| `ret` market status | 34 |
| `resp` sub acks | 21 |
| `ret` betStatus | 2 |
| `ret` live^sports | 1 |
| sends (reg + subs) | 21 |

**Zero `personal_topic` receives.** The bet result did not arrive over the socket — it came in the
encrypted HTTP response. `personal_topic` is subscribed but stayed silent for this 51-second
window; its purpose (settlement, cashout offers, balance pushes) is not determinable here.

---

## 6. Balance

| Endpoint | Value |
|---|---|
| `pocket/v1/wallet/assetsInfo` | `{"balance":98643000, …}` |
| `pocket/v1/finAccs/finAcc/userBal/NGN` | `{"avlBal":98643000,"lastUpdatedTime":…}` |
| `games/lobby/v2/games/wallet_info` | `{"balance":9864.3,"currency":"NGN","userId":13139274}` |

Two representations of the same figure fix the scale: **integer fixed-point ×10000**
(98,643,000 = NGN 9,864.30).

**Refetch after the bet:** `userBal/NGN` at 21:55:06.525, 2.888 s after the order returned —
`98643000 → 97643000`, and `lastUpdatedTime: 1787608503597` shows the server debited at the moment
of acceptance. So unlike Betnacional, the balance *is* re-read from the server after a bet.

---

## 7. Contrast with Betnacional

| | SportyBet | Betnacional |
|---|---|---|
| Placement | 1 synchronous call, ~13.4 s | `create-bet` → poll `bet-request-status` |
| Body | **AES-128-CBC encrypted** both ways | Plaintext JSON |
| In-play delay | ~13.4 s, fixed | none observed |
| Auth | Cookies (`accessToken`) | Bearer JWT header |
| Real-time | Socket.IO — odds only | Custom WS — `EVENT` / `USER` / `GLOBAL` |
| Bet confirmation | HTTP response | HTTP poll **and** WS push (racing) |
| Balance after bet | Refetched (+2.9 s) | Not refetched; patched from WS pushes |
| Money | integer ×10000 | float BRL |
| Live match data | Sportradar widget + own odds socket | Own BFF + WS |
| Probabilities | **Pushed to client** | `current_probability` in bet slip only |

---

## 8. Open questions

| # | Question |
|---|---|
| 1 | The `/base/cipher` key-wrapping cipher (`C.c.encrypt`) — undetermined; capture a fresh session (clear `sessionStorage`) to see the handshake on the wire. |
| 2 | Whether the ~13.4 s is a fixed constant or scales with sport/market — time several in-play bets and one pre-match bet to compare. |
| 3 | What price the bet was struck at, given the 1.55 → 1.30 move during acceptance. Requires decrypting the response with the live `sessionStorage` key. |
| 4 | What `personal_topic` actually delivers — needs a longer capture spanning a settlement or cashout. |
| 5 | Whether encryption covers other endpoints in flows not captured here (deposit, withdrawal, KYC). |
| 6 | Whether `factsCenter/Outcomes` is also called at submit time in other flows, or only on slip restore. |

---

## 9. Reproducing the reconstruction

```
1. Selection   → POST /api/ng/factsCenter/Outcomes           (plaintext request + response)
2. Stake       → avlBal delta across the order                (÷10000 for currency units)
3. Accepted?   → biz-code response header (10000 = OK)
4. Confirmed?  → realSportsGame/cashAbleBets/count increments
5. Debit time  → userBal.lastUpdatedTime
6. Price drift → WebSocket topic {…}^{marketId}^{specifier}^odds
```

Nothing in that list requires the AES key.

---

*Account identifiers and session tokens present in the capture are deliberately abbreviated or
omitted in this document.*
