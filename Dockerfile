# Dockerfile
FROM mcr.microsoft.com/playwright/python:v1.45.0-jammy

WORKDIR /app
COPY . /app

# Python deps
RUN pip install --no-cache-dir -r requirements.txt

# Säkerställ Playwright-browsers & deps
RUN playwright install --with-deps

# Exponera port som Render förser via $PORT
EXPOSE 8000

# Starta FastAPI
CMD ["sh", "-c", "uvicorn webapi:app --host 0.0.0.0 --port ${PORT:-8000}"]
# Dockerfile
FROM mcr.microsoft.com/playwright/python:v1.45.0-focal

# Installera OCR-verktyg
RUN apt-get update && apt-get install -y \
    tesseract-ocr \
    tesseract-ocr-swe \
    tesseract-ocr-eng \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY . .

# Installera Pythonberoenden
RUN pip install --no-cache-dir \
    fastapi uvicorn[standard] python-multipart \
    pandas openpyxl \
    pillow opencv-python-headless pytesseract

# Installera Playwright browser dependencies
RUN playwright install --with-deps chromium

CMD ["uvicorn", "webapi:app", "--host", "0.0.0.0", "--port", "8000"]

