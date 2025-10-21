# ----- OCR: extrahera H1/H2 ur en annonsbild -----
from PIL import Image
import numpy as np
import easyocr
from pathlib import Path

# Skapa en reader en gång (sv + en, CPU)
_OCR_READER = easyocr.Reader(['sv', 'en'], gpu=False)

def extract_headlines_from_image(img_path: str | Path) -> tuple[str, str]:
    """
    Läser text ur en bild och heuristiskt väljer två 'största' rader som H1/H2.
    Returnerar (h1, h2).
    """
    img_path = Path(img_path)
    if not img_path.exists():
        return ("", "")

    im = Image.open(img_path).convert("RGB")
    w, h = im.size

    # easyocr -> listor av: [bbox, text, conf]
    results = _OCR_READER.readtext(np.array(im), detail=1, paragraph=False)

    words = []
    for bbox, text, conf in results:
        text = (text or "").strip()
        if not text:
            continue

        # bbox är 4 punkter [[x1,y1],[x2,y2]...]
        xs = [p[0] for p in bbox]
        ys = [p[1] for p in bbox]
        x = min(xs)
        y = min(ys)
        ww = max(xs) - x
        hh = max(ys) - y
        if ww <= 0 or hh <= 0:
            continue

        # Grov "storleks-score" för raden: area relativt bilden
        area = ww * hh
        font_score = area / max(1.0, (w * h))

        words.append({
            "x": float(x),
            "y": float(y),
            "w": float(ww),
            "h": float(hh),
            "text": text,
            "score": float(font_score),
        })

    # Inget att jobba med
    if not words:
        return ("", "")

    # Sortera ord ungefär uppifrån-ned, vänster->höger
    words.sort(key=lambda d: (d["y"], d["x"]))

    # Gruppéra ord till rader baserat på liknande y-led (höjd)
    lines = []
    used = [False] * len(words)

    for i, base in enumerate(words):
        if used[i]:
            continue
        line = [base]
        used[i] = True

        # Tillåt variation i y upp till ~60% av ordhöjd
        y_tol = base["h"] * 0.6

        for j in range(i + 1, len(words)):
            if used[j]:
                continue
            cand = words[j]
            if abs(cand["y"] - base["y"]) <= max(y_tol, cand["h"] * 0.6):
                line.append(cand)
                used[j] = True

        # Sätt ihop radens text
        line.sort(key=lambda d: d["x"])
        full_text = " ".join(wd["text"] for wd in line).strip()
        if len(full_text) < 2:
            continue

        avg_h = float(sum(wd["h"] for wd in line) / max(1, len(line)))
        avg_score = float(sum(wd["score"] for wd in line) / max(1, len(line)))

        lines.append({
            "text": full_text,
            "avg_h": avg_h,
            "score": avg_score,
        })

    if not lines:
        return ("", "")

    # Heuristisk rankning: främst stor höjd + "stor" area (score)
    for ln in lines:
        ln["rank"] = ln["avg_h"] * 1.0 + ln["score"] * 3.0 + (len(ln["text"]) / 500.0)

    lines.sort(key=lambda l: l["rank"], reverse=True)

    h1 = lines[0]["text"] if len(lines) > 0 else ""
    h2 = lines[1]["text"] if len(lines) > 1 else ""
    return (h1, h2)
