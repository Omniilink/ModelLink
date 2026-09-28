FROM ghcr.io/imputnet/cobalt:latest
USER root
WORKDIR /tmp/yt
RUN npm init -y && npm i youtubei.js@18.1.0
RUN mkdir -p /app/node_modules/.pnpm/youtubei.js@18.1.0/node_modules \
    && cp -r /tmp/yt/node_modules/youtubei.js /app/node_modules/.pnpm/youtubei.js@18.1.0/node_modules/ \
    && for d in /tmp/yt/node_modules/*; do b=$(basename "$d"); if [ "$b" != "youtubei.js" ]; then cp -r "$d" /app/node_modules/.pnpm/youtubei.js@18.1.0/node_modules/; fi; done \
    && ln -sfn /app/node_modules/.pnpm/youtubei.js@18.1.0/node_modules/youtubei.js /app/node_modules/youtubei.js
RUN cd /app && node --input-type=module -e "import('youtubei.js').then(m=>console.log('VOS='+m.Constants.SUPPORTED_CLIENTS.includes('VISIONOS')))"
COPY internal.js /app/src/stream/internal.js
RUN node --check /app/src/stream/internal.js
WORKDIR /app
