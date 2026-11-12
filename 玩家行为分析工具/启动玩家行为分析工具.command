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
  echo "未找到 Python 3.9 或更高版本。"
  echo "请先安装 Python，再重新双击本文件。"
  read "?按回车关闭窗口..."
  exit 1
fi

if ! "$PYTHON_BIN" -c 'import pandas, numpy' >/dev/null 2>&1; then
  VENV_DIR="$TOOL_DIR/.venv"
  VENV_PYTHON="$VENV_DIR/bin/python"
  echo "首次启动正在准备分析环境，请保持网络连接。"
  if ! "$VENV_PYTHON" -c 'import pandas, numpy' >/dev/null 2>&1; then
    if ! { [[ -x "$VENV_PYTHON" ]] || "$PYTHON_BIN" -m venv "$VENV_DIR"; } ||
       ! "$VENV_PYTHON" -m pip install -r "$TOOL_DIR/scripts/requirements.txt"; then
      echo "分析依赖安装失败。请检查网络和 Python 安装，再双击启动器重试。"
      read "?按回车关闭窗口..."
      exit 1
    fi
  fi
  PYTHON_BIN="$VENV_PYTHON"
fi

export PYTHONDONTWRITEBYTECODE=1
exec "$PYTHON_BIN" "$TOOL_DIR/scripts/app.py" --open
