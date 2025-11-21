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
)


@app.get("/")
def root():
    return {"status": "ok", "message": "Ads fetcher backend up & running"}


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
        result = await asyncio.wait_for(coro, timeout=timeout_sec)
        return result
    except asyncio.TimeoutError:
        msg = f"timeout_{step_name}"
        write_status(job_dir, status="error", message=msg)
        append_log(job_dir, f"TIMEOUT i steg: {step_name}")
        raise
    except Exception as e:
        msg = f"error_{step_name}: {e}"
        write_status(job_dir, status="error", message=msg)
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

            write_status(job_dir, progress=70, message="Bearbetar och bygger Excel…")
            # Kör synk del i thread och sätt timeout (t.ex. 6 min)
            loop = asyncio.get_running_loop()
            await asyncio.wait_for(
                loop.run_in_executor(None, process_candidates_and_save, job_dir),
                timeout=6 * 60
            )

        await asyncio.wait_for(whole(), timeout=OVERALL_DEADLINE_SEC)

        write_status(job_dir, status="done", progress=100, message="Klar")
        append_log(job_dir, "JOB DONE")
    except asyncio.TimeoutError:
        append_log(job_dir, f"JOB TIMEOUT (övergripande {OVERALL_DEADLINE_SEC}s).")
        # status bör redan vara satt av run_with_timeout, men safety:
        st = read_status(job_dir) or {}
        if st.get("status") != "error":
            write_status(job_dir, status="error", message="timeout_overall")
    except Exception as e:
        append_log(job_dir, f"JOB ERROR: {e}\n{traceback.format_exc()}")
        st = read_status(job_dir) or {}
        if st.get("status") != "error":
            write_status(job_dir, status="error", message=str(e))


@app.post("/run")
async def run_job(ar_input: str = Form(...)):
    """
    Startar ett jobb. Returnerar job_id direkt.
    """
    if not ar_input:
        raise HTTPException(status_code=400, detail="missing_ar_input")

    job_id = str(uuid.uuid4())
    job_dir = RUNS_DIR / job_id
    job_dir.mkdir(parents=True, exist_ok=True)

    write_status(job_dir, status="queued", progress=0, message="Köar jobb…")
    append_log(job_dir, f"JOB CREATED med ar_input={ar_input}")
# DEBUG: visa JSON-kandidater i loggen
try:
    cand_path = job_dir / "ads_candidates.json"
    if cand_path.exists():
        import json
        data = json.loads(cand_path.read_text(encoding="utf-8"))
        print("==== DEBUG JSON DUMP START ====")
        print(json.dumps(data, indent=2, ensure_ascii=False)[:20000])  # max 20k tecken
        print("==== DEBUG JSON DUMP END ====")
except Exception as e:
    print("DEBUG JSON error:", e)

    # Starta bakgrunds-task
    asyncio.create_task(do_job(job_id, ar_input))

    return {"job_id": job_id}


@app.get("/status/{job_id}")
def get_status(job_id: str):
    job_dir = RUNS_DIR / job_id
    if not job_dir.exists():
        raise HTTPException(status_code=404, detail="unknown_job_id")
    st = read_status(job_dir)
    if not st:
        raise HTTPException(status_code=404, detail="status_missing")
    return st


@app.get("/result/{job_id}")
def get_result(job_id: str):
    job_dir = RUNS_DIR / job_id
    excel = job_dir / "ads_extracted.xlsx"
    if not excel.exists():
        # Försök hitta variant med suffix
        for f in job_dir.glob("ads_extracted*.xlsx"):
            excel = f
            break
        else:
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
