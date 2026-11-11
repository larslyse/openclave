# OpenClave in a container, for the API model routes only (Anthropic, xAI keys).
# This image has no Claude or Codex CLI, no voice and no note search (qmd); use
# the normal install (README) for those.
FROM python:3.12-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY . .
ENV OPENCLAVE_HOME=/data \
    PYTHONUNBUFFERED=1
# /data holds openclave.toml and runtime state; mount your vault at /vault and
# set [paths] vault_root = "/vault" there. bind must be 0.0.0.0 in a container.
VOLUME ["/data", "/vault"]
EXPOSE 8777
CMD ["python", "run.py"]
