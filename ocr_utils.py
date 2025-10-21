# ocr_utils.py
import pytesseract
from pytesseract import Output
from PIL import Image
import cv2
import numpy as np
from collections import defaultdict

def ocr_h1_h2_from_image(img_path: str, lang: str = "swe+eng"):
    """
    Returnerar (h1, h2) gissade rubriker ur en bild.
    Heuristik: plockar de två "största" textraderna med god OCR-confidence.
    """
    try:
        im = cv2.imdecode(np.fromfile(img_path, dtype=np.uint8), cv2.IMREAD_COLOR)
        if im is None:
            im = cv2.cvtColor(np.array(Image.open(img_path).convert("RGB")), cv2.COLOR_RGB2BGR)

        # Förbehandling (gråskala + uppskalning)
        g = cv2.cvtColor(im, cv2.COLOR_BGR2GRAY)
        h, w = g.shape[:2]
        if max(h, w) < 1400:
            g = cv2.resize(g, (w * 2, h * 2), interpolation=cv2.INTER_CUBIC)
        g = cv2.bilateralFilter(g, 7, 50, 50)
        g = cv2.adaptiveThreshold(
            g, 255, cv2.ADAPTIVE_THRESH_GAUSSIAN_C, cv2.THRESH_BINARY, 31, 10
        )

        data = pytesseract.image_to_data(g, lang=lang, output_type=Output.DICT)
        lines = defaultdict(lambda: {"texts": [], "heights": [], "confs": []})
        n = len(data["text"])
        for i in range(n):
            txt = (data["text"][i] or "").strip()
            conf = float(data["conf"][i]) if data["conf"][i] not in ("-1", None, "") else -1
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
            avg_h = np.mean(info["heights"])
            conf_m = np.mean(info["confs"])
            score = avg_h * (conf_m / 100) * (1 + np.log1p(len(text)))
            scored.append((score, text))

        if not scored:
            return None, None

        scored.sort(reverse=True, key=lambda x: x[0])
        h1 = scored[0][1]
        h2 = scored[1][1] if len(scored) > 1 else None
        if h2 and h1 and h1.lower().strip() == h2.lower().strip():
            h2 = None
        return h1, h2
    except Exception:
        return None, None
