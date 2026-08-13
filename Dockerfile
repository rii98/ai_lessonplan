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

EXPOSE 8000

# FastEmbed downloads its ONNX model on first use; cache lives in /app/.cache
ENV FASTEMBED_CACHE_PATH=/app/.cache/fastembed

CMD ["uvicorn", "lessonforge.api.main:app", "--host", "0.0.0.0", "--port", "8000"]
