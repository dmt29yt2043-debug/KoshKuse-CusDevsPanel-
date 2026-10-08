FROM python:3.12-slim

# ffmpeg: нормализация аудио в .m4a и длительность (ARCHITECTURE.md, Р8)
RUN apt-get update && apt-get install -y --no-install-recommends ffmpeg \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /srv
COPY pyproject.toml ./
COPY cusdev ./cusdev
COPY config ./config
RUN pip install --no-cache-dir --upgrade pip && pip install --no-cache-dir .

ENV DATA_DIR=/data TAXONOMY_PATH=/srv/config/taxonomy.yaml PYTHONUNBUFFERED=1
EXPOSE 8000
CMD ["uvicorn", "cusdev.web.app:app", "--host", "0.0.0.0", "--port", "8000", \
     "--proxy-headers", "--forwarded-allow-ips", "*"]
