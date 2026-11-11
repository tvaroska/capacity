FROM python:3.13-slim AS base

COPY --from=ghcr.io/astral-sh/uv:0.9.1 /uv /uvx /bin/

WORKDIR /app

COPY pyproject.toml uv.lock .python-version README.md ./
RUN uv sync --frozen --no-install-project --no-dev

COPY capacity/ capacity/
RUN uv sync --frozen --no-dev

RUN useradd --system --uid 10001 --no-create-home app
USER app

ENV PORT=8080
EXPOSE 8080

CMD ["sh", "-c", "exec /app/.venv/bin/uvicorn capacity.api:app --host 0.0.0.0 --port ${PORT:-8080}"]
