#!/usr/bin/env bash
# 一条命令跑完：示例数据初始化 → 服务健康检查 → 模块自检 → 报告留档
# 失败时会指出环节或模块名，修复后重跑同一命令从断点继续
set -euo pipefail
cd "$(dirname "$0")"
if [ ! -d .venv ]; then
  python3 -m venv .venv
fi
.venv/bin/pip install -q -r requirements.txt
exec .venv/bin/python -m app.pipeline "$@"
