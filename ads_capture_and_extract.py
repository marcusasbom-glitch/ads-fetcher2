# ads_capture_and_extract.py
# ============================================================
# OBS! Behåll din befintliga 'capture_network' (scraping) oförändrad.
# Den här filen lägger till OCR (H1/H2) och uppdaterar Excel-exporten.
# ============================================================

from __future__ import annotations
from pathlib import Path
import os
import json
import pandas as pd

# --- OCR imports ---
import pytesseract
from pytesseract import Output
from PIL import Image
import cv2
import numpy as np
from collections import defaultdict


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
def process_candidates_and_save(run_dir: Path) -> bool:
    """
    Läser insamlade annonser från run_dir, kör OCR för H1/H2 och skriver ads_extracted.xlsx.
    Förväntar sig en JSON-lista med annonser i run_dir/ads_collected.json (eller justera enligt din struktur).
    Varje annons bör ha minst: ar_id, source, url, image_path (lokal filväg till bild).
    """
    try:
        # 1) Läs in dina insamlade annonser (anpassa filnamn om du använder ett annat)
        ads_json = run_dir / "ads_collected.json"
        if not ads_json.exists():
            # Om du har ett annat format/filnamn – ändra här så det matchar din capture-kod.
            return False

        ads = json.loads(ads_json.read_text(encoding="utf-8"))

        rows = []
        for ad in ads:
            # Anpassa fältnamn om dina keys skiljer sig
            img_path = ad.get("image_path")
            h1 = h2 = None
            if img_path and os.path.exists(img_path):
                h1, h2 = ocr_h1_h2_from_image(img_path)

            row = {
                "AR-ID": ad.get("ar_id"),
                "Källa": ad.get("source"),
                "Annons-URL": ad.get("url"),
                "Bildfil": os.path.basename(img_path) if img_path else "",
                "H1 (OCR)": h1,
                "H2 (OCR)": h2,
            }
            rows.append(row)

        # 2) Skriv Excel
        out = run_dir / "ads_extracted.xlsx"
        df = pd.DataFrame(rows)
        with pd.ExcelWriter(out, engine="openpyxl") as xw:
            df.to_excel(xw, index=False, sheet_name="Ads")

        return True
    except Exception as e:
        # Logga till stdout – Render visar i loggar
        print("Fel i process_candidates_and_save:", e)
        return False
