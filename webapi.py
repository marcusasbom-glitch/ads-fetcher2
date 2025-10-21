# webapi.py
from fastapi import FastAPI, Form, BackgroundTasks, Request, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from pathlib import Path
import os, json, uuid, asyncio

from ads_capture_and_extract import capture_network, process_candidates_and_save

# webapi.py
from fastapi import FastAPI, Form, Request, HTTPException
# ... (samma imports som innan)
import asyncio, uuid, json, os
from pathlib import Path

app = FastAPI()
RUNS_DIR = Path(os.getenv("RUNS_DIR", "/tmp/runs"))
RUNS_DIR.mkdir(parents=True, exist_ok=True)

def write_status(job_dir: Path, payload: dict):
    (job_dir / "status.json").write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")

async def do_job(job_id: str, ar_input: str):
    job_dir = RUNS_DIR / job_id
    try:
        write_status(job_dir, {"status": "running"})
        # kör din pipeline
        await capture_network(ar_input, run_dir=job_dir)          # async
        ok = process_candidates_and_save(run_dir=job_dir)         # sync
        if not ok:
            write_status(job_dir, {"status": "error", "error": "processing_failed"})
            return
        write_status(job_dir, {"status": "done"})
    except Exception as e:
        write_status(job_dir, {"status": "error", "error": str(e)})

@app.post("/run")
async def run(ar_input: str = Form(...)):
    job_id = uuid.uuid4().hex[:12]
    job_dir = RUNS_DIR / job_id
    job_dir.mkdir(parents=True, exist_ok=True)
    write_status(job_dir, {"status": "queued"})
    # VIKTIGT: schemalägg direkt på nuvarande event-loop
    asyncio.create_task(do_job(job_id, ar_input.strip()))
    return {"job_id": job_id, "status": "queued"}

@app.get("/status/{job_id}")
def status(job_id: str, request: Request):
    job_dir = RUNS_DIR / job_id
    st = job_dir / "status.json"
    if not st.exists():
        raise HTTPException(status_code=404, detail="unknown_job_id")
    data = json.loads(st.read_text(encoding="utf-8"))
    excel = job_dir / "ads_extracted.xlsx"
    data["result_url"] = (
        str(request.url_for("download", job_id=job_id)) if excel.exists() else None
    )
    return data

