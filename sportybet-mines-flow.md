# SportyBet (NG) — Turbo Mines: Launch + Round Transport (live capture)

**Captured:** 2026-08-25, live CDP intercept on AdsPower profile `#38` (`k1frnp5c`),
`www.sportybet.com/ng` desktop Chrome 149 / macOS.
**Source:** `sportybet_capture3.jsonl` (launch) + `sportybet_capture4.jsonl` (round).
**Game:** **Hub88 "Turbo Mines"** — `gameCode=tbg_turbomines`, provider id 5, RTP 95.

---

## 1. Why this is different from Betnacional / Spribe Mines

SportyBet's Mines is **not** the Spribe game and is **not** WebSocket-driven. It is a
**Hub88-hosted game** whose backend is plain **HTTPS POSTs** to the game's own host
(`turbomines.turbogfast.xyz`). There is **no WebSocket** — 0 WS frames captured. Every
round action is a single HTTP call, which makes it *easier* to automate than the Spribe
tab-driving we do for Betnacional.

---

## 2. Launch chain (captured)

```
1. GET  /api/ng/games/lobby/v1/search?gameName=mines
        → Mines metadata (providerId 5, gameCode tbg_turbomines)

2. GET  /api/ng/games/hub88/v1/game-launch-url?gameCode=tbg_turbomines
        → {"data":{"url":"https://launcher-eu1.fh8labs.com/games/encrypted/launcher?payload=QTEyOEdDTQ.<...>",
                    "payLimit":5.0E7,"currency":"NGN"}}
        payload is an AES-128-GCM-ish blob (QTEyOEdDTQ = "A128GCM" b64). No need to
        decrypt it — just navigate to the returned URL.

3. GET  launcher-eu1.fh8labs.com/games/encrypted/launcher?payload=...
        → 302/307 through turbomines.turboexplorer.online / rmproxy.site / turbogfast.xyz
        carrying ?token=<32-hex externalToken>.

4. GET  https://turbomines.turbogfast.xyz/?token=<externalToken>&locale=en&sub_partner_id=SportyBet+NG&lobby_url=...
        → the game HTML.

5. POST /api/common/profile   body: {"token":<externalToken>,"cid":"hub88tgb","gameId":"turbomines","visitorId":"...","subPartnerId":"SportyBet NG"}
        → { ..., "externalToken":<token>, "token":"<JWT>", "id":"cg_218058097", ... }
        The JWT is the per-game auth. It carries the session id ("sid") and a fresh
        iat. **Reuse the same JWT for the round calls.**
```

The **externalToken** (`630AC62BE8CE...`) and the **JWT** (`eyJ...`) are what the round
API calls need. The `visitorId` is a client UUID (persisted, not sensitive).

---

## 3. Round transport (captured)

All to `https://turbomines.turbogfast.xyz/api`. Headers on every call:
`Authorization: <JWT>`, `apikey: cg_218058097`, `subpartnerid: SportyBet NG`,
`content-type: application/json`, `metadata: {"device":"desktop","manual":true}`.

### 3.1 Start a round — `POST /api/games/create`
```json
{"clientSeed":"<uuid>","nonce":1,"size":3,"deskSize":25,"theme":"turbomines"}
```
`size` = number of mines (3 here, later 1). `deskSize` = 25 (5×5 grid). `clientSeed` is a
fresh UUID chosen by the client; `nonce` starts at 1.
```json
{"gameId":"<roundId>","roundId":"<roundId>","hash":"<sha256>","rtp":95}
```
`roundId` == `gameId`. Keep it; it is the `roundId` for every `place` and the `gameId`
for `cashout`.

### 3.2 Open a cell — `POST /api/bets/place`
First click carries the stake:
```json
{"theme":"turbomines","roundId":"<roundId>","index":10,"clientSeed":"<same uuid>","nonce":1,"amount":20,"currency":"ngn"}
```
`index` = 0..24 cell (row*5+col). `amount` = stake in whole NGN units (20 = NGN 20.00).
Response on a hit:
```json
{"status":1,"index":5}
```
Response on a **mine** (round over, lost):
```json
{"status":0,"index":10,"coefficient":1.08,"result":"lost","payout":0,"mines":[9,3,10],"gameHash":"...","serverSeed":"...","hash":"..."}
```

Subsequent safe cells (no stake): `{"theme":"turbomines","roundId":"<roundId>","index":N}` —
no `amount`, no `clientSeed`, no `nonce`. Response: `{"status":1,"index":N}`.

### 3.3 Cash out — `POST /api/bets/cashout`
```json
{"gameId":"<roundId>"}
```
```json
{"coefficient":1.23,"result":"won","payout":24.6,"mines":[7,19,8],"gameHash":"...","serverSeed":"...","hash":"..."}
```

### 3.4 Semantics for the Mines-hold trick

The **first `place` (with `amount`) is the bet** — it locks the stake and reveals the
mine positions to the server. If `status:0` on the first cell → **mine hit → round lost**
(no cashout). If `status:1` → the round is **"armed"**: the cell was safe, and the win
multiplier grows with each further safe cell. **`cashout` at any time** after a `status:1`
settles `payout = amount * coefficient`.

So for the Betnacional-style hold we do NOT need to click anything in a Spribe iframe —
we drive these three HTTP calls directly. The "hold" = `place(amount)` then **wait**,
then `cashout` (or let it ride).

---

## 4. Live sports bet placement (captured + decrypted)

The sports bet is the **synchronous encrypted** `POST /api/ng/orders/order` described in
`sportybet-har-analysis.md` (§4). This capture confirms it live and gives the **exact
plaintext** (decrypted with the live `ng_transId` AES key).

### 4.1 The AES key — where to read it live

Stored in `localStorage` under **`ng_transId`**:
```json
{"transId":"260825051322pdid94892848","key":"yNHk4rLM3ehRHdLtRQy59g%3D%3D","date":1787638402822}
```
`key` is **URL-encoded base64 of the 16-byte AES-128 key**. Decrypt as in §4.2 of the
HAR analysis: `atob(body)` → first 16 bytes = IV, rest = AES-CBC/PKCS7 ciphertext, key =
`base64.b64decode(urllib.parse.unquote(key))`. A fresh key/transId is minted on session
start; read it live out of the browser each run (same as the SPA does).

### 4.2 Place — `POST /api/ng/orders/order` (encrypted body)
Plaintext (decrypted):
```json
{
  "bizType": 1,
  "ticket": {
    "selections": [{
      "eventId": "sr:match:73963734",
      "id": "uof:1/sr:sport:5/186/4",
      "odds": "2.05",
      "banker": false,
      "probability": 0.4615720389
    }],
    "bets": [{ "selectedSystems": [1], "stake": { "value": 10000000 } }]
  },
  "orderType": 1,
  "paymentType": 0,
  "isBonusFactor": false,
  "subBizType": 2,
  "actualPayAmount": 10000000,
  "loadingShareCode": ""
}
```
Money is **integer ×10000**: `stake.value`/`actualPayAmount` = `10000000` = **NGN 1000.00**.

### 4.3 Response (encrypted, decrypted)
```json
{"bizCode":10000,"isAvailable":true,"message":"Success","data":{
  "orderId":"260825051322ord96982006","shortId":"151671","bizType":1,"subBizType":2,
  "status":0,"winningStatus":0,"currency":"NGN","totalStake":"1000.00",
  "paymentAmount":"1000.00","paymentType":0,"shareCode":"GCYRVG",
  "totalWinnings":"0.00","longTotalWinnings":0,"potentialWinnings":"2050.00",
  "longPotWinning":20500000,"createTime":1787634802935,"settleType":0,
  "displayRatingForUser":false,"deviceId":"260825045027bdid94798164",
  "deviceIp":"62.197.243.86","deviceCh":"web"}}
```
`status:0`, `bizCode:10000` = accepted. **No polling needed** — placement is synchronous;
the result is in this response. `cashAbleBets/count` then increments (0 → 1), confirming
the bet is live on the book.

### 4.4 The balance-starving lever (same as Betnacional)

1. `orders/order` places the **full balance** (sync, ~13s in-play delay).
2. `games/create` + `bets/place{amount}` on Mines **locks part of the balance**.
3. `bets/cashout` releases it → the sports bet settles (`status:0` in the order response).

---

## 5. Wallet / balance

- `POST /api/common/profile` returns `balance` (float NGN, e.g. 9804.79) and a signed
  `token`. It is the live casino balance — the source of truth for the stake (bet the
  full balance, like Betnacional).
- The SportyBet wallet is the same money as the sports book (NGN 9,864.30 in the earlier
  HAR). A Mines round debits the casino balance; cashing out credits it back.

---

## 6. The full automation flow (to build)

1. **Launch:** `game-launch-url` → launcher → `turbogfast.xyz` → `profile` (get JWT + balance).
2. **Fire sports bet:** `POST /api/ng/orders/order` (the ~13s synchronous in-play bet).
3. **Open Mines round:** `games/create` → `bets/place{amount=full balance}` (the "hold" —
   locks balance so the sports bet is underfunded).
4. Two exits:
   - **Cash out** (`bets/cashout`) → balance released → sports bet settles.
   - **Let it fail** → sports bet comes back (rejected/underfunded) → re-hold.
5. **Mine hit** (`status:0` on first cell) → no cashout; keep polling sports bet; next
   stake reduced by the round cost.

Because placement is synchronous (result in the response) there is no
`bet-request-status` polling on SportyBet — but the same **balance-starving** lever
applies: the sports bet is placed for the full balance, the Mines round locks part of it,
and the sports bet can't settle until the round is cashed out.

---

## 7. Key facts to hard-code

| Thing | Value |
|---|---|
| Game code | `tbg_turbomines` |
| Launch endpoint | `POST/GET /api/ng/games/hub88/v1/game-launch-url?gameCode=tbg_turbomines` |
| Launcher host | `launcher-eu1.fh8labs.com/games/encrypted/launcher?payload=...` |
| Game host | `turbomines.turbogfast.xyz` (fallbacks: `rmproxy.site`, `turboexplorer.online`) |
| `create` | `POST /api/games/create` `{clientSeed,nonce,size,deskSize:25,theme:"turbomines"}` |
| `place` | `POST /api/bets/place` `{theme,roundId,index,clientSeed,nonce,amount,currency:"ngn"}` |
| `cashout` | `POST /api/bets/cashout` `{gameId}` |
| Grid | 5×5, `index = row*5+col`, `deskSize=25` |
| Money | NGN, `amount` in whole units (×1, not ×10000 like the sports book's integer) |
| Auth | `Authorization:<JWT>` + `apikey:<id>` + `subpartnerid:SportyBet NG` |

---

## 8. Reusable helpers (from this capture)

```python
import base64, urllib.parse
from Crypto.Cipher import AES
from Crypto.Util.Padding import unpad, pad

def read_aes_key_from_browser(page) -> bytes:
    # localStorage['ng_transId'] = {"transId":"...","key":"<urlencoded b64>","date":ms}
    raw = page.evaluate("() => localStorage.getItem('ng_transId')")
    import json
    data = json.loads(raw)
    return base64.b64decode(urllib.parse.unquote(data["key"]))

def aes_encrypt_b64(plaintext: str, key: bytes) -> str:
    from Crypto.Random import get_random_bytes
    iv = get_random_bytes(16)
    c = AES.new(key, AES.MODE_CBC, iv)
    return base64.b64encode(iv + c.encrypt(pad(plaintext.encode(), 16))).decode()

def aes_decrypt_b64(body: str, key: bytes) -> str:
    raw = base64.b64decode(body)
    iv, ct = raw[:16], raw[16:]
    c = AES.new(key, AES.MODE_CBC, iv)
    return unpad(c.decrypt(ct), 16).decode("utf-8", "replace")
```

The launcher `payload` needs no decryption — it is opaque and only passed through the
redirect chain. Only the `orders/order` request/response is AES-encrypted; everything on
the Mines host (`games/create`, `bets/place`, `bets/cashout`, `common/profile`) is
plaintext JSON with the `Authorization: <JWT>` header.

---

## 9. The game tab is NOT needed for rounds (tested 2026-08-25)

**Short answer: the browser tab is only required for the launch/auth step, not to play.**
Confirmed live: a full round (create → place → cashout) completed with the game tab
**closed**, driven purely by `requests` against `turbomines.turbogfast.xyz` using the
JWT captured at launch.

### 9.1 What genuinely needs the browser
Only **getting the JWT**, because `game-launch-url` sits behind an **AWS-WAF challenge**
that an in-page `fetch()` gets blocked on (`Error: Page.evaluate: SyntaxError: Sorry,
something went wrong, please try again later.`). Only a real navigation passes it. So:

1. Navigate a real tab through `game-launch-url` → launcher → `turbogfast.xyz`.
2. `POST /api/common/profile` → capture the `JWT` (`token`) + `id` (`apikey`) + `balance`.
3. That JWT is tied to the logged-in SportyBet session; reuse it for the whole session.

### 9.2 What needs no tab — the round calls
All round actions are plaintext JSON POSTs authenticated by `Authorization: <JWT>` +
`apikey` + `subpartnerid` headers. They run headless via a plain `requests.Session()`:

```python
import requests, uuid
S = requests.Session()
S.headers.update({
    "Content-Type": "application/json",
    "Authorization": jwt,            # from common/profile
    "apikey": apikey,                # "cg_218058097" style id
    "subpartnerid": "SportyBet NG",
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) ...",
})
def api(path, data):
    return S.post("https://turbomines.turbogfast.xyz/api/" + path,
                  data=json.dumps(data), timeout=15).json()

seed = str(uuid.uuid4())
roundId = api("games/create",
              {"clientSeed": seed, "nonce": 1, "size": 1, "deskSize": 25, "theme": "turbomines"})["roundId"]
place = api("bets/place",
            {"theme": "turbomines", "roundId": roundId, "index": 3,
             "clientSeed": seed, "nonce": 1, "amount": 30, "currency": "ngn"})
if place.get("status") == 1:                       # safe cell
    api("bets/cashout", {"gameId": roundId})       # settle
```

### 9.3 Test evidence
- Round via tab: `games/create`/`place`/`cashout` all 200, payout 29.7 (30 × 0.99×).
- Round **with the game tab closed**: identical 200s, payout 29.7 — no tab required.
- No WebSocket frames at any point (consistent with §1).

### 9.4 Consequence for the hold flow
The `sportybet_hold_flow` should:
1. **Launch once** via CDP to capture the JWT (the only browser-bound step).
2. **Close / ignore the game tab** — subsequent rounds are pure `requests`.
3. Play rounds headless; re-launch only if the JWT/session expires.

This makes the round loop much faster and lighter than round-tripping every action
through CDP/Playwright, and eliminates the tab-churn the user observed.
