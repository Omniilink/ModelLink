This folder is no longer used by a dedicated merge-model container
(the workspace merge model was removed). Workspace prompt batching now uses
the server's own Ollama model (OMNIILINK_WS_BATCH_MODEL, default
qwen2.5-coder:3b) — install it on the server's Ollama:

    ollama pull qwen2.5-coder:3b

Files placed here are git-ignored and never uploaded.
