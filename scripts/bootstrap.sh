#!/bin/zsh
set -euo pipefail
cd "${0:A:h:h}"
mkdir -p .state .tools
export UV_CACHE_DIR="$PWD/.state/uv-cache"
export UV_PYTHON_INSTALL_DIR="$PWD/.runtime"
if [[ ! -x .tools/bin/uv ]]; then
  python3 -m pip install --target .tools uv
fi
.tools/bin/uv python install 3.11 --no-bin
PYTHON_PAS="$(.tools/bin/uv python find --managed-python 3.11)"
.tools/bin/uv venv --python "$PYTHON_PAS" .venv
.tools/bin/uv pip sync --python .venv/bin/python requirements.lock.txt
.venv/bin/python scripts/manage_db.py start
.venv/bin/python scripts/manage_db.py migrate
.venv/bin/python scripts/setup_superset.py
echo 'Окружение готово. Выполните первую загрузку: .venv/bin/python -m src.pipeline --historical'
