"""SportyPilot user config — small JSON file next to the license key file.

Location: same platform directory as licensing/store.py's license.json:
  Windows: %APPDATA%/SportyPilot/config.json
  macOS:   ~/Library/Application Support/SportyPilot/config.json
  other:   ~/.config/sportypilot/config.json

Keys (all optional):
  adspower_api_base  AdsPower local API base, e.g. "http://127.0.0.1:50325"
  adspower_api_token AdsPower API token, if the AdsPower API key auth is on
  ui_port            local UI port for sportybet_hold_ui.py

Resolution orders (highest wins):
  ui port:             CLI --port → env SPORTYPILOT_PORT → config.ui_port
                       → DEFAULT_PORT (8790)
"""
from __future__ import annotations

import json
import os
from pathlib import Path

from .store import store_path

DEFAULT_ADSPOWER_API_BASE = "http://127.0.0.1:50325"


def config_path() -> Path:
    return store_path().with_name("config.json")


def load() -> dict:
    try:
        data = json.loads(config_path().read_text())
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def save(cfg: dict) -> None:
    path = config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(cfg, indent=2))
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass


def update(**fields) -> dict:
    """Merge non-None fields into the config file; empty strings delete."""
    cfg = load()
    for k, v in fields.items():
        if v is None:
            continue
        if v == "":
            cfg.pop(k, None)
        else:
            cfg[k] = v
    save(cfg)
    return cfg


def adspower_api_base() -> str:
    return (os.environ.get("ADSPOWER_API_BASE")
            or str(load().get("adspower_api_base") or "").strip()
            or DEFAULT_ADSPOWER_API_BASE)


def adspower_api_token() -> str | None:
    tok = (os.environ.get("ADSPOWER_API_TOKEN")
           or str(load().get("adspower_api_token") or "").strip())
    return tok or None


def ui_port(default: int) -> int:
    env = os.environ.get("SPORTYPILOT_PORT")
    if env:
        try:
            return int(env)
        except ValueError:
            pass
    try:
        return int(load().get("ui_port"))
    except (TypeError, ValueError):
        return default
