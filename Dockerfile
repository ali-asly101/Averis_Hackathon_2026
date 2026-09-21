# One container: builds the React UI, then runs the Python API which also serves it.
# Works on Hugging Face Spaces (port 7860), Render / Railway / Cloud Run ($PORT wins).
#
# The build context must contain data/ (inbox/ + attachments/, NO ground_truth.json).
# deploy/deploy_hf.py prepares exactly that - see DEPLOY.md.

# ---- 1. build the web UI ------------------------------------------------------
FROM node:20-slim AS ui
WORKDIR /ui
COPY frontend/package.json frontend/package-lock.json ./
RUN npm ci --no-audit --no-fund
COPY frontend/ ./
RUN npm run build

# ---- 2. the app -----------------------------------------------------------------
FROM python:3.12-slim

# opencv (used by the OCR engine) needs these system libraries
RUN apt-get update \
 && apt-get install -y --no-install-recommends libgl1 libglib2.0-0 \
 && rm -rf /var/lib/apt/lists/*

# Hugging Face Spaces runs containers as uid 1000
RUN useradd -m -u 1000 app
WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY sdoc/ sdoc/
COPY data/ data/
COPY --from=ui /ui/dist frontend/dist

# Process the inbox now, at build time (rules + OCR, no AI key needed), so the
# site opens instantly with results and spends no AI quota on every restart.
# The AI is kept for what visitors upload through Upload & Check.
RUN SDOC_DATA=/app/data SDOC_OUT=/app/seed SDOC_CACHE_DIR=/tmp/build_cache SDOC_DISABLE_AI=1 \
    python -m sdoc run \
 && rm -rf /tmp/build_cache \
 && test -f /app/seed/results.json

# Settings for a public deploy. Secrets (LLM_API_KEY, SDOC_ADMIN_TOKEN) are NOT
# baked in: set them in the hosting platform's secrets / environment settings.
ENV PYTHONUNBUFFERED=1 \
    SDOC_DATA=/app/data \
    SDOC_OUT=/tmp/sdoc/output \
    SDOC_CACHE_DIR=/tmp/sdoc/cache \
    SDOC_API_HOST=0.0.0.0 \
    SDOC_API_PORT=7860 \
    SDOC_SEED_DIR=/app/seed \
    SDOC_AUTORUN=1 \
    SDOC_RUN_COOLDOWN=300 \
    SDOC_CHECKS_PER_HOUR=30 \
    SDOC_MAX_UPLOAD_MB=10

USER app
EXPOSE 7860
CMD ["python", "-m", "sdoc", "serve"]
