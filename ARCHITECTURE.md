# SportyBet Automation — Full System Architecture

Version 1.0 — 2026-08-28

---

## 1. System Overview

The system has three major parts:

1. **Client App** (desktop, runs on the user's machine) — receives signals, evaluates rules, places bets on SportyBet.
2. **AWS Backend** — license validation, signal distribution, telemetry, admin operations.
3. **Signal Pipeline** — your internal producer that detects match events (goals, corners, cards) and pushes them to subscribed clients in near real time.

```
┌────────────────┐   HTTPS/WSS   ┌──────────────────────────────┐
│  Client App(s) │◄─────────────►│         AWS Backend           │
│  (user PC)     │               │  API GW + Lambda + DynamoDB   │
└───────┬────────┘               │  AppSync/IoT Core (signals)   │
        │                        └───────────▲──────────────────┘
        │ places bets                         │ publish
        ▼                                     │
┌────────────────┐               ┌────────────┴─────────────────┐
│  SportyBet     │               │  Signal Pipeline (your side) │
│  (browser/API) │               │  match-data ingest → detect  │
└────────────────┘               │  events → fan-out            │
                                 └──────────────────────────────┘
```

---

## 2. Client App Architecture

Recommended stack: **Electron or Tauri** (cross-platform desktop), or **Python + Playwright** if the bet placement is browser-automation based. Internally, modular monolith:

```
client/
├── core/
│   ├── license/          # activation, local cache, heartbeat
│   ├── signals/          # WSS subscriber, signal parser, dedup
│   ├── strategy/         # rule engine: signal → bet decision
│   ├── executor/         # bet placement (SportyBet session/browser)
│   ├── wallet/           # stake sizing, balance tracking, limits
│   └── state/            # local DB (SQLite): bets, signals, config
├── ui/                   # dashboard: live signals, bet log, P/L
└── main.*                # app entry, auto-updater
```

### 2.1 Module responsibilities

- **license/** — one-time activation against the server; stores a signed, machine-bound token locally (see §4).
- **signals/** — persistent WebSocket connection; validates signal signatures; deduplicates by `signal_id`; buffers on disconnect with replay on reconnect.
- **strategy/** — user-configurable rules per signal type:
  - GOAL → e.g. "back over 2.5 if goal before 20' and odds ≥ 1.8"
  - CORNER → corner-count markets
  - CARD → booking markets
  Rules are data (JSON), not code, so you can ship strategy updates without an app release.
- **executor/** — the riskiest module. Isolated behind an interface so you can swap browser-automation vs. private-API implementations. Includes: bet-slip construction, stake injection, confirmation parsing, retry with idempotency guard (never double-place).
- **wallet/** — daily loss limits, max stake, cool-down after N losses. Hard stops enforced client-side *and* server-side.
- **state/** — SQLite (encrypted with SQLCipher, key derived from license token) for bet history, signal log, and config.

### 2.2 Runtime flow

```
Signal received (WSS)
  → signature + license check
  → dedup (signal_id seen?)
  → strategy engine evaluates rules
  → decision: SKIP | BET(market, selection, stake)
  → wallet guard (limits OK?)
  → executor places bet
  → result recorded locally + telemetry event to server
```

---

## 3. AWS Backend

### 3.1 Services

| Concern | Service | Why |
|---|---|---|
| REST API (license, config) | **API Gateway + Lambda** | serverless, cheap at this scale |
| Real-time signal push | **AWS IoT Core (MQTT over WSS)** or **AppSync subscriptions** | managed fan-out, per-client auth |
| License store | **DynamoDB** (`licenses` table) | single-digit-ms reads, TTL support |
| Signal log / telemetry | **Kinesis Firehose → S3** (+ Athena for queries) | cheap analytics |
| Secrets | **Secrets Manager** | signing keys, upstream data API keys |
| AuthN/Z on API | **Lambda authorizer** validating license tokens | no Cognito needed (users are anonymous licensees) |
| Admin console | **S3 + CloudFront** static site + the same API | manage licenses, push strategy updates |
| Signing | **KMS** (asymmetric key) | sign license tokens and signals |

### 3.2 DynamoDB schema — `licenses`

| Attribute | Type | Notes |
|---|---|---|
| `license_key` (PK) | string | e.g. `SBET-XXXX-XXXX-XXXX` |
| `status` | string | `active` / `suspended` / `revoked` |
| `machine_id` | string | set on first activation (1 machine per key, or N seats) |
| `plan` | string | which signal channels are enabled |
| `expires_at` | number | epoch; enforce server-side |
| `activated_at` / `last_seen` | number | audit |
| `token_version` | number | bump to invalidate all issued tokens |

### 3.3 API endpoints

```
POST /v1/license/activate     { license_key, machine_id, app_version }
                              → { token (JWT, signed by KMS), expires_at, plan }

POST /v1/license/heartbeat    Authorization: Bearer <token>
                              → { status, expires_at, strategy_bundle_version }
                              (also the revocation check — see §4.3)

GET  /v1/strategy/bundle      → signed JSON of latest rule packs

POST /v1/telemetry            → bet outcomes, errors (batched)
```

### 3.4 Signal channel

- Clients subscribe to MQTT topic `signals/{plan}/live` after activation; IoT Core policy is attached per-thing or via the token authorizer so an expired license literally cannot receive data.
- Each signal payload:

```json
{
  "signal_id": "uuid",
  "type": "GOAL | CORNER | CARD | PENALTY | RED_CARD",
  "match_id": "sportybet-match-id",
  "minute": 34,
  "detail": { "team": "home", "score": [1,0] },
  "issued_at": 1724800000,
  "sig": "Ed25519 signature over canonical payload"
}
```

- Latency budget: event detected → client receives < 2s (signal speed is your product's core value).

---

## 4. Licensing Design (your key requirement)

### 4.1 Model: validate once, cache locally, periodic silent re-check

1. **Activation (online, once):** app sends `license_key + machine_id` to `/license/activate`. Server checks key validity, binds it to the machine, returns a **signed JWT** (valid e.g. 7 days) containing `plan`, `expires_at`, `token_version`.
2. **Local storage (offline-friendly):** the JWT is stored locally, *hashed/bound to the machine*:
   - Store `JWT` + `HMAC(JWT, machine_fingerprint + app_secret)` — on startup, recompute the HMAC; if the file was copied to another machine, fingerprint differs → token rejected.
   - Machine fingerprint: stable hardware/OS IDs (motherboard UUID / `MachineGuid` on Windows, `IOPlatformUUID` on macOS), hashed with SHA-256. Never send raw hardware IDs to the server — hash them.
   - Optionally encrypt the whole local state DB (SQLCipher) with a key derived from this fingerprint.
3. **Startup check (offline-tolerant):** if JWT not expired and HMAC valid → run. No server call needed.
4. **Heartbeat (when online):** every N hours (and on startup when online) the app calls `/license/heartbeat`. Server can return `suspended`/`revoked` or a bumped `token_version` → app locks immediately. If server unreachable, keep working until JWT expiry (grace period, configurable).

This gives you: one server validation, local hashed persistence, offline grace, and a revocation path.

### 4.2 Anti-sharing controls

- 1 machine per key by default; re-activation on new machine requires admin reset or consumes a seat.
- Heartbeat carries `machine_id`; if two machines heartbeat the same key → auto-suspend.
- Signed JWTs can't be forged without the KMS private key; HMAC binding can't be moved without the fingerprint secret.

### 4.3 Revocation ladder

| Level | Mechanism | Effect |
|---|---|---|
| Soft | heartbeat returns `suspended` | app locks on next online check |
| Hard | `expires_at` passed, no renewal | app stops even offline |
| Kill | `token_version` bumped | all issued tokens invalid immediately |
| Data cut | IoT/AppSync policy detached | signals stop arriving (strongest) |

---

## 5. Signal Pipeline (server side)

```
Data source (feed provider or your scrapers)
  → ingest Lambda/ECS workers (one per competition)
  → event detector (state machine per match: possession/goal/corner/card deltas)
  → dedup + sequence check (DynamoDB per-match state)
  → sign payload (KMS)
  → publish to IoT topic signals/{plan}/live
  → archive to S3
```

Key design points:
- **State machine per match** so you emit "GOAL" exactly once even if the feed flaps.
- **Sequence numbers** per match so clients can detect missed signals and request replay (`GET /v1/signals/replay?match_id=&after_seq=`).
- **Clock discipline:** always use feed event time, not receipt time.

---

## 6. Security Summary

- All traffic TLS 1.2+; WSS for signals.
- License JWTs and signal payloads signed with asymmetric keys (KMS); clients embed only the public key.
- No user passwords in the system at all — license key *is* the credential.
- Rate-limit API Gateway (usage plans per key) to blunt abuse.
- App hardening: code-sign the installer, obfuscate the client bundle, detect debuggers at the licensing layer. Accept that determined crackers exist — your real protection is that signals are worthless without a live server subscription.

## 7. Ops & Reliability

- CloudWatch alarms: signal latency p99, heartbeat failure rate, Lambda errors.
- Auto-updater for the client (e.g. electron-updater from S3) — you will need to ship executor fixes fast when SportyBet changes their UI.
- Admin console actions: issue/revoke keys, reset machine binding, push strategy bundle, broadcast app-message.

## 8. Suggested Build Order

1. AWS skeleton: DynamoDB table + activate/heartbeat Lambdas + JWT signing. *(licensing first — it's your business model)*
2. Client license module + local hashed cache + offline grace.
3. Signal channel: IoT Core + a manual "test publish" admin button → client receives & verifies.
4. Strategy engine + executor with a **dry-run mode** (log what it *would* bet).
5. Real bet placement behind a per-user feature flag.
6. Telemetry, admin console, auto-updates.

## 9. Legal / Risk Note (important)

Automating bets on SportyBet almost certainly violates their Terms of Service; accounts will get limited/banned and, depending on jurisdiction, gambling-software licensing may apply to selling this. Design the executor so account bans are a contained, per-user failure (they are), and get legal advice in your target market before selling licenses.
