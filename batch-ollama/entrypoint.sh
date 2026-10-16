#!/bin/sh
# Dedicated batch Ollama: pulls the workspace batch model (qwen2.5-coder:3b by
# default), warms it, and keeps it resident 24/7 (OLLAMA_KEEP_ALIVE=-1).
set -e
MODEL="${OMNIILINK_WS_BATCH_MODEL:-qwen2.5-coder:3b}"

# The CPU-only image may ship the binary as /ollama (not on PATH); put it on PATH.
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

echo "[batch-ollama] starting ollama (model: $MODEL, keep-alive: -1)"
ollama serve &
SERVE_PID=$!

i=0
while ! ollama list >/dev/null 2>&1; do
  i=$((i + 1))
  [ "$i" -ge 60 ] && break
  sleep 1
done

if ollama list 2>/dev/null | awk '{print $1}' | grep -qx "$MODEL"; then
  echo "[batch-ollama] $MODEL already present."
else
  echo "[batch-ollama] pulling $MODEL ..."
  if ! ollama pull "$MODEL"; then
    echo "[batch-ollama] pull FAILED (no internet?). Put a GGUF in ./models and import, or pull manually."
  fi
fi

# Warm it so it loads now and stays resident (KEEP_ALIVE=-1).
echo "[batch-ollama] warming $MODEL ..."
ollama run "$MODEL" "hi" >/dev/null 2>&1 || true
echo "[batch-ollama] $MODEL is resident and ready."

wait $SERVE_PID
