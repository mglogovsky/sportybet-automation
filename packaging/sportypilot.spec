# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller spec for SportyPilot (the SportyBet Hold UI app).

Collects:
  - sportybet_hold_ui.py        (entry point)
  - sportybet_hold_flow.py      (worker flow, imported normally)
  - adspower.py                 (AdsPower CDP client — vendored in this repo,
                                 collected like any other local source file)
  - hold_ui.html                (served page, bundled as data)
  - licensing/                  (license client/store/gate package)

Notes:
  - Build natively per OS (PyInstaller does not cross-compile).
  - console=False: no terminal window; the app is the Chrome --app window.
  - No bundled browser: the app drives the user's AdsPower over CDP.
  - hold_ui.html is read via Path(__file__).parent, which inside the frozen
    bundle resolves to the extraction dir — placing it at the data root works.
  - Playwright ships its node driver inside the package — collect it whole,
    otherwise the frozen app can't launch browsers.
"""
from pathlib import Path

from PyInstaller.utils.hooks import collect_all

REPO = Path(SPECPATH).parent.resolve()                 # sportybet-automation/

datas = [
    (str(REPO / "hold_ui.html"), "."),
]
binaries = []
hiddenimports = [
    "licensing",
    "licensing.client",
    "licensing.store",
    "licensing.gate",
    "licensing.config",
    "build_config",
]

_d, _b, _h = collect_all("playwright")
datas += _d
binaries += _b
hiddenimports += _h

# pywebview (native window, WKWebView) — collect_all pulls in the platform
# backends (webview.platforms.cocoa …) that PyInstaller's static analysis
# misses because they're imported dynamically.
_d, _b, _h = collect_all("webview")
datas += _d
binaries += _b
hiddenimports += _h

a = Analysis(
    [str(REPO / "sportybet_hold_ui.py")],
    pathex=[str(REPO)],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
)

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="FeedWire - Sporty Bet",
    # .ico applies to the Windows build; ignored on macOS (BUNDLE uses .icns).
    icon=str(REPO / "packaging" / "FeedWire.ico"),
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,
    disable_windowed_traceback=False,
)

coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    name="FeedWire - Sporty Bet",
)

# macOS .app bundle (ignored on Windows)
app = BUNDLE(
    coll,
    name="FeedWire - Sporty Bet.app",
    icon=str(REPO / "packaging" / "FeedWire.icns"),
    bundle_identifier="pro.feedwire.sportybet",
    info_plist={
        "CFBundleName": "FeedWire - Sporty Bet",
        "CFBundleDisplayName": "FeedWire - Sporty Bet",
        "CFBundleShortVersionString": "0.1.0",
        "CFBundleVersion": "0.1.0",
        "LSUIElement": False,
    },
)
