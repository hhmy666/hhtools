#!/bin/zsh

set -u

TOOL_DIR="${0:A:h}"
cd "$TOOL_DIR" || exit 1

PYTHON_BIN=""
for candidate in python3 python; do
  if command -v "$candidate" >/dev/null 2>&1; then
    if "$candidate" -c 'import sys; raise SystemExit(0 if sys.version_info >= (3, 9) else 1)' >/dev/null 2>&1; then
      PYTHON_BIN="$candidate"
      break
    fi
  fi
done

if [[ -z "$PYTHON_BIN" ]]; then
  echo "未找到 Python 3.9 或更高版本。请安装 Python 后重新启动。"
  read "?按回车关闭窗口..."
  exit 1
fi

export PYTHONDONTWRITEBYTECODE=1
exec "$PYTHON_BIN" "$TOOL_DIR/scripts/app.py" --open
