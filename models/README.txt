Drop your workspace-merge model here.

Copy the gemma GGUF you trained into this folder on the machine running
Docker (the ProDesk), for example:

    copy "C:\Users\Isaac\Desktop\benchmarked_ggufs\google_gemma-2-2b-it_gguf\gemma-2-2b-it.Q4_K_M.gguf" C:\Modellink\models\

The "merge-ollama" container imports the first *.gguf it finds here as the
merge model (gemma-merge:2b) on first start and serves it to the app.

Notes:
  - *.gguf files in this folder are git-ignored and never uploaded.
  - Only the first *.gguf is used; keep one file here.
  - After copying a new .gguf, recreate the container so it re-imports:
        docker compose up -d --force-recreate merge-ollama
