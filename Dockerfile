# Wissensraum-AI Server (Release 3.0, step 10.8). Build: docker build -t wissensraum-server .
# ponytail: the base image is pinned by tag; pin it by digest on the VPS after the first pull
# (docker inspect --format '{{index .RepoDigests 0}}' python:3.13-slim-bookworm).
FROM python:3.13-slim-bookworm

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1 \
    WR_HOST=0.0.0.0 WR_PORT=8000 WR_LOG_DIR=/data/logs
# Phase 6 (decision 11.11.2026): Tesseract with German and English in the shared image; only the worker
# calls it (the server service sets WISSENSRAUM_OCR=0). One Tesseract thread per call (OMP_THREAD_LIMIT).
RUN apt-get update \
    && apt-get install -y --no-install-recommends tesseract-ocr tesseract-ocr-deu tesseract-ocr-eng \
    && rm -rf /var/lib/apt/lists/* \
    && useradd --uid 10001 --home-dir /data --no-create-home wissensraum \
    && mkdir -p /data/logs /data/exports && chown -R wissensraum /data
ENV OMP_THREAD_LIMIT=1
WORKDIR /app
# --chown (from the VPS, 11.11.2026): the runtime user must read the code even when the checkout on the
# host is not world-readable (umask 077). The root file system stays read-only in compose.yaml.
COPY --chown=10001:10001 requirements.txt requirements-server.txt requirements-ocr.txt ./
RUN pip install --no-compile -r requirements-server.txt -r requirements-ocr.txt
COPY --chown=10001:10001 *.py schema.sql ./
COPY --chown=10001:10001 templates templates
COPY --chown=10001:10001 static static

USER wissensraum
EXPOSE 8000
# The Host header must be the configured address (TrustedHostMiddleware).
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 CMD python -c \
  "import os,urllib.request,urllib.parse; h=urllib.parse.urlsplit(os.environ['WR_BASE_URL']).hostname; urllib.request.urlopen(urllib.request.Request('http://127.0.0.1:8000/healthz', headers={'Host': h}), timeout=4)"
CMD ["python", "server.py"]
