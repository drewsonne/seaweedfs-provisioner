FROM python:3.14-slim

COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv

WORKDIR /app

COPY pyproject.toml .
RUN uv pip install --system --no-cache .

COPY controller/ .

RUN useradd --system --no-create-home --user-group operator
USER operator

CMD ["kopf", "run", "--all-namespaces", "--liveness=http://0.0.0.0:8080/healthz", "main.py"]
