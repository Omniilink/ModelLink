# Per-command execution sandbox: a fresh ephemeral Linux container.
# Only the target project folder is mounted (at /work). No host paths,
# no credentials, no source code are visible inside.
# Build: docker build -f sandbox.Dockerfile -t omniilink-sandbox:latest .
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PYENV_ROOT=/opt/pyenv \
    PATH="/opt/pyenv/shims:/opt/pyenv/bin:$PATH"

RUN apt-get update \
 && apt-get install -y --no-install-recommends \
      nodejs npm git curl ca-certificates \
      make build-essential libssl-dev zlib1g-dev libbz2-dev \
      libreadline-dev libsqlite3-dev libncursesw5-dev xz-utils \
      libxml2-dev libxmlsec1-dev libffi-dev liblzma-dev \
 && rm -rf /var/lib/apt/lists/* \
 && ln -sf /usr/bin/python3 /usr/bin/python \
 && npm install -g n \
 && git clone --depth 1 https://github.com/pyenv/pyenv.git /opt/pyenv \
 && /opt/pyenv/bin/pyenv --version \
 && node --version \
 && useradd -m -u 1000 coder || true

WORKDIR /work

CMD ["sh"]
