# Per-command execution sandbox: a fresh ephemeral Linux container.
# Only the target project folder is mounted (at /work). No host paths,
# no credentials, no source code are visible inside.
# Build: docker build -f sandbox.Dockerfile -t omniilink-sandbox:latest .
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

RUN apt-get update \
 && apt-get install -y --no-install-recommends nodejs npm \
 && rm -rf /var/lib/apt/lists/* \
 && ln -sf /usr/bin/python3 /usr/bin/python \
 && useradd -m -u 1000 coder || true

WORKDIR /work

CMD ["sh"]
