# webapi.py
import os
import json
import uuid
import asyncio
from pathlib import Path

from fastapi import FastAPI, Form, BackgroundTasks, Request, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse

# Dina Playwright-funktioner
from ads_capture_and_extract import capture_network, process_candidates_and_save

app = FastAPI()

# CORS – tillåt allt (enkelt för Pipedream / Squarespace)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

# Var alla körningar hamnar
RUNS_DIR = Path(os.getenv("RUNS_DIR", "/tmp/runs"))
RUNS_DIR.mkdir(parents=True, exist_ok=True)


@app.get("/")
def root():
    return {"ok": True, "paths": ["/run (POST)", "/status/{job_id}", "/download/{job_id}", "/health"]}


@app.get("/health")
def health():
    return {"status": "healthy"}


async def do_work(job_id: str, ar_input: str, email: str | None):
    """
    Själva bakgrundsjobbet: kör Playwright-capture + processing,
    skriv status under /tmp/runs/<job_id>/status.json
    """
    job_dir = RUNS_DIR / job_id
    job_dir.mkdir(parents=True, exist_ok=True)
    status_path = job_dir / "status.json"

    def write_status(obj: dict):
        status_path.write_text(json.dumps(obj, ensure_ascii=False), encoding="utf-8")

    write_status({"status": "running", "result_url": None})

    try:
        # 1) Hämta nätverk / material
        await capture_network(ar_input, run_dir=job_dir)

        # 2) Processa och skapa Excel
        ok = process_candidates_and_save(run_dir=job_dir)
        if not ok:
            write_status({"status": "error", "error": "processing_failed"})
            return

        write_status({"status": "done"})
    except Exception as e:
        # Säkrare felrapportering i status
        write_status({"status": "error", "error": str(e)})


def _init_job(job_id: str):
    (RUNS_DIR / job_id).mkdir(parents=True, exist_ok=True)
    (RUNS_DIR / job_id / "status.json").write_text(
        json.dumps({"status": "queued"}), encoding="utf-8"
    )


@app.post("/run")
async def run(request: Request):
    """
    Startar ett jobb och returnerar direkt.
    Accepterar BÅDE application/json och application/x-www-form-urlencoded.
    """
    ct = request.headers.get("content-type", "")
    ar_input = None
    email = None

    try:
        if "application/json" in ct:
            data = await request.json()
            ar_input = (data.get("ar_input") or "").strip()
            email = data.get("email")
        else:
            # Fångar form-varianten (Pipedream / Squarespace kan skicka så)
            form = await request.form()
            ar_input = (form.get("ar_input") or "").strip()
            email = form.get("email")
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Bad payload: {e}")

    if not ar_input:
        raise HTTPException(status_code=422, detail="ar_input is required")

    job_id = uuid.uuid4().hex[:12]
    _init_job(job_id)

    # Kör i bakgrund och svara OMEDELBART
    asyncio.create_task(do_work(job_id, ar_input, email))

    return {"job_id": job_id, "status": "queued"}


@app.get("/status/{job_id}")
def status(job_id: str, request: Request):
    """
    Hämtar status för ett jobb. När Excel finns, skickas en "result_url".
    """
    job_dir = RUNS_DIR / job_id
    status_path = job_dir / "status.json"
    if not status_path.exists():
        return {"error": "unknown_job_id"}

    data = json.loads(status_path.read_text(encoding="utf-8"))
    excel = job_dir / "ads_extracted.xlsx"
    data["result_url"] = (
        str(request.url_for("download", job_id=job_id)) if excel.exists() else None
    )
    return data


@app.get("/download/{job_id}", name="download")
def download(job_id: str):
    """
    Returnerar Excel-filen för ett klart jobb.
    """
    job_dir = RUNS_DIR / job_id
    excel = job_dir / "ads_extracted.xlsx"
    if not excel.exists():
        raise HTTPException(status_code=404, detail="Result file not found")

    return FileResponse(
        path=str(excel),
        filename=f"ads_extracted_{job_id}.xlsx",
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    )
