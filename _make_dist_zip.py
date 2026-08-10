# -*- coding: utf-8 -*-
"""Package the built exe into a versioned dist zip matching the sibling
projects' convention:  <Name>_dist_v<X>_<Y>_<Z>.zip  containing a top
folder <Name>_dist/ with the runnable distributable inside.
"""
import os
import re
import sys
import zipfile

sys.stdout.reconfigure(encoding="utf-8")

NAME = "Auto-Continue"
HERE = os.path.dirname(os.path.abspath(__file__))

# Derive the version from the source of truth. Hardcoding it here meant the
# zip kept the previous release's name after a version bump, so the file on
# disk claimed to be a version it wasn't.
with open(os.path.join(HERE, "auto_continue.py"), encoding="utf-8") as fh:
    _m = re.search(r'^APP_VERSION\s*=\s*"([\d.]+)"', fh.read(), re.M)
if not _m:
    print("ERROR: cannot read APP_VERSION from auto_continue.py")
    sys.exit(1)
VERSION = _m.group(1).replace(".", "_")

TOP = f"{NAME}_dist"
OUT = rf"D:\claude\{NAME}_dist_v{VERSION}.zip"

# (source path, name inside the zip's top folder)
MEMBERS = [
    (os.path.join(HERE, "dist", "Auto-Continue.exe"), "Auto-Continue.exe"),
    (os.path.join(HERE, "README.md"), "README.md"),
    (os.path.join(HERE, "LICENSE"), "LICENSE"),
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

# Only the newest build is worth keeping: an older zip sitting beside it is
# something someone will eventually pick up believing it is current — this
# folder had a v2.0.5 package in it four releases later.
#
# The pattern is deliberately exact. D:\claude is shared with the sibling
# projects (CMIS, COM, REA, Scope_Extractor all drop packages here), so
# anything not named for THIS project at a numeric version is left alone.
_KEEP = os.path.basename(OUT)
_pat = re.compile(rf"^{re.escape(NAME)}_dist_v\d+(?:_\d+)*\.zip$")
for _name in sorted(os.listdir(os.path.dirname(OUT))):
    if _name == _KEEP or not _pat.match(_name):
        continue
    _old = os.path.join(os.path.dirname(OUT), _name)
    try:
        os.remove(_old)
        print(f"pruned older package: {_name}")
    except OSError as _e:
        print(f"could not remove {_name}: {_e}")
