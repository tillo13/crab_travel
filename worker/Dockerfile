FROM python:3.12-slim

WORKDIR /app

# Minimal deps — scraper needs requests/psycopg2 only
RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc g++ libffi-dev libssl-dev libpq-dev \
    && rm -rf /var/lib/apt/lists/*

COPY worker/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Whole repo copied so the worker can import utilities/timeshare_ii_scraper.py
COPY . .

EXPOSE 8080

# 1 worker, 2 threads, 900s (15min) timeout — Cloud Run maxes at 60min.
# A single region crawl takes 1-3 min; drain-all takes 30-60 min and
# that's exactly why this lives on Cloud Run, not App Engine.
CMD ["gunicorn", "-b", "0.0.0.0:8080", "-w", "1", "--threads", "2", "--timeout", "3600", "worker.app:app"]
