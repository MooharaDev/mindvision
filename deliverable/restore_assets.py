#!/usr/bin/env python3
"""restore_assets.py — undo the transfer-safe renaming of the UI assets.

The EFT gateway does not accept .html/.css/.js files, so the package ships
them with a `.txt` suffix appended (templates/index.html.txt, static/*.txt).
Run this ONCE after unzipping — after checking MANIFEST.txt, which lists the
files under their shipped (.txt) names:

    python restore_assets.py

It strips the trailing .txt from every *.html.txt / *.css.txt / *.js.txt
under templates/ and static/. A pure rename: the file bytes are untouched,
so nothing about the content or format changes. Idempotent — running it
again finds nothing to do.
"""
from pathlib import Path

BASE = Path(__file__).resolve().parent
REAL_SUFFIXES = (".html", ".css", ".js")

renamed = 0
for folder in ("templates", "static"):
    d = BASE / folder
    if not d.is_dir():
        print(f"skip  {folder}/ — not found (wrong working directory?)")
        continue
    for p in sorted(d.glob("*.txt")):
        target = p.with_name(p.name[: -len(".txt")])
        if not target.name.endswith(REAL_SUFFIXES):
            continue                      # some other .txt — leave it alone
        if target.exists():
            print(f"skip  {folder}/{target.name} already exists")
            continue
        p.rename(target)
        print(f"ok    {folder}/{p.name} -> {target.name}")
        renamed += 1

print(f"\n{renamed} file(s) restored — the console is ready to run."
      if renamed else
      "\nNothing to restore — the assets already have their real names.")
