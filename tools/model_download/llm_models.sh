#!/usr/bin/env bash
set -euo pipefail

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
model_dir="${repo_root}/artifacts/models/llm"
mkdir -p "${model_dir}"

download() {
  local model_name="$1"
  local url="$2"
  local output="$3"
  local target_dir="${model_dir}/${model_name}"
  local final="${target_dir}/${output}"
  local partial="${final}.part"
  mkdir -p "${target_dir}"
  if [[ -s "${final}" ]]; then
    echo "already exists: ${output}"
    return
  fi
  if [[ -e "${final}" ]]; then
    mv "${final}" "${partial}"
  fi
  echo "downloading: ${output}"
  curl --fail --silent --show-error --location --retry 3 --continue-at - \
    "${url}" --output "${partial}"
  mv "${partial}" "${final}"
}

# Official/maintainer GGUF repositories. Q4_K_M is a practical Pi CPU baseline.
download "qwen3-0.6b" "https://huggingface.co/ggml-org/Qwen3-0.6B-GGUF/resolve/main/Qwen3-0.6B-Q4_0.gguf" \
  "Qwen3-0.6B-Q4_0.gguf"
download "qwen3-1.7b" "https://huggingface.co/ggml-org/Qwen3-1.7B-GGUF/resolve/main/Qwen3-1.7B-Q4_K_M.gguf" \
  "Qwen3-1.7B-Q4_K_M.gguf"
download "gemma-3-1b-it" "https://huggingface.co/ggml-org/gemma-3-1b-it-GGUF/resolve/main/gemma-3-1b-it-Q4_K_M.gguf" \
  "gemma-3-1b-it-Q4_K_M.gguf"
download "granite-4.0-350m" "https://huggingface.co/ibm-granite/granite-4.0-350m-GGUF/resolve/main/granite-4.0-350m-Q4_K_M.gguf" \
  "granite-4.0-350m-Q4_K_M.gguf"

echo "models are in: ${model_dir}"
