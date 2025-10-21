# ads_capture_and_extract.py
# ============================================================
# OBS! Behåll din befintliga 'capture_network' (scraping) oförändrad.
# Den här filen lägger till OCR (H1/H2) och uppdaterar Excel-exporten.
# ============================================================

# --- ersätt HELA capture_network med denna version ---
import asyncio, mimetypes
from pathlib import Path
from typing import List, Dict

from playwright.async_api import async_playwright

async def capture_network(ar_input: str, run_dir: Path) -> None:
    """
    Minimal generisk capture:
      - Navigerar till ar_input (om det ser ut som en URL), annars försöker bygga FB Ads Library-länk funktionsmässigt.
      - Sparar ALLA image/*-resurser från nätverket till run_dir/images/.
      - Om inga bilder hittas: tar en fullständig screenshot som fallback.
      - Skriver run_dir/ads_collected.json (lista med dicts).
    """
    images: List[Dict] = []
    img_dir = run_dir / "images"
    img_dir.mkdir(parents=True, exist_ok=True)

    # Hjälpare: spara ett nätverkssvar som bild
    async def _save_image_response(resp):
        try:
            ct = (resp.headers.get("content-type") or "").split(";")[0].strip().lower()
            if not ct.startswith("image/"):
                return
            body = await resp.body()
            if not body:
                return
            ext = mimetypes.guess_extension(ct) or ".bin"
            name = f"img_{len(images)+1:03d}{ext}"
            path = img_dir / name
            path.write_bytes(body)
            images.append({
                "ar_id": ar_input,
                "source": "network",
                "url": resp.url,
                "image_path": str(path),
                "content_type": ct,
            })
        except Exception:
            # svälj – network-flöde får aldrig krascha hela jobbet
            return

    # Bestäm navigations-URL
    def _to_url(s: str) -> str:
        s = (s or "").strip()
        if s.startswith("http://") or s.startswith("https://"):
            return s
        # Enkel heuristic: om det liknar AR-id, försök Ads Library (du kan byta till din exakta mall)
        # OBS: detta är bara en safe placeholder. Om du redan bygger url i din originalkod – behåll din version.
        return f"https://www.facebook.com/ads/library/?id={s}"

    url = _to_url(ar_input)

    # Playwright-körning
    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=True)
        context = await browser.new_context()
        page = await context.new_page()

        # Fånga alla responses och spara image/*
        page.on("response", lambda resp: asyncio.create_task(_save_image_response(resp)))

        # Nav + enkel väntan
        try:
            await page.goto(url, wait_until="domcontentloaded", timeout=30000)
        except Exception:
            # även om navigation bråkar vill vi ändå försöka fånga ev. redan loggade responses
            pass

        # Låt sidan ladda en stund (annars missar vi bilder som streamas in)
        # Justera om du vill: kortare vid snabba sidor, längre om Ads-sidan är seg
        await page.wait_for_timeout(6000)

        # Fallback om inga nätverksbilder fångats → ta en screenshot
        if not images:
            shot = img_dir / "screenshot.png"
            try:
                await page.screenshot(path=str(shot), full_page=True)
                images.append({
                    "ar_id": ar_input,
                    "source": "screenshot",
                    "url": page.url,
                    "image_path": str(shot),
                    "content_type": "image/png",
                })
            except Exception:
                # som sista utväg – lämna listan tom (process-steget skapar tomt Excel ändå)
                pass

        await context.close()
        await browser.close()

    # Skriv JSON (även om tom lista – process-steget hanterar detta och skriver tom Excel)
    (run_dir / "ads_collected.json").write_text(
        json.dumps(images, ensure_ascii=False),
        encoding="utf-8"
    )



# ============================================================
# OCR-HJÄLP: ocr_h1_h2_from_image
# ============================================================
def ocr_h1_h2_from_image(img_path: str, lang: str = "swe+eng"):
    """
    Returnerar (h1, h2) gissade rubriker ur en bild.
    Heuristik: välj de två radtexter som ser "störst" ut (bounding-box-höjd)
    med tillräcklig OCR-confidence.
    """
    try:
        # Läs bild (robust mot icke-ASCII-vägar)
        im = cv2.imdecode(np.fromfile(img_path, dtype=np.uint8), cv2.IMREAD_COLOR)
        if im is None:
            im = cv2.cvtColor(np.array(Image.open(img_path).convert("RGB")), cv2.COLOR_RGB2BGR)

        # Förbehandling: gråskala, ev. uppskalning, brusreducering, threshold
        g = cv2.cvtColor(im, cv2.COLOR_BGR2GRAY)
        h, w = g.shape[:2]
        if max(h, w) < 1400:
            g = cv2.resize(g, (w * 2, h * 2), interpolation=cv2.INTER_CUBIC)
        g = cv2.bilateralFilter(g, 7, 50, 50)
        g = cv2.adaptiveThreshold(
            g, 255, cv2.ADAPTIVE_THRESH_GAUSSIAN_C, cv2.THRESH_BINARY, 31, 10
        )

        # OCR – få radnivådata
        data = pytesseract.image_to_data(g, lang=lang, output_type=Output.DICT)

        # Gruppera ord till rader
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
        for key, info in lines.items():
            text = " ".join(info["texts"]).strip()
            if len(text) < 3:
                continue
            avg_h = float(np.mean(info["heights"])) if info["heights"] else 0.0
            mean_conf = float(np.mean(info["confs"])) if info["confs"] else 0.0
            # större rader + högre conf + lite bonus för längre text
            score = avg_h * (mean_conf / 100.0) * (1 + np.log1p(len(text)))
            scored.append((score, text))

        if not scored:
            return None, None

        scored.sort(reverse=True, key=lambda x: x[0])
        h1 = scored[0][1]
        h2 = scored[1][1] if len(scored) > 1 else None

        # Små städningar & undvik exakt dublett
        if h2 and h1 and h2.strip().lower() == h1.strip().lower():
            h2 = None
        return h1, h2
    except Exception:
        return None, None


# ============================================================
# DIN BEFINTLIGA capture_network – BEHÅLL OFÖRÄNDRAD
# ============================================================
# VIKTIGT:
#  - Ersätt hela den här funktionen med din nuvarande implementering som redan funkar.
#  - Signaturen måste vara: async def capture_network(ar_input: str, run_dir: Path) -> None
#  - Den ska spara alla hämtade annonser (inkl. bildvägar) till t.ex. run_dir / "ads_collected.json"
#    eller det du redan använder.
async def capture_network(ar_input: str, run_dir: Path) -> None:
    """
    REPLACE ME: klistra in din befintliga capture_network-huvudfunktion här.
    Den här tomma versionen finns bara för att filen ska vara körbar om du råkar missa.
    """
    # ---- Bör INTE lämnas så här! ----
    # Om du råkar deploya med den här stubben kommer process-steget inte hitta några annonser.
    # Lägg in din fungerande scraping-kod här.
    pass


# ============================================================
# UPPDATERAD process_candidates_and_save – skriver H1/H2 till Excel
# ============================================================
# --- ersätt hela din process_candidates_and_save med detta ---
from pathlib import Path
import os, json
import pandas as pd

# se till att du har importerat OCR-funktionen högst upp i filen:
# from ocr_utils import ocr_h1_h2_from_image

def _scan_images(root: Path):
    exts = {".jpg", ".jpeg", ".png", ".webp", ".bmp", ".tif", ".tiff"}
    for p in root.rglob("*"):
        if p.is_file() and p.suffix.lower() in exts:
            yield p

def process_candidates_and_save(run_dir: Path, ar_input: str = None) -> bool:
    """
    Bearbeta insamlade annonser i run_dir och skriv ads_extracted.xlsx.
    - Först: försök läsa run_dir/ads_collected.json (lista av dictar med minst image_path)
    - Fallback: om ingen data → skanna alla bilder i run_dir och kör OCR.
    - Fail-safe: skriv ALLTID ett Excel (även om tomt) och returnera True.
    """
    rows = []
    msgs = []

    ads_json = run_dir / "ads_collected.json"
    if ads_json.exists():
        try:
            ads = json.loads(ads_json.read_text(encoding="utf-8"))
            msgs.append(f"ads_collected.json hittad ({len(ads)} annonser).")
        except Exception as e:
            msgs.append(f"ads_collected.json kunde inte läsas: {e}")
            ads = []
        for ad in ads:
            img_path = ad.get("image_path")
            h1 = h2 = None
            if img_path and os.path.exists(img_path):
                try:
                    h1, h2 = ocr_h1_h2_from_image(img_path)
                except Exception:
                    pass
            rows.append({
                "AR-ID": ad.get("ar_id") or ar_input,
                "Källa": ad.get("source"),
                "Annons-URL": ad.get("url"),
                "Bildfil": os.path.basename(img_path) if img_path else "",
                "H1 (OCR)": h1,
                "H2 (OCR)": h2,
            })

    # Fallback om inga rader hittades
    if not rows:
        imgs = list(_scan_images(run_dir))
        msgs.append(f"Fallback: hittade {len(imgs)} bild(er) under {run_dir}.")
        for p in imgs:
            h1 = h2 = None
            try:
                h1, h2 = ocr_h1_h2_from_image(str(p))
            except Exception:
                pass
            rows.append({
                "AR-ID": ar_input,
                "Källa": None,
                "Annons-URL": None,
                "Bildfil": p.name,
                "H1 (OCR)": h1,
                "H2 (OCR)": h2,
            })

    # Skriv ALLTID ett Excel (även om tomt) så att pipelinen inte faller
    out = run_dir / "ads_extracted.xlsx"
    df = pd.DataFrame(rows, columns=["AR-ID","Källa","Annons-URL","Bildfil","H1 (OCR)","H2 (OCR)"])
    with pd.ExcelWriter(out, engine="openpyxl") as xw:
        df.to_excel(xw, index=False, sheet_name="Ads")

    # Skriv en liten statusfil så du ser vad som hände
    debug_info = {
        "rows_written": len(rows),
        "notes": msgs,
    }
    (run_dir / "processing_debug.json").write_text(json.dumps(debug_info, ensure_ascii=False), encoding="utf-8")

    return True


