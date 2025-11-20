# ads_capture_and_extract.py
# Capture + extraction för Google Ads Transparency
# Ny version: enklare heuristik – letar rekursivt efter annons-objekt i all JSON.

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

# ----- Globala "pekare" som kan flyttas till valfri run_dir -----
OUTPUT_DIR = Path("network_dump")
CANDIDATES_PATH = OUTPUT_DIR / "ads_candidates.json"
IMAGES_DIR = Path("images")
OUTPUT_EXCEL = "ads_extracted.xlsx"

MAX_ADS = int(os.getenv("MAX_ADS", "300"))
DOWNLOAD_IMAGES = os.getenv("DOWNLOAD_IMAGES", "1") not in ("0", "false", "False")


def set_paths(base_dir: Path | str | None):
    """
    Pekar om alla outputvägar till given run_dir (används per-jobb).
    """
    global OUTPUT_DIR, CANDIDATES_PATH, IMAGES_DIR, OUTPUT_EXCEL
    if base_dir is None:
        base_dir = Path(".")
    else:
        base_dir = Path(base_dir)

    OUTPUT_DIR = base_dir / "network_dump"
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

    CANDIDATES_PATH = base_dir / "ads_candidates.json"

    IMAGES_DIR = base_dir / "images"
    IMAGES_DIR.mkdir(parents=True, exist_ok=True)

    OUTPUT_EXCEL = str(base_dir / "ads_extracted.xlsx")


# ---------- Hjälpfunktioner ----------

def sanitize_filename(name: str) -> str:
    """
    Rensar bort otillåtna tecken i filnamn.
    """
    return re.sub(r"[^a-zA-Z0-9._-]", "_", name)


def get_available_filename(base: str) -> str:
    """
    Om filen finns, lägg på _1, _2, etc.
    """
    p = Path(base)
    if not p.exists():
        return str(p)
    stem = p.stem
    suffix = p.suffix
    for i in range(1, 9999):
        candidate = p.with_name(f"{stem}_{i}{suffix}")
        if not candidate.exists():
            return str(candidate)
    return str(p)


# ---------- Playwright capture ----------

async def capture_network(ar_input: str, run_dir: str | Path | None = None) -> bool:
    """
    Kör Playwright, fångar nätverkstrafik och skriver responses till OUTPUT_DIR.
    Skapar också ads_candidates.json som innehåller ALL JSON som vi senare söker annonser i.
    """
    set_paths(run_dir)

    # Bygg URL om det inte är en URL redan
    if ar_input.startswith("http"):
        url = ar_input
    else:
        url = (
            f"https://adstransparency.google.com/advertiser/{ar_input}"
            "?origin=ata&region=SE&preset-date=Last+7+days&platform=SEARCH"
        )
    print(f"🔗 Laddar: {url}")

    responses_meta = []

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        context = await browser.new_context(
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64)",
            locale="sv-SE",
        )
        page = await context.new_page()

        async def on_response(response):
            try:
                url = response.url
                req = response.request
                method = req.method
                status = response.status
                ct = response.headers.get("content-type", "").lower()

                meta = {
                    "url": url,
                    "method": method,
                    "status": status,
                    "content_type": ct,
                }

                should_save = False
                ext = None

                if "application/json" in ct:
                    should_save = True
                    ext = ".json"
                elif any(s in url for s in ["ad", "creative", "asset", "search", "list"]):
                    if any(t in ct for t in ["text/html", "text/plain", "application/javascript"]):
                        should_save = True
                        ext = ".txt"

                if should_save:
                    safe_name = sanitize_filename(url)
                    if len(safe_name) > 80:
                        safe_name = safe_name[-80:]
                    base_name = f"{int(time.time()*1000)}_{safe_name}"
                    meta["saved_as"] = base_name + (ext or "")

                    if "application/json" in ct:
                        try:
                            body = await response.body()
                            txt = body.decode("utf-8", errors="ignore")
                            (OUTPUT_DIR / (base_name + ".json")).write_text(txt, encoding="utf-8")
                            meta["saved"] = str((base_name + ".json"))
                        except Exception as e:
                            meta["error"] = f"json_save_error: {e}"
                    elif any(ct2 in ct for ct2 in ("text/html", "text/plain", "application/javascript")):
                        try:
                            text = await response.text()
                            filep = OUTPUT_DIR / (base_name + ".txt")
                            filep.write_text(text, encoding="utf-8")
                            meta["saved"] = str(filep.name)
                        except Exception as e:
                            meta["error"] = f"text_save_error: {e}"
                    else:
                        try:
                            body = await response.body()
                            if body and len(body) < 5_000_000:
                                (OUTPUT_DIR / (base_name + ".bin")).write_bytes(body)
                                meta["saved"] = base_name + ".bin"
                        except Exception as e:
                            meta["error"] = f"binary_save_error: {e}"

                responses_meta.append(meta)
            except Exception as e:
                print("on_response error:", e)

        page.on("response", on_response)

        try:
            await page.goto(url, wait_until="domcontentloaded", timeout=45000)
        except Exception as e:
            print("⚠️ page.goto error:", e)

        # Scrolla lite för att trigga lazy loads
        for _ in range(5):
            try:
                await page.evaluate("window.scrollBy(0, window.innerHeight);")
                await asyncio.sleep(0.5)
            except Exception:
                await asyncio.sleep(0.5)

        await asyncio.sleep(1.0)

        # index
        (OUTPUT_DIR / "responses_index.json").write_text(
            json.dumps(responses_meta, indent=2, ensure_ascii=False),
            encoding="utf-8"
        )
        print(f"📦 Sparade index med {len(responses_meta)} responses => {OUTPUT_DIR/'responses_index.json'}")

        # ---- LÄS ALLA JSON-FILER OCH SPARA SOM KANDIDATER ----
        ad_candidates = []
        for f in sorted(OUTPUT_DIR.glob("*.json")):
            try:
                txt = f.read_text(encoding="utf-8")
                cleaned = txt.lstrip(")]}',\n ")
                parsed = json.loads(cleaned)
                ad_candidates.append({
                    "source_file": f.name,
                    "parsed": parsed
                })
            except Exception as e:
                print(f"⚠️ kunde inte parsa {f.name}: {e}")
                continue

        CANDIDATES_PATH.write_text(
            json.dumps(ad_candidates, indent=2, ensure_ascii=False),
            encoding="utf-8"
        )
        print(f"🔎 Sparade {len(ad_candidates)} JSON-kandidater i {CANDIDATES_PATH}")

        await browser.close()
    return True


# ---------- Post-processing / extraction ----------

def process_candidates_and_save(run_dir: str | Path | None = None) -> bool:
    """
    Läser ads_candidates.json, letar rekursivt efter "annons-liknande" objekt,
    laddar ned bilder och skapar en Excel med metadata + inbäddade bilder.
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
    ads_count = 0

    # ----- Funktioner för att hitta annonser i JSON -----

    AD_KEYS = {
        "advertiserName", "advertiser", "headline", "headlineText",
        "description", "bodyText", "creativeId", "adId",
        "callToAction", "ctaText", "imageUrl", "finalUrl",
        "landingPageUrl", "displayUrl"
    }

    def scan_for_img(obj):
        IMG_SRC_RE = re.compile(r'src="([^"]+)"')
        if isinstance(obj, str):
            if "tpc.googlesyndication.com" in obj or obj.endswith((".png", ".jpg", ".jpeg", ".webp")):
                return obj
            m = IMG_SRC_RE.search(obj)
            if m:
                return m.group(1)
        elif isinstance(obj, dict):
            for _v in obj.values():
                res = scan_for_img(_v)
                if res:
                    return res
        elif isinstance(obj, list):
            for it in obj:
                res = scan_for_img(it)
                if res:
                    return res
        return None

    def find_ads_in_obj(obj, source_file, out_list):
        """
        Går rekursivt igenom `obj` och om vi hittar dict med flera "annons-nycklar",
        lägger vi till den som ad-kandidat.
        """
        if isinstance(obj, dict):
            keys = set(obj.keys())
            if len(keys & AD_KEYS) >= 2:
                out_list.append({"source_file": source_file, "node": obj})
            for v in obj.values():
                find_ads_in_obj(v, source_file, out_list)
        elif isinstance(obj, list):
            for item in obj:
                find_ads_in_obj(item, source_file, out_list)

    all_ads_nodes = []
    for cand in candidates:
        src_file = cand.get("source_file", "")
        parsed = cand.get("parsed")
        find_ads_in_obj(parsed, src_file, all_ads_nodes)

    print(f"🕵️ Hittade totalt {len(all_ads_nodes)} potentiella annons-objekt i JSON.")

    for ad in all_ads_nodes:
        if ads_count >= MAX_ADS:
            print(f"⏹️ Avbryter efter MAX_ADS={MAX_ADS} annonser.")
            break

        src_file = ad["source_file"]
        entry = ad["node"]

        # --- Plocka ut fält ---
        creative_id = (
            entry.get("creativeId") or
            entry.get("adId") or
            entry.get("id") or
            entry.get("2") or
            ""
        )

        advertiser = (
            entry.get("advertiserName") or
            entry.get("advertiser") or
            entry.get("brandName") or
            ""
        )

        headline = (
            entry.get("headline") or
            entry.get("headlineText") or
            entry.get("title") or
            entry.get("primaryText") or
            ""
        )

        description = (
            entry.get("description") or
            entry.get("bodyText") or
            entry.get("secondaryText") or
            entry.get("snippet") or
            ""
        )

        image_url = (
            entry.get("imageUrl") or
            entry.get("image_url") or
            entry.get("thumbnailUrl") or
            entry.get("thumbnail") or
            ""
        )

        if not image_url:
            image_url = scan_for_img(entry)

        image_file = ""
        if image_url and DOWNLOAD_IMAGES:
            try:
                if image_url.startswith("//"):
                    image_url = "https:" + image_url
                r = session.get(image_url, timeout=10, stream=True)
                if r.status_code == 200:
                    ext = "png"
                    ct = r.headers.get("content-type", "").lower()
                    if "jpeg" in ct or "jpg" in ct:
                        ext = "jpg"
                    elif "png" in ct:
                        ext = "png"
                    elif "webp" in ct:
                        ext = "webp"
                    filename = f"{(creative_id or 'creative')}_{len(rows)+1}.{ext}"
                    file_path = IMAGES_DIR / filename
                    with open(file_path, "wb") as wf:
                        for chunk in r.iter_content(1024):
                            wf.write(chunk)
                    image_file = str(file_path)
            except Exception as e:
                print(f"⚠️ kunde inte ladda ner bild ({image_url}): {e}")
                image_file = ""

        rows.append({
            "SourceFile": src_file,
            "CreativeID": creative_id,
            "Annonsör": advertiser,
            "Rubrik": headline,
            "Beskrivning": description,
            "Bild-URL": image_url or "",
            "Bildfil": image_file
        })
        ads_count += 1

    if not rows:
        print("Hittade inga annonser i JSON. Skapar tom Excel.")
        excel_path = get_available_filename(OUTPUT_EXCEL)
        df = pd.DataFrame([{
            "Info": "Inga annonser hittades för detta AR-ID / tidsintervall."
        }])
        df.to_excel(excel_path, index=False)
        print(f"Tom Excel skapad: {excel_path}")
        return True

    # ----- Skapa Excel -----
    excel_path = get_available_filename(OUTPUT_EXCEL)
    df = pd.DataFrame(rows, columns=[
        "SourceFile",
        "CreativeID",
        "Annonsör",
        "Rubrik",
        "Beskrivning",
        "Bild-URL",
        "Bildfil"
    ])
    df.to_excel(excel_path, index=False)
    print(f"📊 Grund-Excel (utan inbäddade bilder) sparad som: {excel_path}")

    # ----- Bädda in bilder -----
    wb = load_workbook(excel_path)
    ws = wb.active

    for idx, row in enumerate(rows, start=2):  # rad 2..n (1 = header)
        img_path = row.get("Bildfil")
        if img_path and Path(img_path).exists():
            try:
                img = Image.open(img_path)
                max_w, max_h = 200, 200
                w, h = img.size
                scale = min(max_w / w, max_h / h, 1.0)
                if scale < 1.0:
                    img = img.resize((int(w * scale), int(h * scale)))
                bio = BytesIO()
                img.save(bio, format="PNG")
                bio.seek(0)

                xlimg = XLImage(bio)
                xlimg.width = 120
                xlimg.height = 90
                ws.add_image(xlimg, f"G{idx}")
                ws.row_dimensions[idx].height = 80
            except Exception as e:
                print(f"Fel vid inbäddning av bild för rad {idx}: {e}")

    for i, col in enumerate(df.columns, start=1):
        col_letter = get_column_letter(i)
        maxlen = max((len(str(x)) for x in df[col]), default=len(col))
        ws.column_dimensions[col_letter].width = min(maxlen + 8, 80)

    wb.save(excel_path)
    print(f"✅ Excel med inbäddade bilder sparad som: {excel_path}")
    return True


if __name__ == "__main__":
    # Enkel CLI-test
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("ar_input", help="AR-ID eller full URL till Google Ads Transparency.")
    parser.add_argument("--run_dir", default="test_run", help="Output-katalog.")
    args = parser.parse_args()

    async def main():
        ok = await capture_network(args.ar_input, run_dir=args.run_dir)
        if ok:
            print("Capture klar, kör process_candidates_and_save...")
            process_candidates_and_save(args.run_dir)

    asyncio.run(main())
