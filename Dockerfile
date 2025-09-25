FROM mcr.microsoft.com/devcontainers/python:1-3.11-bullseye
RUN apt-get update && apt-get install -y --no-install-recommends ffmpeg ca-certificates && rm -rf /var/lib/apt/lists/*
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY . .
RUN useradd -m app && chown -R app:app /app
USER app
CMD ["python", "bot.py"]
