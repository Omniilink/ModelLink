FROM __GGUF__
TEMPLATE """<start_of_turn>user
{{ if .System }}{{ .System }} {{ end }}{{ .Prompt }}<end_of_turn>
<start_of_turn>model
{{ .Response }}<end_of_turn>
"""
# Merge-tuned sampling: deterministic, no creative drift. The server also
# sends temperature 0.1 / num_ctx per request; these are the model defaults.
PARAMETER temperature 0.1
PARAMETER repeat_penalty 1
PARAMETER stop "<start_of_turn>"
PARAMETER stop "<end_of_turn>"
PARAMETER penalize_newline false
PARAMETER num_ctx 16384
