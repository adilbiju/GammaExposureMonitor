#!/usr/bin/env bash
set -euo pipefail

PROJECT_DIR="$(cd "$(dirname "$0")" && pwd)"
PYTHON_BIN="$PROJECT_DIR/.venv/bin/python"

if [[ ! -x "$PYTHON_BIN" ]]; then
  echo "Backend environment missing. Run: python3 -m venv .venv && .venv/bin/pip install -r backend/requirements.txt"
  exit 1
fi

if [[ ! -d "$PROJECT_DIR/frontend/node_modules" ]]; then
  echo "Frontend dependencies missing. Run: cd frontend && pnpm install"
  exit 1
fi

cleanup() {
  kill "$BACKEND_PID" "$FRONTEND_PID" 2>/dev/null || true
}
trap cleanup EXIT INT TERM

cd "$PROJECT_DIR"
"$PYTHON_BIN" -m uvicorn app.main:app --app-dir backend --host 127.0.0.1 --port 8000 &
BACKEND_PID=$!

cd "$PROJECT_DIR/frontend"
./node_modules/.bin/vite --host 127.0.0.1 --port 5173 &
FRONTEND_PID=$!

echo "GEX Lab is starting at http://127.0.0.1:5173"
wait "$BACKEND_PID" "$FRONTEND_PID"

