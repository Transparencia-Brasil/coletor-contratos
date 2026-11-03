#!/usr/bin/env bash
# Recebe os mesmos argumentos de coleta_contratos.py, de qualquer diretório.
set -euo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
REPO_DIR="$(cd -- "$SCRIPT_DIR/../.." && pwd)"

# Prefere o Python do .venv do repositório, que tem as dependências instaladas.
if [ -x "$REPO_DIR/.venv/bin/python" ]; then
  PYTHON="$REPO_DIR/.venv/bin/python"
elif [ -x "$REPO_DIR/.venv/Scripts/python.exe" ]; then
  PYTHON="$REPO_DIR/.venv/Scripts/python.exe"
elif command -v python3 >/dev/null 2>&1; then
  PYTHON="python3"
else
  PYTHON="python"
fi

SCRIPT="$SCRIPT_DIR/coleta_contratos.py"
# No WSL, o Python do Windows (.exe) não abre caminhos /mnt/c/...; converte para C:\...
if [[ "$PYTHON" == *.exe ]] && command -v wslpath >/dev/null 2>&1; then
  SCRIPT="$(wslpath -w "$SCRIPT")"
fi

exec "$PYTHON" "$SCRIPT" "$@"
