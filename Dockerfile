# syntax=docker/dockerfile:1
FROM node:22-alpine AS web-build
WORKDIR /web
COPY frontend/package*.json ./
RUN npm ci
COPY frontend/ ./
RUN npm run build

FROM python:3.12-slim AS runtime
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1
RUN apt-get update \
    && apt-get install -y --no-install-recommends aria2 gosu tini \
    && rm -rf /var/lib/apt/lists/*
WORKDIR /app
COPY backend/requirements.txt ./backend/requirements.txt
RUN pip install -r backend/requirements.txt
COPY backend/app ./backend/app
COPY docker/entrypoint.sh /usr/local/bin/docker-entrypoint.sh
COPY --from=web-build /web/dist ./frontend/dist
RUN mkdir -p /config /downloads /library \
    && useradd --create-home --uid 1000 --shell /usr/sbin/nologin appuser \
    && chown -R appuser:appuser /app /config /downloads /library \
    && chmod 0755 /usr/local/bin/docker-entrypoint.sh
ENV PUID=99 \
    PGID=100
EXPOSE 8080 51413/tcp 51413/udp
VOLUME ["/config", "/downloads", "/library"]
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 CMD gosu "${PUID:-99}:${PGID:-100}" python -c "import json, urllib.request; data=json.load(urllib.request.urlopen('http://127.0.0.1:8080/api/health', timeout=3)); raise SystemExit(0 if data.get('ok') and data.get('aria2') else 1)"
ENTRYPOINT ["/usr/bin/tini", "--", "/usr/local/bin/docker-entrypoint.sh"]
CMD ["uvicorn", "app.main:app", "--app-dir", "/app/backend", "--host", "0.0.0.0", "--port", "8080"]

