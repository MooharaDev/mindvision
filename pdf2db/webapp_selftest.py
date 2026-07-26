#!/usr/bin/env python3
"""
webapp_selftest.py — offline end-to-end test of the Corpus web console.

Runs the Flask app in-process (no network, no browser, mock LLM only) against
a fabricated archive in a temp directory, exercising every API the frontend
uses: settings (incl. key masking + file permissions), schema library, archive
staging + scanning + schema drafting, corpus creation by promoting a staging
area AND via direct upload AND via server path, zip extraction with traversal
protection, images extraction + serving, paged/filtered/status-filtered tables,
rename, artifacts, the review-and-correct write-back into SQLite, retry, and
deletion. Also scans the shipped frontend files for external URLs (air-gap
compliance).

Run anywhere (esp. inside the air gap after transfer):
    python webapp_selftest.py
Exit code 0 = all checks passed.
"""

import io
import json
import os
import re
import stat
import sys
import tempfile
import time
import zipfile
from pathlib import Path

os.environ["PDF2DB_WEB_DATA"] = tempfile.mkdtemp(prefix="pdf2db_webtest_")
sys.path.insert(0, str(Path(__file__).resolve().parent))

import pymupdf  # noqa: E402
import webapp  # noqa: E402  (reads PDF2DB_WEB_DATA at import time)

client = webapp.app.test_client()
CHECKS = []


def check(cond, label):
    CHECKS.append(bool(cond))
    print(("PASS " if cond else "FAIL ") + label)


def pdf_bytes(text, with_image=False):
    doc = pymupdf.open()
    page = doc.new_page()
    page.insert_textbox(pymupdf.Rect(36, 36, 559, 500), text, fontsize=9)
    if with_image:
        # noisy samples so the PNG stays above the image_min_bytes filter,
        # like a real photograph would
        pm = pymupdf.Pixmap(pymupdf.csRGB, 96, 96, os.urandom(96 * 96 * 3), False)
        page.insert_image(pymupdf.Rect(50, 520, 250, 720), stream=pm.tobytes("png"))
    data = doc.tobytes()
    doc.close()
    return data


def wait_done(job_id, timeout=60):
    for _ in range(timeout * 2):
        j = client.get(f"/api/jobs/{job_id}/status").get_json()
        if j["status"] in ("done", "failed"):
            return j
        time.sleep(0.5)
    return j


FILLER = ("Inspection finding: wall thinning consistent with corrosion was "
          "observed on the carbon steel component during the survey. ") * 10
SCHEMA = {"table": "docs", "record_unit": "folder", "task_description": "test",
          "fields": [
              {"name": "title", "type": "string", "required": True, "description": "t"},
              {"name": "category", "type": "enum", "options": ["alpha", "beta"],
               "required": True, "description": "c"}]}


def main():
    # ---- frontend serves + air-gap scan ----
    r = client.get("/")
    check(r.status_code == 200 and b"<title>Corpus</title>" in r.data,
          "index.html serves")
    blob = r.data.decode()
    for f in ["app.css", "app.js", "create.js", "database.js", "settings.js"]:
        rr = client.get(f"/static/{f}")
        check(rr.status_code == 200 and len(rr.data) > 500, f"static/{f} serves")
        blob += rr.data.decode()
    ext = re.findall(r'https?://(?!www\.w3\.org)[^\s"\')]+', blob)
    check(not ext, f"zero external URLs in frontend ({ext[:2]})")
    check(client.get("/healthz").get_json()["ok"] is True, "healthz")

    # ---- settings ----
    r = client.put("/api/settings", json={"llm_base_url": "mock",
                                          "llm_model": "m-set",
                                          "llm_api_key": "supersecret9876"})
    s = r.get_json()
    check(s["has_key"] and s["key_hint"] == "…9876"
          and "supersecret" not in r.data.decode(), "settings saved, key masked")
    mode = stat.S_IMODE(os.stat(webapp.SETTINGS_PATH).st_mode)
    check(mode == 0o600, f"settings.json chmod 600 (got {oct(mode)})")
    check(client.post("/api/settings/test", json={}).get_json()["ok"],
          "test connection (mock)")

    # ---- schema library ----
    bad = client.post("/api/schema/validate", json={"table": "BAD!", "fields": []})
    check(not bad.get_json()["ok"], "invalid schema rejected by validator")
    check(client.post("/api/schemas", json={"name": "lib1", "schema": SCHEMA})
          .status_code == 200, "schema saved to library")
    check(any(x["name"] == "lib1" for x in client.get("/api/schemas").get_json()),
          "library lists schema")
    check(client.post("/api/schemas", json={"name": "../evil", "schema": SCHEMA})
          .status_code == 400, "malicious schema name rejected")

    # ---- staging: look at an archive before committing to a run ----
    check(client.post("/api/staging", content_type="multipart/form-data",
                      data={"files": [(io.BytesIO(b"not a pdf at all"), "x.txt")]})
          .status_code == 400, "archive with no PDFs is refused")
    r = client.post("/api/staging", content_type="multipart/form-data", data={
        "source_name": "archive_one",
        "files": [(io.BytesIO(pdf_bytes(FILLER)), "caseS/one.pdf"),
                  (io.BytesIO(pdf_bytes(FILLER)), "caseT/two.pdf")]})
    st = r.get_json()
    sid, scan = st["staging_id"], st["scan"]
    check(scan["n_pdfs"] == 2 and scan["units"]["folder"]["n_records"] == 2
          and scan["units"]["folder"]["examples"] == ["caseS", "caseT"],
          f"staging scan counts records per unit ({scan['units']})")
    check(scan["probe"]["n_probed"] >= 1
          and scan["probe"]["samples"][0]["chars"] > 100,
          "staging scan measures real text yield")
    check(client.get(f"/api/staging/{sid}").get_json()["scan"]["n_pdfs"] == 2,
          "a staging area can be re-read")
    sug = client.post(f"/api/staging/{sid}/suggest-schema",
                      json={"record_unit": "folder"}).get_json()
    check(sug["ok"] and sug["source"] == "generic" and sug["schema"]["fields"],
          "mock backend returns a generic starter schema")
    check(not webapp.pdf2db.validate_schema(dict(sug["schema"])),
          "drafted schema passes validation")

    # ---- promote a staging area into a corpus ----
    r = client.post("/api/jobs", content_type="multipart/form-data",
                    data={"schema": json.dumps(SCHEMA), "staging_id": sid,
                          "title": "Archive one"})
    pjob = r.get_json()["job_id"]
    check(not (webapp.STAGING_DIR / sid).exists(),
          "staging area is moved into the corpus, not copied")
    last = wait_done(pjob)
    check(last["status"] == "done" and last["summary"]["n_records"] == 2,
          f"promoted staging built a corpus ({last.get('error')})")
    check(last["title"] == "Archive one" and last["source"] == "archive_one",
          f"corpus keeps its title and source ({last['title']})")
    check(client.get(f"/api/jobs/{pjob}/meta").get_json()["scan"]["n_pdfs"] == 2,
          "the pre-run scan travels with the corpus")
    check(client.patch(f"/api/jobs/{pjob}", json={"title": "Renamed"})
          .get_json()["title"] == "Renamed", "corpus rename accepted")
    check(client.get(f"/api/jobs/{pjob}/status").get_json()["title"] == "Renamed",
          "rename is visible in the corpus list")
    check(client.patch(f"/api/jobs/{pjob}", json={"title": "  "}).status_code == 400,
          "empty rename rejected")
    arts = client.get(f"/api/jobs/{pjob}/artifacts").get_json()
    check(any(a["name"] == "records.csv" and a["bytes"] > 0 and a["help"]
              for a in arts) and any(a["name"] == "docs.db" for a in arts),
          f"artifacts listed with size and help ({[a['name'] for a in arts]})")
    all_rows = client.get(f"/api/jobs/{pjob}/table/records").get_json()
    ok_rows = client.get(f"/api/jobs/{pjob}/table/records?status=ok").get_json()
    si = ok_rows["columns"].index("status")
    check(ok_rows["total"] <= all_rows["total"]
          and all(row[si] == "ok" for row in ok_rows["rows"]),
          "status filter narrows the records table")
    check(client.delete(f"/api/jobs/{pjob}").get_json().get("ok"),
          "promoted corpus deleted")

    # ---- job via upload: nested files + zip with traversal member ----
    zbuf = io.BytesIO()
    with zipfile.ZipFile(zbuf, "w") as z:
        z.writestr("caseB/nested/r2.pdf", pdf_bytes(FILLER))
        z.writestr("../evil.pdf", b"nope")
    zbuf.seek(0)
    r = client.post("/api/jobs", content_type="multipart/form-data", data={
        "schema": json.dumps(SCHEMA),
        "files": [
            (io.BytesIO(pdf_bytes(FILLER, with_image=True)), "caseA/deep/r1.pdf"),
            (io.BytesIO(b"\x89PNG\r\n\x1a\nfake"), "caseA/photo.png"),
            (zbuf, "archive.zip"),
        ]})
    j = r.get_json()
    check(r.status_code == 200 and any("evil" in s for s in j["skipped"]),
          "upload accepted; zip traversal member skipped")
    job = j["job_id"]
    last = wait_done(job)
    check(last["status"] == "done" and last["summary"]["n_records"] == 2,
          f"upload job done with 2 records ({last.get('error')})")
    check(last["endpoint"] == "mock" and last["model"] == "m-set",
          "job used stored settings")
    rs = (webapp.JOBS_DIR / job / "out" / "run_summary.json").read_text()
    check("supersecret" not in rs, "API key absent from run_summary.json")

    # ---- images: extracted, linked, served, traversal-blocked ----
    t = client.get(f"/api/jobs/{job}/table/images").get_json()
    check(t["total"] >= 2, f"images table has embedded+loose rows ({t['total']})")
    cols = t["columns"]
    emb = next(r for r in t["rows"] if r[cols.index("source")] == "embedded")
    check(emb[cols.index("record_id")] == "caseA", "image linked to its record")
    img = client.get(f"/api/jobs/{job}/image/" + emb[cols.index("file")])
    check(img.status_code == 200 and len(img.data) > 100, "embedded image serves")
    check(client.get(f"/api/jobs/{job}/image/../schema.json").status_code in
          (404, 308), "image path traversal blocked")

    # ---- loose images: catalogued in place, previewable, not a file reader ----
    loose = next(r for r in t["rows"] if r[cols.index("source")] == "loose")
    check(loose[cols.index("record_id")] == "caseA" and not loose[cols.index("file")],
          "loose image linked to its record and left in the archive")
    lp = client.get(f"/api/jobs/{job}/source-image/" + loose[cols.index("origin")])
    check(lp.status_code == 200 and lp.data.startswith(b"\x89PNG"),
          "loose image serves from the source archive")
    check(client.get(f"/api/jobs/{job}/source-image/caseA/deep/r1.pdf")
          .status_code == 404, "source-image refuses a non-image file")
    check(client.get(f"/api/jobs/{job}/source-image/../schema.json").status_code
          in (404, 308), "source-image path traversal blocked")

    # ---- record + stats read straight from SQLite (indexed, not a CSV scan) ----
    st = client.get(f"/api/jobs/{job}/stats").get_json()
    check(st["n_records"] == 2 and sum(n for _, n in st["statuses"]) == 2,
          f"stats aggregate from the database ({st['statuses']})")
    check(any(v == "alpha" or v == "beta" for v, _ in st["enums"].get("category", [])),
          f"enum distribution present ({st['enums']})")

    # ---- paged tables + server-side search ----
    p1 = client.get(f"/api/jobs/{job}/table/records?limit=1&offset=0").get_json()
    p2 = client.get(f"/api/jobs/{job}/table/records?limit=1&offset=1").get_json()
    check(p1["total"] == 2 and len(p1["rows"]) == 1 and len(p2["rows"]) == 1
          and p1["rows"][0] != p2["rows"][0], "pagination works (limit/offset)")
    qq = client.get(f"/api/jobs/{job}/table/records?q=caseA").get_json()
    check(qq["total"] == 1, "server-side search filters across the table")

    # ---- review: invalid rejected, valid written back to SQLite ----
    rid = p1["rows"][0][p1["columns"].index("record_id")]
    check(client.post(f"/api/jobs/{job}/review",
                      json={"record_id": rid, "verdict": "corrected",
                            "reviewer": "T", "corrections": {"category": "NOPE"}})
          .status_code == 400, "invalid enum correction rejected")
    r = client.post(f"/api/jobs/{job}/review",
                    json={"record_id": rid, "verdict": "corrected",
                          "reviewer": "Tester",
                          "corrections": {"category": "beta"}})
    check(r.get_json().get("ok"), "valid correction accepted")
    import sqlite3
    con = sqlite3.connect(webapp.JOBS_DIR / job / "out" / "docs.db")
    row = con.execute('SELECT "category","status" FROM "records" '
                      'WHERE "record_id"=?', (rid,)).fetchone()
    n_img_db = con.execute('SELECT COUNT(*) FROM "images"').fetchone()[0]
    con.close()
    check(row == ("beta", "reviewed"), f"correction + reviewed in SQLite ({row})")
    check(n_img_db == t["total"], "images table in SQLite matches API")

    # ---- retry uses stored settings ----
    check(client.post(f"/api/jobs/{job}/retry", json={}).get_json().get("ok"),
          "retry accepted without key (stored settings)")
    check(wait_done(job)["status"] == "done", "retry completed")

    # ---- server-path ingestion ----
    src = Path(tempfile.mkdtemp(prefix="pdf2db_src_"))
    (src / "incA").mkdir()
    (src / "incA" / "r.pdf").write_bytes(pdf_bytes(FILLER))
    (src / "incB").mkdir()
    (src / "incB" / "r.pdf").write_bytes(pdf_bytes(FILLER))
    chk = client.post("/api/server-path/check", json={"path": str(src)}).get_json()
    check(chk["ok"] and chk["n_pdfs"] == 2, f"server path check ({chk})")
    check(not client.post("/api/server-path/check",
                          json={"path": str(src / 'nope')}).get_json()["ok"],
          "bad server path rejected")
    r = client.post("/api/jobs", content_type="multipart/form-data",
                    data={"schema": json.dumps(SCHEMA), "server_path": str(src)})
    sp_job = r.get_json()["job_id"]
    last = wait_done(sp_job)
    check(last["status"] == "done" and last["summary"]["n_records"] == 2,
          f"server-path job done in place ({last.get('error')})")
    check(not (webapp.JOBS_DIR / sp_job / "archive").exists(),
          "server-path job made no archive copy")
    client.delete(f"/api/jobs/{sp_job}")
    check((src / "incA" / "r.pdf").exists(),
          "deleting a server-path job leaves the source folder untouched")

    # ---- deletion ----
    check(client.delete(f"/api/jobs/{job}").get_json().get("ok"), "job deleted")
    check(client.get(f"/api/jobs/{job}/status").status_code == 404,
          "deleted job is gone")
    check(client.delete("/api/schemas/lib1").get_json().get("ok"),
          "library schema deleted")

    n_fail = CHECKS.count(False)
    print(f"\n{len(CHECKS) - n_fail}/{len(CHECKS)} webapp checks passed")
    return 1 if n_fail else 0


if __name__ == "__main__":
    sys.exit(main())
