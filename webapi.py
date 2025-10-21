# webapi.py
from fastapi import FastAPI, Form, BackgroundTasks, Request, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from pathlib import Path
import os, json, uuid

# IMPORT: använd din tidigare fungerande modul
from ads_capture_and_extract import capture_network, process_candidates_and_save

app = FastAPI()
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

def write_status(job_dir: Path, payload: dict):
    (job_dir / "status.json").write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")

async def do_job(job_id: str, ar_input: str):
    job_dir = RUNS_DIR / job_id
    job_dir.mkdir(parents=True, exist_ok=True)

    write_status(job_dir, {"status": "running", "result_url": None})
    try:
        # KÖR DIN BEPRÖVADE PIPELINE
        await capture_network(ar_input, run_dir=job_dir)
        ok = process_candidates_and_save(run_dir=job_dir)
        if not ok:
            write_status(job_dir, {"status": "error", "error": "processing_failed"})
            return
        write_status(job_dir, {"status": "done"})
    except Exception as e:
        write_status(job_dir, {"status": "error", "error": str(e)})

@app.post("/run")
async def run(background_tasks: BackgroundTasks, ar_input: str = Form(...)):
    job_id = uuid.uuid4().hex[:12]
    job_dir = RUNS_DIR / job_id
    job_dir.mkdir(parents=True, exist_ok=True)
    write_status(job_dir, {"status": "queued", "result_url": None})
    background_tasks.add_task(do_job, job_id, ar_input.strip())
    return {"job_id": job_id, "status": "queued"}

@app.get("/status/{job_id}")
def status(job_id: str, request: Request):
    job_dir = RUNS_DIR / job_id
    st = job_dir / "status.json"
    if not st.exists():
        return {"error": "unknown_job_id"}

    data = json.loads(st.read_text(encoding="utf-8"))
    excel = job_dir / "ads_extracted.xlsx"
    data["result_url"] = (str(request.url_for("download", job_id=job_id))
                          if excel.exists() else None)
    return data

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
    )
