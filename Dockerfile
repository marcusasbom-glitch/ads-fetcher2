FROM mcr.microsoft.com/playwright/python:1.34.0-focal

WORKDIR /app
COPY . /app

RUN pip install --no-cache-dir -r requirements.txt
RUN playwright install

CMD ["python", "ads_capture_and_extract.py"]
