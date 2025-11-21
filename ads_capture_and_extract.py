# ads_capture_and_extract.py
# Capture + extraction för Google Ads Transparency med run_dir-stöd

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

# ----- Globala paths som pekas om per jobb -----
OUTPUT_DIR = Path("network_dump")
CANDIDATES_PATH = OUTPUT_DIR / "ads_candidates.json"
IMAGES_DIR = Path("images")
OUTPUT_EXCEL = "ads_extracted.xlsx"

MAX_ADS = int(os.getenv("MAX_ADS", "300"))
DOWNLOAD_IMAGES = os.getenv("DOWNLOAD_IMAGES", "1") not in ("0", "false", "False")


def set_paths(base_dir: Path | str | None):
    """Pekar om alla output-vägar till given run_dir (används per-jobb)."""
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
    """Tar bort otillåtna tecken i filnamn."""
    return re.sub(r"[^a-zA-Z0-9._-]", "_", name)


def get_available_filename(base: str) -> str:
    """Om filen finns, lägg på _1, _2, etc."""
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
    Skapar ads_candidates.json med JSON vi senare skannar efter annonser.

    Viktigt: vi letar efter JSON / JSON+protobuf baserat på Content-Type,
    inte bara domännamn, så att vi får med t.ex. ogads-pa.clients6.google.com.
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

    responses_meta: list[dict] = []

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        context = await browser.new_context(
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64)",
            locale="sv-SE",
        )
        page = await context.new_page()

        async def on_response(response):
            try:
                r_url = response.url
                method = response.request.method if response.request else "GET"
                status = response.status
                headers = response.headers or {}
                ct = headers.get("content-type", "").lower()

                meta = {
                    "url": r_url,
                    "method": method,
                    "status": status,
                    "content_type": ct,
                }

                # Vi är intresserade av alla svar som verkar vara JSON-aktiga
                looks_jsonish_ct = "json" in ct or "protobuf" in ct

                if looks_jsonish_ct:
                    try:
                        body = await response.body()
                    except Exception as e:
                        meta["body_error"] = f"body_error: {e}"
                        responses_meta.append(meta)
                        return

                    if not body or len(body) > 5_000_000:
                        responses_meta.append(meta)
                        return

                    try:
                        txt = body.decode("utf-8", errors="ignore")
                    except Exception:
                        responses_meta.append(meta)
                        return

                    trimmed = txt.lstrip()
                    if trimmed.startswith(")]}'"):
                        trimmed_json = trimmed[4:].lstrip()
                    else:
                        trimmed_json = trimmed

                    is_jsonish = trimmed_json.startswith("{") or trimmed_json.startswith("[")
                    if not is_jsonish:
                        responses_meta.append(meta)
                        return

                    safe_name = sanitize_filename(r_url)
                    if len(safe_name) > 80:
                        safe_name = safe_name[-80:]
                    base_name = f"{int(time.time()*1000)}_{safe_name}"
                    (OUTPUT_DIR / (base_name + ".json")).write_text(
                        trimmed_json, encoding="utf-8"
                    )
                    meta["saved"] = base_name + ".json"

                responses_meta.append(meta)

            except Exception as e:
                print("on_response error:", e)

        page.on("response", on_response)

        try:
            await page.goto(url, wait_until="domcontentloaded", timeout=45000)
        except Exception as e:
            print("⚠️ page.goto error:", e)

        # scrolla några gånger för att trigga lazy loads
        for _ in range(5):
            try:
                await page.evaluate("window.scrollBy(0, window.innerHeight);")
            except Exception:
                pass
            await asyncio.sleep(0.5)

        await asyncio.sleep(1.0)

        # spara index
        (OUTPUT_DIR / "responses_index.json").write_text(
            json.dumps(responses_meta, indent=2, ensure_ascii=False),
            encoding="utf-8",
        )
        print(
            f"📦 Sparade index med {len(responses_meta)} responses => "
            f"{OUTPUT_DIR / 'responses_index.json'}"
        )

        # läs alla json-filer (förutom responses_index.json) och lägg dem som kandidater
        ad_candidates = []
        json_files = sorted(OUTPUT_DIR.glob("*.json"))
        print(f"🗂️ Hittade {len(json_files)} .json-filer i {OUTPUT_DIR}")
        for f in json_files:
            if f.name == "responses_index.json":
                continue
            try:
                txt = f.read_text(encoding="utf-8")
                parsed = json.loads(txt)
                ad_candidates.append({"source_file": f.name, "parsed": parsed})
            except Exception as e:
                print(f"⚠️ kunde inte parsa {f.name}: {e}")
                continue

        CANDIDATES_PATH.write_text(
            json.dumps(ad_candidates, indent=2, ensure_ascii=False),
            encoding="utf-8",
        )
        print(f"🔎 Sparade {len(ad_candidates)} JSON-kandidater i {CANDIDATES_PATH}")

        await browser.close()

    return True


# ---------- Post-processing / extraction ----------

def process_candidates_and_save(run_dir: str | Path | None = None) -> bool:
    """
    Läser ads_candidates.json, letar rekursivt efter annons-objekt,
    laddar ned bilder och skapar en Excel med metadata + inbäddade bilder.

    Om inga annonser hittas:
      - platta ut HELA JSON till rader (SourceFile, Path, Value)
      - känn igen bild-URL:er
      - ladda ned och bädda in bilder i samma ark
    """
    set_paths(run_dir)

    if not CANDIDATES_PATH.exists():
        print(
            f"Fel: kunde inte hitta {CANDIDATES_PATH}. "
            f"Kör först capture_network(ar_input)."
        )
        return False

    with open(CANDIDATES_PATH, "r", encoding="utf-8") as f:
        candidates = json.load(f)

    rows: list[dict] = []
    session = requests.Session()
    session.headers.update({"User-Agent": "Mozilla/5.0"})
    ads_count = 0

    # Nycklar som brukar finnas i text-baserade annonsobjekt
    AD_TEXT_KEYS = {
        "advertiserName",
        "advertiser",
        "headline",
        "headlineText",
        "description",
        "bodyText",
        "creativeId",
        "adId",
        "callToAction",
        "ctaText",
        "finalUrl",
        "landingPageUrl",
        "displayUrl",
        "imageUrl",
        "image_url",
        "title",
        "primaryText",
        "secondaryText",
    }

    # Google-nummernycklar som ofta används i protobuff-liknande JSON
    NUMERIC_KEYS = {"2", "3", "7", "8", "12"}

    IMG_SRC_RE = re.compile(r'<img[^>]+src=["\']([^"\']+)["\']', re.IGNORECASE)
    HTTP_URL_RE = re.compile(r"https?://[^\s\"'<>]+", re.IGNORECASE)

    def scan_for_img(obj):
        """Försök hitta bild-URL i vilken str/dict/list som helst."""
        if isinstance(obj, str):
            if obj.endswith((".png", ".jpg", ".jpeg", ".webp")):
                return obj
            m = IMG_SRC_RE.search(obj)
            if m:
                return m.group(1)
            m2 = HTTP_URL_RE.search(obj)
            if m2 and m2.group(0).endswith((".png", ".jpg", ".jpeg", ".webp")):
                return m2.group(0)
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

    def find_ads_in_obj(obj, source_file: str, out_list: list[dict]):
        """
        Går rekursivt genom JSON och samlar dicts som ser ut som annonser:
        - antingen har flera "vanliga" textnycklar,
        - eller har flera av nummernycklarna 2,3,7,8,12.
        """
        if isinstance(obj, dict):
            keys = {str(k) for k in obj.keys()}
            has_text_keys = len(keys & AD_TEXT_KEYS) >= 2
            has_numeric_pattern = len(keys & NUMERIC_KEYS) >= 3

            if has_text_keys or has_numeric_pattern:
                out_list.append({"source_file": source_file, "node": obj})

            for v in obj.values():
                find_ads_in_obj(v, source_file, out_list)

        elif isinstance(obj, list):
            for item in obj:
                find_ads_in_obj(item, source_file, out_list)

    # ---- Försök 1: hitta riktiga annons-noder ----
    all_ads_nodes: list[dict] = []
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

        creative_id = (
            entry.get("creativeId")
            or entry.get("adId")
            or entry.get("id")
            or entry.get("2")
            or ""
        )

        advertiser = (
            entry.get("advertiserName")
            or entry.get("advertiser")
            or entry.get("brandName")
            or entry.get("12")
            or ""
        )

        headline = (
            entry.get("headline")
            or entry.get("headlineText")
            or entry.get("title")
            or entry.get("7")
            or entry.get("primaryText")
            or ""
        )

        description = (
            entry.get("description")
            or entry.get("bodyText")
            or entry.get("secondaryText")
            or entry.get("8")
            or entry.get("snippet")
            or ""
        )

        image_url = (
            entry.get("imageUrl")
            or entry.get("image_url")
            or entry.get("thumbnailUrl")
            or entry.get("thumbnail")
            or ""
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

        rows.append(
            {
                "SourceFile": src_file,
                "CreativeID": creative_id,
                "Annonsör": advertiser,
                "Rubrik": headline,
                "Beskrivning": description,
                "Bild-URL": image_url or "",
                "Bildfil": image_file,
            }
        )
        ads_count += 1

    # ---- FALLBACK: platta ut all JSON + bilder ----
    if not rows:
        print("Hittade inga annonser enligt heuristiken – skapar JSON-dump med bilder istället.")

        flat_rows: list[dict] = []
        MAX_FLAT_ROWS = 5000  # skydd så vi inte gör gigantisk fil

        def flatten(obj, source_file: str, path: str):
            nonlocal flat_rows
            if len(flat_rows) >= MAX_FLAT_ROWS:
                return

            if isinstance(obj, dict):
                for k, v in obj.items():
                    new_path = f"{path}.{k}" if path else str(k)
                    flatten(v, source_file, new_path)
            elif isinstance(obj, list):
                for i, v in enumerate(obj):
                    new_path = f"{path}[{i}]" if path else f"[{i}]"
                    flatten(v, source_file, new_path)
            else:
                if isinstance(obj, str):
                    val = obj.strip()
                    if not val:
                        return

                    row = {
                        "SourceFile": source_file,
                        "Path": path,
                        "Value": val,
                        "ImageURL": "",
                        "ImageFile": "",
                    }

                    # Bild-URL?
                    if val.lower().startswith("http") and any(
                        val.lower().endswith(ext) for ext in (".png", ".jpg", ".jpeg", ".webp")
                    ):
                        row["ImageURL"] = val
                        if DOWNLOAD_IMAGES:
                            try:
                                img_url = val
                                if img_url.startswith("//"):
                                    img_url = "https:" + img_url
                                r = session.get(img_url, timeout=10, stream=True)
                                if r.status_code == 200:
                                    ext = "png"
                                    ct = r.headers.get("content-type", "").lower()
                                    if "jpeg" in ct or "jpg" in ct:
                                        ext = "jpg"
                                    elif "png" in ct:
                                        ext = "png"
                                    elif "webp" in ct:
                                        ext = "webp"
                                    filename = f"flat_{len(flat_rows)+1}.{ext}"
                                    file_path = IMAGES_DIR / filename
                                    with open(file_path, "wb") as wf:
                                        for chunk in r.iter_content(1024):
                                            wf.write(chunk)
                                    row["ImageFile"] = str(file_path)
                            except Exception as e:
                                print(f"⚠️ kunde inte ladda ner bild (flat) {val}: {e}")

                    flat_rows.append(row)

        for cand in candidates:
            src_file = cand.get("source_file", "")
            parsed = cand.get("parsed")
            flatten(parsed, src_file, "")

        print(f"JSON-dump: la till {len(flat_rows)} rader (text + ev. bilder).")

        excel_path = get_available_filename(OUTPUT_EXCEL)
        if not flat_rows:
            df = pd.DataFrame(
                [{"Info": "Kunde inte hitta några textsträngar i JSON heller."}]
            )
            df.to_excel(excel_path, index=False)
            print(f"📄 JSON-dump Excel (ingen data) skapad: {excel_path}")
            return True

        df = pd.DataFrame(flat_rows, columns=["SourceFile", "Path", "Value", "ImageURL", "ImageFile"])
        df.to_excel(excel_path, index=False)
        print(f"📄 JSON-dump Excel skapad: {excel_path}")

        # Bädda in bilder i kolumn E ("ImageFile")
        wb = load_workbook(excel_path)
        ws = wb.active

        for idx, row in enumerate(flat_rows, start=2):
            img_path = row.get("ImageFile")
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
                    ws.add_image(xlimg, f"E{idx}")
                    ws.row_dimensions[idx].height = 80
                except Exception as e:
                    print(f"Fel vid inbäddning av flat-bild för rad {idx}: {e}")

        for i, col in enumerate(df.columns, start=1):
            col_letter = get_column_letter(i)
            maxlen = max((len(str(x)) for x in df[col]), default=len(col))
            ws.column_dimensions[col_letter].width = min(maxlen + 8, 80)

        wb.save(excel_path)
        print(f"✅ JSON-dump Excel med inbäddade bilder sparad som: {excel_path}")
        return True

    # ---- Normalt flöde: vi hittade annons-rader i `rows` ----
    excel_path = get_available_filename(OUTPUT_EXCEL)
    df = pd.DataFrame(
        rows,
        columns=[
            "SourceFile",
            "CreativeID",
            "Annonsör",
            "Rubrik",
            "Beskrivning",
            "Bild-URL",
            "Bildfil",
        ],
    )
    df.to_excel(excel_path, index=False)
    print(f"📊 Grund-Excel (utan inbäddade bilder) sparad som: {excel_path}")

    # Bädda in bilder i kolumn "Bildfil"
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
                ws.add_image(xlimg, f"G{idx}")  # kolumn G = Bildfil
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
    parser.add_argument(
        "ar_input", help="AR-ID eller full URL till Google Ads Transparency."
    )
    parser.add_argument("--run_dir", default="test_run", help="Output-katalog.")
    args = parser.parse_args()

    async def main():
        ok = await capture_network(args.ar_input, run_dir=args.run_dir)
        if ok:
            print("Capture klar, kör process_candidates_and_save...")
            process_candidates_and_save(args.run_dir)

    asyncio.run(main())
