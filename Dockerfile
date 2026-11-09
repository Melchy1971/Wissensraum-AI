# Wissensraum-AI Server (Release 3.0, step 10.8). Build: docker build -t wissensraum-server .
# ponytail: the base image is pinned by tag; pin it by digest on the VPS after the first pull
# (docker inspect --format '{{index .RepoDigests 0}}' python:3.13-slim-bookworm).
FROM python:3.13-slim-bookworm

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1 \
    WR_HOST=0.0.0.0 WR_PORT=8000 WR_LOG_DIR=/data/logs
RUN useradd --uid 10001 --home-dir /data --no-create-home wissensraum \
    && mkdir -p /data/logs && chown -R wissensraum /data
WORKDIR /app
COPY requirements.txt requirements-server.txt ./
RUN pip install --no-compile -r requirements-server.txt
COPY *.py schema.sql ./
COPY templates templates
COPY static static

USER wissensraum
EXPOSE 8000
# The Host header must be the configured address (TrustedHostMiddleware).
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 CMD python -c \
  "import os,urllib.request,urllib.parse; h=urllib.parse.urlsplit(os.environ['WR_BASE_URL']).hostname; urllib.request.urlopen(urllib.request.Request('http://127.0.0.1:8000/healthz', headers={'Host': h}), timeout=4)"
CMD ["python", "server.py"]
