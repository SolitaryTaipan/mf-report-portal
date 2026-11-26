FROM python:3.12-slim

# Chromium prints the PDF; Inter is the report font (as in the sample); DejaVu covers ₹ and symbols.
RUN apt-get update \
 && apt-get install -y --no-install-recommends chromium fonts-inter fonts-dejavu-core tzdata \
 && rm -rf /var/lib/apt/lists/*

ENV PYTHONUNBUFFERED=1 \
    TZ=Asia/Kolkata \
    CHROME_NO_SANDBOX=1 \
    PORTAL_HOST=0.0.0.0 \
    PORT=8000

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY . .

RUN useradd --uid 1000 --create-home app && mkdir -p /app/reports && chown -R app:app /app
USER app

EXPOSE 8000
HEALTHCHECK --interval=60s --timeout=5s CMD python -c "import urllib.request,os; urllib.request.urlopen('http://127.0.0.1:'+os.environ.get('PORT','8000')+'/healthz')"
CMD ["python", "portal.py"]
