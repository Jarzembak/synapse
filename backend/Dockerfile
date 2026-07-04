FROM python:3.12-slim

RUN apt-get update && apt-get install -y --no-install-recommends \
    ffmpeg git unzip rclone \
    && rm -rf /var/lib/apt/lists/*

# deno: JS runtime yt-dlp needs for reliable YouTube extraction
ADD https://github.com/denoland/deno/releases/latest/download/deno-x86_64-unknown-linux-gnu.zip /tmp/deno.zip
RUN unzip -o /tmp/deno.zip -d /usr/local/bin && rm /tmp/deno.zip && deno --version

WORKDIR /srv

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app ./app

ENV PYTHONUNBUFFERED=1

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
