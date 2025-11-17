#!/usr/bin/env bash
set -euo pipefail

# Function to run ingestion scripts
run_ingestion() {
  echo "[$(date)] Starting ingestion process..."
  
  # Run Redmine ingestion (excluding open issues)
  if [[ -n "${REDMINE_URL:-}" && -n "${REDMINE_TOKEN:-}" ]]; then
    echo "[$(date)] Running Redmine ingestion..."
    python3 /app/ingest_redmine_to_anyllm.py
  else
    echo "[$(date)] Skipping Redmine ingestion (missing credentials)"
  fi
  
  # Run Wiki.js ingestion
  if [[ -n "${WIKIJS_URL:-}" && -n "${WIKIJS_TOKEN:-}" ]]; then
    echo "[$(date)] Running Wiki.js ingestion..."
    python3 /app/ingest_wikijs_to_anyllm.py
  else
    echo "[$(date)] Skipping Wiki.js ingestion (missing credentials)"
  fi
  
  echo "[$(date)] Ingestion process completed"
}

# If DEFAULT_SCHEDULE is set, schedule the job;
# if empty, run once at startup.
if [[ -n "${DEFAULT_SCHEDULE:-}" ]]; then
  echo "${DEFAULT_SCHEDULE} cd /app && run_ingestion >> /var/log/ingestor.log 2>&1" > /etc/cron.d/rag-ingestor
  chmod 0644 /etc/cron.d/rag-ingestor
  crontab /etc/cron.d/rag-ingestor
  touch /var/log/ingestor.log
  echo "[cron] Starting cron scheduler..."
  cron -f
else
  echo "[oneshot] Running ETL once..."
  run_ingestion
fi
