# ads_capture_and_extract.py – snabb version utan OCR & utan inbäddade bilder

import asyncio
import json
import os
from pathlib import Path

import pandas as pd
import requests
from playwright.async_api import async_playwright

# ============================================================
# Dynamiska paths (Render skapar en unik run-mapp per jobb)
# ============================================================

OUTPUT_DIR = Path("network_dump")
CANDIDATES_PATH = OUTPUT_DIR / "ads_candidates.json"
OUTPUT_EXCEL = "ads_extracted.xlsx"

# Totalt max antal annonser i Excel
MAX_ADS = int(os.getenv("MAX_ADS", "700"))


def set_paths(base_dir):
    """Repoint global paths into the run dir."""
    global OUTPUT_DIR, CANDIDATES_PATH, OUTPUT_EXCEL

    base = Path(base_dir)
    OUTPUT_DIR = base / "network_dump"
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

    CANDIDATES_PATH = base / "ads_candidates.json"
    OUTPUT_EXCEL = str(base / "ads_extracted.xlsx")


# ============================================================
# Hjälpfunktioner
# ============================================================

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

    const MIN_AREA = 10000; // 100x100
    const elements = root.querySelectorAll("article, div, section");

    for (const el of elements) {
        const txt = (el.innerText || "").trim();
        const imgNodes = Array.from(el.querySelectorAll("img"));
        if (!imgNodes.length) continue;

        const imgInfos = imgNodes.map(i => {
            const rect = i.getBoundingClientRect();
            const w = i.naturalWidth || i.width || rect.width || 0;
            const h = i.naturalHeight || i.height || rect.height || 0;
            const area = w * h;
            const ratio = h > 0 ? (w / h) : 0;
            return {
                src: i.src,
                w: w,
                h: h,
                area: area,
                top: rect.top || 0,
                left: rect.left || 0,
                ratio: ratio
            };
        });

        const hasBig = imgInfos.some(info => info.area >= MIN_AREA);
        if (!hasBig) continue;

        const lines = (txt || "").split("\\n").map(s => s.trim()).filter(Boolean);

        let headNode =
            el.querySelector("h1, h2, h3, h4") ||
            el.querySelector('a[role="heading"]') ||
            el.querySelector("a");

        let headline = headNode ? (headNode.innerText || "").trim() : "";
        if (!headline && lines.length > 1) {
            headline = lines[1];
        }

        const advertiser = lines.length ? lines[0] : "";

        cards.push({
            advertiser: advertiser,
            headline: headline,
            text: txt,
            lines: lines,
            image_infos: imgInfos
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

        # Scroll i huvudsidan
        for _ in range(8):
            await page.evaluate("window.scrollBy(0, window.innerHeight)")
            await asyncio.sleep(0.5)

        all_ads = []

        # Huvud-frame
        try:
            main_ads = await page.evaluate(IFRAME_JS_SCRAPER)
            print(f"🧩 Huvud-frame: {len(main_ads)} annonskort")
            all_ads.extend(main_ads)
        except Exception as e:
            print("Fel vid DOM-scrape i huvud-frame:", e)

        # Alla övriga frames – scrolla och scrapa
        for frame in page.frames:
            if frame == page.main_frame:
                continue
            try:
                for _ in range(8):
                    await frame.evaluate("window.scrollBy(0, window.innerHeight)")
                    await asyncio.sleep(0.4)

                frame_ads = await frame.evaluate(IFRAME_JS_SCRAPER)
                print(f"🪟 Frame {frame.url} → {len(frame_ads)} annonskort")
                all_ads.extend(frame_ads)
            except Exception as e:
                print("⚠️ Frame ej läsbar (cross-origin):", frame.url, e)

        print("✅ TOTALT hittade annonskort:", len(all_ads))

        # klipp ned till MAX_ADS om det behövs
        if len(all_ads) > MAX_ADS:
            all_ads = all_ads[: MAX_ADS]

        OUTPUT_DIR.mkdir(exist_ok=True, parents=True)
        CANDIDATES_PATH.write_text(
            json.dumps(
                [{"source_file": "frames_dom", "parsed": all_ads}],
                indent=2, ensure_ascii=False
            ),
            encoding="utf-8"
        )

        await browser.close()
        return True


# ============================================================
# EXCEL – enbart text + Bild-URL (ingen nerladdning, ingen OCR)
# ============================================================

def process_candidates_and_save(run_dir):
    set_paths(run_dir)

    if not CANDIDATES_PATH.exists():
        print("❌ Saknar ads_candidates.json")
        return False

    data = json.loads(CANDIDATES_PATH.read_text(encoding="utf-8"))
    if not data:
        print("❌ Tomma kandidater")
        return False

    ads = data[0].get("parsed") or []
    print("⏳ Bearbetar annonser (totalt):", len(ads))

    rows = []

    for idx, ad in enumerate(ads, start=1):
        if idx > MAX_ADS:
            break

        infos = ad.get("image_infos") or []

        # välj "bästa" bild-URL (störst, bonus för landskap)
        best = None
        best_score = -1.0
        for info in infos:
            try:
                area = float(info.get("area") or 0)
                ratio = float(info.get("ratio") or 0)
            except Exception:
                area = 0.0
                ratio = 0.0
            score = area
            if ratio > 1.2:
                score *= 1.5
            if score > best_score:
                best_score = score
                best = info

        img_url = best["src"] if best and best.get("src") else ""

        lines = ad.get("lines") or []
        advertiser = ad.get("advertiser", "")
        headline = ad.get("headline", "")
        description = ""
        if len(lines) >= 3:
            description = " ".join(lines[2:])
        elif len(lines) == 2:
            description = lines[1]

        rows.append({
            "Index": idx,
            "Annonsör": advertiser,
            "Rubrik": headline,
            "Beskrivning": description,
            "Text": ad.get("text", ""),
            "Bild-URL": img_url,
        })

    if not rows:
        df = pd.DataFrame([{"Info": "Inga annonser hittades"}])
        excel = get_available_filename(OUTPUT_EXCEL)
        df.to_excel(excel, index=False)
        print("📄 Excel utan annonser:", excel)
        return True

    df = pd.DataFrame(rows)
    excel = get_available_filename(OUTPUT_EXCEL)
    df.to_excel(excel, index=False)
    print("✅ Excel sparad:", excel)
    return True
