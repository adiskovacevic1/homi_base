# voice-bot: Discord voice in/out. Node runs the Discord side; a Python sidecar does speech-to-text (faster-whisper);
# Piper does text-to-speech. Everything local — no speech APIs, no accounts.
FROM node:22-bookworm-slim

ENV DEBIAN_FRONTEND=noninteractive PIP_BREAK_SYSTEM_PACKAGES=1 PYTHONUNBUFFERED=1 \
    HF_HOME=/models/hf WHISPER_MODEL=base.en PIPER_VOICE=en_US-lessac-medium
RUN apt-get update && apt-get install -y --no-install-recommends \
        ca-certificates curl ffmpeg python3 python3-pip tini xz-utils \
        build-essential python3-dev \
    && rm -rf /var/lib/apt/lists/*

# --- speech to text: faster-whisper (CTranslate2, CPU), model pre-downloaded into the image
RUN pip3 install --no-cache-dir faster-whisper==1.1.1 requests \
    && python3 -c "from faster_whisper import WhisperModel; WhisperModel('${WHISPER_MODEL}', device='cpu', compute_type='int8')"

# --- text to speech: Piper binary + one voice
RUN mkdir -p /opt/piper /models/piper \
    && curl -fsSL https://github.com/rhasspy/piper/releases/download/2023.11.14-2/piper_linux_x86_64.tar.gz | tar -xz -C /opt \
    && curl -fsSL -o /models/piper/${PIPER_VOICE}.onnx      https://huggingface.co/rhasspy/piper-voices/resolve/main/en/en_US/lessac/medium/en_US-lessac-medium.onnx \
    && curl -fsSL -o /models/piper/${PIPER_VOICE}.onnx.json https://huggingface.co/rhasspy/piper-voices/resolve/main/en/en_US/lessac/medium/en_US-lessac-medium.onnx.json \
    && ln -s /opt/piper/piper /usr/local/bin/piper && piper --version

WORKDIR /app
COPY package.json ./
RUN npm install --omit=dev && npm cache clean --force
COPY index.js stt_server.py ./
RUN mkdir -p /data && useradd -r -u 10002 vbot && chown -R vbot /data /app /models
USER vbot
ENTRYPOINT ["/usr/bin/tini", "--"]
CMD ["node", "index.js"]
