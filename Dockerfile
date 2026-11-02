FROM python:3.12-slim-bookworm

ARG VCS_REF=unknown
LABEL org.opencontainers.image.title="Xiaoya ZipVoice Server" \
      org.opencontainers.image.source="https://github.com/yylcreate/voice_server" \
      org.opencontainers.image.revision=$VCS_REF

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    XIAOYA_VOICE_CONFIG=/run/voice/config.json \
    XIAOYA_VOICE_TARGET=amd-4vcpu-12gb \
    OMP_NUM_THREADS=2 \
    MKL_NUM_THREADS=2 \
    OPENBLAS_NUM_THREADS=1 \
    NUMEXPR_NUM_THREADS=2

# Debian预留voice组，专用账号使用独立名称并保持UID/GID10001。--yyl
# 原 groupadd --gid 10001 voice / useradd --uid 10001 --gid voice ... voice
RUN apt-get update \
 && apt-get install -y --no-install-recommends ffmpeg libgomp1 libstdc++6 ca-certificates \
 && rm -rf /var/lib/apt/lists/* \
 && groupadd --gid 10001 xiaoya-voice \
 && useradd --uid 10001 --gid xiaoya-voice --no-create-home --shell /usr/sbin/nologin xiaoya-voice

WORKDIR /app
COPY requirements.txt ./
RUN python -m pip install --disable-pip-version-check -r requirements.txt \
 && python -c "import fastapi, numpy, onnxruntime, sherpa_onnx"

COPY server.py voice_engine.py voice_profiles.py voice_loudness.py runtime_profiles.py ./
COPY deploy/check_ready.py deploy/check_target.py ./deploy/

USER 10001:10001
EXPOSE 8793
HEALTHCHECK --interval=30s --timeout=5s --start-period=180s --retries=3 \
 CMD python -c "import json,urllib.request; r=json.load(urllib.request.urlopen('http://127.0.0.1:8793/health',timeout=3)); assert r['ready'] and r['runtime']['target']=='amd-4vcpu-12gb'"
CMD ["python", "-m", "uvicorn", "server:app_factory", "--factory", "--host", "0.0.0.0", "--port", "8793", "--workers", "1", "--no-access-log"]
