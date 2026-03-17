# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller spec for SpaceRouter Desktop GUI."""

import sys
from PyInstaller.utils.hooks import collect_submodules

block_cipher = None

hiddenimports = [
    # Conditionally imported at runtime
    "miniupnpc",
    # pydantic v2 uses a Rust-compiled core loaded dynamically
    "pydantic",
    "pydantic_core",
    "pydantic_settings",
    "pydantic_settings.main",
    # dotenv loaded by pydantic-settings
    "dotenv",
    # httpx transport stack
    "httpx",
    "httpcore",
    "h11",
    "certifi",
    "idna",
    "sniffio",
    "anyio",
    "anyio._backends._asyncio",
    # cryptography internals sometimes missed
    "cryptography.hazmat.primitives.asymmetric.rsa",
    "cryptography.hazmat.primitives.hashes",
    "cryptography.hazmat.primitives.serialization",
    "cryptography.x509",
    "cryptography.x509.oid",
    # pywebview
    "webview",
]

# Platform-specific webview backends
if sys.platform == "darwin":
    hiddenimports += [
        "webview.platforms.cocoa",
        "objc",
        "Foundation",
        "AppKit",
        "WebKit",
    ]
elif sys.platform == "win32":
    hiddenimports += [
        "webview.platforms.edgechromium",
        "webview.platforms.winforms",
        "clr",
    ]

# Collect all pydantic submodules to handle dynamic imports
hiddenimports += collect_submodules("pydantic")
hiddenimports += collect_submodules("pydantic_core")

a = Analysis(
    ["gui/app.py"],
    pathex=[],
    binaries=[],
    datas=[
        ("gui/assets", "gui/assets"),
    ],
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[
        "pytest",
        "pytest_asyncio",
        "respx",
        "_pytest",
    ],
    noarchive=False,
    optimize=0,
)

pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name="SpaceRouter",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)

# macOS .app bundle
if sys.platform == "darwin":
    app = BUNDLE(
        exe,
        name="SpaceRouter.app",
        icon=None,  # TODO: add icon.icns when available
        bundle_identifier="com.spacerouter.desktop",
        info_plist={
            "CFBundleShortVersionString": "0.1.0",
            "CFBundleName": "SpaceRouter",
            "NSHighResolutionCapable": True,
        },
    )
