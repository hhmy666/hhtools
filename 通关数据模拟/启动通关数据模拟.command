#!/bin/zsh
set -eu

tool_dir="${0:A:h}"
cd "$tool_dir"
for python_bin in "${TILE_SIM_PYTHON:-}" /opt/homebrew/opt/python@3.13/libexec/bin/python3 /opt/homebrew/bin/python3 /usr/local/bin/python3 python3; do
  [[ -n "$python_bin" ]] || continue
  if command -v "$python_bin" >/dev/null 2>&1 &&
      "$python_bin" -c 'import sys; raise SystemExit(sys.version_info < (3, 11))' >/dev/null 2>&1; then
    export PYTHONDONTWRITEBYTECODE=1
    if [[ -x /usr/bin/caffeinate ]]; then
      exec /usr/bin/caffeinate -i "$python_bin" "$tool_dir/scripts/app.py" --open
    fi
    exec "$python_bin" "$tool_dir/scripts/app.py" --open
  fi
done
printf '%s\n' '通关数据模拟需要 Python 3.11 或更新版本。安装后请重新打开启动器。'
exit 1
