"""Build-time configuration for SportyPilot (licensed build).

Values are env-overridable so a server move or token rotation is a rebuild /
relaunch, not a code edit. Baked into frozen builds via the PyInstaller spec.

Per PLAN-licensing.md v2 decision 1: the license endpoint ships with NO
bearer token — LICENSE_API_TOKEN defaults to EMPTY, and an empty token means
the client sends no Authorization header at all. The mode is kept so the
server can reverse the decision later without a client change.
"""
from __future__ import annotations

import os

VERSION = "0.1.0"

SERVER_BASE_URL = os.environ.get("LICENSE_SERVER_URL", "https://localhost:8443")
LICENSE_API_TOKEN = os.environ.get("LICENSE_API_TOKEN", "")
