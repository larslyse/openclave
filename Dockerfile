# OpenClave in a container: the web app on the API routes (xAI, Anthropic).
# The CLI routes (claude, codex), local voice and qmd vault search need those
# tools installed on the host; see INSTALL.md for a full install.
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
