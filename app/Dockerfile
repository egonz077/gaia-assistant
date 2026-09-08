FROM python:3.12-slim
WORKDIR /srv
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY . .
RUN apt-get update && apt-get install -y --no-install-recommends cron && rm -rf /var/lib/apt/lists/* \
    && echo "0 13 * * * cd /srv && python followup_cron.py >> /var/log/cron.log 2>&1" | crontab -
CMD service cron start && uvicorn main:app --host 0.0.0.0 --port 8000
