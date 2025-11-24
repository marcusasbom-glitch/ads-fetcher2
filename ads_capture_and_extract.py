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
