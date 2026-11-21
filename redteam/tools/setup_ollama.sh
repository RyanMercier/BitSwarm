#!/usr/bin/env bash
# Create ollama model variants with a 12k context for the red-team runs.
# ollama's default context (2-4k) truncates the warm-start assignment.
# Run on the GPU server AFTER stopping vLLM (each 70B needs both GPUs).
set -euo pipefail

ABLIT="huihui_ai/llama3.3-abliterated:70b-instruct-q4_K_M"
STOCK="llama3.3:70b"

mk() {  # mk <from-model> <new-name>
  local tmp; tmp=$(mktemp)
  printf 'FROM %s\nPARAMETER num_ctx 12288\n' "$1" > "$tmp"
  ollama create "$2" -f "$tmp"
  rm -f "$tmp"
}

case "${1:-ablit}" in
  ablit)
    mk "$ABLIT" llama-ablit-ctx
    echo 'created llama-ablit-ctx' ;;
  stock)
    ollama pull "$STOCK"
    mk "$STOCK" llama-stock-ctx
    echo 'created llama-stock-ctx' ;;
  *) echo 'usage: setup_ollama.sh ablit|stock' >&2; exit 2 ;;
esac
