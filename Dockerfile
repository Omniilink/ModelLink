# Omniilink API server image (Linux).
# Build:  docker build -f Dockerfile -t omniilink:latest .
# Run:    see docker-compose.yml (preferred) or:
#   docker run --rm -p 8080:8080 \
#     -v ./user_data:/app/user_data -v ./omnilink.db:/app/omnilink.db \
#     omniilink:latest
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Headless Chromium for the Trade Me valuation scraper (category pages
# render listings client-side; plain HTTP returns an empty app shell).
RUN playwright install --with-deps chromium

# docker CLI for the optional execution sandbox (sibling containers).
# The daemon socket is mounted at runtime, not baked in.
COPY --from=docker:28-cli /usr/local/bin/docker /usr/local/bin/docker

COPY modellink/ ./modellink/
COPY web/ ./web/
COPY components/ ./components/
COPY companion/ ./companion/
COPY run-server.py ./

# Writable at runtime via volumes (see compose): user_data/, omnilink.db,
# network_usage.json, modellink/server.log, tunnel.log
RUN mkdir -p user_data modellink && touch omnilink.db network_usage.json bw_usage.json tunnel.log modellink/server.log

EXPOSE 8080

# Baked at build time (update.bat passes --build-arg via compose args).
# Lets /api/health report the exact commit without git in the image.
ARG GIT_SHA=unknown
ENV OMNIILINK_BUILD=${GIT_SHA}

CMD ["python", "-m", "uvicorn", "modellink.api_server:app", \
     "--host", "0.0.0.0", "--port", "8080", \
     "--no-server-header", "--no-date-header"]
