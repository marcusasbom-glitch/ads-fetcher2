# webapi.py

from fastapi import FastAPI, Form, Request, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import (
    FileResponse,
    Response,
    JSONResponse,
    PlainTextResponse,
    HTMLResponse,
)

from pathlib import Path
import os, json, uuid, asyncio, traceback, time
from io import BytesIO
import re

from openpyxl import Workbook
from PIL import Image
import pytesseract

from ads_capture_and_extract import capture_network, process_candidates_and_save

app = FastAPI()

# ---------------------------------------------------
# Hjälpfunktion: rensa bort ogiltiga Excel-tecken
# (alla kontrolltecken \x00–\x1F utom \t,\n,\r)
# ---------------------------------------------------
_ILLEGAL_RE = re.compile(r"[\x00-\x08\x0b-\x0c\x0e-\x1f]")

def clean_excel_text(value) -> str:
    if value is None:
        return ""
    if not isinstance(value, str):
        value = str(value)
    return _ILLEGAL_RE.sub("", value)

# ---------------------------------------------------
# CORS
# ---------------------------------------------------
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # lås ner senare om du vill
    allow_methods=["*"],
    allow_headers=["*"],
    allow_credentials=False,
)

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

# ---------------------------------------------------
# Root / health
# ---------------------------------------------------
@app.get("/")
def root():
    return JSONResponse(
        {
            "ok": True,
            "service": "ads-fetcher",
            "endpoints": [
                "/ping",
                "/run",
                "/status/{job_id}",
                "/download/{job_id}",
                "/logs/{job_id}",
                "/ocr_job/{job_id}",
                "/widget",
            ],
        }
    )

@app.get("/ping")
def ping():
    return {"ok": True}

@app.head("/")
def head_root():
    return Response(status_code=200)

@app.get("/favicon.ico")
def favicon():
    return Response(status_code=204)

# ---------------------------------------------------
# Lagring / status
# ---------------------------------------------------
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

# ---------------------------------------------------
# Job-körning (scraper)
# ---------------------------------------------------
OVERALL_DEADLINE_SEC = int(os.getenv("OVERALL_DEADLINE_SEC", "1200"))  # 20 min

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

        async def whole():
            # 1. Capture network
            write_status(job_dir, progress=5, message="Fångar nätverk…")
            await run_with_timeout(
                capture_network(ar_input, run_dir=job_dir),
                timeout_sec=12 * 60,
                step_name="capture_network",
                job_dir=job_dir,
            )

            # 2. Debug JSON (valfritt)
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

            # 3. Bearbeta & skapa Excel
            write_status(job_dir, progress=70, message="Bearbetar och bygger Excel…")
            loop = asyncio.get_running_loop()
            await asyncio.wait_for(
                loop.run_in_executor(None, process_candidates_and_save, job_dir),
                timeout=15 * 60,
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

# ---------------------------------------------------
# API: run/status/download/logs
# ---------------------------------------------------
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

# ---------------------------------------------------
# OCR – batchar över flera anrop, med state per jobb
# ---------------------------------------------------
def ocr_images_batch(
    image_paths,
    start_index: int,
    max_images: int,
    max_seconds: int,
):
    """
    Kör OCR på en batch av bilder:

    - image_paths: sorterad lista med Path-objekt
    - start_index: index i listan att börja på
    - max_images: max antal bilder i denna batch
    - max_seconds: max tid för denna batch

    Returnerar:
      (excel_bytes, processed_count, new_index, reason_str)
    """
    start_time = time.time()

    wb = Workbook()
    ws = wb.active
    ws.title = "OCR_Ads"
    ws.append(["Bildfil", "Rubrik", "Beskrivning", "Rå_OCR_text"])

    total = len(image_paths)
    valid_exts = {".png", ".jpg", ".jpeg", ".webp"}

    processed = 0
    idx = start_index
    reason = "completed"  # default, ändras vid break

    # Loop max max_images eller tills listan tar slut
    while idx < total and processed < max_images:
        img_path = image_paths[idx]
        if img_path.suffix.lower() not in valid_exts:
            idx += 1
            continue

        # tidsgräns före tunga operationer
        if time.time() - start_time > max_seconds:
            reason = "time_limit"
            ws.append([
                "",
                f"Avbröt OCR (tidsgräns {max_seconds}s nådd)",
                "",
                "",
            ])
            break

        try:
            img = Image.open(img_path)

            # nerskalning för fart/minne
            try:
                img.thumbnail((1200, 1200))
            except Exception:
                pass

            raw_text = pytesseract.image_to_string(img, lang="swe+eng")
        except Exception as e:
            safe_err = clean_excel_text(e)
            ws.append([clean_excel_text(img_path.name), "OCR-fel", "", safe_err])
            idx += 1
            processed += 1
            continue

        text = clean_excel_text(raw_text)
        lines = [l.strip() for l in text.splitlines() if l.strip()]
        if lines:
            title = clean_excel_text(lines[0][:200])
            desc  = clean_excel_text(" ".join(lines[1:])[:800]) if len(lines) > 1 else ""
        else:
            title, desc = "", ""

        ws.append([
            clean_excel_text(img_path.name),
            title,
            desc,
            text,
        ])

        idx += 1
        processed += 1

    if processed >= max_images and idx < total:
        reason = "image_limit"
        ws.append([
            "",
            f"Avbröt OCR (max {max_images} bilder i denna batch)",
            "",
            "",
        ])
    elif idx >= total and reason == "completed":
        # Alla bilder klara i denna batch
        ws.append([
            "",
            "OCR klar för alla bilder i detta jobb.",
            "",
            "",
        ])

    out = BytesIO()
    wb.save(out)
    out.seek(0)
    return out, processed, idx, reason

@app.get("/ocr_job/{job_id}")
def ocr_job(job_id: str):
    """
    Kör OCR i batchar för ett jobb. Håller reda på progress i ocr_state.json.

    Varje anrop:
      - tar nästa batch (OCR_MAX_IMAGES, default 30)
      - respekterar tidsgräns OCR_MAX_SECONDS (default 840s)
      - returnerar en Excel-fil med just denna batch
      - uppdaterar ocr_state.json med nästa startindex

    När alla bilder är klara returneras 400 med detail="ocr_already_completed".
    """
    job_dir = RUNS_DIR / job_id
    if not job_dir.exists():
        raise HTTPException(status_code=404, detail="unknown_job_id")

    images_dir = job_dir / "images"
    if not images_dir.exists():
        raise HTTPException(status_code=404, detail="no_images_for_job")

    image_paths = sorted(
        [p for p in images_dir.iterdir() if p.is_file()]
    )
    if not image_paths:
        raise HTTPException(status_code=404, detail="no_images_for_job")

    max_images = int(os.getenv("OCR_MAX_IMAGES", "30"))
    max_seconds = int(os.getenv("OCR_MAX_SECONDS", "840"))  # 14 min

    state_path = job_dir / "ocr_state.json"
    next_index = 0
    state = {}

    if state_path.exists():
        try:
            state = json.loads(state_path.read_text(encoding="utf-8"))
            next_index = int(state.get("next_index", 0))
        except Exception:
            next_index = 0
            state = {}

    total = len(image_paths)

    if next_index >= total:
        # All OCR redan klar
        raise HTTPException(status_code=400, detail="ocr_already_completed")

    append_log(job_dir, f"OCR batch start: job_id={job_id}, start_index={next_index}, max_images={max_images}, max_seconds={max_seconds}")

    try:
        excel_bytes, processed, new_index, reason = ocr_images_batch(
            image_paths=image_paths,
            start_index=next_index,
            max_images=max_images,
            max_seconds=max_seconds,
        )
    except Exception as e:
        append_log(job_dir, f"OCR ERROR: {e}")
        raise HTTPException(status_code=500, detail=f"OCR-fel: {e}")

    # uppdatera state
    new_state = {
        "next_index": int(new_index),
        "total_images": total,
        "last_batch_processed": int(processed),
        "last_reason": reason,
        "updated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
    }
    write_json(state_path, new_state)

    append_log(job_dir, f"OCR batch done: processed={processed}, new_index={new_index}, reason={reason}")

    filename = f"ads_ocr_{job_id}_batch_{next_index}_to_{new_index-1}.xlsx"
    return Response(
        content=excel_bytes.getvalue(),
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": f'attachment; filename=\"{filename}\"'},
    )

# ---------------------------------------------------
# Widget-HTML för iframe (Squarespace)
# ---------------------------------------------------
WIDGET_HTML = """<!DOCTYPE html>
<html lang="sv">
<head>
  <meta charset="utf-8" />
  <title>Annons-scraper</title>
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <style>
    body {
      font-family: system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
      background:#f7f7f7;
      margin:0;
      padding:16px;
    }
    #ads-scraper-widget {
      border:1px solid #ddd;
      border-radius:8px;
      max-width:650px;
      margin:0 auto;
      padding:16px;
      background:#fff;
    }
    .label { display:block; margin-bottom:4px; font-weight:600; }
    input[type=text] { width:100%; padding:6px; margin-bottom:8px; }
    button { padding:6px 12px; cursor:pointer; }
    #debugLog {
      margin-top:16px;
      background:#000;
      color:#0f0;
      font-family:monospace;
      font-size:11px;
      padding:6px;
      height:140px;
      overflow:auto;
      white-space:pre-wrap;
    }
    .progress-bar-bg {
      background:#eee;
      border-radius:4px;
      overflow:hidden;
      height:14px;
      max-width:320px;
      margin-bottom:8px;
    }
    .progress-bar-fill {
      width:0%;
      height:100%;
      background:#4caf50;
      transition:width 0.3s;
    }
  </style>
</head>
<body>
  <div id="ads-scraper-widget">
    <h3>Annonsinsamling från Google Ads Transparency Center</h3>

    <p>Steg 1: Ange AR-ID eller full URL till annonsörssidan.</p>

    <label for="arInput" class="label">AR-ID eller URL</label>
    <input id="arInput" type="text" placeholder="t.ex. AR01255062578594316289" />

    <button id="startBtn" type="button">Starta scraping</button>
    <span id="startStatus" style="font-size:0.9rem;"></span>

    <hr style="margin:16px 0;" />

    <div id="jobInfo" style="display:none;">
      <h4>Jobbstatus</h4>

      <p style="margin:4px 0;">
        <strong>Status:</strong>
        <span id="jobStatusText">–</span>
      </p>

      <p style="margin:4px 0;">
        <strong>Meddelande:</strong>
        <span id="jobMessage">–</span>
      </p>

      <p style="margin:4px 0 8px 0;">
        <strong>Progress:</strong>
        <span id="jobProgress">–</span>
      </p>

      <div class="progress-bar-bg">
        <div id="jobProgressBar" class="progress-bar-fill"></div>
      </div>

      <p id="downloadWrapper" style="display:none;margin:4px 0;">
        <strong>Excel:</strong>
        <a id="downloadLink" href="#" target="_blank">Ladda ner annonsfil</a>
      </p>

      <p id="logsWrapper" style="display:none;margin:4px 0;">
        <strong>Loggar:</strong>
        <a id="logsLink" href="#" target="_blank">Visa logg</a>
      </p>

      <hr style="margin:16px 0;" />

      <h4>Steg 2 (valfritt): OCR på annonsbilder</h4>
      <p style="margin-top:0;">
        När scraping är klar kan du köra OCR på annonsbilderna i batchar.
        Varje klick på knappen kör nästa batch tills alla är klara.
      </p>

      <button id="ocrImagesBtn" type="button" disabled style="margin-right:8px;">
        Kör OCR på annonsbilderna (batch)
      </button>
      <span id="ocrImagesStatus" style="font-size:0.9rem;"></span>
    </div>

    <div id="debugLog"></div>
  </div>

  <script>
  (function() {
    // Viktigt: peka alltid mot Render-API:t, inte Squarespace-domänen
    const API_BASE = "https://ads-fetcher.onrender.com";

    const arInput       = document.getElementById("arInput");
    const startBtn      = document.getElementById("startBtn");
    const startStatus   = document.getElementById("startStatus");

    const jobInfo       = document.getElementById("jobInfo");
    const jobStatusText = document.getElementById("jobStatusText");
    const jobMessage    = document.getElementById("jobMessage");
    const jobProgress   = document.getElementById("jobProgress");
    const jobProgressBar= document.getElementById("jobProgressBar");

    const downloadWrapper = document.getElementById("downloadWrapper");
    const downloadLink    = document.getElementById("downloadLink");
    const logsWrapper     = document.getElementById("logsWrapper");
    const logsLink        = document.getElementById("logsLink");

    const ocrBtn          = document.getElementById("ocrImagesBtn");
    const ocrStatus       = document.getElementById("ocrImagesStatus");

    const debugLog        = document.getElementById("debugLog");

    let currentJobId = null;
    let pollTimer    = null;

    function log(msg) {
      const ts = new Date().toISOString().slice(11,19);
      debugLog.textContent += "[" + ts + "] " + msg + "\\n";
      debugLog.scrollTop = debugLog.scrollHeight;
    }

    function resetUI() {
      jobInfo.style.display      = "none";
      jobStatusText.textContent  = "–";
      jobMessage.textContent     = "–";
      jobProgress.textContent    = "–";
      jobProgressBar.style.width = "0%";
      downloadWrapper.style.display = "none";
      logsWrapper.style.display     = "none";
      ocrBtn.disabled           = true;
      ocrStatus.textContent     = "";
    }

    async function startJob() {
      const val = (arInput.value || "").trim();
      if (!val) {
        startStatus.textContent = "Fyll i AR-ID eller URL först.";
        return;
      }

      resetUI();
      startStatus.textContent = "Startar jobb...";
      startBtn.disabled = true;
      log("Startar scraping för: " + val);

      try {
        const fd = new FormData();
        fd.append("ar_input", val);

        const resp = await fetch(API_BASE + "/run", {
          method: "POST",
          body: fd
        });

        if (!resp.ok) {
          const t = await resp.text();
          startStatus.textContent = "Fel vid start: " + t;
          log("Fel vid /run: " + resp.status + " " + t);
          startBtn.disabled = false;
          return;
        }

        const data = await resp.json();
        currentJobId = data.job_id;
        log("Jobb skapat, id=" + currentJobId);

        startStatus.textContent = "Jobb skapat (ID: " + currentJobId + "). Hämtar status...";
        jobInfo.style.display = "block";

        if (pollTimer) clearInterval(pollTimer);
        pollTimer = setInterval(function() { pollStatus(currentJobId); }, 3000);
        pollStatus(currentJobId);
      } catch (err) {
        console.error(err);
        log("Tekniskt fel vid startJob: " + err);
        startStatus.textContent = "Tekniskt fel: " + err;
        startBtn.disabled = false;
      }
    }

    async function pollStatus(jobId) {
      try {
        const url = API_BASE + "/status/" + encodeURIComponent(jobId);
        log("Pollar status: " + url);

        const resp = await fetch(url, { method: "GET" });

        if (!resp.ok) {
          const t = await resp.text();
          jobStatusText.textContent = "Fel (" + resp.status + ")";
          jobMessage.textContent    = t;
          jobProgress.textContent   = "–";
          jobProgressBar.style.width = "0%";
          log("Fel vid /status: " + resp.status + " " + t);
          clearInterval(pollTimer);
          pollTimer = null;
          startBtn.disabled = false;
          return;
        }

        const data = await resp.json();

        jobStatusText.textContent = data.status || "-";
        jobMessage.textContent    = data.message || "";
        if (data.progress != null) {
          jobProgress.textContent = data.progress + "%";
          jobProgressBar.style.width =
            Math.min(100, Math.max(0, Number(data.progress))) + "%";
        } else {
          jobProgress.textContent = "–";
          jobProgressBar.style.width = "0%";
        }

        if (data.result_url) {
          downloadWrapper.style.display = "block";
          downloadLink.href = data.result_url;
        }

        logsWrapper.style.display = "block";
        logsLink.href = API_BASE + "/logs/" + encodeURIComponent(jobId);

        if (data.status === "done") {
          clearInterval(pollTimer);
          pollTimer = null;
          startBtn.disabled = false;
          startStatus.textContent = "Jobb klart.";
          log("Jobb klart.");
          ocrBtn.disabled = false;
          ocrStatus.textContent = "Scraping klar – du kan nu köra OCR på annonsbilderna i batchar.";
        } else if (data.status === "error") {
          clearInterval(pollTimer);
          pollTimer = null;
          startBtn.disabled = false;
          startStatus.textContent = "Jobbet misslyckades.";
          log("Jobbet markerat som error.");
        } else {
          startStatus.textContent = "Jobb pågår (" + (data.status || "okänt") + ")...";
        }

      } catch (err) {
        console.error(err);
        jobMessage.textContent = "Tekniskt fel vid polling: " + err;
        log("Tekniskt fel vid polling: " + err);
        startBtn.disabled = false;
        if (pollTimer) {
          clearInterval(pollTimer);
          pollTimer = null;
        }
      }
    }

    async function runOCR() {
      if (!currentJobId) {
        ocrStatus.textContent = "Inget aktivt jobb-ID. Kör scraping först.";
        return;
      }

      ocrBtn.disabled = true;
      ocrStatus.textContent = "Kör OCR på nästa batch...";
      log("Startar OCR-batch för jobb " + currentJobId);

      try {
        const url = API_BASE + "/ocr_job/" + encodeURIComponent(currentJobId);
        const resp = await fetch(url, { method: "GET" });

        if (!resp.ok) {
          const txt = await resp.text();
          ocrStatus.textContent = "Fel vid OCR: " + txt;
          log("Fel vid /ocr_job: " + resp.status + " " + txt);
          ocrBtn.disabled = false;
          return;
        }

        const blob = await resp.blob();
        const dlUrl = URL.createObjectURL(blob);
        const a = document.createElement("a");
        a.href = dlUrl;
        a.download = "ads_ocr_" + currentJobId + "_batch.xlsx";
        document.body.appendChild(a);
        a.click();
        a.remove();
        URL.revokeObjectURL(dlUrl);

        ocrStatus.textContent = "OCR-batch klar – fil nedladdad. Kör igen för nästa batch om det finns fler bilder.";
        log("OCR-batch-fil nedladdad.");
      } catch (err) {
        console.error(err);
        ocrStatus.textContent = "Tekniskt fel vid OCR: " + err;
        log("Tekniskt fel vid OCR: " + err);
      } finally {
        ocrBtn.disabled = false;
      }
    }

    startBtn.addEventListener("click", startJob);
    ocrBtn.addEventListener("click", runOCR);

    log("Widget laddad. API_BASE=" + API_BASE);
  })();
  </script>
</body>
</html>
"""

@app.get("/widget", response_class=HTMLResponse)
def widget():
    return HTMLResponse(WIDGET_HTML)
