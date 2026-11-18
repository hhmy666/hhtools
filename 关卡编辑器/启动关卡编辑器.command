#!/bin/zsh
set -eu
cd -- "${0:A:h}"
for python_bin in "${LEVEL_EDITOR_PYTHON:-}" /opt/homebrew/opt/python@3.13/libexec/bin/python3 /opt/homebrew/bin/python3 /usr/local/bin/python3 python3; do
  [[ -n "$python_bin" ]] || continue
  if command -v "$python_bin" >/dev/null 2>&1 && "$python_bin" -c 'import sys; raise SystemExit(sys.version_info < (3, 10))' >/dev/null 2>&1; then
    export PYTHONDONTWRITEBYTECODE=1
    exec "$python_bin" 后端/server.py
  fi
done
printf '%s\n' '需要 Python 3.10 或更高版本；请按「文档/使用说明.md」安装后重新打开启动器。' '按回车关闭…'
read -r
