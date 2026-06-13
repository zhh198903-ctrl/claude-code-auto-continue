# -*- coding: utf-8 -*-
"""Package the built exe into a versioned dist zip matching the sibling
projects' convention:  <Name>_dist_v<X>_<Y>_<Z>.zip  containing a top
folder <Name>_dist/ with the runnable distributable inside.
"""
import os
import sys
import zipfile

sys.stdout.reconfigure(encoding="utf-8")

VERSION = "1_0_8"
NAME = "Auto-Continue"
TOP = f"{NAME}_dist"
OUT = rf"D:\claude\{NAME}_dist_v{VERSION}.zip"

# (source path, name inside the zip's top folder)
MEMBERS = [
    (r"D:\claude\auto_continue\dist\Auto-Continue.exe", "Auto-Continue.exe"),
    (r"D:\claude\auto_continue\README.md", "README.md"),
]

for src, _ in MEMBERS:
    if not os.path.exists(src):
        print(f"ERROR missing source: {src}")
        sys.exit(1)

# Remove a stale zip of the same version so we always write fresh.
if os.path.exists(OUT):
    os.remove(OUT)
    print(f"removed stale: {OUT}")

with zipfile.ZipFile(OUT, "w", zipfile.ZIP_DEFLATED, compresslevel=6) as z:
    for src, inner in MEMBERS:
        arc = f"{TOP}/{inner}"
        z.write(src, arcname=arc)
        print(f"added {arc}  ({os.path.getsize(src)/1e6:.1f} MB raw)")

print(f"\nOUT: {OUT}  ({os.path.getsize(OUT)/1e6:.1f} MB zipped)")
