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

Dependency wheels: every wheels/*.whl is packaged so the target can install
fully offline (pip install --no-index --find-links wheels -r requirements.txt).
Targets are Linux x86_64 and Windows amd64 only (no macOS in the network).
Refresh them on the connected side with:
    pip download -r requirements.txt -d wheels --only-binary=:all: \
        --platform manylinux_2_28_x86_64 --python-version 310
    (repeat for win_amd64, and python-version 311-313 for markupsafe,
     which ships per-version wheels)
"""

import argparse
import hashlib
import sys
import time
import zipfile
from pathlib import Path

VERSION = "4.3"
BASE = Path(__file__).resolve().parent
# Runtime files + context docs, nothing else. No test harnesses (pdf2db.py
# --selftest is built into the engine itself) and no build tooling; integrity
# is checked on the far side with sha256sum -c, so this script does not ride
# along. The .md files travel deliberately: an internal Claude Code instance
# uses CLAUDE.md/README*/PRODUCT/DESIGN as project context when maintaining
# this code inside the network.
FILES = [
    "pdf2db.py",
    "webapp.py",
    "requirements.txt",
    "CLAUDE.md",
    "README.md",
    "README_TRANSFER.md",
    "templates/index.html",
    "static/app.css",
    "static/app.js",
    "static/create.js",
    "static/database.js",
    "static/settings.js",
    "schemas/failure_reports.json",  # served by /api/example-schema
]
ROOT_DOCS = ["PRODUCT.md", "DESIGN.md"]  # live in the repo root, one level up
SIZE_LIMIT = 3 * 1024 ** 3  # EFT hard limit


def sha256(data):
    return hashlib.sha256(data).hexdigest()


def build(out_dir):
    # (source path on disk, path inside the zip / in the manifest)
    pairs = [(BASE / f, f) for f in FILES] + \
            [(BASE.parent / d, d) for d in ROOT_DOCS]
    missing = [str(src) for src, _ in pairs if not src.exists()]
    if missing:
        print(f"FATAL: missing files: {missing}")
        return 1
    wheels = sorted((BASE / "wheels").glob("*.whl"))
    if not wheels:
        print("FATAL: wheels/ is empty — the package must carry the pinned "
              "dependencies so the target can install offline. See the "
              "pip download commands at the top of this file.")
        return 1
    pairs += [(w, f"wheels/{w.name}") for w in wheels]
    name = f"pdf2db_eft_v{VERSION}_{time.strftime('%Y%m%d')}.zip"
    out = Path(out_dir) / name
    manifest_lines = []
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as z:
        for src, arc in pairs:
            data = src.read_bytes()
            z.writestr(f"pdf2db/{arc}", data)
            manifest_lines.append(f"{sha256(data)}  {arc}")
        # plain .txt so the transfer gateway's file-type allowlist accepts it;
        # sha256sum -c reads it regardless of the name
        z.writestr("pdf2db/MANIFEST.txt", "\n".join(manifest_lines) + "\n")
    size = out.stat().st_size
    print(f"built  {out}")
    print(f"       {len(pairs)} files ({len(wheels)} wheels), "
          f"{size / 1048576:.1f} MB "
          f"({size / SIZE_LIMIT:.2%} of the 3 GB EFT limit)")
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
        try:  # current name first; .sha256 kept readable for pre-v4.3 packages
            raw = z.read("pdf2db/MANIFEST.txt")
        except KeyError:
            raw = z.read("pdf2db/MANIFEST.sha256")
        manifest = raw.decode().strip().splitlines()
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
