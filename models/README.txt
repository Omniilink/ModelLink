Drop your workspace-merge model here.

Copy the gemma GGUF you trained into this folder on the machine running
Docker (the ProDesk), for example:

    copy "C:\Users\Isaac\Desktop\benchmarked_ggufs\google_gemma-2-2b-it_gguf\gemma-2-2b-it.Q4_K_M.gguf" C:\Modellink\models\

The "merge-ollama" container imports EVERY *.gguf it finds here (one Ollama
model per file, named from the filename, e.g. qwen2.5-coder-3b-instruct.q4_k_m)
on start and serves them to the app. Pick the active one in Admin → Workspace
merge AI. Only one model is loaded at a time: switching models loads the new
one and unloads the old one automatically.

Notes:
  - *.gguf files in this folder are git-ignored and never uploaded.
  - After adding .gguf files, restart so they are imported:
        docker compose restart merge-ollama
  - Watch the import: docker compose logs merge-ollama --tail 30
  - List what is installed: docker compose exec merge-ollama ollama list
