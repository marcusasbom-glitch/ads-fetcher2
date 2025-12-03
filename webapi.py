# webapi.py
from fastapi import FastAPI, Form, Request, HTTPException, UploadFile, File
from fastapi.responses import FileResponse, Response, JSONResponse, PlainTextResponse

from pathlib import Path
import os, json, uuid, asyncio, traceback, time
import tempfile
from io import BytesIO

from openpyxl import load_workbook, Workbook
from openpyxl.utils.cell import coordinate_to_tuple
from PIL import Image
import pytesseract
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, Response, JSONResponse, PlainTextResponse
from pathlib import Path
import os, json, uuid, asyncio, traceback, time
from ads_capture_and_extract import capture_network, process_candidates_and_save
from fastapi.responses import FileResponse, Response, JSONResponse, PlainTextResponse
from pathlib import Path
import os, json, uuid, asyncio, traceback, time
import tempfile
from io import BytesIO
from openpyxl import load_workbook
from openpyxl.utils.cell import coordinate_to_tuple
from PIL import Image
import pytesseract

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
                timeout=15 * 60
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
    @app.get("/ocr_job/{job_id}")
def ocr_job(job_id: str):
    """
    Kör OCR på alla bilder i job_dir/images för ett befintligt jobb
    och returnerar ett Excel med resultatet.
    """
    job_dir = RUNS_DIR / job_id
    images_dir = job_dir / "images"

    if not job_dir.exists():
        raise HTTPException(status_code=404, detail="unknown_job_id")

    if not images_dir.exists() or not any(images_dir.iterdir()):
        raise HTTPException(status_code=404, detail="no_images_for_job")

    try:
        excel_bytes = ocr_images_in_dir(images_dir)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"OCR-fel: {e}")

    filename = f"ads_ocr_{job_id}.xlsx"

    return Response(
        content=excel_bytes.getvalue(),
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'}
    )


def add_ocr_to_excel(input_path: Path, output_path: Path):
    """
    Läser en Excel med annonser + inbäddade bilder i första bladet.
    För rader där Rubrik/Beskrivning är tomma och det finns bild,
    körs OCR och dessa fält fylls i.
    """
    MAX_OCR_ROWS = int(os.getenv("MAX_OCR_FROM_EXCEL", "80"))

    wb = load_workbook(input_path)
    ws = wb.active

    # Hitta kolumnerna Rubrik / Beskrivning (skapa om de inte finns)
    header_row = 1
    header_map = {}
    for col in range(1, ws.max_column + 1):
        val = ws.cell(row=header_row, column=col).value
        if isinstance(val, str):
            header_map[val.strip()] = col

    rubrik_col = header_map.get("Rubrik")
    if rubrik_col is None:
        rubrik_col = ws.max_column + 1
        ws.cell(row=header_row, column=rubrik_col, value="Rubrik")

    beskriv_col = header_map.get("Beskrivning")
    if beskriv_col is None:
        beskriv_col = ws.max_column + 1
        ws.cell(row=header_row, column=beskriv_col, value="Beskrivning")

    # Mappa radnummer -> inbäddad bild (första bilden per rad)
    row_to_image = {}

    for img in getattr(ws, "_images", []):
        row = None
        anchor = img.anchor
        try:
            # vanligast i openpyxl
            row = anchor._from.row + 1
        except Exception:
            try:
                row = anchor.from_.row + 1
            except Exception:
                if isinstance(getattr(anchor, "anchor", None), str):
                    r, _ = coordinate_to_tuple(anchor.anchor)
                    row = r
        if row is None:
            continue

        try:
            data = img._data()
            pil = Image.open(BytesIO(data))
            row_to_image.setdefault(row, []).append(pil)
        except Exception as e:
            print("OCR: kunde inte läsa bild för rad", row, e)

    # Kör OCR på de första MAX_OCR_ROWS raderna där det saknas text
    ocr_done = 0
    for row in range(2, ws.max_row + 1):
        if ocr_done >= MAX_OCR_ROWS:
            break
        if row not in row_to_image:
            continue

        rubrik_cell = ws.cell(row=row, column=rubrik_col)
        beskriv_cell = ws.cell(row=row, column=beskriv_col)

        # hoppa rader som redan har text
        if (rubrik_cell.value and str(rubrik_cell.value).strip()) or (
            beskriv_cell.value and str(beskriv_cell.value).strip()
        ):
            continue

        img_pil = row_to_image[row][0]
        try:
            text = pytesseract.image_to_string(img_pil, lang="swe+eng")
        except Exception as e:
            print("OCR-fel på rad", row, e)
            continue

        lines = [l.strip() for l in text.splitlines() if l.strip()]
        if not lines:
            continue

        rubrik_cell.value = lines[0][:120]
        if len(lines) > 1:
            beskriv_cell.value = " ".join(lines[1:])[:500]

        ocr_done += 1

    wb.save(output_path)
def ocr_images_in_dir(images_dir: Path) -> BytesIO:
    """
    Kör OCR på alla bildfiler i images_dir och returnerar ett Excel (bytes i minnet).
    Kolumner:
      Bildfil, Rubrik, Beskrivning, Rå_OCR_text
    """
    wb = Workbook()
    ws = wb.active
    ws.title = "OCR_Ads"
    ws.append(["Bildfil", "Rubrik", "Beskrivning", "Rå_OCR_text"])

    exts = {".png", ".jpg", ".jpeg", ".webp"}

    for img_path in sorted(images_dir.iterdir()):
        if img_path.suffix.lower() not in exts:
            continue

        try:
            img = Image.open(img_path)
            # svenska + engelska
            text = pytesseract.image_to_string(img, lang="swe+eng")
        except Exception as e:
            ws.append([img_path.name, f"OCR-fel: {e}", "", ""])
            continue

        lines = [l.strip() for l in text.splitlines() if l.strip()]

        if lines:
            title = lines[0][:200]
            desc = " ".join(lines[1:])[:800] if len(lines) > 1 else ""
        else:
            title, desc = "", ""

        ws.append([img_path.name, title, desc, text])

    out = BytesIO()
    wb.save(out)
    out.seek(0)
    return out

@app.post("/ocr_ads")
async def ocr_ads(file: UploadFile = File(...)):
    """
    Tar emot en Excel-fil med annonser, kör OCR på inbäddade annonsbilder,
    lägger till textkolumner och returnerar en ny Excel-fil.
    """
    # Tillfällig arbetsmapp
    with tempfile.TemporaryDirectory() as tmpdir:
        tmp = Path(tmpdir)

        # Spara uppladdad fil
        in_path = tmp / file.filename
        in_path.write_bytes(await file.read())

        out_path = tmp / "ads_with_ocr.xlsx"

        # Kör OCR-bearbetning
        try:
            add_ocr_to_excel(in_path, out_path)
        except Exception as e:
            raise HTTPException(status_code=500, detail=f"OCR-fel: {e}")

        # Returnera filen
        return FileResponse(
            str(out_path),
            filename="ads_with_ocr.xlsx",
            media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
        )


