# ads_capture_and_extract.py
# Komplett capture + extraction med run_dir-stöd

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
            "?origin=ata&region=SE&preset-date=Last+7+days&platform=SEARCH"
        )
    print(f"🔗 Laddar: {url}")

    responses_meta = []

    async with async_playwright() as p:
        # headless=True i container
        browser = await p.chromium.launch(headless=True)
        context = await browser.new_context(
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64)",
            locale="sv-SE",
        )
        page = await context.new_page()

        # Hook: fånga nätverk
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

                # Spara bara intressanta typer
                should_save = False
                ext = None

                if "application/json" in ct:
                    should_save = True
                    ext = ".json"
                elif any(s in url for s in ["ad", "creative", "asset", "search", "list"]):
                    # fallback: vissa HTML/text-responses
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
                            # Vissa Google-responses börjar med )]}',
                            txt = body.decode("utf-8", errors="ignore")
                            (OUTPUT_DIR / (base_name + ".json")).write_text(txt, encoding="utf-8")
                            meta["saved"] = str((base_name + ".json"))
                        except Exception as e:
                            meta["error"] = f"json_save_error: {e}"
                    elif any(ct in ct for ct in ("text/html", "text/plain", "application/javascript")):
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

        # Scrolla lite för att trigga lazy loads (färre varv för snabbare körning)
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

        # Försök parsa JSON-filer och hitta annons-liknande data
        def looks_like_ad_json(obj):
            """
            Heuristik: leta efter nycklar som "creative", "ad", "headline", "description" etc.
            """
            if isinstance(obj, dict):
                keys = " ".join(obj.keys()).lower()
                if any(k in keys for k in ("ad", "creative", "headline", "description", "asset")):
                    return True
                for v in obj.values():
                    if looks_like_ad_json(v):
                        return True
            elif isinstance(obj, list):
                for item in obj:
                    if looks_like_ad_json(item):
                        return True
            return False

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


    # --- Hjälpfunktioner för att hitta bild-URL ---
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
                if res: return res
        elif isinstance(obj, list):
            for it in obj:
                res = scan_for_img(it)
                if res: return res
        return None

    for cand in candidates:
        if ads_count >= MAX_ADS:
            print(f"⏹️ Avbryter efter MAX_ADS={MAX_ADS} annonser.")
            break
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

        if not creative_list or not isinstance(creative_list, list):
            continue

        for entry in creative_list:
            if ads_count >= MAX_ADS:
                break
            if not isinstance(entry, dict):
                continue

            # Försök extrahera rubrik, beskrivning, annonsör, bild-url
            creative_id = entry.get("2") or entry.get("creativeId") or entry.get("id") or ""

            advertiser = (
                entry.get("advertiserName")
                or entry.get("advertiser")
                or entry.get("5")
                or ""
            )

            headline = ""
            description = ""

            # Leta i vanliga fält
            if "headline" in entry:
                headline = entry.get("headline", "")
            elif "3" in entry and isinstance(entry["3"], dict) and "headline" in entry["3"]:
                headline = entry["3"].get("headline", "")
            elif "headlineText" in entry:
                headline = entry.get("headlineText", "")

            if "description" in entry:
                description = entry.get("description", "")
            elif "4" in entry and isinstance(entry["4"], dict) and "body" in entry["4"]:
                description = entry["4"].get("body", "")
            elif "bodyText" in entry:
                description = entry.get("bodyText", "")

            # Fallback: vissa Google-strukturer
            if not headline:
                for k in ("8", "title", "primaryText"):
                    if k in entry and isinstance(entry[k], str):
                        headline = entry[k]
                        break

            if not description:
                for k in ("9", "descriptionText", "secondaryText"):
                    if k in entry and isinstance(entry[k], str):
                        description = entry[k]
                        break

            # Bild-URL
            image_url = None
            assets = entry.get("3") or entry.get("creative") or {}
            if isinstance(assets, dict):
                if "3" in assets and isinstance(assets["3"], dict):
                    possible = assets["3"].get("3") or assets["3"].get("2")
                    if isinstance(possible, str):
                        image_url = possible
                if not image_url and "2" in assets:
                    # ibland lista
                    if isinstance(assets["2"], list) and assets["2"]:
                        first = assets["2"][0]
                        if isinstance(first, dict):
                            image_url = first.get("2") or first.get("3") or first.get("1")
                        elif isinstance(first, str):
                            image_url = first
            if not image_url:
                image_url = scan_for_img(assets)

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
                        if "jpeg" in ct or "jpg" in ct: ext = "jpg"
                        elif "png" in ct: ext = "png"
                        elif "webp" in ct: ext = "webp"
                        filename = f"{(creative_id or 'creative')}_{len(rows)+1}.{ext}"
                        file_path = IMAGES_DIR / filename
                        with open(file_path, "wb") as wf:
                            for chunk in r.iter_content(1024):
                                wf.write(chunk)
                        image_file = str(file_path)
                except Exception:
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
        print("Hittade inga annonser i candidates. Skapar tom Excel.")
        excel_path = get_available_filename(OUTPUT_EXCEL)
        # Skapa ett enkelt ark med ett meddelande
        df = pd.DataFrame([{
            "Info": "Inga annonser hittades för detta AR-ID / tidsintervall."
        }])
        df.to_excel(excel_path, index=False)
        print(f"Tom Excel skapad: {excel_path}")
        return True

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

    # Lägg in bilder i Excel
    wb = load_workbook(excel_path)
    ws = wb.active

    # För varje rad, hämta bildfil och bädda in i kolumn G
    for idx, row in enumerate(rows, start=2):  # rad 2..n (1 = header)
        img_path = row.get("Bildfil")
        if img_path and Path(img_path).exists():
            try:
                img = Image.open(img_path)
                # Skala ner om jättestor
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


