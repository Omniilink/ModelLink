#!/bin/sh
# OmniLink merge-model container entrypoint.
#
# Starts Ollama, imports EVERY *.gguf found in /models (one Ollama model per
# file, named from the filename) that is not already imported, then serves.
# Only one model is ever loaded at a time (OLLAMA_MAX_LOADED_MODELS=1): when
# the app switches to another model, Ollama loads the new one and unloads the
# old one automatically. If no GGUF is present it still serves, and the app
# simply reports "model not installed".
set -e

# The CPU-only image may ship the binary as /ollama (not on PATH); put it on
# PATH so the rest of this script can just call `ollama`.
if ! command -v ollama >/dev/null 2>&1; then
  for cand in /ollama /usr/bin/ollama /usr/local/bin/ollama; do
    if [ -x "$cand" ]; then
      mkdir -p /usr/local/bin
      ln -sf "$cand" /usr/local/bin/ollama
      PATH="/usr/local/bin:$PATH"
      export PATH
      break
    fi
  done
fi

echo "[merge-ollama] starting ollama"
ollama serve &
SERVE_PID=$!

# Wait for the API to answer (up to ~60s).
i=0
while ! ollama list >/dev/null 2>&1; do
  i=$((i + 1))
  [ "$i" -ge 60 ] && break
  sleep 1
done

count=0
for GGUF in /models/*.gguf; do
  [ -e "$GGUF" ] || continue
  base=$(basename "$GGUF" .gguf)
  # lowercase, keep [a-z0-9._-], turn the rest into single dashes
  name=$(printf '%s' "$base" | tr '[:upper:]' '[:lower:]' | sed 's/[^a-z0-9._-]/-/g; s/-\+/-/g; s/^-\|-$//g' | cut -c1-64)
  [ -z "$name" ] && name="model-$count"
  if ollama list 2>/dev/null | awk '{print $1}' | grep -qx "$name"; then
    echo "[merge-ollama] $name already imported."
  else
    echo "[merge-ollama] importing $GGUF as $name ..."
    sed "s|__GGUF__|$GGUF|" /merge/Modelfile.tpl > /tmp/Modelfile
    if ollama create "$name" -f /tmp/Modelfile; then
      echo "[merge-ollama] $name import complete."
    else
      echo "[merge-ollama] $name import FAILED — see the ollama errors above."
    fi
  fi
  count=$((count+1))
done
if [ "$count" -eq 0 ]; then
  echo "[merge-ollama] No .gguf in /models — serving with no merge model."
  echo "[merge-ollama] Put GGUF files in the host './models' folder and restart this container."
fi

# Keep the server in the foreground so the container stays alive.
wait $SERVE_PID
