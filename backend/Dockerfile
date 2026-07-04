FROM python:3.12-slim

RUN apt-get update && apt-get install -y --no-install-recommends \
    ffmpeg git unzip rclone \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /srv

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# GPU=1 (set by docker-compose.gpu.yml) adds the CUDA runtime libraries
# faster-whisper/CTranslate2 need for whisper_device=cuda. Default build stays
# CPU-only and lean.
ARG GPU=0
RUN if [ "$GPU" = "1" ]; then \
        pip install --no-cache-dir nvidia-cublas-cu12 nvidia-cudnn-cu12 && \
        python -c "import os, nvidia.cublas.lib, nvidia.cudnn.lib; \
print(os.path.dirname(nvidia.cublas.lib.__file__)); \
print(os.path.dirname(nvidia.cudnn.lib.__file__))" > /etc/ld.so.conf.d/nvidia-pip.conf && \
        ldconfig; \
    fi

# deno: JS runtime yt-dlp needs for reliable YouTube extraction. Kept BELOW the
# pip layers — this ADD re-checks the 'latest' URL every build, and a new deno
# release must not invalidate the requirements/CUDA layers above.
ADD https://github.com/denoland/deno/releases/latest/download/deno-x86_64-unknown-linux-gnu.zip /tmp/deno.zip
RUN unzip -o /tmp/deno.zip -d /usr/local/bin && rm /tmp/deno.zip && deno --version

COPY app ./app

ENV PYTHONUNBUFFERED=1

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
