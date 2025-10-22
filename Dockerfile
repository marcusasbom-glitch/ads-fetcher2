# ---- Dockerfile (Playwright + OCR + requests) ----
FROM mcr.microsoft.com/playwright/python:v1.45.0-jammy

# OCR-binaries (svenska + engelska)
RUN apt-get update && apt-get install -y \
    tesseract-ocr \
    tesseract-ocr-swe \
    tesseract-ocr-eng \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY . .

# Pythonpaket
RUN pip install --no-cache-dir \
    fastapi uvicorn[standard] python-multipart \
    pandas openpyxl \
    pillow opencv-python-headless pytesseract numpy \
    requests

# (Chromium finns i basimagen, men detta säkerställer rätt version/dep.)
RUN python -m playwright install --with-deps chromium

CMD ["uvicorn", "webapi:app", "--host", "0.0.0.0", "--port", "8000"]
