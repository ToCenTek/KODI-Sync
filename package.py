#!/usr/bin/env python3
"""Build addon zip using macOS zip -r -X (Kodi requires first entry = folder)."""
import os, re, subprocess, sys, zipfile

BASE = os.path.dirname(os.path.abspath(__file__))
SRCNAME = "service.multiscreen-sync"
SRC = os.path.join(BASE, SRCNAME)
DST = os.path.join(BASE, f"{SRCNAME}.zip")

EXCLUDES = [
    "*.DS_Store",
    "__MACOSX/*",
    "*/.git/*",
    "*/__pycache__/*",
    "*.pyc",
    "*/.gitignore",
    "*/README.md",
    "*/MODIFICATION.md",
    "*/LICENSE",
]

if os.path.exists(DST):
    os.remove(DST)

# Build exclude flags: -x "pattern1" -x "pattern2" ...
exclude_args = []
for pat in EXCLUDES:
    exclude_args.extend(["-x", pat])

cmd = ["zip", "-r", DST, SRCNAME] + exclude_args
print(f"Running: {' '.join(cmd)}")
ret = subprocess.run(cmd, cwd=BASE, capture_output=True, text=True)
if ret.returncode != 0:
    print(ret.stderr)
    sys.exit(1)
print(ret.stdout)

# Verify
with zipfile.ZipFile(DST) as zf:
    names = zf.namelist()
    bad = zf.testzip()
    if bad:
        print(f"CRC ERROR: {bad}")
        sys.exit(1)
    v = zf.read(f"{SRCNAME}/addon.xml").decode()
    ver = re.search(r'version="([^"]+)"', v).group(1)
    first = names[0] if names else "(empty)"
    dirs = [n for n in names if n.endswith("/")]
    print(f"\nOK: {len(names)} entries ({len(dirs)} dirs, {len(names)-len(dirs)} files)")
    print(f"First entry: {first}")
    print(f"Size: {os.path.getsize(DST):,} bytes")
    print(f"Version: {ver}")
