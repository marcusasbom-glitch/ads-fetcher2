# webapi.py
import os, uuid, json, asyncio
from pathlib import Path
from fastapi import FastAPI, Form, BackgroundTasks
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

# === CORS: byt till dina domäner när du vet dem ===
ALLOW_ORIGINS = os.getenv("ALLOW_ORIGINS", "*").split(",")

app = FastAPI()
app.add_middleware(
    CORSMiddleware,
    allow_origins=ALLOW_ORIGINS,
    allow_methods=["POST","GET","OPTIONS"],
    allow_headers=["*"],
)

RUNS_DIR = Path(os.getenv("RUNS_DIR", "/tmp/runs"))
RUNS_DIR.mkdir(parents=True, exist_ok=True)

# Om du har dina riktiga funktioner, importera dem:
# from ads_capture_and_extract import capture_network, process_candidates_and_save

async def do_work(job_dir: Path, ar_input: str, email: str|None):
    # --- här kör du din riktiga pipeline ---
    # await capture_network(ar_input)
    # ok = process_candidates_and_save()
    # Spara något som resultat. Här skapar vi en tom Excel som exempel:
    out = job_dir / "ads_extracted.xlsx"
    out.touch()

    # Uppdatera status
    (job_dir / "status.json").write_text(json.dumps({
        "status": "done",
        "result_url": None,  # sätt S3/Drive-länk här om du laddar upp
        "file_path": str(out),
        "email": email,
    }), encoding="utf-8")

@app.post("/run")
async def run_endpoint(background_tasks: BackgroundTasks,
                       ar_input: str = Form(...),
                       email: str | None = Form(None)):

    if not ar_input.strip():
        return JSONResponse({"error": "Missing ar_input"}, status_code=400)

    job_id = uuid.uuid4().hex[:12]
    job_dir = RUNS_DIR / job_id
    job_dir.mkdir(parents=True, exist_ok=True)
    (job_dir / "status.json").write_text(json.dumps({"status":"queued"}), encoding="utf-8")

    # Kör Playwright i bakgrunden så vi kan svara direkt
    background_tasks.add_task(do_work, job_dir, ar_input.strip(), email)

    return {"job_id": job_id, "status": "queued"}

@app.get("/status/{job_id}")
def status(job_id: str):
    p = RUNS_DIR / job_id / "status.json"
    if not p.exists():
        return JSONResponse({"error": "unknown job_id"}, status_code=404)
    return JSONResponse(json.loads(p.read_text(encoding="utf-8")))
