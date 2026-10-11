FROM node:22-alpine AS web
WORKDIR /web
COPY web/package.json web/package-lock.json ./
RUN npm ci
COPY web/ ./
RUN npm run build

FROM python:3.13.7-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt \
    && useradd --create-home --uid 10001 bot \
    && mkdir -p /data \
    && chown bot:bot /data

COPY --chown=bot:bot src ./src
COPY --chown=bot:bot scripts ./scripts
COPY --from=web --chown=bot:bot /web/dist ./web_dist

USER bot
CMD ["python", "-m", "src.bot"]
