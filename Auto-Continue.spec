# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller spec for the Auto-Continue GUI watchdog.

Build:
    pyinstaller Auto-Continue.spec --clean --noconfirm

Output:
    dist/Auto-Continue.exe   (single-file, no console, no deps)
"""
from PyInstaller.utils.hooks import collect_data_files

# uiautomation ships a `bin/` folder with the native UIAutomationCore shim;
# PyInstaller must copy it next to the bundled module.
datas = collect_data_files("uiautomation")

a = Analysis(
    ["Auto-Continue.pyw"],
    pathex=[],
    binaries=[],
    datas=datas,
    hiddenimports=[
        "pytz",
        "uiautomation",
    ],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[
        # Trim PyQt6 modules we don't import to keep the exe smaller.
        "PyQt6.QtWebEngineCore",
        "PyQt6.QtWebEngineWidgets",
        "PyQt6.QtMultimedia",
        "PyQt6.QtMultimediaWidgets",
        "PyQt6.QtBluetooth",
        "PyQt6.QtNetwork",
        "PyQt6.QtQml",
        "PyQt6.QtQuick",
        "PyQt6.QtQuick3D",
        "PyQt6.QtQuickWidgets",
        "PyQt6.QtPdf",
        "PyQt6.QtPdfWidgets",
        "PyQt6.QtCharts",
        "PyQt6.QtDataVisualization",
        "PyQt6.Qt3DCore",
        "PyQt6.Qt3DRender",
        "PyQt6.Qt3DAnimation",
        "PyQt6.QtSensors",
        "PyQt6.QtSerialPort",
        "PyQt6.QtNfc",
        "PyQt6.QtPositioning",
        "PyQt6.QtLocation",
        "PyQt6.QtTest",
        "PyQt6.QtHelp",
        "PyQt6.QtDesigner",
        "PyQt6.QtOpenGL",
        "PyQt6.QtOpenGLWidgets",
        "PyQt6.QtSvg",
        "PyQt6.QtSvgWidgets",
        "PyQt6.QtPrintSupport",
        "PyQt6.QtSql",
        "PyQt6.QtXml",
        # Heavy stdlib modules not used.
        "tkinter",
        "unittest",
    ],
    noarchive=False,
    optimize=0,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name="Auto-Continue",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=False,        # GUI app, no console window
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=None,
    # Pin to current user's app to prevent shared %TEMP% extraction races
    # when multiple watchdogs run in parallel (User runs multiple sessions).
    contents_directory=".",
)
