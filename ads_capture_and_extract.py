import asyncio
import json
import re
from pathlib import Path
from typing import List, Dict, Any, Optional

import httpx
from bs4 import BeautifulSoup
from PIL import Image
from io import BytesIO

from openpyxl import Workbook
from openpyxl.drawing.image import Image as XLImage
from openpyxl.utils import get_column_letter

from playwright.async_api import async_playwright


# ---------------------------------------------------------
# Hjälpfunktioner
# ---------------------------------------------------------

def normalize_url(ar_input: str) -> str:
    """
    Om ar_input ser ut som ett AR-ID (t.ex. 'AR181828...') bygg en
    Annonsöversikt-URL. Annars antar vi att ar_input är en full URL.
    """
    ar_input = (ar_input or "").strip()
    if re.match(r"^AR[0-9]+", ar_input, flags=re.I):
        # Bygg URL liknande den vi såg i loggarna.
        # Anpassa origin/region/preset/platform om du vill.
        return (
            f"https://adstransparency.google.com/advertiser/{ar_input}"
            f"?origin=ata&region=SE&preset-date=Last+30+days&platform=SEARCH"
        )
    # Annars tolkar vi det som en full URL
    return ar_input


async def scroll_page(page, steps: int = 10, delay_ms: int = 500):
    """
    En enkel auto-scroll för att ladda fler kort. Kan justeras.
    """
    for _ in range(steps):
        await page.mouse.wheel(0, 1500)
        await page.wait_for_timeout(delay_ms)


# ---------------------------------------------------------
# Playwright / DOM-extraktion
# ---------------------------------------------------------

extract_ads_js = """
() => {
  // Försök hitta kort i listan. Selektorerna kan variera.
  // Nedan en generisk strategi: leta element som ser ut som "annonskort"
  // och extrahera headline, body/description och ev. bild <img>.
  const items = [];
  // Exempel: kort kan ha role="listitem" eller speciella klasser. 
  // För att vara robust: plocka alla card-liknande containers och försök hitta fält inuti.
  const cards = document.querySelectorAll('[role="listitem"], article, div[data-ad-card], .ad-card, .card');

  cards.forEach(card => {
    const textBlocks = card.querySelectorAll('h1, h2, h3, [role="heading"], .headline, .title');
    let headline = '';
    if (textBlocks && textBlocks.length > 0) {
      headline = textBlocks[0].textContent?.trim() ?? '';
    }

    // Leta efter brödtext
    let body = '';
    const bodyCand = card.querySelectorAll('p, .description, .body, [data-body]');
    if (bodyCand && bodyCand.length > 0) {
      // hämta den första men slå ihop lite text om möjligt
      body = Array.from(bodyCand).map(el => el.textContent?.trim() ?? '').filter(Boolean).join(' / ');
    }

    // Leta efter bild
    let imgUrl = '';
    const img = card.querySelector('img');
    if (img && img.src) {
      imgUrl = img.src;
    }
    if (!headline && !body && !imgUrl) return;

    items.push({
      headline,
      body,
      image_url: imgUrl
    });
  });

  return items;
}
"""


async def capture_network(ar_input: str, run_dir: Path) -> None:
    """
    - Bygger rätt URL från AR-ID eller använder given URL
    - Öppnar sidan, väntar in, scrollar, extraherar annonskandidater
    - Sparar 'ads_candidates.json' i run_dir
    """
    run_dir.mkdir(parents=True, exist_ok=True)
    url = normalize_url(ar_input)

    candidates_path = run_dir / "ads_candidates.json"
    html_path = run_dir / "page.html"

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=True)
        context = await browser.new_context(
            viewport={"width": 1366, "height": 900}
        )
        page = await context.new_page()

        # Gå till sidan
        await page.goto(url, wait_until="domcontentloaded", timeout=90_000)
        # Vänta in ytterligare lite rendering
        await page.wait_for_timeout(2000)

        # Prova auto-scroll för att ladda fler kort
        await scroll_page(page, steps=12, delay_ms=500)

        # Försök extrahera via JS
        items = await page.evaluate(extract_ads_js)

        # Som fallback: spara HTML så vi kan felsöka vid behov
        html = await page.content()
        html_path.write_text(html, encoding="utf-8")

        await context.close()
        await browser.close()

    # Spara kandidater
    with candidates_path.open("w", encoding="utf-8") as f:
        json.dump({"url": url, "items": items}, f, ensure_ascii=False, indent=2)


# ---------------------------------------------------------
# Bearbeta kandidater -> Excel med inbäddade bilder
# ---------------------------------------------------------

def _download_image_bytes(url: str, timeout: float = 20.0) -> Optional[bytes]:
    """
    Hämta ner en bild som bytes, eller returnera None om det misslyckas.
    """
    if not url:
        return None
    try:
        with httpx.Client(timeout=timeout, follow_redirects=True) as client:
            r = client.get(url)
            if r.status_code == 200 and r.content:
                return r.content
    except Exception:
        return None
    return None


def _fit_image_for_xlsx(image_bytes: bytes, max_w: int = 420, max_h: int = 280) -> Optional[BytesIO]:
    """
    Skala om bilden proportionellt för att passa snyggt i cellen.
    Returnerar BytesIO (PNG) att bädda in i Excel.
    """
    try:
        im = Image.open(BytesIO(image_bytes)).convert("RGB")
        im.thumbnail((max_w, max_h), Image.LANCZOS)
        out = BytesIO()
        im.save(out, format="PNG", optimize=True)
        out.seek(0)
        return out
    except Exception:
        return None


def process_candidates_and_save(run_dir: Path) -> bool:
    """
    Läser 'ads_candidates.json' och skapar 'ads_extracted.xlsx'.
    Embeddar nedladdade bilder i en kolumn.
    Returnerar True om filen skapades.
    """
    candidates_path = run_dir / "ads_candidates.json"
    if not candidates_path.exists():
        return False

    data = json.loads(candidates_path.read_text(encoding="utf-8"))
    items: List[Dict[str, Any]] = data.get("items", [])

    # Rensa tomma items
    cleaned = []
    for it in items:
        head = (it.get("headline") or "").strip()
        body = (it.get("body") or "").strip()
        img = (it.get("image_url") or "").strip()
        if head or body or img:
            cleaned.append({"headline": head, "body": body, "image_url": img})

    if not cleaned:
        # Ingen data – skapa ändå en enkel Excel som markör
        wb = Workbook()
        ws = wb.active
        ws.title = "Ads"
        ws.append(["Headline", "Body", "Image"])
        out = run_dir / "ads_extracted.xlsx"
        wb.save(out)
        return True

    wb = Workbook()
    ws = wb.active
    ws.title = "Ads"
    ws.append(["Headline", "Body", "Image"])

    # Formatera kolumnbredder
    ws.column_dimensions[get_column_letter(1)].width = 50  # Headline
    ws.column_dimensions[get_column_letter(2)].width = 70  # Body
    ws.column_dimensions[get_column_letter(3)].width = 40  # Image

    row = 2
    for it in cleaned:
        ws.cell(row=row, column=1, value=it.get("headline") or "")
        ws.cell(row=row, column=2, value=it.get("body") or "")

        img_url = it.get("image_url") or ""
        if img_url:
            img_bytes = _download_image_bytes(img_url)
            if img_bytes:
                fitted = _fit_image_for_xlsx(img_bytes)
                if fitted:
                    xlimg = XLImage(fitted)
                    # Placera bilden i kolumn C
                    cell_addr = f"{get_column_letter(3)}{row}"
                    ws.add_image(xlimg, cell_addr)
                    # Höj raden lite för att få plats
                    ws.row_dimensions[row].height = 220

        row += 1

    out = run_dir / "ads_extracted.xlsx"
    wb.save(out)
    return out.exists()
