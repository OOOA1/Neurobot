# стейдж с ffmpeg (существующий тег)
FROM jrottenberg/ffmpeg:6.1-ubuntu2204 AS ff

FROM python:3.11-slim-bookworm
ARG DEBIAN_FRONTEND=noninteractive
RUN apt-get update && apt-get install -y --no-install-recommends ca-certificates && rm -rf /var/lib/apt/lists/*

# копируем бинарники ffmpeg/ffprobe из первого стейджа
COPY --from=ff /usr/local/bin/ffmpeg /usr/local/bin/ffmpeg
COPY --from=ff /usr/local/bin/ffprobe /usr/local/bin/ffprobe

ENV PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY . .
CMD ["python", "bot.py"]