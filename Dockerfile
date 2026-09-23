FROM denoland/deno:2.5.2 AS deno

FROM python:3.13.7-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

COPY --from=deno /usr/bin/deno /usr/local/bin/deno

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt \
    && useradd --create-home --uid 10001 bot

COPY --chown=bot:bot src ./src
COPY --chown=bot:bot scripts ./scripts

USER bot
CMD ["python", "-m", "src.bot"]
