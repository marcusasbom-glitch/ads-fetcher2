# ---- Dockerfile (Playwright-bild + Tesseract + OCR-paket) ----
FROM mcr.microsoft.com/playwright/python:v1.45.0-jammy

# OCR-binaries (svenska + engelska)
RUN apt-get update && apt-get install -y \
    tesseract-ocr \
    tesseract-ocr-swe \
    tesseract-ocr-eng \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY . .

# Pythonpaket (playwright-klienten finns redan i basimagen)
RUN pip install --no-cache-dir \
    fastapi uvicorn[standard] python-multipart \
    pandas openpyxl \
    pillow opencv-python-headless pytesseract

# (Valfritt – i den här basimagen är Chromium redan installerad.
#  Men att köra install igen skadar inte.)
RUN python -m playwright install --with-deps chromium

CMD ["uvicorn", "webapi:app", "--host", "0.0.0.0", "--port", "8000"]
