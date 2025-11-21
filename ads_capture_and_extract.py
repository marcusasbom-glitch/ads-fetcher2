# ads_capture_and_extract.py
# Ny version: skrapar annonser direkt från sidan (DOM) i stället för nätverks-JSON.

import asyncio
import os
import json
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
    """Pekar om alla output-vägar till given run_dir (används per jobb)."""
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


def sanitize_filename(name: str) -> str:
    """Tar bort otillåtna tecken i filnamn."""
    import re
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


# ---------- Playwright: hämta DOM-annonser ----------

async def capture_network(ar_input: str, run_dir: str | Path | None = None) -> bool:
    """
    Öppnar Google Ads Transparency-sidan, scrollar lite och plockar ut annonskort direkt
    från DOM:en (text + bild-URL:er). Sparar som ads_candidates.json.
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

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        context = await browser.new_context(
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64)",
            locale="sv-SE",
        )
        page = await context.new_page()

        try:
            await page.goto(url, wait_until="domcontentloaded", timeout=45000)
        except Exception as e:
            print("⚠️ page.goto error:", e)

        # Scrolla några gånger för att trigga lazy loads
        for _ in range(8):
            try:
                await page.evaluate("window.scrollBy(0, window.innerHeight);")
            except Exception:
                pass
            await asyncio.sleep(0.7)

        await asyncio.sleep(1.0)

        # Plocka ut annonskort direkt från DOM
        dom_ads = await page.evaluate(
            """
            () => {
              const cards = [];
              const candidates = Array.from(document.querySelectorAll('article, div, section'));

              const isLikelyAdCard = (el) => {
                const text = (el.innerText || '').toLowerCase();
                if (!text) return false;
                // Sponsrad på svenska/engelska
                if (!text.includes('sponsrad') && !text.includes('sponsored')) return false;
                // ha åtminstone en bild
                if (!el.querySelector('img')) return false;
                return true;
              };

              for (const el of candidates) {
                if (!isLikelyAdCard(el)) continue;

                const text = (el.innerText || '').trim();
                if (!text) continue;

                const imgs = Array.from(el.querySelectorAll('img'))
                  .map(i => i.src)
                  .filter(u => !!u);

                if (!imgs.length) continue;

                // rubrik = första h-taggen eller länk med större text
                let headlineNode =
                  el.querySelector('h1, h2, h3, h4') ||
                  el.querySelector('a[role="heading"]') ||
                  el.querySelector('a');

                const headline = headlineNode ? (headlineNode.innerText || '').trim() : '';

                // annonsör = försök hitta rad med företagsnamn (över rubriken brukar vara bra)
                let advertiser = '';
                const lines = text.split('\\n').map(s => s.trim()).filter(Boolean);
                if (lines.length > 0) {
                  advertiser = lines[0];
                }

                cards.push({
                  advertiser,
                  headline,
                  text,
                  image_urls: imgs
                });
              }

              return cards;
            }
            """
        )

        print(f"🧩 Hittade {len(dom_ads)} DOM-annonskort")

        # Spara som "kandidater" i samma format som tidigare pipeline
        OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
        dom_file = OUTPUT_DIR / "dom_ads.json"
        dom_file.write_text(json.dumps(dom_ads, ensure_ascii=False, indent=2), encoding="utf-8")

        CANDIDATES_PATH.write_text(
            json.dumps([{"source_file": dom_file.name, "parsed": dom_ads}], ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        print(f"💾 Sparade DOM-annonser till {CANDIDATES_PATH}")

        await browser.close()

    return True


# ---------- Post-processing / Excel ----------

def process_candidates_and_save(run_dir: str | Path | None = None) -> bool:
    """
    Läser ads_candidates.json (DOM-annonser), laddar ned bilder och skapar
    en Excel med metadata + inbäddade bilder.
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

    # Kandidater är en lista med ett objekt: {source_file: "dom_ads.json", parsed: [...]}
    if not candidates:
        print("Inga kandidater i ads_candidates.json.")
        return False

    rows = []
    session = requests.Session()
    session.headers.update({"User-Agent": "Mozilla/5.0"})

    ads_count = 0

    for cand in candidates:
        src_file = cand.get("source_file", "")
        parsed = cand.get("parsed", [])
        if not isinstance(parsed, list):
            continue

        for idx, ad in enumerate(parsed, start=1):
            if ads_count >= MAX_ADS:
                print(f"⏹️ Avbryter efter MAX_ADS={MAX_ADS} annonser.")
                break

            advertiser = (ad.get("advertiser") or "").strip()
            headline   = (ad.get("headline") or "").strip()
            text       = (ad.get("text") or "").strip()
            image_urls = ad.get("image_urls") or []

            # välj första bild-URL om det finns flera
            image_url = image_urls[0] if image_urls else ""

            image_file = ""
            if image_url and DOWNLOAD_IMAGES:
                try:
                    url = image_url
                    if url.startswith("//"):
                        url = "https:" + url
                    r = session.get(url, timeout=10, stream=True)
                    if r.status_code == 200:
                        ext = "png"
                        ct = r.headers.get("content-type", "").lower()
                        if "jpeg" in ct or "jpg" in ct:
                            ext = "jpg"
                        elif "png" in ct:
                            ext = "png"
                        elif "webp" in ct:
                            ext = "webp"
                        filename = sanitize_filename(f"ad_{ads_count+1}.{ext}")
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
                    "Index": ads_count + 1,
                    "Annonsör": advertiser,
                    "Rubrik": headline,
                    "Text": text,
                    "Bild-URL": image_url,
                    "Bildfil": image_file,
                }
            )
            ads_count += 1

    if not rows:
        print("Det gick att läsa DOM, men inga annonskort hittades. Skapar enkel Excel.")
        excel_path = get_available_filename(OUTPUT_EXCEL)
        df = pd.DataFrame(
            [{"Info": "Inga annonser hittades (DOM-scrape gav 0 kort)."}]
        )
        df.to_excel(excel_path, index=False)
        print(f"📄 Excel skapad utan annonser: {excel_path}")
        return True

    # ----- Skapa Excel med data -----
    excel_path = get_available_filename(OUTPUT_EXCEL)
    df = pd.DataFrame(
        rows,
        columns=[
            "SourceFile",
            "Index",
            "Annonsör",
            "Rubrik",
            "Text",
            "Bild-URL",
            "Bildfil",
        ],
    )
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
                # kolumn G = "Bildfil"
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
