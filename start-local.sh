#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")"
if [[ ! -x .venv/bin/python ]]; then
  echo '缺少 Python 环境，请按 README.md 完成安装。'; exit 1
fi
requested_host="${HOST:-}"
if [[ -f variables.env ]]; then
  set -a; source ./variables.env; set +a
fi
host="${requested_host:-${HOST:-0.0.0.0}}"
if [[ "${1:-}" == '--dev' ]]; then
  if [[ ! -f frontend/node_modules/vite/bin/vite.js ]]; then
    echo '请先执行 cd frontend && npm install'; exit 1
  fi
  backend_pid=''; frontend_pid=''
  cleanup() {
    trap - EXIT INT TERM
    [[ -z "$backend_pid" ]] || kill "$backend_pid" 2>/dev/null || true
    [[ -z "$frontend_pid" ]] || kill "$frontend_pid" 2>/dev/null || true
  }
  trap cleanup EXIT INT TERM
  mkdir -p logs
  .venv/bin/python -m uvicorn app:app --app-dir backend --host "$host" --port 8080 >logs/backend.log 2>&1 &
  backend_pid=$!
  (cd frontend && exec node node_modules/vite/bin/vite.js --host "$host" --port 5173 --strictPort) >logs/frontend.log 2>&1 &
  frontend_pid=$!
  echo '开发网页：http://localhost:5173 · 日志：logs/'
  while kill -0 "$backend_pid" 2>/dev/null && kill -0 "$frontend_pid" 2>/dev/null; do sleep 2; done
  echo '服务已停止，请检查 logs/'; exit 1
fi
if [[ ! -f frontend/dist/index.html ]]; then
  echo '请先执行 cd frontend && npm install && npm run build'; exit 1
fi
echo 'Video Assistant：http://localhost:8080'
echo '按 Ctrl+C 停止服务。'
exec .venv/bin/python -m uvicorn app:app --app-dir backend --host "$host" --port 8080
