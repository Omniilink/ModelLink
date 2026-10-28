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

# Runtime shared libraries for the headless Godot editor (lazy-downloaded on
# first game export into user_data/.godot — the binary itself and its ~1.2GB
# templates are NOT baked in). The editor runs --headless but still resolves
# its X11/GL/audio libs at load; /api/coding/godot/status reports any gap.
RUN apt-get update \
 && apt-get install -y --no-install-recommends \
      libx11-6 libxcursor1 libxinerama1 libxrandr2 libxi6 libgl1 \
      libasound2 libpulse0 libudev1 libwayland-client0 libxkbcommon0 \
      libdbus-1-3 libfontconfig1 \
 && rm -rf /var/lib/apt/lists/*

# docker CLI for the optional execution sandbox (sibling containers).
# The daemon socket is mounted at runtime, not baked in.
COPY --from=docker:28-cli /usr/local/bin/docker /usr/local/bin/docker

# The website frontend lives on the `website` branch and is deployed
# separately. Mount or copy it to /app/web if this container should also
# serve the UI; the API itself does not require it.
COPY modellink/ ./modellink/
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
