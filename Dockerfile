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
