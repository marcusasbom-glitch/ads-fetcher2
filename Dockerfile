# ---- Dockerfile (python:slim + manuell Playwright) ----
FROM python:3.11-slim

# Systemberoenden + Tesseract (Playwright deps installeras av kommandot nedan)
RUN apt-get update && apt-get install -y \
    tesseract-ocr tesseract-ocr-swe tesseract-ocr-eng \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY . .

# Installera Python-deps, inkl. playwright
RUN pip install --no-cache-dir \
    fastapi uvicorn[standard] python-multipart \
    pandas openpyxl \
    pillow opencv-python-headless pytesseract \
    playwright

# Installera Chromium + dess systemberoenden via Playwright
RUN python -m playwright install --with-deps chromium

CMD ["uvicorn", "webapi:app", "--host", "0.0.0.0", "--port", "8000"]
