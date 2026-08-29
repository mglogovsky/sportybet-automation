#!/usr/bin/env bash
# Build SportyPilot.app for macOS (run on a Mac — PyInstaller doesn't cross-compile).
set -euo pipefail
cd "$(dirname "$0")/.."

# The build python must have the app's runtime deps (see requirements.txt —
# requests, playwright, pycryptodome) or the frozen app misses them.
# Override with PYTHON=...
PYTHON="${PYTHON:-/Users/martinglogovsky/Documents/GitHub/unified-listener-and-cashout/.venv/bin/python}"

"$PYTHON" -m pip install --quiet pyinstaller

rm -rf build dist
"$PYTHON" -m PyInstaller --noconfirm --distpath dist --workpath build \
    packaging/sportypilot.spec

# Ad-hoc codesign so Gatekeeper shows a consistent identity (beta only).
# For distribution to other Macs, replace with a real Developer ID cert and
# notarize; until then users must right-click → Open (see README).
codesign --force --deep --sign - "dist/FeedWire - Sporty Bet.app" || true

cd dist
zip -qry FeedWire-SportyBet-macos.zip "FeedWire - Sporty Bet.app"
echo "built: dist/FeedWire - Sporty Bet.app and dist/FeedWire-SportyBet-macos.zip"
