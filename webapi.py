# webapi.py
from fastapi import FastAPI, Form, BackgroundTasks, Request, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
import os, json, uuid, shutil, asyncio
from pathlib import Path
from contextlib import contextmanager

# --- IMPORTERA DINA FUNKTIONER ---
# Se till att ads_capture_and_extract.py ligger i projektroten
from ads_capture_and_extract import capture_network, process_candidates_and_save

app = FastAPI()
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])

RUNS_DIR = Path(os.getenv("RUNS_DIR", "/tmp/runs"))
RUNS_DIR.mkdir(parents=True, exist_ok=True)

@contextmanager
def pushd(new_dir: Path):
    old = Path.cwd()
    os.chdir(new_dir)
    try:
        yield
    finally:
        os.chdir(old)

@app.get("/")
def root():
    return {"ok": True, "paths": ["/run (POST)", "/status/{job_id}", "/download/{job_id}"]}

@app.get("/health")
def health():
    return {"status": "healthy"}

async def do_work(job_id: str, ar_input: str, email: str | None):
    """
    Kör Playwright-capture + processing i jobbkatalogen.
    Skapar status.json under körning och när klart.
    """
    job_dir = RUNS_DIR / job_id
    job_dir.mkdir(parents=True, exist_ok=True)
    status_path = job_dir / "status.json"

    def write_status(obj):
        status_path.write_text(json.dumps(obj, ensure_ascii=False), encoding="utf-8")

    write_status({"status": "running", "result_url": None})

    # Kör i jobbkatalogen så att alla scriptens standardvägar hamnar isolerade
    try:
        with pushd(job_dir):
            # 1) Capture (Playwright) - asynkront
            await capture_network(ar_input)

            # 2) Processa och skapa Excel
            ok = process_candidates_and_save()
            if not ok:
                write_status({"status": "error", "error": "processing_failed"})
                return

            # Ditt process-script brukar skapa en fil som heter 'ads_extracted.xlsx' i cwd
            excel = job_dir / "ads_extracted.xlsx"
            if not excel.exists():
                # fallback: försök hitta första .xlsx om namnet avviker
                xlxs = list(job_dir.glob("*.xlsx"))
                if xlxs:
                    xlxs[0].rename(excel)
            write_status({"status": "done"})  # result_url sätts i /status när den frågas
    except Exception as e:
        write_status({"status": "error", "error": str(e)})

@app.post("/run")
async def run(background_tasks: BackgroundTasks,
              ar_input: str = Form(...),
              email: str | None = Form(None)):
    job_id = uuid.uuid4().hex[:12]
    (RUNS_DIR / job_id).mkdir(parents=True, exist_ok=True)
    (RUNS_DIR / job_id / "status.json").write_text(json.dumps({"status":"queued"}), encoding="utf-8")

    # Kör bakgrundsjobb
    background_tasks.add_task(do_work, job_id, ar_input.strip(), email)
    return {"job_id": job_id, "status": "queued"}

@app.get("/status/{job_id}")
def status(job_id: str, request: Request):
    job_dir = RUNS_DIR / job_id
    status_path = job_dir / "status.json"
    if not status_path.exists():
        return {"error": "unknown_job_id"}

    data = json.loads(status_path.read_text(encoding="utf-8"))
    # Om resultatet finns – bygg en nedladdningslänk (download-endpoint)
    excel = job_dir / "ads_extracted.xlsx"
    if excel.exists():
        # Bygg absolut URL till download-route
        result_url = str(request.url_for("download", job_id=job_id))
    else:
        result_url = None
    data["result_url"] = result_url
    return data

@app.get("/download/{job_id}", name="download")
def download(job_id: str):
    job_dir = RUNS_DIR / job_id
    excel = job_dir / "ads_extracted.xlsx"
    if not excel.exists():
        raise HTTPException(status_code=404, detail="Result file not found")
    return FileResponse(path=str(excel),
                        filename=f"ads_extracted_{job_id}.xlsx",
                        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")
