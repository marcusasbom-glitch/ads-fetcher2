# webapi.py
from fastapi import FastAPI, Form, BackgroundTasks, Request, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
import os, json, uuid
from pathlib import Path

# Importera dina funktioner
from ads_capture_and_extract import capture_network, process_candidates_and_save

app = FastAPI()

# Tillåt CORS för alla (säker nog här)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

RUNS_DIR = Path(os.getenv("RUNS_DIR", "/tmp/runs"))
RUNS_DIR.mkdir(parents=True, exist_ok=True)

@app.get("/")
def root():
    return {"ok": True, "paths": ["/run (POST)", "/status/{job_id}", "/download/{job_id}", "/health"]}

@app.get("/health")
def health():
    return {"status": "healthy"}

async def do_work(job_id: str, ar_input: str, email: str | None):
    job_dir = RUNS_DIR / job_id
    job_dir.mkdir(parents=True, exist_ok=True)
    status_path = job_dir / "status.json"

    def write_status(obj: dict):
        status_path.write_text(json.dumps(obj, ensure_ascii=False), encoding="utf-8")

    write_status({"status": "running", "result_url": None})

    try:
        # Kör Playwright-capture + process med run_dir
        await capture_network(ar_input, run_dir=job_dir)
        ok = process_candidates_and_save(run_dir=job_dir)
        if not ok:
            write_status({"status": "error", "error": "processing_failed"})
            return
        write_status({"status": "done"})
    except Exception as e:
        write_status({"status": "error", "error": str(e)})

@app.post("/run")
async def run(background_tasks: BackgroundTasks,
              ar_input: str = Form(...),
              email: str | None = Form(None)):
    job_id = uuid.uuid4().hex[:12]
    (RUNS_DIR / job_id).mkdir(parents=True, exist_ok=True)
    (RUNS_DIR / job_id / "status.json").write_text(json.dumps({"status":"queued"}), encoding="utf-8")
    background_tasks.add_task(do_work, job_id, ar_input.strip(), email)
    return JSONResponse(
        content={"job_id": job_id, "status": "queued"},
        headers={"Access-Control-Allow-Origin": "*"}
    )

@app.get("/status/{job_id}")
def status(job_id: str, request: Request):
    job_dir = RUNS_DIR / job_id
    status_path = job_dir / "status.json"
    if not status_path.exists():
        return JSONResponse(
            {"error": "unknown_job_id"},
            headers={"Access-Control-Allow-Origin": "*"}
        )

    data = json.loads(status_path.read_text(encoding="utf-8"))
    excel = job_dir / "ads_extracted.xlsx"
    data["result_url"] = str(request.url_for("download", job_id=job_id)) if excel.exists() else None
    return JSONResponse(
        content=data,
        headers={"Access-Control-Allow-Origin": "*"}
    )

@app.get("/download/{job_id}", name="download")
def download(job_id: str):
    job_dir = RUNS_DIR / job_id
    excel = job_dir / "ads_extracted.xlsx"
    if not excel.exists():
        raise HTTPException(status_code=404, detail="Result file not found")
    return FileResponse(
        path=str(excel),
        filename=f"ads_extracted_{job_id}.xlsx",
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Access-Control-Allow-Origin": "*"}
    )
