FROM python:3.12-slim AS base

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

# The build backend (setuptools) needs the package source present to build the
# wheel, so copy source + config before installing. A single install builds and
# installs the project together with its dependencies.
COPY pyproject.toml ./
COPY src ./src
COPY config ./config

RUN pip install --upgrade pip && pip install .

EXPOSE 8000

# FastEmbed downloads its ONNX model on first use; cache lives in /app/.cache
ENV FASTEMBED_CACHE_PATH=/app/.cache/fastembed

CMD ["uvicorn", "lessonforge.api.main:app", "--host", "0.0.0.0", "--port", "8000"]
