# Self-update helper: a throwaway container that runs git + compose against
# the HOST checkout (bind-mounted at /work) with the daemon socket mounted.
# Built by compose (updater-image service) so update.bat always refreshes it.
# The admin panel refuses to self-update until this image exists.
FROM docker:28-cli

RUN apk add --no-cache git docker-cli-compose python3 py3-yaml \
 && git --version \
 && docker compose version \
 && python3 -c "import yaml; print('pyyaml', yaml.__version__)"

COPY updater/omni-update /usr/local/bin/omni-update
RUN chmod +x /usr/local/bin/omni-update

CMD ["omni-update"]
