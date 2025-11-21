# webapi.py
from fastapi import FastAPI, Form, Request, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, Response, JSONResponse, PlainTextResponse
from pathlib import Path
import os, json, uuid, asyncio, traceback, time

from ads_capture_and_extract import capture_network, process_candidates_and_save

app = FastAPI()

# ----- CORS -----
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],        # lås gärna ner till dina domäner när allt funkar
    allow_methods=["*"],
    allow_headers=["*"],
    allow_credentials=False,
)

# OPTIONS catch-all så preflight aldrig blir 405
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

# ----- Root/health -----
@app.get("/")
def root():
    return JSONResponse({"ok": True, "service": "ads-fetcher",
                         "endpoints": ["/ping", "/run", "/status/{job_id}", "/download/{job_id}", "/logs/{job_id}"]})

@app.head("/")
def root_head():
    return Response(status_code=200)

@app.get("/favicon.ico")
def favicon():
    return Response(status_code=204)

@app.get("/ping")
def ping():
    return {"ok": True}

# ----- Lagring -----
RUNS_DIR = Path(os.getenv("RUNS_DIR", "/tmp/runs"))
RUNS_DIR.mkdir(parents=True, exist_ok=True)

def write_json(p: Path, obj: dict):
    p.write_text(json.dumps(obj, ensure_ascii=False), encoding="utf-8")

def append_log(job_dir: Path, line: str):
    lp = job_dir / "log.txt"
    ts = time.strftime("%Y-%m-%d %H:%M:%S")
    with lp.open("a", encoding="utf-8") as f:
        f.write(f"[{ts}] {line}\n")

def write_status(job_dir: Path, **fields):
    sp = job_dir / "status.json"
    data = {"status": "running", "progress": None, "message": None}
    if sp.exists():
        try:
            data.update(json.loads(sp.read_text(encoding="utf-8")))
        except Exception:
            pass
    data.update(fields)
    write_json(sp, data)

def read_status(job_dir: Path):
    sp = job_dir / "status.json"
    if not sp.exists():
        return None
    try:
        return json.loads(sp.read_text(encoding="utf-8"))
    except Exception:
        return None

# ----- Jobb -----
OVERALL_DEADLINE_SEC = int(os.getenv("OVERALL_DEADLINE_SEC", "1200"))  # 20 min hårdgräns

async def run_with_timeout(coro, timeout_sec: int, step_name: str, job_dir: Path):
    try:
        return await asyncio.wait_for(coro, timeout=timeout_sec)
    except asyncio.TimeoutError:
        append_log(job_dir, f"TIMEOUT i steg: {step_name} ({timeout_sec}s)")
        raise RuntimeError(f"timeout_{step_name}")
    except Exception as e:
        append_log(job_dir, f"FEL i steg: {step_name}: {e}")
        raise

async def do_job(job_id: str, ar_input: str):
    job_dir = RUNS_DIR / job_id
    append_log(job_dir, f"JOB START ar_input='{ar_input}'")
    try:
        write_status(job_dir, status="running", progress=1, message="Initierar…")

        # WATCHDOG för hela jobbet
        async def whole():
            write_status(job_dir, progress=5, message="Fångar nätverk…")
            # Capture (lägg gärna egen timeout här – t.ex. 12 min)
            await run_with_timeout(
                capture_network(ar_input, run_dir=job_dir),
                timeout_sec=12 * 60,
                step_name="capture_network",
                job_dir=job_dir,
            )

            # DEBUG: dumpa JSON-kandidater till logg (för felsökning)
            try:
                cand_path = job_dir / "ads_candidates.json"
                if cand_path.exists():
                    with cand_path.open("r", encoding="utf-8") as f:
                        data = json.load(f)
                    append_log(job_dir, "==== DEBUG JSON DUMP START ====")
                    dump_text = json.dumps(data, ensure_ascii=False)[:20000]
                    append_log(job_dir, dump_text)
                    append_log(job_dir, "==== DEBUG JSON DUMP END ====")
            except Exception as e:
                append_log(job_dir, f"DEBUG JSON error: {e}")

            write_status(job_dir, progress=70, message="Bearbetar och bygger Excel…")
            # Kör synk del i thread och sätt timeout (t.ex. 6 min)
            loop = asyncio.get_running_loop()
            await asyncio.wait_for(
                loop.run_in_executor(None, process_candidates_and_save, job_dir),
                timeout=6 * 60
            )

        await asyncio.wait_for(whole(), timeout=OVERALL_DEADLINE_SEC)

        excel = job_dir / "ads_extracted.xlsx"
        if excel.exists():
            write_status(job_dir, status="done", progress=100, message="Klart.")
            append_log(job_dir, "JOB DONE")
        else:
            write_status(job_dir, status="error", message="Excel saknas efter bearbetning.")
            append_log(job_dir, "JOB ERROR: Excel saknas")
    except Exception as e:
        tb = traceback.format_exc(limit=5)
        write_status(job_dir, status="error", message=str(e))
        append_log(job_dir, f"JOB ERROR: {e}\n{tb}")

@app.post("/run")
async def run(ar_input: str = Form(...)):
    job_id = uuid.uuid4().hex[:12]
    job_dir = RUNS_DIR / job_id
    job_dir.mkdir(parents=True, exist_ok=True)
    write_status(job_dir, status="queued", progress=0, message="Köad")
    append_log(job_dir, "Job skapades; ställer i kö…")

    asyncio.create_task(do_job(job_id, ar_input.strip()))
    return {"job_id": job_id, "status": "queued"}

@app.get("/status/{job_id}")
def status(job_id: str, request: Request):
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
    job_dir = RUNS_DIR / job_id
    excel = job_dir / "ads_extracted.xlsx"
    if not excel.exists():
        raise HTTPException(status_code=404, detail="Result file not found")
    return FileResponse(
        path=str(excel),
        filename=f"ads_extracted_{job_id}.xlsx",
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    )

@app.get("/logs/{job_id}")
def get_logs(job_id: str):
    job_dir = RUNS_DIR / job_id
    p = job_dir / "log.txt"
    if not p.exists():
        raise HTTPException(status_code=404, detail="unknown_job_id")
    return PlainTextResponse(p.read_text(encoding="utf-8"))
