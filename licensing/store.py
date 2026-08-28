"""Local license key file — plain JSON, the server is the judge.

Location (platform-appropriate):
  Windows: %APPDATA%/SportyPilot/license.json
  macOS:   ~/Library/Application Support/SportyPilot/license.json
  other:   ~/.config/sportypilot/license.json

Contents: {"key": "SBET-XXXX-XXXX-XXXX"}   — file perms 0600.

SPORTYPILOT_LICENSE_STORE overrides the path (tests / portable installs).
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path


def store_path() -> Path:
    override = os.environ.get("SPORTYPILOT_LICENSE_STORE")
    if override:
        return Path(override).expanduser()
    if sys.platform == "win32":
        base = Path(os.environ.get("APPDATA") or Path.home() / "AppData" / "Roaming")
        return base / "SportyPilot" / "license.json"
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / "SportyPilot" / "license.json"
    return Path.home() / ".config" / "sportypilot" / "license.json"


def load() -> str | None:
    """The stored key, or None if there is no (usable) key file."""
    try:
        data = json.loads(store_path().read_text())
        key = str(data.get("key") or "").strip()
        return key or None
    except Exception:
        return None


def save(key: str) -> None:
    key = key.strip().upper()  # normalize: keys are case-insensitive
    path = store_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(path.parent, 0o700)
    except OSError:
        pass
    path.write_text(json.dumps({"key": key}))
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass


def delete() -> None:
    try:
        store_path().unlink()
    except OSError:
        pass
