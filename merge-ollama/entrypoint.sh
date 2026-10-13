#!/bin/sh
# OmniLink merge-model container entrypoint.
#
# Starts Ollama, imports the first *.gguf found in /models as the workspace
# merge model (once), then serves. If no GGUF is present it still serves, and
# the app simply reports "model not installed".
set -e
MODEL="${OMNIILINK_MERGE_MODEL:-gemma-merge:2b}"

echo "[merge-ollama] starting ollama (merge model: $MODEL)"
ollama serve &
SERVE_PID=$!

# Wait for the API to answer (up to ~60s).
i=0
while ! ollama list >/dev/null 2>&1; do
  i=$((i + 1))
  [ "$i" -ge 60 ] && break
  sleep 1
done

GGUF=$(ls /models/*.gguf 2>/dev/null | head -n 1 || true)
if [ -z "$GGUF" ]; then
  echo "[merge-ollama] No .gguf in /models — serving with no merge model."
  echo "[merge-ollama] Put your gemma GGUF in the host './models' folder and restart this container."
elif ollama list 2>/dev/null | awk '{print $1}' | grep -qx "$MODEL"; then
  echo "[merge-ollama] $MODEL already imported."
else
  echo "[merge-ollama] importing $GGUF as $MODEL ..."
  sed "s|__GGUF__|$GGUF|" /merge/Modelfile.tpl > /tmp/Modelfile
  if ollama create "$MODEL" -f /tmp/Modelfile; then
    echo "[merge-ollama] import complete."
  else
    echo "[merge-ollama] import FAILED — see the ollama errors above."
  fi
fi

# Keep the server in the foreground so the container stays alive.
wait $SERVE_PID
