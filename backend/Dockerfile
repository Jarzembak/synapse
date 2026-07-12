FROM python:3.12-slim@sha256:423ed6ab25b1921a477529254bfeeabf5855151dc2c3141699a1bfc852199fbf

RUN apt-get update && apt-get install -y --no-install-recommends \
    ffmpeg git unzip rclone \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /srv

COPY requirements.txt constraints.txt ./
RUN pip install --no-cache-dir -c constraints.txt -r requirements.txt

# GPU=1 (set by docker-compose.gpu.yml) adds the CUDA runtime libraries
# faster-whisper/CTranslate2 need for whisper_device=cuda. Ollama (its own
# container) gets the GPU directly. Default build stays CPU-only and lean.
#
# NOTE: we deliberately keep the CPU `onnxruntime` (pulled in by kokoro-onnx and
# piper-tts). onnxruntime-gpu's current release needs the CUDA-13 runtime, which
# these CUDA-12 cublas/cudnn libs don't satisfy, and swapping it broke ALL TTS
# (both engines import onnxruntime). Kokoro/Piper therefore run TTS on CPU;
# Piper is the fast CPU path. The GPU still accelerates Whisper and Ollama.
#
# The python one-liner prints each pip-installed CUDA lib dir (semicolon-
# separated simple statements — no literal newlines, which the shell would pass
# to python as invalid line-continuations) so ldconfig finds libcublas/libcudnn.
ARG GPU=0
ARG NVIDIA_CUBLAS_VERSION=12.9.2.10
ARG NVIDIA_CUDNN_VERSION=9.24.0.43
RUN if [ "$GPU" = "1" ]; then \
        pip install --no-cache-dir \
            "nvidia-cublas-cu12==${NVIDIA_CUBLAS_VERSION}" \
            "nvidia-cudnn-cu12==${NVIDIA_CUDNN_VERSION}" && \
        python -c "import nvidia.cublas.lib, nvidia.cudnn.lib; print(list(nvidia.cublas.lib.__path__)[0]); print(list(nvidia.cudnn.lib.__path__)[0])" > /etc/ld.so.conf.d/nvidia-pip.conf && \
        ldconfig; \
    fi

# deno: JS runtime yt-dlp needs for reliable YouTube extraction. Pin the exact
# release and verify the official release-asset digest so builds cannot change
# underneath an unchanged checkout.
ARG DENO_VERSION=2.9.2
ARG DENO_SHA256=934d1bd5cb09eaed7f2e4a4fc58208d04a3c5c0fcde9f319d93d735265c67a4a
ADD https://github.com/denoland/deno/releases/download/v${DENO_VERSION}/deno-x86_64-unknown-linux-gnu.zip /tmp/deno.zip
RUN echo "${DENO_SHA256}  /tmp/deno.zip" | sha256sum -c - \
    && unzip -o /tmp/deno.zip -d /usr/local/bin \
    && rm /tmp/deno.zip \
    && deno --version

COPY app ./app

ENV PYTHONUNBUFFERED=1

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
