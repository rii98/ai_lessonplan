# Fake Edboox — dev only. Deliberately independent of the lessonforge package.
FROM python:3.12-slim
ENV PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1
WORKDIR /srv
COPY requirements.txt .
RUN pip install -r requirements.txt
COPY app.py demo.html ./
RUN useradd --create-home edboox && mkdir -p /data && chown edboox /data
USER edboox
EXPOSE 8200
CMD ["uvicorn", "app:app", "--host", "0.0.0.0", "--port", "8200"]
