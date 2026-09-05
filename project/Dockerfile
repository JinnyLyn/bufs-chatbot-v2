# Pinned by digest, not by the `3.14` tag: a floating tag means two builds of this same
# file can produce different base contents, so an upstream compromise or regression
# propagates silently. Refresh the digest deliberately when bumping the base.
FROM python:3.14@sha256:8edbf9e42c7fb168b9c523718ed907117e6d2e60f5889c0c499bbda3a787da53
# NOTE: This image builds the LEGACY Gradio demo (project/app.py, port 7860).
#       It is NOT the production FastAPI + Next.js stack.
#
# A `.dockerignore` at the repo root keeps project/.env, logs/ and .git out of the build
# context — Docker's context is the working tree, so .gitignore does NOT apply, and the
# `COPY . .` below would otherwise bake the live Langfuse secret into an immutable layer.

ENV PYTHONUNBUFFERED=1
ENV OLLAMA_HOST=0.0.0.0:11434
ENV GRADIO_SERVER_NAME=0.0.0.0
ENV GRADIO_SERVER_PORT=7860
ENV OLLAMA_HOME=/home/user/.ollama

RUN apt-get update && apt-get install -y \
    curl \
    ca-certificates \
    zstd \
    && rm -rf /var/lib/apt/lists/*

# `curl … | sh` as root executes whatever the vendor serves at build time, with no
# version, checksum or signature, and the script itself is what fetches the binary.
# Download a pinned release archive and verify its digest before anything is executed.
# The checksum below is the value published in that release's sha256sum.txt; bump
# OLLAMA_VERSION and OLLAMA_SHA256 together and re-read the digest from the release.
ARG OLLAMA_VERSION=0.33.2
ARG OLLAMA_SHA256=9785247dea264d9072f09f6c9c0eb4b8e666892826a3d8388eba3e8fb9ed1db9
RUN curl -fsSL -o /tmp/ollama.tar.zst \
      "https://github.com/ollama/ollama/releases/download/v${OLLAMA_VERSION}/ollama-linux-amd64.tar.zst" \
 && echo "${OLLAMA_SHA256}  /tmp/ollama.tar.zst" | sha256sum -c - \
 && tar -C /usr --zstd -xf /tmp/ollama.tar.zst \
 && rm /tmp/ollama.tar.zst

RUN useradd -m -u 1000 user
USER user

ENV PATH="/home/user/.local/bin:$PATH"
WORKDIR /app

COPY --chown=user requirements.txt .
RUN pip install --no-cache-dir --upgrade -r requirements.txt

COPY --chown=user . .

USER root
COPY <<EOF /app/start.sh
#!/bin/bash
set -e

ollama serve &
echo "Waiting for Ollama..."
until curl -s http://localhost:11434 >/dev/null; do
  sleep 1
done

ollama pull qwen3:4b-instruct-2507-q4_K_M || echo "WARNING: model pull failed; pull manually inside the container"

cd project
python app.py
EOF

RUN chmod +x /app/start.sh && chown user:user /app/start.sh

USER user
EXPOSE 7860
CMD ["/app/start.sh"]