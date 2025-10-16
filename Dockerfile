# Basimage med Playwright
FROM mcr.microsoft.com/playwright/python:1.47.0-jammy

WORKDIR /app
COPY . /app

RUN pip install --no-cache-dir -r requirements.txt
RUN playwright install --with-deps

EXPOSE 8000
CMD ["uvicorn", "webapi:app", "--host", "0.0.0.0", "--port", "8000"]
