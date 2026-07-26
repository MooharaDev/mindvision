#!/usr/bin/env python3
"""
make_eft_package.py — build (or verify) the EFT transfer zip for Corpus.

Build:   python make_eft_package.py [--out DIR]
         Creates pdf2db_eft_<version>_<yyyymmdd>.zip containing exactly the
         whitelisted deliverable files plus MANIFEST.sha256 (a checksum per
         file, so integrity can be verified after the transfer).

Verify:  python make_eft_package.py --verify path/to/package.zip
         Recomputes every checksum inside the zip against its manifest.

Never includes: webapp_data/, test outputs, virtualenvs, caches, or any data.
"""

import argparse
import hashlib
import sys
import time
import zipfile
from pathlib import Path

VERSION = "4.2"
BASE = Path(__file__).resolve().parent
FILES = [
    "pdf2db.py",
    "webapp.py",
    "webapp_selftest.py",
    "make_eft_package.py",
    "requirements.txt",
    "README.md",
    "README_TRANSFER.md",
    "templates/index.html",
    "static/app.css",
    "static/app.js",
    "static/create.js",
    "static/database.js",
    "static/settings.js",
    "schemas/failure_reports.json",
]
SIZE_LIMIT = 3 * 1024 ** 3  # EFT hard limit


def sha256(data):
    return hashlib.sha256(data).hexdigest()


def build(out_dir):
    missing = [f for f in FILES if not (BASE / f).exists()]
    if missing:
        print(f"FATAL: missing files: {missing}")
        return 1
    name = f"pdf2db_eft_v{VERSION}_{time.strftime('%Y%m%d')}.zip"
    out = Path(out_dir) / name
    manifest_lines = []
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as z:
        for f in FILES:
            data = (BASE / f).read_bytes()
            z.writestr(f"pdf2db/{f}", data)
            manifest_lines.append(f"{sha256(data)}  {f}")
        z.writestr("pdf2db/MANIFEST.sha256", "\n".join(manifest_lines) + "\n")
    size = out.stat().st_size
    print(f"built  {out}")
    print(f"       {len(FILES)} files, {size / 1024:.0f} KB "
          f"({size / SIZE_LIMIT:.4%} of the 3 GB EFT limit)")
    print("verify after transfer with: "
          f"python make_eft_package.py --verify {name}")
    return 0


def verify(zip_path):
    p = Path(zip_path)
    if not p.exists():
        print(f"FATAL: {p} not found")
        return 1
    bad = []
    with zipfile.ZipFile(p) as z:
        manifest = z.read("pdf2db/MANIFEST.sha256").decode().strip().splitlines()
        for line in manifest:
            digest, fname = line.split(None, 1)
            fname = fname.strip()
            try:
                actual = sha256(z.read(f"pdf2db/{fname}"))
            except KeyError:
                bad.append(f"{fname}: MISSING")
                continue
            if actual != digest:
                bad.append(f"{fname}: checksum mismatch")
            else:
                print(f"ok  {fname}")
    if bad:
        print("\nFAILED:")
        for b in bad:
            print("  " + b)
        return 1
    print(f"\nall {len(manifest)} files verified — package is intact")
    return 0


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default=str(BASE.parent),
                    help="directory to write the zip into (default: parent dir)")
    ap.add_argument("--verify", metavar="ZIP",
                    help="verify an existing package instead of building")
    args = ap.parse_args()
    sys.exit(verify(args.verify) if args.verify else build(args.out))
