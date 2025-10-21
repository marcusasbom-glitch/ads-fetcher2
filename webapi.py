# webapi.py
from __future__ import annotations

from fastapi import FastAPI, Form, Request, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import (
    FileResponse,
    Response,
    JSONResponse,
    PlainTextResponse,
)
from pathlib import Path
import os
import json
import uuid
import asyncio
import traceback
import time

# === Din pipeline ===
# capture_network: async – hämtar/lagrar material i run_dir
# process_candidates_and_save: sync – bearbetar & skriver ads_extracted.xlsx
from ads_capture_and_extract import capture_network, process_candidates_and_save

# -------------------------------------------------------------------
# App & CORS
# -------------------------------------------------------------------
app = FastAPI()

# Öppen CORS för enkel testning. När allt fungerar kan du låsa ner allow_origins.
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],        # ex: ["https://din-domän.se", "https://www.din-domän.se"]
    allow_methods=["*"],        # GET, POST, OPTIONS, ...
    allow_headers=["*"],        # Content-Type m.m.
    allow_credentials=False,    # håll False när du använder "*"
)

# Fångar ALLA OPTIONS (preflight) och svarar 204 så att proxies/gateways inte ger 405.
@app.options("/{rest_of_path:path}")
def preflight_catchall(rest_of_path: str, request: Request):
    origin = request.headers.get("origin", "*")
    acrh   = request.headers.get("access-control-request-headers", "*")
    headers = {
        "Access-Control-Allow-Origin": origin,
        "Vary": "Origin",
        "Access-Control-Allow-Methods": "GET,POST,OPTIONS",
        "Access-Control-Allow-Headers": acrh or "*",
        "Access-Control-Max-Age": "86400",
    }
    return Response(status_code=204, headers=headers)

# -------------------------------------------------------------------
# Root/health
# -------------------------------------------------------------------
@app.get("/")
def root():
    return JSONResponse({
        "ok": True,
        "service": "ads-fetcher",
        "endpoints": ["/ping", "/run", "/status/{job_id}", "/download/{job_id}", "/logs/{job_id}", "/debug/{job_id}"]
    })

@app.head("/")
def root_head():
    # tomt 200-svar för health checks som använder HEAD
    return Response(status_code=200)

@app.get("/favicon.ico")
def favicon():
    # ingen favicon – returnera 204 så slipper vi 405 i loggarna
    return Response(status_code=204)

@app.get("/ping")
def ping():
    return {"ok": True}

# -------------------------------------------------------------------
# Lagring & hjälpmetoder
# -------------------------------------------------------------------
RUNS_DIR = Path(os.getenv("RUNS_DIR", "/tmp/runs"))
RUNS_DIR.mkdir(parents=True, exist_ok=True)

def _write_json(p: Path, obj: dict):
    p.write_text(json.dumps(obj, ensure_ascii=False), encoding="utf-8")

def _append_log(job_dir: Path, line: str):
    lp = job_dir / "log.txt"
    ts = time.strftime("%Y-%m-%d %H:%M:%S")
    with lp.open("a", encoding="utf-8") as f:
        f.write(f"[{ts}] {line}\n")

def _write_status(job_dir: Path, **fields):
    sp = job_dir / "status.json"
    data = {"status": "running", "progress": None, "message": None, "error": None}
    if sp.exists():
        try:
            data.update(json.loads(sp.read_text(encoding="utf-8")))
        except Exception:
            pass
    data.update(fields)
    _write_json(sp, data)

def _read_status(job_dir: Path):
    sp = job_dir / "status.json"
    if not sp.exists():
        return None
    try:
        return json.loads(sp.read_text(encoding="utf-8"))
    except Exception:
        return None

# -------------------------------------------------------------------
# Jobb-logik
# -------------------------------------------------------------------
# Hårdgräns för hela jobbet (sekunder)
OVERALL_DEADLINE_SEC = int(os.getenv("OVERALL_DEADLINE_SEC", "1200"))  # 20 min

async def do_job(job_id: str, ar_input: str):
    """Kör hela pipeline för ett job_id."""
    job_dir = RUNS_DIR / job_id
    _append_log(job_dir, f"JOB START ar_input='{ar_input}'")
    try:
        _write_status(job_dir, status="running", progress=1, message="Initierar…")

        # 1) Capture – Playwright (async) med timeout
        _write_status(job_dir, progress=5, message="Fångar nätverk…")
        try:
            await asyncio.wait_for(
                capture_network(ar_input, run_dir=job_dir),
                timeout=12 * 60,  # 12 minuter
            )
        except asyncio.TimeoutError:
            _write_status(job_dir, status="error", error="timeout_capture_network", message="Timeout i capture_network")
            _append_log(job_dir, "TIMEOUT i capture_network (12 min)")
            return

        # 2) Processing & Excel (körs i thread pool) med timeout
        _write_status(job_dir, progress=70, message="Bearbetar och bygger Excel…")
        try:
            ok = await asyncio.wait_for(
                asyncio.get_running_loop().run_in_executor(None, process_candidates_and_save, job_dir, ar_input),
                timeout=6 * 60,  # 6 minuter
            )
        except asyncio.TimeoutError:
            _write_status(job_dir, status="error", error="timeout_processing", message="Timeout i efterbearbetning")
            _append_log(job_dir, "TIMEOUT i process_candidates_and_save (6 min)")
            return

        if not ok:
            _write_status(job_dir, status="error", error="processing_failed", message="Inga annonser hittades eller fil saknas")
            _append_log(job_dir, "JOB ERROR: processing_failed")
            return

        excel = job_dir / "ads_extracted.xlsx"
        if excel.exists():
            _write_status(job_dir, status="done", progress=100, message="Klart.")
            _append_log(job_dir, "JOB DONE")
        else:
            _write_status(job_dir, status="error", error="excel_missing", message="Excel saknas efter bearbetning")
            _append_log(job_dir, "JOB ERROR: Excel saknas")
    except Exception as e:
        tb = traceback.format_exc(limit=5)
        _write_status(job_dir, status="error", error=type(e).__name__, message=str(e))
        _append_log(job_dir, f"JOB ERROR: {e}\n{tb}")

# -------------------------------------------------------------------
# API-endpoints
# -------------------------------------------------------------------
@app.post("/run")
async def run(ar_input: str = Form(...)):
    """Startar ett nytt jobb och returnerar job_id direkt."""
    job_id = uuid.uuid4().hex[:12]
    job_dir = RUNS_DIR / job_id
    job_dir.mkdir(parents=True, exist_ok=True)
    _write_status(job_dir, status="queued", progress=0, message="Köad")
    _append_log(job_dir, "Job skapades; ställer i kö…")

    # Schemalägg coroutinen på nuvarande event-loop
    asyncio.create_task(do_job(job_id, ar_input.strip()))
    return {"job_id": job_id, "status": "queued"}

@app.get("/status/{job_id}")
def status(job_id: str, request: Request):
    """Returnerar status för ett jobb och (om klart) en download-URL."""
    job_dir = RUNS_DIR / job_id
    data = _read_status(job_dir)
    if not data or "status" not in data:
        raise HTTPException(status_code=404, detail="unknown_job_id")

    excel = job_dir / "ads_extracted.xlsx"
    data["result_url"] = (
        str(request.url_for("download", job_id=job_id)) if excel.exists() else None
    )
    return data

@app.get("/download/{job_id}", name="download")
def download(job_id: str):
    """Returnerar Excel-filen för ett färdigt jobb."""
    job_dir = RUNS_DIR / job_id
    excel = job_dir / "ads_extracted.xlsx"
    if not excel.exists():
        raise HTTPException(status_code=404, detail="Result file not found")
    return FileResponse(
        path=str(excel),
        filename=f"ads_extracted_{job_id}.xlsx",
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    )

# -------------------------------------------------------------------
# Felsökning
# -------------------------------------------------------------------
@app.get("/logs/{job_id}")
def get_logs(job_id: str):
    """
    Returnerar log.txt om den finns; annars status.json som text.
    Om inget av dem finns listar vi filerna för att underlätta felsökning.
    """
    job_dir = RUNS_DIR / job_id
    if not job_dir.exists():
        raise HTTPException(status_code=404, detail="job_dir_missing")

    log_p = job_dir / "log.txt"
    st_p  = job_dir / "status.json"

    if log_p.exists():
        return PlainTextResponse(log_p.read_text(encoding="utf-8"))

    if st_p.exists():
        text = st_p.read_text(encoding="utf-8")
        return PlainTextResponse(f"[no log.txt]\nstatus.json:\n{text}")

    files = []
    for p in job_dir.rglob("*"):
        if p.is_file():
            files.append(p.relative_to(job_dir).as_posix())
    raise HTTPException(status_code=404, detail={"reason": "no_log_or_status", "files": files})

@app.get("/debug/{job_id}")
def debug_job(job_id: str):
    """Listar hela jobbkatalogen (väldigt användbart när något saknas)."""
    job_dir = RUNS_DIR / job_id
    if not job_dir.exists():
        raise HTTPException(status_code=404, detail="job_dir_missing")
    tree = []
    for p in job_dir.rglob("*"):
        tree.append({
            "path": p.relative_to(job_dir).as_posix(),
            "dir": p.is_dir(),
            "size": (p.stat().st_size if p.is_file() else None)
        })
    return {"job_dir": str(job_dir), "files": tree}
