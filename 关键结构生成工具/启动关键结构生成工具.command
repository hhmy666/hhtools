#!/bin/zsh

set -eu

tool_root="${0:A:h}"
cd "$tool_root"

python_bin=""
for candidate in python3 python; do
  if command -v "$candidate" >/dev/null 2>&1 &&
     "$candidate" -c 'import sys; raise SystemExit(sys.version_info < (3, 10))' >/dev/null 2>&1; then
    python_bin="$candidate"
    break
  fi
done

if [[ -z "$python_bin" ]]; then
  echo "未找到 Python 3。请先安装 Python 3.10 或更高版本。"
  read -r "?按回车关闭…"
  exit 1
fi

export PYTHONDONTWRITEBYTECODE=1
echo "正在启动关键结构生成工具…"
echo "此窗口保持打开即可；关闭窗口会停止本地工具。"
echo "生成任务期间会阻止系统自动睡眠，屏幕可以正常熄灭；请勿手动睡眠或合盖。"
exec "$python_bin" "$tool_root/scripts/app.py" \
  --settings "$tool_root/运行数据/settings.json" \
  --open
