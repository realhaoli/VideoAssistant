#!/usr/bin/env bash
set -euo pipefail
root="$(cd "$(dirname "$0")" && pwd)"
if [[ -x "$root/.venv/bin/python" ]]; then
  exec "$root/.venv/bin/python" "$root/start-local.py" "$@"
fi
echo '缺少 Python 环境，请按 README.md 完成安装。'
exit 1
