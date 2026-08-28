# License gate — architecture (draft for approval, 2026-08-28)

Goal: before the main UI loads, require a license key. A valid key activates
**this machine only** (the key is bound to a machine hash). Invalid / missing /
expired → the app shows only the license screen; the API refuses everything else.

## 0. Honesty box (read first)

This is a local Python + HTML app. Anyone with read access to the source can
edit the check out. Licensing here is a **speed bump against casual
sharing/cloning**, not DRM. The design below is the strongest thing that is
still simple and fully offline. If you ever need real enforcement, the only
upgrade path is an online activation server (§7).

## 1. Machine identity (the "hash to this machine")

One stable hardware/install ID per platform, read with zero admin rights:

| Platform | Source | How |
|---|---|---|
| macOS | **IOPlatformUUID** (hardware UUID; survives reboots and OS reinstalls) | `ioreg -rd1 -c IOPlatformExpertDevice` |
| Windows | **MachineGuid** (created at Windows install; stable per install) | registry `HKLM\SOFTWARE\Microsoft\Cryptography\MachineGuid` via stdlib `winreg` |

Fallbacks: macOS → `system_profiler` serial number; Windows → WMI
`Win32_ComputerSystemProduct.UUID` (VMs, weird hardware). `machine_id()`
picks by `sys.platform` and returns the first that reads cleanly.

- Never stored raw. What we persist and display is:

```
machine_hash = SHA-256( "sportybet-hold-v1:" + machine_id )          # internal
machine_code = base32(machine_hash)[:16]  →  "7K2Q-9D4F-X1AB-8C3E"   # shown in UI
```

The **machine code** is what the operator sends you when asking for a key.
(The HMAC key scheme in §2 is platform-agnostic — only this ID source
differs per OS.)

## 2. License key format (offline, machine-bound, expirable)

Human-typable, self-describing, verifiable without any server:

```
SBH1-20261231-K7QX9DM4T2A8C3EF
 │      │            │
 │      │            └─ HMAC-SHA256(SECRET, "SBH1|20261231|<machine_hash>"),
 │      │               truncated to 10 bytes, base32 (16 chars)
 │      └─ expiry date (UTC) — embedded so the app can display it
 └─ format version
```

- `SECRET` is a random 32-byte string embedded in the code (obfuscation-grade
  only — see §0) and in the issuer CLI. Only someone with the secret can mint
  valid keys.
- Validation = recompute HMAC for (expiry, this machine's hash) and
  constant-time-compare + check expiry against `now`. No key database needed.
- Keys can also be made **non-expiring** with expiry `NEVER`
  (`SBH1-NEVER-…`), same scheme.
- Input is normalized before checking: uppercase, strip spaces/dashes, so
  pasting with weird formatting can't fail spuriously.

## 3. The gate (server-side — the only place that matters)

A JS-only gate is bypassable with one `curl` to `/api/action`. So the gate
lives in `sportybet_hold_ui.py`:

```
unlicensed                          licensed
────────────────────────────        ────────────────────────────────
GET /            → license page     GET /            → hold_ui.html
GET /api/license → status JSON      GET /api/state   → normal
POST /api/license→ activate         GET /api/profiles→ normal
everything else  → 403 JSON         POST /api/action → normal
```

- `App` gets a `LicenseManager`. It evaluates once at boot and after every
  activation attempt. `start_session()` also re-checks (defense in depth, in
  case route gating is ever refactored).
- The **flow worker cannot start unlicensed**, even if someone hand-crafts
  requests.

### License state machine

```
        boot
          │
   license file exists?
     no ──┴── yes
     │        │
     ▼        ▼
 UNLICENSED  validate: signature ok? machine_hash matches? not expired?
     │        │            any fail                all pass
     │        ▼               │                      │
     │   UNLICENSED ◄─────────┘                LICENSED
     │        │  (reason shown: invalid /        (expiry shown
     │        │   wrong machine / expired)       in header chip)
     ▼        ▼
   license page ←→ POST /api/license {key} ──ok──▶ write file → LICENSED
```

## 4. License file

- Path (per platform, via one `app_dir()` helper — no repo-relative paths):
  - macOS: `~/Library/Application Support/SportyBetHold/license.json`
  - Windows: `%APPDATA%\SportyBetHold\license.json`
    (e.g. `C:\Users\<name>\AppData\Roaming\SportyBetHold\license.json`)
- Outside the repo and outside the .app/.exe, so app updates never wipe it.
- Contents:

```json
{
  "key": "SBH1-20261231-K7QX9DM4T2A8C3EF",
  "machine_hash": "9f2c…(sha256 hex)",
  "activated_at": 1787870000.0
}
```

- Re-validated on **every boot** (signature + machine + expiry). A copied
  license file on a different machine fails the machine check and is ignored.

## 5. The license page (UI)

Same design system as the main UI (dark card, mono accents):

```
┌──────────────────────────────────────┐
│            🔑  Activation            │
│  this build is locked to one machine │
│                                      │
│  MACHINE CODE                        │
│  ┌────────────────────────────┐ ┌──┐ │
│  │ 7K2Q-9D4F-X1AB-8C3E        │ │⧉ │ │  ← copy button
│  └────────────────────────────┘ └──┘ │
│  send this code to get your key      │
│                                      │
│  LICENSE KEY                         │
│  ┌────────────────────────────────┐  │
│  │ SBH1-…                         │  │
│  └────────────────────────────────┘  │
│  [ error line, red, when invalid ]   │
│                                      │
│  ▶  ACTIVATE                         │
└──────────────────────────────────────┘
```

- Small, separate `license.html` served only in the unlicensed state — the
  main page is never delivered before activation.
- After a successful POST the page swaps to the normal UI (no restart).
- Once licensed, a tiny header chip shows `lic · 2026-12-31` (or `lic ·
  never`); clicking it is a no-op — just informational. 30 days before expiry
  it turns amber; expired → back to the license page at next boot.

## 6. Issuer side (you only — never shipped)

`make_license.py` (lives wherever you keep it, not in this repo ideally):

```
$ python3 make_license.py --machine-code 7K2Q-9D4F-X1AB-8C3E --days 90
SBH1-20261126-QX9DM4T2A8C3EF7K
```

- Contains the same SECRET; decodes the machine code back to the hash prefix
  (we mint against the full 16-char code — 80 bits, collision-safe).
- Options: `--days N` or `--never`.
- You lose the SECRET → you can't mint; rotating it invalidates all old keys
  (acceptable at this scale).

## 7. Rejected / deferred options

- **Online activation server** (key ↔ server DB, seat counts, revocation):
  real control, but needs infra, uptime, offline grace logic. Defer — the
  offline scheme covers "don't let a copied folder run elsewhere".
- **Asymmetric signed licenses (Ed25519)**: nicer (public key in app,
  no shared secret), but the minting CLI and key parsing are heavier; the
  HMAC scheme gives the same practical strength here because the app source
  is visible anyway (§0). Switch later without changing the UX: the key
  string format is versioned (`SBH1-…` → `SBH2-…`).
- **Machine code from MAC address**: changes with interfaces/VPNs — rejected.
- **Guarding only the UI**: bypassable via direct API calls — rejected (§3).

## 8. Implementation checklist (once approved)

1. `license_mgr.py` — platform machine id (`machine_id()` per §1), hash/code,
   key mint/verify, license file IO, `app_dir()` (macOS + Windows).
2. `make_license.py` — issuer CLI (same crypto, `--days/--never`).
3. `license.html` — activation page (shared CSS).
4. `sportybet_hold_ui.py` — LicenseManager wiring: route gate, `/api/license`
   GET/POST, `start_session` guard, header chip field in `/api/state`;
   `open_window()` Windows branch.
5. `hold_ui.html` — header license chip (reads `S.license`).
6. `sportybet_hold.spec` + `build_app.sh` / `build_app.bat` — PyInstaller
   builds for macOS and Windows (run on each OS, or CI matrix later).
7. Tests: mint→activate round-trip, wrong-machine rejection, expired
   rejection, typo-tolerant input, 403s while unlicensed, boot re-validation,
   `machine_id()` on both platforms.

## 9. Open questions for you

1. **Expiry**: keys never expire, or default 90/365 days? (scheme supports both)
2. **Who mints**: only you, or should there be a tiny self-service step where
   a buyer's machine code is pasted somewhere? (currently: you, via CLI)
3. **License file location** ok? (macOS `~/Library/Application Support/
   SportyBetHold/`, Windows `%APPDATA%\SportyBetHold\`)
4. Should an **unlicensed** app still allow viewing the log/state (read-only),
   or hard-block everything? (proposal: hard-block, simplest and safest)
5. **Windows builds**: do you have a Windows machine to build on, or should I
   set up the GitHub Actions matrix so both artifacts build in CI?

## 10. Hardening: hiding the source (compiling)

Without this, §0's caveat bites: the license check is plain Python anyone can
delete. Levels, weakest-strongest:

### Level 1 — PyInstaller app bundle (recommended default)

- `pyinstaller --windowed` produces `SportyBet Hold.app` (macOS) and
  `SportyBetHold.exe` (Windows): Python source → embedded bytecode,
  double-clickable, no terminal needed. Casual users can no longer read or
  patch anything.
- **PyInstaller does not cross-compile**: each artifact is built ON its
  platform. One shared `sportybet_hold.spec` + a thin wrapper per OS
  (`build_app.sh` for macOS, `build_app.bat` for Windows). Optional later:
  a GitHub Actions matrix (`macos-latest`, `windows-latest`) that builds both
  on every tag — then you never need a Windows machine yourself.
- Everything the app reads at runtime gets bundled as data: `hold_ui.html`,
  `license.html`, `adspower.py`, the venv deps (playwright, pycryptodome —
  both have Windows wheels; AdsPower has a Windows client and its local API
  is identical).
- Platform shims to write once:
  - `open_window(url)`: macOS `open -na "Google Chrome" --args --app=…` →
    Windows `cmd /c start "" "chrome.exe" --app=…` (probe the standard
    `C:\Program Files\Google\Chrome\Application\chrome.exe` paths, fall back
    to `start <url>` in the default browser).
  - `app_dir()` per §4; machine id per §1.
- Config/state live OUTSIDE the install (`app_dir()`) — a bundled app must
  not depend on install-relative paths.
- Weakness: the bundle can be unpacked (pyinstxtractor) and bytecode
  decompiled. Fine for "don't let casual users clone it", not for adversaries.
- Code-signing warnings: macOS Gatekeeper (right-click → Open, ad-hoc
  `codesign`, or an Apple Developer cert removes it); Windows SmartScreen
  (an EV/OV code-signing cert removes it; without one, users click
  "More info → Run anyway" — normal for small tools).
- Updates become your job (ship a new .app/.exe; the license file survives
  because it lives in app_dir).

### Level 2 — Nuitka (or Cython) on top (optional, if Level 1 leaks)

- Compiles Python to C → native code; reversing requires disassembly, not a
  decompiler. Compile the whole program with Nuitka, or — cheaper — only the
  sensitive modules (`license_mgr`, the flow) with Cython and keep the rest
  under PyInstaller.
- Cost: slower builds, and the Playwright driver (its bundled node runtime)
  needs explicit testing under the compiler. Do this only if Level 1 proves
  insufficient; the license scheme doesn't change.

### Level 3 — server-side brain (the only real protection)

- Client becomes a thin shell; the bet-building/firing logic (and license
  validation) runs on an API you host. Source is never on the user's machine.
- Real cost: hosting, latency in the fire path (bad for this flow's timing),
  and a hybrid split is awkward (browser driving must stay local anyway).
- Verdict: not worth it for a handful of operators. The versioned key format
  (`SBH1-…`) leaves room to add online validation later without changing UX.

### What compiling changes in the checklist

- §8 gains a build step: `build_app.sh` (PyInstaller spec, data files,
  ad-hoc codesign) + a smoke-test run of the .app on a clean profile.
- `license_mgr` reads/writes config under `~/Library/Application Support/
  SportyBetHold/` in BOTH dev and bundled modes (one code path, no surprises).
- The issuer CLI `make_license.py` is never bundled — it stays with you as
  plain source (only the app needs hiding).
