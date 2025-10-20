# Dockerfile
FROM mcr.microsoft.com/playwright/python:v1.47.0-jammy

WORKDIR /app
COPY . /app
# OCR (Tesseract) + språk
RUN apt-get update && apt-get install -y \
    tesseract-ocr \
    tesseract-ocr-eng \
    tesseract-ocr-swe \
    libtesseract-dev \
 && rm -rf /var/lib/apt/lists/*

RUN pip install --no-cache-dir -r requirements.txt
# Basimagen har redan browsers, men detta är ok:
RUN playwright install --with-deps

EXPOSE 8000
CMD ["sh", "-c", "uvicorn webapi:app --host 0.0.0.0 --port ${PORT:-8000}"]

