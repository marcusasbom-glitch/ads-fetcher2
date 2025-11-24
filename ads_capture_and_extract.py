# ads_capture_and_extract.py – IFRAMES VERSION (felsäker, inga f-strings i JS)

import asyncio
import json
import os
from io import BytesIO
from pathlib import Path

import pandas as pd
import requests
from PIL import Image
from openpyxl import load_workbook
from openpyxl.drawing.image import Image as XLImage
from openpyxl.utils import get_column_letter
from playwright.async_api import async_playwright


# ============================================================
# Dynamiska paths (Render skapar en unik run-mapp per jobb)
# ============================================================

OUTPUT_DIR = Path("network_dump")
CANDIDATES_PATH = OUTPUT_DIR / "ads_candidates.json"
IMAGES_DIR = Path("images")
OUTPUT_EXCEL = "ads_extracted.xlsx"

MAX_ADS = int(os.getenv("MAX_ADS", "300"))
DOWNLOAD_IMAGES = os.getenv("DOWNLOAD_IMAGES", "1") not in ("0", "false", "False")


def set_paths(base_dir):
    """Repoint global paths into the run dir."""
    global OUTPUT_DIR, CANDIDATES_PATH, IMAGES_DIR, OUTPUT_EXCEL

    base = Path(base_dir)
    OUTPUT_DIR = base / "network_dump"
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

    CANDIDATES_PATH = base / "ads_candidates.json"

    IMAGES_DIR = base / "images"
    IMAGES_DIR.mkdir(parents=True, exist_ok=True)

    OUTPUT_EXCEL = str(base / "ads_extracted.xlsx")


# ============================================================
# Hygienfunktioner
# ============================================================

def sanitize_filename(name):
    import re
    return re.sub(r"[^a-zA-Z0-9._-]", "_", name)


def get_available_filename(base):
    p = Path(base)
    if not p.exists():
        return str(p)
    stem = p.stem
    ext = p.suffix
    for i in range(1, 9999):
        alt = p.with_name(f"{stem}_{i}{ext}")
        if not alt.exists():
            return str(alt)
    return str(p)


# ============================================================
# PLAYWRIGHT – DOM scraping i ALLA FRAMES
# ============================================================

IFRAME_JS_SCRAPER = """
() => {
    const cards = [];
    const root = document.body;
    if (!root) return cards;

    const elements = root.querySelectorAll("article, div, section");

    const isAd = (el) => {
        const txt = (el.innerText || "").toLowerCase();
        if (!txt) return false;

        const hasImg = el.querySelector("img");
        if (!hasImg) return false;

        if (txt.includes("sponsrad") || txt.includes("sponsored"))
            return true;

        return txt.split("\\n").filter(x => x.trim()).length >= 3;
    };

    for (const el of elements) {
        if (!isAd(el)) continue;

        const text = (el.innerText || "").trim();
        if (!text) continue;

        const imgs = Array.from(el.querySelectorAll("img"))
            .map(i => i.src)
            .filter(Boolean);

        if (!imgs.length) continue;

        let headNode =
            el.querySelector("h1, h2, h3, h4") ||
            el.querySelector('a[role="heading"]') ||
            el.querySelector("a");

        const headline = headNode ? (headNode.innerText || "").trim() : "";

        const lines = text.split("\\n").map(x => x.trim()).filter(Boolean);
        const advertiser = lines.length ? lines[0] : "";

        cards.push({
            advertiser: advertiser,
            headline: headline,
            text: text,
            image_urls: imgs
        });
    }

    return cards;
}
"""


async def capture_network(ar_input, run_dir):
    """Scrape ALLA frames (iframes) på Google Ads Transparency-sidan."""

    set_paths(run_dir)

    if ar_input.startswith("http"):
        url = ar_input
    else:
        url = (
            "https://adstransparency.google.com/advertiser/"
            + ar_input +
            "?origin=ata&region=SE&preset-date=Last+7+days&platform=SEARCH"
        )

    print("🔗 Laddar:", url)

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        ctx = await browser.new_context(
            user_agent="Mozilla/5.0",
            locale="sv-SE"
        )
        page = await ctx.new_page()

        await page.goto(url, wait_until="domcontentloaded", timeout=45000)

        # Scroll för lazy load
        for _ in range(10):
            await page.evaluate("window.scrollBy(0, window.innerHeight)")
            await asyncio.sleep(0.6)

        # Hämta annonser i main frame
        dom_ads = await page.evaluate(IFRAME_JS_SCRAPER)
        print(f"🧩 Huvud-frame: {len(dom_ads)} annonser")

        # Hämta från iframes
        for frame in page.frames:
            if frame == page.main_frame:
                continue
            try:
                frame_ads = await frame.evaluate(IFRAME_JS_SCRAPER)
                print(f"🪟 Frame {frame.url} → {len(frame_ads)} annonser")
                dom_ads.extend(frame_ads)
            except Exception:
                print("⚠️ Frame ej läsbar (cross-origin):", frame.url)

        print("✅ TOTALT hittade annonser:", len(dom_ads))

        # Spara
        OUTPUT_DIR.mkdir(exist_ok=True, parents=True)
        CANDIDATES_PATH.write_text(
            json.dumps([{"source_file": "frames", "parsed": dom_ads}], indent=2, ensure_ascii=False),
            encoding="utf-8"
        )

        await browser.close()
        return True


# ============================================================
# EXCEL + bildnedladdning
# ============================================================

def process_candidates_and_save(run_dir):
    set_paths(run_dir)

    if not CANDIDATES_PATH.exists():
        print("❌ Saknar ads_candidates.json")
        return False

    data = json.loads(CANDIDATES_PATH.read_text(encoding="utf-8"))

    rows = []
    ads = data[0]["parsed"]

    print("⏳ Bearbetar annonser:", len(ads))

    session = requests.Session()
    session.headers.update({"User-Agent": "Mozilla/5.0"})

    for idx, ad in enumerate(ads, start=1):
        if idx > MAX_ADS:
            break

        img_url = ad["image_urls"][0] if ad["image_urls"] else ""
        img_file = ""

        # Ladda ned bild
        if img_url and DOWNLOAD_IMAGES:
            try:
                if img_url.startswith("//"):
                    img_url = "https:" + img_url

                r = session.get(img_url, timeout=10)
                if r.status_code == 200:
                    ct = r.headers.get("content-type", "")
                    ext = "png"
                    if "jpg" in ct:
                        ext = "jpg"
                    if "webp" in ct:
                        ext = "webp"

                    fname = sanitize_filename(f"ad_{idx}.{ext}")
                    path = IMAGES_DIR / fname
                    path.write_bytes(r.content)
                    img_file = str(path)
            except Exception:
                pass

        rows.append({
            "Index": idx,
            "Annonsör": ad.get("advertiser", ""),
            "Rubrik": ad.get("headline", ""),
            "Text": ad.get("text", ""),
            "Bild-URL": img_url,
            "Bildfil": img_file,
        })

    if not rows:
        df = pd.DataFrame([{"Info": "Inga annonser hittades"}])
        excel = get_available_filename(OUTPUT_EXCEL)
        df.to_excel(excel, index=False)
        return True

    df = pd.DataFrame(rows)
    excel = get_available_filename(OUTPUT_EXCEL)
    df.to_excel(excel, index=False)

    # Bädda in bilder
    wb = load_workbook(excel)
    ws = wb.active

    for r, row in enumerate(rows, start=2):
        f = row["Bildfil"]
        if not f or not Path(f).exists():
            continue

        try:
            img = Image.open(f)
            w, h = img.size
            scale = min(150 / w, 150 / h, 1)
            img = img.resize((int(w * scale), int(h * scale)))

            bio = BytesIO()
            img.save(bio, format="PNG")
            bio.seek(0)

            xlimg = XLImage(bio)
            ws.add_image(xlimg, f"F{r}")
            ws.row_dimensions[r].height = 120
        except Exception:
            pass

    wb.save(excel)
    print("📄 Excel skapad:", excel)
    return True
