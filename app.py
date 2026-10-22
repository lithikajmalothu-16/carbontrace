"""
CarbonTrace - Step 13: the website.

Pages
  /            Overview (Head of Sustainability): totals, progress, emissions
  /upload      Upload a supplier file, or try one of the sample files
  /queue       Review queue (ESG Analyst): flagged items, riskiest first
  /item/{id}   One submission: values + sources, flags, agent investigation, approve / correct / reject
  /audit       Audit trail: every event, who did it, when
  /export.csv  Approved data as CSV (for Excel / Power BI)

Run locally:  uvicorn app:app --reload     then open http://127.0.0.1:8000
"""
import csv
import io
import json
import os
import shutil
import sqlite3
import time
import uuid
from datetime import datetime, timezone

from dotenv import load_dotenv
from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from agent import get_client, review_item
from checks import load_baseline, load_factors, run_checks
from extract import extract

load_dotenv()

BASE = os.path.dirname(os.path.abspath(__file__))
SAMPLES = os.path.join(BASE, "sample_data")
DATA_DIR = os.getenv("DATA_DIR", BASE)            # on Azure this points to persistent storage
UPLOADS = os.path.join(DATA_DIR, "uploads")
DB_PATH = os.path.join(DATA_DIR, "carbontrace.db")
os.makedirs(UPLOADS, exist_ok=True)

MAX_FILE_MB = 3
ALLOWED = (".pdf", ".xlsx")
MAX_UPLOADS_PER_HOUR = 60   # protects your Azure budget when the site is public
RISK_ORDER = {"security_concern": 0, "likely_data_error": 1, "missing_information": 2,
              "likely_real_change": 3, "duplicate": 4}

BASELINE = load_baseline(os.path.join(SAMPLES, "baseline_2025.csv"))
FACTORS = load_factors(os.path.join(SAMPLES, "emission_factors.csv"))

app = FastAPI(title="CarbonTrace")
app.mount("/static", StaticFiles(directory=os.path.join(BASE, "static")), name="static")
templates = Jinja2Templates(directory=os.path.join(BASE, "templates"))
_recent_uploads = []


# ---------------------------------------------------------------- database
def db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    with db() as c:
        c.execute("""CREATE TABLE IF NOT EXISTS submissions (
            id INTEGER PRIMARY KEY AUTOINCREMENT, file TEXT, stored_path TEXT, uploaded_at TEXT,
            result TEXT, status TEXT, final_mwh REAL, final_tco2e REAL)""")
        c.execute("""CREATE TABLE IF NOT EXISTS audit (
            id INTEGER PRIMARY KEY AUTOINCREMENT, submission_id INTEGER, at TEXT,
            actor TEXT, action TEXT, detail TEXT)""")


def now():
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")


def log(conn, sid, actor, action, detail=""):
    conn.execute("INSERT INTO audit (submission_id, at, actor, action, detail) VALUES (?,?,?,?,?)",
                 (sid, now(), actor, action, detail))


def seen_so_far(conn):
    """Rebuild duplicate memory from everything already in the database."""
    seen = {"fingerprints": {}, "keys": {}}
    for row in conn.execute("SELECT result FROM submissions"):
        r = json.loads(row["result"])
        seen["fingerprints"].setdefault(r["audit"]["fingerprint_sha256"], r["file"])
        seen["keys"].setdefault((r["matched_supplier"] or r["supplier"], r["year"], r["total_mwh"]), r["file"])
    return seen


# ---------------------------------------------------------------- the pipeline
def process(path, original_name, actor="system", result=None):
    """READ -> CHECK -> INVESTIGATE, then save with a full audit trail.
    If `result` is given (pre-computed demo data), Azure isn't called again."""
    with db() as conn:
        if result is None:
            data = extract(path)
            data["file"] = original_name
            result = run_checks(data, BASELINE, FACTORS, seen_so_far(conn))
            if result["status"] == "needs_review":
                result["investigation"] = review_item(get_client(), os.getenv("OPENAI_DEPLOYMENT",
                                                                             "carbontrace-model"), result)
        status = result["status"]
        cur = conn.execute(
            "INSERT INTO submissions (file, stored_path, uploaded_at, result, status, final_mwh, final_tco2e) "
            "VALUES (?,?,?,?,?,?,?)",
            (original_name, path, now(), json.dumps(result, default=str), status,
             result["total_mwh"] if status == "auto_validated" else None,
             result["emissions_tco2e"] if status == "auto_validated" else None))
        sid = cur.lastrowid
        log(conn, sid, actor, "uploaded", original_name)
        log(conn, sid, "Document Intelligence", "read",
            f"{len(result['site_values'])} site value(s); total from {result.get('total_source') or 'n/a'}")
        flags = ", ".join(f["type"] for f in result["flags"]) or "none"
        log(conn, sid, "Rule checks", "checked", f"flags: {flags}")
        inv = result.get("investigation")
        if inv:
            tools = " -> ".join(t["tool"] for t in inv.get("trace", [])) or "no AI call"
            log(conn, sid, f"Agent ({inv['label_source']})", "investigated", f"{inv['label']} | tools: {tools}")
        if status == "auto_validated":
            log(conn, sid, "Rule checks", "auto-validated", "All checks passed; no human review needed")
        return sid


@app.on_event("startup")
def startup():
    init_db()


# ---------------------------------------------------------------- pages
@app.get("/", response_class=HTMLResponse)
def overview(request: Request):
    with db() as conn:
        rows = [dict(r) for r in conn.execute("SELECT * FROM submissions")]
    counts = {s: sum(1 for r in rows if r["status"] == s)
              for s in ("auto_validated", "needs_review", "approved", "corrected", "rejected")}
    total = len(rows)
    accepted = [r for r in rows if r["status"] in ("auto_validated", "approved", "corrected")]
    stats = {
        "total": total,
        "auto_pct": round(100 * counts["auto_validated"] / total) if total else 0,
        "waiting": counts["needs_review"],
        "decided": counts["approved"] + counts["corrected"] + counts["rejected"],
        "tco2e": round(sum(r["final_tco2e"] or 0 for r in accepted), 1),
        "mwh": round(sum(r["final_mwh"] or 0 for r in accepted), 1),
    }
    return templates.TemplateResponse(request, "overview.html", {"stats": stats, "counts": counts})


@app.get("/upload", response_class=HTMLResponse)
def upload_page(request: Request, error: str = ""):
    samples = sorted(f for f in os.listdir(SAMPLES) if f.endswith(ALLOWED))
    return templates.TemplateResponse(request, "upload.html", {"samples": samples, "error": error})


def check_rate_limit():
    cutoff = time.time() - 3600
    _recent_uploads[:] = [t for t in _recent_uploads if t > cutoff]
    if len(_recent_uploads) >= MAX_UPLOADS_PER_HOUR:
        raise HTTPException(429, "Demo upload limit reached for this hour. Please try a bit later.")
    _recent_uploads.append(time.time())


@app.post("/upload")
async def upload(file: UploadFile = File(...)):
    check_rate_limit()
    name = os.path.basename(file.filename or "")
    if not name.lower().endswith(ALLOWED):
        return RedirectResponse("/upload?error=Only PDF or Excel (.xlsx) files are accepted", 303)
    content = await file.read()
    if len(content) > MAX_FILE_MB * 1024 * 1024:
        return RedirectResponse(f"/upload?error=File is larger than {MAX_FILE_MB} MB", 303)
    path = os.path.join(UPLOADS, f"{uuid.uuid4().hex}_{name}")
    with open(path, "wb") as f:
        f.write(content)
    sid = process(path, name, actor="Visitor upload")
    return RedirectResponse(f"/item/{sid}", 303)


@app.post("/try-sample")
def try_sample(sample: str = Form(...)):
    check_rate_limit()
    name = os.path.basename(sample)
    src = os.path.join(SAMPLES, name)
    if not os.path.isfile(src) or not name.endswith(ALLOWED):
        raise HTTPException(404, "Sample not found")
    path = os.path.join(UPLOADS, f"{uuid.uuid4().hex}_{name}")
    shutil.copy(src, path)
    sid = process(path, name, actor="Sample run")
    return RedirectResponse(f"/item/{sid}", 303)


@app.post("/load-demo")
def load_demo():
    """Load the 8 sample results from results.json (already processed) - no Azure cost."""
    results_path = os.path.join(BASE, "results.json")
    if not os.path.exists(results_path):
        raise HTTPException(400, "Run run_pipeline.py and agent.py first to create results.json")
    with open(results_path, encoding="utf-8") as f:
        results = json.load(f)
    for r in results:
        src = os.path.join(SAMPLES, r["file"])
        path = os.path.join(UPLOADS, f"{uuid.uuid4().hex}_{r['file']}")
        shutil.copy(src, path)
        process(path, r["file"], actor="Demo loader", result=r)
    return RedirectResponse("/queue", 303)


@app.post("/reset")
def reset():
    with db() as conn:
        conn.execute("DELETE FROM submissions")
        conn.execute("DELETE FROM audit")
    for f in os.listdir(UPLOADS):
        os.remove(os.path.join(UPLOADS, f))
    return RedirectResponse("/", 303)


@app.get("/queue", response_class=HTMLResponse)
def queue(request: Request):
    with db() as conn:
        rows = [dict(r) for r in conn.execute("SELECT * FROM submissions ORDER BY id")]
    items = []
    for r in rows:
        res = json.loads(r["result"])
        inv = res.get("investigation") or {}
        items.append({**r, "res": res, "inv": inv,
                      "risk": RISK_ORDER.get(inv.get("label"), 9) if r["status"] == "needs_review" else 10})
    waiting = sorted([i for i in items if i["status"] == "needs_review"], key=lambda i: i["risk"])
    done = [i for i in items if i["status"] != "needs_review"]
    return templates.TemplateResponse(request, "queue.html", {"waiting": waiting, "done": done})


@app.get("/item/{sid}", response_class=HTMLResponse)
def item(request: Request, sid: int):
    with db() as conn:
        row = conn.execute("SELECT * FROM submissions WHERE id=?", (sid,)).fetchone()
        if not row:
            raise HTTPException(404, "Submission not found")
        events = [dict(e) for e in conn.execute("SELECT * FROM audit WHERE submission_id=? ORDER BY id", (sid,))]
    r = dict(row)
    res = json.loads(r["result"])
    return templates.TemplateResponse(request, "item.html",
                                      {"r": r, "res": res, "inv": res.get("investigation") or {}, "events": events})


@app.get("/file/{sid}")
def view_file(sid: int):
    """Open the original supplier file - this is the 'click through to the source' part of the audit trail."""
    with db() as conn:
        row = conn.execute("SELECT file, stored_path FROM submissions WHERE id=?", (sid,)).fetchone()
    if not row or not os.path.exists(row["stored_path"]):
        raise HTTPException(404, "File not found")
    media = "application/pdf" if row["file"].lower().endswith(".pdf") else \
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
    return FileResponse(row["stored_path"], media_type=media,
                        headers={"Content-Disposition": f'inline; filename="{row["file"]}"'})


@app.post("/item/{sid}/decide")
def decide(sid: int, action: str = Form(...), reviewer: str = Form(...),
           reason: str = Form(""), corrected_mwh: str = Form("")):
    reviewer = reviewer.strip()[:60]
    reason = reason.strip()[:500]
    if not reviewer:
        raise HTTPException(400, "Reviewer name is required")
    if action not in ("approve", "correct", "reject"):
        raise HTTPException(400, "Unknown action")
    if action in ("correct", "reject") and not reason:
        raise HTTPException(400, "A reason is required to correct or reject")

    with db() as conn:
        row = conn.execute("SELECT * FROM submissions WHERE id=?", (sid,)).fetchone()
        if not row:
            raise HTTPException(404, "Submission not found")
        if row["status"] != "needs_review":
            raise HTTPException(400, "Already decided")
        res = json.loads(row["result"])
        factor = res.get("emission_factor")

        if action == "approve":
            mwh = res["total_mwh"]
            if mwh is None:
                raise HTTPException(400, "Can't approve without a total - use Correct and enter the value")
            status, detail = "approved", reason or "Approved as submitted"
        elif action == "correct":
            try:
                mwh = float(corrected_mwh.replace(",", ""))
            except ValueError:
                raise HTTPException(400, "Enter the corrected total in MWh")
            status = "corrected"
            detail = f"Total changed from {res['total_mwh']} to {mwh} MWh. Reason: {reason}"
        else:
            mwh, status, detail = None, "rejected", reason

        tco2e = round(mwh * factor, 1) if (mwh is not None and factor) else None  # code does the math
        conn.execute("UPDATE submissions SET status=?, final_mwh=?, final_tco2e=? WHERE id=?",
                     (status, mwh, tco2e, sid))
        log(conn, sid, reviewer, status, detail)
    return RedirectResponse(f"/item/{sid}", 303)


@app.get("/audit", response_class=HTMLResponse)
def audit(request: Request):
    with db() as conn:
        events = [dict(e) for e in conn.execute(
            "SELECT a.*, s.file FROM audit a LEFT JOIN submissions s ON s.id=a.submission_id ORDER BY a.id DESC")]
    return templates.TemplateResponse(request, "audit.html", {"events": events})


@app.get("/export.csv")
def export():
    """Accepted data for Excel / Power BI."""
    with db() as conn:
        rows = [dict(r) for r in conn.execute("SELECT * FROM submissions ORDER BY id")]
    out = io.StringIO()
    w = csv.writer(out)
    w.writerow(["id", "file", "supplier", "country", "status", "final_mwh", "final_tco2e",
                "emission_factor", "flags", "label", "label_source", "uploaded_at"])
    for r in rows:
        res = json.loads(r["result"])
        inv = res.get("investigation") or {}
        w.writerow([r["id"], r["file"], res.get("matched_supplier") or res.get("supplier"), res.get("country"),
                    r["status"], r["final_mwh"], r["final_tco2e"], res.get("emission_factor"),
                    ";".join(f["type"] for f in res["flags"]), inv.get("label", ""), inv.get("label_source", ""),
                    r["uploaded_at"]])
    out.seek(0)
    return StreamingResponse(iter([out.getvalue()]), media_type="text/csv",
                             headers={"Content-Disposition": "attachment; filename=carbontrace_export.csv"})
