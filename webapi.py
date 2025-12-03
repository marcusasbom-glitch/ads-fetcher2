# webapi.py

from fastapi import FastAPI, Form, Request, HTTPException, UploadFile, File
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, Response, JSONResponse, PlainTextResponse, HTMLResponse

from pathlib import Path
import os, json, uuid, asyncio, traceback, time, tempfile

from ads_capture_and_extract import capture_network, process_candidates_and_save
from ocr_module import add_ocr_to_excel   # <-- Se till att denna modul finns

app = FastAPI()

# ----------------------
# CORS
# ----------------------
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
    allow_credentials=False,
)

@app.options("/{rest_of_path:path}")
def preflight(rest_of_path: str, request: Request):
    origin = request.headers.get("origin", "*")
    req_headers = request.headers.get("access-control-request-headers", "*")

    return Response(
        status_code=204,
        headers={
            "Access-Control-Allow-Origin": origin,
            "Access-Control-Allow-Methods": "GET,POST,OPTIONS",
            "Access-Control-Allow-Headers": req_headers,
            "Access-Control-Max-Age": "86400",
        },
    )

# ----------------------
# Bas / Health
# ----------------------
@app.get("/")
def root():
    return {"ok": True, "service": "ads-fetcher"}


@app.get("/ping")
def ping():
    return {"ok": True}

# ----------------------
# Lagning av filer
# ----------------------
RUNS_DIR = Path("/tmp/runs")
RUNS_DIR.mkdir(exist_ok=True, parents=True)

def write_json(path: Path, data: dict):
    path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")

def append_log(job_dir: Path, msg: str):
    ts = time.strftime("%Y-%m-%d %H:%M:%S")
    with open(job_dir / "log.txt", "a", encoding="utf-8") as f:
        f.write(f"[{ts}] {msg}\n")

def write_status(job_dir: Path, **fields):
    status_path = job_dir / "status.json"
    status = {"status": "running", "progress": 0, "message": ""}

    if status_path.exists():
        try:
            status.update(json.loads(status_path.read_text()))
        except:
            pass
    
    status.update(fields)
    write_json(status_path, status)

def read_status(job_dir: Path):
    path = job_dir / "status.json"
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text())
    except:
        return None


# ----------------------
# Job Handlers
# ----------------------

OVERALL_DEADLINE_SEC = 1200  # max 20 minuter

async def do_job(job_id: str, ar_input: str):
    job_dir = RUNS_DIR / job_id
    append_log(job_dir, f"JOB START: {ar_input}")
    write_status(job_dir, status="running", progress=1, message="Initierar…")

    try:

        async def whole():
            write_status(job_dir, progress=5, message="Fångar nätverk…")
            await asyncio.wait_for(
                capture_network(ar_input, run_dir=job_dir),
                timeout=12 * 60
            )

            # Debug JSON dump
            cand = job_dir / "ads_candidates.json"
            if cand.exists():
                try:
                    raw = cand.read_text()
                    append_log(job_dir, "=== JSON DUMP START ===")
                    append_log(job_dir, raw[:20000])
                    append_log(job_dir, "=== JSON DUMP END ===")
                except:
                    pass

            write_status(job_dir, progress=70, message="Bygger Excel…")
            loop = asyncio.get_running_loop()
            await asyncio.wait_for(
                loop.run_in_executor(None, process_candidates_and_save, job_dir),
                timeout=6 * 60
            )

        await asyncio.wait_for(whole(), timeout=OVERALL_DEADLINE_SEC)

        excel = job_dir / "ads_extracted.xlsx"
        if excel.exists():
            write_status(job_dir, status="done", progress=100, message="Klart!")
            append_log(job_dir, "JOB DONE")
        else:
            write_status(job_dir, status="error", message="Excel saknas!")
            append_log(job_dir, "JOB ERROR: Excel saknas")

    except Exception as e:
        tb = traceback.format_exc()
        write_status(job_dir, status="error", message=str(e))
        append_log(job_dir, f"JOB ERROR: {e}\n{tb}")


# ----------------------
# HTTP endpoints
# ----------------------

@app.post("/run")
async def run_scraper(ar_input: str = Form(...)):
    job_id = uuid.uuid4().hex[:12]
    job_dir = RUNS_DIR / job_id
    job_dir.mkdir()

    write_status(job_dir, status="queued", progress=0, message="Köad")
    append_log(job_dir, "Job skapades")

    asyncio.create_task(do_job(job_id, ar_input.strip()))
    return {"job_id": job_id, "status": "queued"}


@app.get("/status/{job_id}")
def status(job_id: str):
    job_dir = RUNS_DIR / job_id
    s = read_status(job_dir)
    if not s:
        raise HTTPException(404, "unknown job id")

    excel = job_dir / "ads_extracted.xlsx"
    if excel.exists():
        s["result_url"] = f"/download/{job_id}"
    else:
        s["result_url"] = None

    return s


@app.get("/download/{job_id}")
def download(job_id: str):
    excel = RUNS_DIR / job_id / "ads_extracted.xlsx"
    if not excel.exists():
        raise HTTPException(404, "no excel")
    return FileResponse(str(excel), filename=f"ads_{job_id}.xlsx")


@app.get("/logs/{job_id}")
def logs(job_id: str):
    log = RUNS_DIR / job_id / "log.txt"
    if not log.exists():
        raise HTTPException(404, "unknown job id")
    return PlainTextResponse(log.read_text())

# ----------------------
# OCR-upload endpoint
# ----------------------
@app.post("/ocr_ads")
async def ocr_ads(file: UploadFile = File(...)):
    suffix = Path(file.filename).suffix or ".xlsx"

    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        inp = tmp / f"input{suffix}"
        inp.write_bytes(await file.read())

        out = tmp / "ads_with_ocr.xlsx"
        add_ocr_to_excel(inp, out)

        return FileResponse(
            str(out),
            filename="ads_with_ocr.xlsx",
            media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
        )


# ----------------------
# Widget endpoint (SquareSpace)
# ----------------------
@app.get("/widget", response_class=HTMLResponse)
def widget():
    html = """
    <div id="ads-widget"></div>
    <script src="https://ads-fetcher.onrender.com/static/widget.js"></script>
    """
    return html
