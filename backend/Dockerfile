FROM python:3.13-slim

COPY --from=ghcr.io/astral-sh/uv:0.13.5 /uv /usr/local/bin/uv

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PYTHONPATH=/app/src \
    PATH=/app/.venv/bin:$PATH

WORKDIR /app

COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev

COPY src ./src
COPY migrations ./migrations

USER 10001:10001
