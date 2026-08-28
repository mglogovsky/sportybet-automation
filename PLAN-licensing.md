# Implementation Plan — Licensing + Packaging (Two Repos) — v2, post-review

Simplified license model (per owner decision):
- License = key string + 30-day validity, granted/reviewed on the betradar-clone server.
- Client checks the server **at every startup** and **every 12 hours** while running.
- Past 30 days (or key revoked) → license dead, software unavailable. That's all.
  No offline grace, no HMAC token cache, no JWT — the server is the single judge.

Two repos:
- **Repo A: `sportybet-automation`** (this repo) — the client app users run.
- **Repo B: `betradar-clone`** (`~/Desktop/betradar-clone`, Java 17 / Spring Boot 3.5.16) — gains the license endpoints.

Status (28 Aug 2026, evening): **Repo B steps B9 1–6 are implemented** on
betradar-clone branch `feature/license-server` (six commits, full suite 1,607
tests green). The B2 contract is live-verified end to end on the replay
profile: mint on the panel → `{"verdict":"OK","expiresAt":…,"secondsLeft":…}`
on the wire with no Set-Cookie → normalization → revoke/activate round-trip →
store file in the sibling format. One refinement vs the sketch below: the
stateless carve-out matches **exactly `POST /api/license/check`** (not the
`/api/license/**` prefix) — a GET of the path 302s to the login page like any
other anonymous request, which changes nothing for the client (it only ever
POSTs; non-200 → UNREACHABLE as specced). B9 step 7 (deploy to the VPS) is
NOT done. Repo A: largely implemented but **uncommitted** — see A0 before
touching anything.

Two decisions settled by this revision (flip them only deliberately, they are
load-bearing in B2/B3 and A1/A4):
1. **No bearer token** on `/api/license/check`. The key in the body is already an
   unguessable credential; a shared token baked into distributed bundles is
   extractable and unrotatable without redistributing every build (no
   auto-update). The client still supports one (empty = no header) so this can
   be reversed server-side later.
2. **Keys are plaintext**, stored as-is and listed in full in the admin panel.
   No show-once ceremony — it was theater the moment the panel lists all keys.
   (The trial-refresh precedent only works because only a bcrypt hash survives
   there.)

---

# REPO B — betradar-clone (license server) — revised

Verified base (code recon, 28 Aug 2026): Spring Boot 3.5.16 / Java 17, plain
JDBC (no JPA), line-based file stores rewritten atomically under
`/var/lib/betradar-clone/`, auth = form login + session cookie, exactly **one**
`SecurityFilterChain` (`relaySecurity`, `config/SecurityConfig.java:136`,
unordered — no bearer/stateless surface exists anywhere yet), `/api/**`
unclaimed by any mapping, admin UI at `/admin.html`, controllers return
verdict + whole-panel JSON, trials panel genuinely on `/admin.html` with its
script in a separate fenced `admin-trials.js`. Closest blueprint: the `trial/`
package (9 files). nginx catch-all already proxies and rate-limits every path
at 20 r/s per IP; systemd `StateDirectory`/`ReadWritePaths` already cover
`/var/lib/betradar-clone` (`deploy/betradar-clone.service:154-156`).

## B1. New `license/` package — files to create

```
src/main/java/sk/glogovsky/betradar/license/
├── License.java              # record: key, status(ACTIVE|REVOKED), issuedAt, expiresAt, note
│                             #   EXPIRED is NEVER stored — it is derived from expiresAt at
│                             #   read time, by validation and by the panel alike.
│                             #   There is NO LicenseSweeper (see B8).
├── LicenseStore.java         # SIBLING FORMAT, not JSON: line-based `KEY=k:v;k:v;…` with the
│                             #   header + "# format: 1" marker, 0600 perms, temp-file +
│                             #   atomic rename — copy trial/TrialStore.java:58-129 mechanics
│                             #   including Loaded{…, usable}. Unreadable file → usable=false,
│                             #   loud log, relay keeps serving; B2 turns that into 503,
│                             #   never into UNKNOWN_KEY. One bad line rejects the whole
│                             #   file (no half-parse). Mint refuses a note containing
│                             #   '=', ';' or a line break.
├── LicenseService.java       # validate(key): normalize (trim, uppercase) → UNKNOWN_KEY /
│                             #   REVOKED / (now ≥ expiresAt → EXPIRED) / OK{expiresAt,
│                             #   secondsLeft}. One place, no client trust.
│                             #   mint(note): SecureRandom, SBET-XXXX-XXXX-XXXX-XXXX from a
│                             #   no-ambiguity alphabet (2-9, A-Z minus I and O) ≈ 80 bits.
│                             #   revoke / activate(= un-revoke) / extend.
│                             #   extend = max(now, expiresAt) + validity — early renewal
│                             #   must not discard remaining days. extend does NOT un-revoke;
│                             #   the panel wording says so.
├── LicenseProperties.java    # @ConfigurationProperties "app.license" — the app.* family,
│                             #   like app.trial / app.account-expiry, NOT top-level license.*:
│                             #   - enabled: default FALSE (house rule: a build carrying this
│                             #     changes nothing until the box asks for it)
│                             #   - store: default RELATIVE ./.licenses like every sibling;
│                             #     the box overrides via env. An absolute /var/lib default
│                             #     would break every local run (ProtectSystem trap runs the
│                             #     other way — see betradar-clone.service:52-60).
│                             #   - validity: Duration @DurationUnit(DAYS), default 30d
│                             #     (TrialProperties.java:75 documents the unitless trap:
│                             #     without the annotation a bare "30" means milliseconds).
├── LicenseThrottle.java      # LoginThrottle's shape (ConcurrentHashMap keyed on client IP
│                             #   via RequestFacts.clientIp — correct behind the proxy thanks
│                             #   to forward-headers-strategy — windowed count, MAX_TRACKED
│                             #   cap) but its own numbers: count ONLY UNKNOWN_KEY verdicts
│                             #   (the enumeration signal; EXPIRED/REVOKED are known keys and
│                             #   a hand-typed activation typo must not lock a customer out),
│                             #   ~20 per 15 min per IP, refuse with 429 — which clients map
│                             #   to UNREACHABLE and retry, never to a definitive lock.
├── LicenseApiController.java   # B2
└── LicenseAdminController.java # B3
```

## B2. Client-facing endpoint (stateless, no session, CSRF off)

```
POST /api/license/check
  Body: { "key": "SBET-XXXX-XXXX-XXXX-XXXX" }
  200:  { "verdict": "OK", "expiresAt": <epoch seconds>, "secondsLeft": <seconds> }
  200:  { "verdict": "EXPIRED" | "REVOKED" | "UNKNOWN_KEY" }
  503:  app.license.enabled=false OR store unreadable — a definitive kill
        verdict must never come from a broken store; clients treat any
        non-200 as UNREACHABLE and retry
  429:  from LicenseThrottle (same client mapping: UNREACHABLE, retry)
  Auth: NONE (decision 1 above). The key is the credential; the endpoint
        reveals nothing without a valid one. If ever reversed: one
        OncePerRequestFilter on the B5 chain + APP_LICENSE_API_TOKEN in env
        + the client's build_config.py — a contained delta.
```

## B3. Admin endpoints (session + ROLE_ADMIN — NOT the trials matcher)

`@RequestMapping("/admin/licenses")` in the `license/` package. Under
`/admin/**` it is **ADMIN-only automatically** — zero SecurityConfig changes
for the endpoints. Do **not** model the fence on `/admin/trials`: that matcher
is deliberately widened to ADMIN+MOD (`SecurityConfig.java:271-275`), and
moderators must not mint licenses. Copy the *controller* idiom from
`trial/TrialAdminController.java`: every response = `{verdict, detail,
…whole panel}`; the `statusFor` mapping (404 `UNKNOWN`, 500
`STORE_UNREADABLE`, 409 conflicts); CSRF token on POSTs comes free in the
main chain.

```
GET  /admin/licenses                → enabled, storeUsable, rows[key,
                                      status(derived: ACTIVE|EXPIRED|REVOKED),
                                      issuedAt, expiresAt, secondsLeft, note],
                                      states vocabulary
POST /admin/licenses/mint {note?}   → mints; key in this response AND in every
                                      panel row (decision 2 above)
POST /admin/licenses/{key}/revoke   → status=REVOKED
POST /admin/licenses/{key}/extend   → max(now, expiresAt) + 30d; refused with
                                      its own verdict on a REVOKED key
POST /admin/licenses/{key}/activate → un-revoke (an expired key also needs
                                      extend; the panel wording says so)
```

"Review and grant" = admin mints/extends from the existing `/admin.html`
panel. Renewal flow: user pays → admin presses extend → the client's next
poll (≤15 min locked, ≤12 h running) silently re-enables it. Nothing ships.

## B4. Admin UI

A Licenses section on `admin.html`, table + verdict style copied from the
trials panel, script in a new **`admin-licenses.js`** mirroring
`admin-trials.js` — and, **in the same commit**, two things this codebase has
been burned by twice:

- the literal `/admin-licenses.js` added to the ADMIN matcher list
  (`SecurityConfig.java:309-313`) — page assets fall through to
  any-signed-in otherwise (the `/admin-audit.js` lesson, restated in that
  file's own comments: "the next such page must arrive on this line in the
  same commit");
- the path added to `SecurityPostureTest`'s admin surface.

If any *inline* script in `admin.html` changes, `ContentSecurityPolicy`
re-hashes it at startup (`ContentSecurityPolicy.java:44`) — the deploy's
restart covers it; verify the hash count in the live header afterward, both
directions (count in jar == count in header).

## B5. SecurityConfig delta — one new bean, nothing else touched

```java
@Bean @Order(1)   // sorts ahead of the existing unordered chain;
                  // relaySecurity itself stays byte-identical
SecurityFilterChain licenseApi(HttpSecurity http) throws Exception {
    http.securityMatcher(PathPatternRequestMatcher.withDefaults()
                .matcher(HttpMethod.POST, "/api/license/check"))  // one verb, one path — as built
        .authorizeHttpRequests(a -> a.anyRequest().permitAll())  // key = credential
        .csrf(AbstractHttpConfigurer::disable)
        .sessionManagement(s -> s.sessionCreationPolicy(STATELESS))
        .requestCache(c -> c.requestCache(new NullRequestCache()));
    return http.build();
}
```

The existing bean needs no `@Order` (unordered = lowest precedence, so the
narrow chain wins on its own paths). The anonymous-allocation property the
main chain fought for must hold here too: the posture test asserts no session
and no Set-Cookie on this path.

## B6. Config + deploy — the real procedure

`src/main/resources/application.yml`, under the existing `app:` block,
commented in house style:

```yaml
  license:
    enabled: ${APP_LICENSE_ENABLED:false}
    validity: ${APP_LICENSE_VALIDITY:30d}     # bare number = DAYS
    store: ${APP_LICENSE_STORE:./.licenses}
```

`deploy/env.example`: document `APP_LICENSE_ENABLED`, `APP_LICENSE_VALIDITY`,
`APP_LICENSE_STORE=/var/lib/betradar-clone/licenses`.

Deploy is **not** `deploy/deploy.sh` — it has never been on the box and the
repo is not checked out there. The real steps:

1. build + full test run locally
2. `scp` the jar to `/tmp/app.jar` on the box
3. `cp -p /opt/betradar-clone/betradar-clone.jar betradar-clone.jar.prev`
4. `install -o root -g root -m 644 /tmp/app.jar /opt/betradar-clone/betradar-clone.jar`
5. hand-add the three `APP_LICENSE_*` lines to `/etc/betradar-clone/env`
6. `systemctl restart betradar-clone`

**No unit change** (StateDirectory already covers the store path) and **no
nginx change** (the catch-all location proxies `/api/license/check` under the
existing `betradar_pages` 20 r/s zone; the box's nginx is hand-managed —
touch nothing; the repo's `nginx.conf` is a template that must never be
installed wholesale).

Smoke, in order: posture tests green locally → deploy → mint a key in
`/admin.html` → `curl` the check endpoint from outside: OK with sane
`secondsLeft` → revoke → `REVOKED` → activate + extend → OK again → garbage
key → `UNKNOWN_KEY` → ~21 garbage keys fast → 429 → `ls -l` the store file
(0600, service user) → CSP hash count matched both ways.

## B7. Tests — first-class work, not a curl afterthought

- `LicenseStoreTest` modeled on `TrialStoreTest`: round-trip, atomic rewrite,
  unreadable-file → `usable=false` (never a half-parse), one bad line rejects
  the file, 0600.
- `LicenseServiceTest`: all four verdicts, the `now == expiresAt` boundary,
  extend-from-future vs extend-from-past arithmetic, extend-on-revoked
  refused, key normalization.
- `SecurityPostureTest` additions: `POST /api/license/check` answers without
  session or CSRF and **allocates no session** (no Set-Cookie); GET on it is
  not served; `/admin/licenses` and `/admin-licenses.js` are 302-to-login
  anonymous and 403 for non-admin (the FormLogin helper exists in the config
  test dir); trials fencing unchanged.
- `LicenseThrottleTest` modeled on `LoginThrottleTest`: only UNKNOWN_KEY
  counts, window, cap, 429.

## B8. Why this shape

- No new database: licenses live in an atomic file store exactly like the
  real `GrantStore`/`TrialStore` siblings — survives restarts, zero migration
  risk, audit Postgres untouched.
- No changes to existing security behavior: one *additional* filter chain for
  `/api/license/**`; `relaySecurity` stays byte-identical, and the only other
  SecurityConfig edit is the `admin-licenses.js` literal (B4).
- **No sweeper, and that is the correction, not an omission.** Sweepers in
  this codebase exist to close sessions and sockets promptly
  (`TrialSweeper.java:17-29`: "a tidier, not the control"), or are the
  control only where sign-in has no request-time fence
  (`AccountExpirySweeper.java:19-26`). A license holds no session and no
  socket; validation *is* the fence and re-derives EXPIRED from `expiresAt`
  on every call. The original plan's sweeper would have mutated `status` into
  a value the enum doesn't have — and any stored EXPIRED silently breaks
  `extend`-as-renewal, because validation requires `status==ACTIVE`. Deriving
  expiry means there is nothing a stopped scheduler could fail to enforce and
  nothing a renewal has to un-mark.
- Fail direction, stated once: an unreadable store answers 503 (clients
  retry), never a definitive verdict (which would mass-lock paying clients
  over a corrupt file) and never a context stop (licensing must not take the
  relay down).

## B9. Work order (server, small → big)

1. `LicenseProperties` + `License` + `LicenseStore` + `LicenseStoreTest` (B1, B7)
2. `LicenseService` + `LicenseServiceTest` (verdicts, boundary, extend arithmetic) (B1, B7)
3. `licenseApi` chain + `LicenseApiController` + `LicenseThrottle` + posture/throttle tests (B2, B5, B7)
4. `LicenseAdminController` + posture additions (B3, B7)
5. `admin.html` Licenses section + `admin-licenses.js` + ADMIN matcher literal + posture path — **one commit** (B4)
6. `application.yml` block + `env.example`; full test run (B6)
7. Deploy per B6 (scp/install/restart + hand-edit env); smoke checklist (B6)
8. Only then the client side's A5 step 4 onward (`build_config.py` points at the live endpoint).

## B10. Explicitly NOT in the server part

No DB audit rows for license actions (the store file + logs + panel are the
record — keeps "audit Postgres untouched" true, no migration). No per-machine
binding. No key hashing (decision 2). No MOD access to the panel. No bearer
token (decision 1). No changes to trials, accounts, or the existing chain
beyond the two literals named in B4.

---

# REPO A — sportybet-automation (client app) — revised

Repo state this plan now starts from: the tree has one commit ("Initial commit")
and an uncommitted implementation of most of the old A1/A2: `licensing/`
(client.py, config.py, gate.py, store.py), `packaging/` (spec + both build
scripts), `test_licensing.py`, gate wiring in `sportybet_hold_ui.py` (:69-71,
:133-135, :279-289, :194-195) and `hold_ui.html` (key overlay :191, lock
overlay :205, pill :176). So the work is in three kinds: reconcile what exists,
bring it up to the corrected spec, and build what's genuinely missing.

## A0. Reconcile and commit first

Resolve two divergences from the original plan before anything else lands:

1. `sportybet_hold_flow.py` has five changed hunks (~562/1148/1306/1371/1404)
   and `cashout_bet` was dropped from the POST allowlist, though the plan
   promised no flow changes. Decide whether each is licensing-related or
   separate work; commit separately either way.
2. `licensing/config.py` plus the `save_config`/`reset` actions and
   configurable UI port weren't in the plan. Adopt them into it (they're
   reasonable) or strip them.
3. Commit in reviewed chunks: flow changes (if kept) → licensing/ → UI wiring
   → packaging/. No new work on an uncommitted blob.
4. Keep the untracked nested `betradar-clone/` directory out of commits and
   builds (.gitignore or remove).

## A1. licensing/ package — the spec the existing code must satisfy

**client.py** — `check(key)` → POST `{server}/api/license/check`, 4s timeout,
stdlib urllib only (keep it dependency-free). Classification rule, the
load-bearing part:

- HTTP 200 with a parseable verdict → that verdict: `("OK", seconds_left,
  expires_at)` or `EXPIRED` / `REVOKED` / `UNKNOWN_KEY`. Return `expires_at`
  too — the pill needs it, the old plan only returned seconds_left.
- Everything else → `UNREACHABLE`: timeout, DNS failure, any non-200
  (401/403/429/5xx included), unparseable body. A transport failure must never
  surface as a definitive lock — the server returns 503 when its own store is
  unreadable for exactly this reason.
- Send `Authorization: Bearer` only if `build_config.LICENSE_API_TOKEN` is
  non-empty. Per decision 1 the server ships without a token, so the default
  is empty — but the client keeps both modes so the server can reverse it.

**store.py** — paths as before (`%APPDATA%/SportyPilot/license.json`,
`~/Library/Application Support/SportyPilot/license.json`); makedirs; chmod
0600 on POSIX; normalize the key on save (strip, uppercase).

**gate.py** — corrected behavior:

- Startup: no key file → `needs_key`. Key present → up to 3 checks over ~60s
  (0s / 15s / 45s) before declaring `locked:UNREACHABLE` — this absorbs the
  ~6s relay restart on every server deploy. Any definitive verdict
  short-circuits the ladder.
- Running: re-check every 12h; also schedule a check at `expires_at` whenever
  `seconds_left < 12h` (bounds post-expiry overrun to minutes instead of 12h);
  check immediately on wake (monotonic-clock gap > ~2 min between ticks).
- Definitive non-OK while running → enqueue the existing graceful stop via
  ControlBridge, state `locked:<verdict>`. UNREACHABLE while running → one
  retry after 15 min, then locked (still the graceful stop).
- While locked, the timer keeps running: poll every 15 min. This is what makes
  "admin extends → client silently re-enables" true without a restart. A poll
  that comes back OK clears the lock and re-enables the UI.
- `activate(key)`: validate against the server first; persist only on OK (a
  typo'd key must not stick). `deactivate()`: delete the file → `needs_key`.
- The gate talks to the flow only through the existing ControlBridge queue
  (already the normal cross-thread path) — never into flow internals.

## A2. UI wiring (mostly exists — verify against this)

- `/api/state` license fields: `{status: ok|needs_key|locked, verdict,
  seconds_left, expires_at, checking}`.
- `start_session` refuses unless the gate says OK (already at :133-135).
- `hold_ui.html`: pill thresholds as planned (🟢 / 🟡 <3d / 🔴). The lock
  overlay must carry two different texts: EXPIRED/REVOKED/UNKNOWN_KEY →
  "License expired or revoked — contact your provider" with Re-check +
  Change-key buttons; UNREACHABLE → "Can't reach the license server — check
  your connection" with a Retry button. One overlay, text and buttons chosen
  by verdict. (The single "contact your provider" message shown to a user
  whose Wi-Fi is down is a guaranteed support call.)
- Key-entry overlay normalizes input client-side and shows the server verdict
  on a failed activation.

## A3. Corrected behavior matrix (replaces the old one)

| Moment | Server says OK | EXPIRED / REVOKED / UNKNOWN | Unreachable |
|---|---|---|---|
| Startup, no key file | — show key entry | — | — show key entry |
| Startup, key stored | run; show days left | locked, definitive text; re-activate offered | 3 tries over ~60s → locked, network text + Retry |
| Every 12h, at expires_at, on wake | update days left | graceful stop → locked | retry in 15 min → locked |
| While locked (any cause) | poll every 15 min; OK → unlock, UI re-enables | stays locked | stays locked, network text |
| Admin extends key | picked up in ≤15 min locked / ≤12h running — nothing ships | | |

## A4. Packaging — corrected

- The app is **not** plain CPython. Runtime deps: `playwright` (the pip
  package — `connect_over_cdp` still runs through its bundled Node driver),
  `requests`, `pycryptodome`. Only Playwright's downloaded browsers are
  unneeded (the app drives AdsPower); the driver binary is needed and
  PyInstaller does not collect it automatically — the spec needs
  `collect_all('playwright')` or explicit datas, and this is the known-fiddly
  pair, so a build is done only after a real launch test on that OS.
- Add a pinned `requirements.txt` — none exists anywhere, and
  `test_licensing.py:32-34` currently points at a venv from a different
  project. Build scripts create a fresh venv from it.
- Windows launch gap: `open_window` is macOS-only (`open -na "Google Chrome"`,
  ui.py:204-215), and `console=False` removes the fallback URL print — a
  Windows user double-clicks the exe and sees nothing. Add a Windows path:
  `chrome.exe --app` via the App Paths registry key / standard install
  locations, falling back to `os.startfile(url)`.
- `build_config.py` (still unbuilt): `SERVER_BASE_URL`, `VERSION`,
  `LICENSE_API_TOKEN` (empty = send no header; empty is the default per
  decision 1), env-overridable, imported by `licensing/client.py`, baked by
  the spec.
- One-folder build recommended over one-file (startup latency, AV false
  positives). Codesigning notes unchanged (self-signed/ad-hoc for beta,
  Gatekeeper right-click-open in the distributed README, AdsPower as a
  documented prerequisite).

## A5. Work order (client)

1. A0 reconciliation and commits.
2. Bring gate.py/client.py up to the A1 spec (classifier rule, startup ladder,
   locked-state polling, expiry-time check) + unit tests for the classifier
   against a fake server (200-verdicts, 503, timeout, garbage).
3. Two-text lock overlay + Retry (A2).
4. requirements.txt + build_config.py.
5. Mac build → launch test. Windows launch path → Windows build on a VM →
   launch test.
6. End-to-end against the live server with a throwaway key: activate; revoke
   while running (graceful stop observed); extend while locked (≤15 min
   pickup); expiry rollover at `expires_at`.

## A6. Explicitly NOT in this part

Unchanged list (signal feed, strategy engine, JWT/HMAC, offline grace,
multi-seat, auto-update, purchased certs) — plus one honesty line worth
keeping in the doc: the gate is client-side Python and a determined user can
strip it; this design enforces against honest customers, and the only hard
guarantee is that the server never answers OK past expiry. The two server-side
dependencies this section had are now settled in the header: no bearer token
(decision 1, client defaults to no header) and 503-on-unreadable-store (B2).

Two flow-on notes: `sportybet_hold_flow.py`'s own CLI `main()` bypasses the
gate entirely — irrelevant for distributed binaries, real if source ever
ships; and the UI server's 127.0.0.1-only bind is already correct, keep it.

---

# Combined order of execution

Server first — the client has nothing to test against otherwise:
B9 steps 1-7 (package → chain → admin → UI panel → config → deploy → smoke),
then A5 steps 1-3 in parallel with the server tail if wanted (they need no
live server), then A5 steps 4-6 against the deployed endpoint.
