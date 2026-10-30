FROM python:3.12-slim AS base

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

# The build backend (setuptools) needs the package source present to resolve the
# project metadata, so copy source + config before installing. Install EDITABLE
# (`-e`): dependencies are baked into the image, but the package itself resolves
# to /app/src — which docker-compose bind-mounts from the host, so a code edit is
# picked up live (with --reload) and never needs an image rebuild. Only a
# dependency change (pyproject.toml) requires `docker compose build`.
COPY pyproject.toml ./
COPY src ./src
COPY config ./config

RUN pip install --upgrade pip && pip install -e .

# Run as an unprivileged user; only the model cache needs to be writable.
RUN useradd --create-home --uid 10001 lessonforge \
    && mkdir -p /app/.cache/fastembed \
    && chown -R lessonforge /app/.cache
USER lessonforge

EXPOSE 8000

# FastEmbed downloads its ONNX model on first use; cache lives in /app/.cache
ENV FASTEMBED_CACHE_PATH=/app/.cache/fastembed

HEALTHCHECK --interval=15s --timeout=5s --start-period=30s --retries=5 \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/health/ready')"

# Migrations are an explicit step (`lessonforge-db migrate`), never run on boot.
CMD ["uvicorn", "lessonforge.api.main:app", "--host", "0.0.0.0", "--port", "8000"]
