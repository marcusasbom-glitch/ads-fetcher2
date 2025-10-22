# ads_capture_and_extract.py
# Komplett capture + extraction med run_dir-stöd + OCR av rubrik/beskrivning ur bild

import asyncio
import os
import json
import time
import re
from pathlib import Path
from io import BytesIO

from playwright.async_api import async_playwright
import requests
import pandas as pd
from PIL import Image
from openpyxl import load_workbook
from openpyxl.drawing.image import Image as XLImage
from openpyxl.utils import get_column_letter

# ==== OCR imports ====
import pytesseract
from pytesseract import Output
import cv2
import numpy as np
from collections import defaultdict

# ----- Globala "pekare" som kan flyttas till valfri run_dir -----
OUTPUT_DIR = Path("network_dump")
CANDIDATES_PATH = OUTPUT_DIR / "ads_candidates.json"
IMAGES_DIR = Path("images")
OUTPUT_EXCEL = "ads_extracted.xlsx"

def set_paths(base_dir: Path | str | None):
    """
    Pekar om alla outputvägar till given run_dir (används per-jobb).
    """
    global OUTPUT_DIR, CANDIDATES_PATH, IMAGES_DIR, OUTPUT_EXCEL
    if base_dir is None:
        base = Path(".")
    else:
        base = Path(base_dir)
    OUTPUT_DIR = base / "network_dump"
    CANDIDATES_PATH = OUTPUT_DIR / "ads_candidates.json"
    IMAGES_DIR = base / "images"
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    IMAGES_DIR.mkdir(parents=True, exist_ok=True)
    OUTPUT_EXCEL = str(base / "ads_extracted.xlsx")


# ----- Heuristik & hjälp -----
AD_KEYWORDS = ["ads", "advertiser", "advertisers", "creative", "creatives",
               "headline", "description", "imageurl", "impression", "creativeId", "creative"]

IMG_SRC_RE = re.compile(r'<img[^>]+src=["\']([^"\']+)["\']', re.IGNORECASE)
HTTP_URL_RE = re.compile(r"https?://[^\s\"'<>]+", re.IGNORECASE)

def looks_like_ad_json(obj):
    try:
        s = json.dumps(obj).lower()
    except Exception:
        return False
    return any(k in s for k in AD_KEYWORDS)

def safe_filename(url: str, method: str, ts: int):
    safe = re.sub(r'[^0-9A-Za-z._-]', '_', url)[:140]
    return f"{ts}_{method}_{safe}"

def extract_img_from_html(html_snippet):
    if not html_snippet:
        return None
    m = IMG_SRC_RE.search(html_snippet)
    if m:
        return m.group(1)
    m2 = re.search(r"(https?://tpc\.googlesyndication\.com/[^\s\"'<>]+)", html_snippet)
    if m2:
        return m2.group(1)
    return None

def try_fetch_preview_js(url, session, timeout=8):
    try:
        r = session.get(url, timeout=timeout)
        if r.status_code != 200:
            return None
        text = r.text
        m = IMG_SRC_RE.search(text)
        if m:
            return m.group(1)
        m2 = re.search(r"(https?://tpc\.googlesyndication\.com/[^\s\"'<>]+)", text)
        if m2:
            return m2.group(1)
        m3 = HTTP_URL_RE.search(text)
        if m3:
            return m3.group(0)
    except Exception:
        return None
    return None

def get_available_filename(base_name):
    base = Path(base_name)
    stem = base.stem
    suff = base.suffix or ".xlsx"
    candidate = base
    counter = 1
    while candidate.exists():
        candidate = base.parent / f"{stem}_{counter}{suff}"
        counter += 1
    return str(candidate)

# ====== OCR: hämta H1/H2 ur annonsbild ======
def ocr_h1_h2_from_image(img_path: str, lang: str = "swe+eng"):
    """
    Returnerar (h1, h2) gissade rubriker ur en bild.
    Heuristik: plocka de 2 "största" textraderna (utifrån bbox-höjd) med vettig conf.
    """
    try:
        im = cv2.imdecode(np.fromfile(img_path, dtype=np.uint8), cv2.IMREAD_COLOR)
        if im is None:
            im = cv2.cvtColor(np.array(Image.open(img_path).convert("RGB")), cv2.COLOR_RGB2BGR)

        g = cv2.cvtColor(im, cv2.COLOR_BGR2GRAY)
        h, w = g.shape[:2]
        if max(h, w) < 1400:
            g = cv2.resize(g, (w * 2, h * 2), interpolation=cv2.INTER_CUBIC)
        g = cv2.bilateralFilter(g, 7, 50, 50)
        g = cv2.adaptiveThreshold(g, 255, cv2.ADAPTIVE_THRESH_GAUSSIAN_C,
                                  cv2.THRESH_BINARY, 31, 10)

        data = pytesseract.image_to_data(g, lang=lang, output_type=Output.DICT)

        lines = defaultdict(lambda: {"texts": [], "heights": [], "confs": []})
        n = len(data["text"])
        for i in range(n):
            txt = (data["text"][i] or "").strip()
            conf = float(data["conf"][i]) if data["conf"][i] not in ("-1", None, "") else -1.0
            if not txt or conf < 40:
                continue
            key = (data["block_num"][i], data["par_num"][i], data["line_num"][i])
            lines[key]["texts"].append(txt)
            lines[key]["heights"].append(int(data["height"][i]) or 0)
            lines[key]["confs"].append(conf)

        scored = []
        for info in lines.values():
            text = " ".join(info["texts"]).strip()
            if len(text) < 3:
                continue
            avg_h = float(np.mean(info["heights"])) if info["heights"] else 0.0
            mean_conf = float(np.mean(info["confs"])) if info["confs"] else 0.0
            score = avg_h * (mean_conf / 100.0) * (1 + np.log1p(len(text)))
            scored.append((score, text))

        if not scored:
            return None, None
        scored.sort(reverse=True, key=lambda x: x[0])
        h1 = scored[0][1]
        h2 = scored[1][1] if len(scored) > 1 else None
        if h2 and h1 and h2.strip().lower() == h1.strip().lower():
            h2 = None
        return h1, h2
    except Exception:
        return None, None


# ---------- Capture (Playwright) ----------
async def capture_network(ar_input: str, run_dir: str | Path | None = None) -> bool:
    """
    Kör Playwright, fångar nätverkstrafik och skriver relevanta responses till OUTPUT_DIR.
    Skapar också ads_candidates.json med parsed JSON som matchar heuristiken.
    Returnerar True om capture kördes färdigt.
    """
    set_paths(run_dir)

    # Bygg URL om det inte är en URL redan
    if ar_input.startswith("http"):
        url = ar_input
    else:
        url = (
            f"https://adstransparency.google.com/advertiser/{ar_input}"
            "?origin=ata&region=SE&preset-date=Last+30+days&platform=SEARCH"
        )
    print(f"🔗 Laddar: {url}")

    responses_meta = []

    async with async_playwright() as p:
        # headless=True i container
        browser = await p.chromium.launch(headless=True)
        context = await browser.new_context(
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64)",
            locale="sv-SE",
            extra_http_headers={"Accept-Language": "sv-SE,sv;q=0.9,en;q=0.8"},
        )
        page = await context.new_page()

        async def on_response(response):
            try:
                r_url = response.url
                status = response.status
                headers = response.headers or {}
                method = response.request.method if response.request else "GET"
                ts = int(time.time() * 1000)
                base_name = safe_filename(r_url, method, ts)
                meta = {"url": r_url, "status": status, "method": method, "headers": headers, "saved": None}

                content_type = headers.get("content-type", "").lower()

                # JSON/text-like
                if "application/json" in content_type or r_url.lower().endswith(".json") or "json" in r_url.lower():
                    try:
                        text = await response.text()
                        cleaned = text.lstrip(")]}',\n ")
                        filep = OUTPUT_DIR / (base_name + ".json")
                        filep.write_text(cleaned, encoding="utf-8")
                        meta["saved"] = str(filep.name)
                    except Exception as e:
                        meta["error"] = f"json_save_error: {e}"

                # HTML / JS / plain text
                elif any(ct in content_type for ct in ("text/html", "text/plain", "application/javascript")):
                    try:
                        text = await response.text()
                        filep = OUTPUT_DIR / (base_name + ".txt")
                        filep.write_text(text, encoding="utf-8")
                        meta["saved"] = str(filep.name)
                    except Exception as e:
                        meta["error"] = f"text_save_error: {e}"

                else:
                    # binary (images etc) - save if reasonably small
                    try:
                        body = await response.body()
                        if body and len(body) < 5_000_000:
                            ext = "bin"
                            if "image/png" in content_type: ext = "png"
                            elif "image/jpeg" in content_type or "image/jpg" in content_type: ext = "jpg"
                            filep = OUTPUT_DIR / (base_name + f".{ext}")
                            filep.write_bytes(body)
                            meta["saved"] = str(filep.name)
                    except Exception as e:
                        meta["error"] = f"binary_save_error: {e}"

                responses_meta.append(meta)
            except Exception as e:
                print("on_response error:", e)

        page.on("response", on_response)

        try:
            await page.goto(url, wait_until="networkidle", timeout=60000)
        except Exception as e:
            print("⚠️ page.goto error:", e)

        # Scroll för att trigga lazy loads
        for _ in range(12):
            try:
                await page.evaluate("window.scrollBy(0, window.innerHeight);")
                await asyncio.sleep(0.8)
            except Exception:
                await asyncio.sleep(0.5)

        await asyncio.sleep(2.0)

        # index
        (OUTPUT_DIR / "responses_index.json").write_text(
            json.dumps(responses_meta, indent=2, ensure_ascii=False),
            encoding="utf-8"
        )
        print(f"✅ Sparade nätverkstrafik i '{OUTPUT_DIR}'. Index som responses_index.json")

        # Scanna JSON-filer för ad-liknande strukturer
        ad_candidates = []
        for f in sorted(OUTPUT_DIR.glob("*.json")):
            try:
                txt = f.read_text(encoding="utf-8")
                cleaned = txt.lstrip(")]}',\n ")
                parsed = json.loads(cleaned)
                if looks_like_ad_json(parsed):
                    snippet = json.dumps(parsed)[:2000]
                    ad_candidates.append({"source_file": f.name, "snippet": snippet, "parsed": parsed})
            except Exception:
                continue

        CANDIDATES_PATH.write_text(
            json.dumps(ad_candidates, indent=2, ensure_ascii=False),
            encoding="utf-8"
        )
        print(f"🔎 Hittade {len(ad_candidates)} JSON som matchar annons-heuristik. Se {CANDIDATES_PATH}")

        await browser.close()
    return True


# ---------- Post-processing / extraction ----------
def process_candidates_and_save(run_dir: str | Path | None = None) -> bool:
    """
    Läser ads_candidates.json, laddar ner bilder och skapar en Excel med metadata + inbäddade bilder.
    NU: OCR-ar varje bild och lägger till kolumnerna 'H1 (OCR)' och 'H2 (OCR)'.
    """
    set_paths(run_dir)

    if not CANDIDATES_PATH.exists():
        print(f"Fel: kunde inte hitta {CANDIDATES_PATH}. Kör först capture_network(ar_input).")
        return False

    with open(CANDIDATES_PATH, "r", encoding="utf-8") as f:
        candidates = json.load(f)

    rows = []
    session = requests.Session()
    session.headers.update({"User-Agent": "Mozilla/5.0"})

    # --- Hjälpfunktioner för att hitta bild-URL ---
    def scan_for_img(obj):
        if isinstance(obj, str):
            if "tpc.googlesyndication.com" in obj or obj.endswith((".png", ".jpg", ".jpeg", ".webp")):
                return obj
            m = IMG_SRC_RE.search(obj)
            if m:
                return m.group(1)
        elif isinstance(obj, dict):
            for _v in obj.values():
                res = scan_for_img(_v)
                if res: return res
        elif isinstance(obj, list):
            for it in obj:
                res = scan_for_img(it)
                if res: return res
        return None

    for cand in candidates:
        src_file = cand.get("source_file", "")
        parsed = cand.get("parsed", cand)
        creative_list = None

        if isinstance(parsed, dict):
            for key in ("1", "items", "ads", "result", "creatives"):
                if key in parsed and isinstance(parsed[key], list):
                    creative_list = parsed[key]
                    break
            if creative_list is None:
                for v in parsed.values():
                    if isinstance(v, list):
                        creative_list = v
                        break
        elif isinstance(parsed, list):
            creative_list = parsed

        if not creative_list:
            continue

        for entry in creative_list:
            if not isinstance(entry, dict):
                continue

            creative_id = entry.get("2") or entry.get("creativeId") or entry.get("id") or ""
            advertiser = entry.get("12") or entry.get("advertiserName") or entry.get("advertiser") or ""
            headline = entry.get("headline") or entry.get("7") or ""
            description = entry.get("description") or entry.get("8") or ""

            image_url = None
            assets = entry.get("3") or entry.get("creative") or {}
            if isinstance(assets, dict):
                if "3" in assets and isinstance(assets["3"], dict):
                    possible_html = assets["3"].get("2") or assets["3"].get("html") or ""
                    image_url = extract_img_from_html(possible_html)
                if not image_url and "1" in assets and isinstance(assets["1"], dict):
                    preview_url = assets["1"].get("4")
                    if preview_url:
                        image_url = try_fetch_preview_js(preview_url, session)
                if not image_url:
                    image_url = scan_for_img(assets)

            if not image_url:
                image_url = scan_for_img(entry)

            image_file = ""
            h1_ocr = None
            h2_ocr = None

            if image_url:
                try:
                    if image_url.startswith("//"):
                        image_url = "https:" + image_url
                    r = session.get(image_url, timeout=10, stream=True)
                    if r.status_code == 200:
                        ext = "png"
                        ct = r.headers.get("content-type", "").lower()
                        if "jpeg" in ct or "jpg" in ct: ext = "jpg"
                        elif "png" in ct: ext = "png"
                        elif "webp" in ct: ext = "webp"
                        filename = f"{(creative_id or 'creative')}_{len(rows)+1}.{ext}"
                        file_path = IMAGES_DIR / filename
                        with open(file_path, "wb") as wf:
                            for chunk in r.iter_content(1024):
                                wf.write(chunk)
                        image_file = str(file_path)

                        # ---- OCR här (på den sparade bilden) ----
                        try:
                            h1_ocr, h2_ocr = ocr_h1_h2_from_image(image_file)
                        except Exception:
                            h1_ocr = h2_ocr = None
                except Exception:
                    image_file = ""

            rows.append({
                "SourceFile": src_file,
                "CreativeID": creative_id,
                "Annonsör": advertiser,
                "Rubrik": headline,
                "Beskrivning": description,
                "Bild-URL": image_url or "",
                "Bildfil": image_file,
                "H1 (OCR)": h1_ocr,
                "H2 (OCR)": h2_ocr,
            })

    if not rows:
        print("Hittade inga annonser i candidates.")
        return False

    excel_path = get_available_filename(OUTPUT_EXCEL)
    df = pd.DataFrame(rows, columns=[
        "SourceFile", "CreativeID", "Annonsör", "Rubrik", "Beskrivning",
        "Bild-URL", "Bildfil", "H1 (OCR)", "H2 (OCR)"
    ])
    df.to_excel(excel_path, index=False)
    print(f"Sparade tabell till {excel_path} (bildvägar i kolumn 'Bildfil').")

    # Infoga bilder i Excel
    wb = load_workbook(excel_path)
    ws = wb.active
    # kolumnindex: A=1.. G=7 (Bildfil i kol G i ovan ordning), våra OCR-kolumner är H/I
    for idx, r in enumerate(rows, start=2):
        img_path = r.get("Bildfil")
        if img_path:
            try:
                xlimg = XLImage(img_path)
                xlimg.width = 160
                xlimg.height = 90
                # Bilden placeras i kolumn G (7) enligt DF-ordningen ovan
                ws.add_image(xlimg, f"G{idx}")
                ws.row_dimensions[idx].height = 80
            except Exception as e:
                print(f"Fel vid inbäddning av bild för rad {idx}: {e}")

    # Auto-bredd
    for i, col in enumerate(df.columns, start=1):
        col_letter = get_column_letter(i)
        maxlen = max((len(str(x)) for x in df[col]), default=len(col))
        ws.column_dimensions[col_letter].width = min(maxlen + 8, 80)

    wb.save(excel_path)
    print(f"✅ Excel med inbäddade bilder + H1/H2 (OCR) sparad som: {excel_path}")
    return True
