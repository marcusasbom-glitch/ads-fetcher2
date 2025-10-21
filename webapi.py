# webapi.py
from fastapi import FastAPI, Form, Request, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, Response
from pathlib import Path
import os, json, uuid, asyncio

# === Din pipeline ===
# capture_network: async – styr Playwright och fångar data till run_dir
# process_candidates_and_save: sync – bearbetar och skriver ads_extracted.xlsx
from ads_capture_and_extract import capture_network, process_candidates_and_save

# -------------------------------------------------------------------
# App & CORS
# -------------------------------------------------------------------
app = FastAPI()

# Öppen CORS (enkelt för test). Vill du låsa ner: byt allow_origins till en lista med dina domäner.
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],        # ex: ["https://din-domän.se", "https://www.din-domän.se"]
    allow_methods=["*"],        # GET, POST, OPTIONS m.fl.
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
        "Access-Control-Allow-Headers": acrh,
        "Access-Control-Max-Age": "86400",
    }
    return Response(status_code=204, headers=headers)

# -------------------------------------------------------------------
# Konfiguration
# -------------------------------------------------------------------
RUNS_DIR = Path(os.getenv("RUNS_DIR", "/tmp/runs"))
RUNS_DIR.mkdir(parents=True, exist_ok=True)

def write_status(job_dir: Path, payload: dict):
    (job_dir / "status.json").write_text(
        json.dumps(payload, ensure_ascii=False), encoding="utf-8"
    )

def read_status(job_dir: Path):
    p = job_dir / "status.json"
    if not p.exists():
        return None
    return json.loads(p.read_text(encoding="utf-8"))

# -------------------------------------------------------------------
# Hjälp-endpoints
# -------------------------------------------------------------------
@app.get("/ping")
def ping():
    return {"ok": True}

# -------------------------------------------------------------------
# Jobb-logik
# -------------------------------------------------------------------
async def do_job(job_id: str, ar_input: str):
    """Kör hela pipeline för ett job_id."""
    job_dir = RUNS_DIR / job_id
    try:
        write_status(job_dir, {"status": "running"})
        # 1) Kör Playwright-capture (async)
        await capture_network(ar_input, run_dir=job_dir)
        # 2) Efterbearbetning (sync)
        ok = process_candidates_and_save(run_dir=job_dir)
        if not ok:
            write_status(job_dir, {"status": "error", "error": "processing_failed"})
            return
        write_status(job_dir, {"status": "done"})
    except Exception as e:
        # Skriv fel så frontend kan visa det
        write_status(job_dir, {"status": "error", "error": str(e)})

# -------------------------------------------------------------------
# API-endpoints
# -------------------------------------------------------------------
@app.post("/run")
async def run(ar_input: str = Form(...)):
    """Startar ett nytt jobb och returnerar job_id direkt."""
    job_id = uuid.uuid4().hex[:12]
    job_dir = RUNS_DIR / job_id
    job_dir.mkdir(parents=True, exist_ok=True)
    write_status(job_dir, {"status": "queued"})

    # Schemalägg coroutinen på NUVARANDE event-loop (inte BackgroundTasks)
    asyncio.create_task(do_job(job_id, ar_input.strip()))
    return {"job_id": job_id, "status": "queued"}

@app.get("/status/{job_id}")
def status(job_id: str, request: Request):
    """Returnerar status för ett jobb och (om klart) en download-URL."""
    job_dir = RUNS_DIR / job_id
    data = read_status(job_dir)
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
