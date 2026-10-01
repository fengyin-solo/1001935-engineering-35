#!/usr/bin/env bash
# 一键流程：示例数据初始化 → 健康检查 → 业务模块自检。
#
# 用法：
#   ./devflow.sh           # 等同 up：灌数据、按需起服务、健康检查、逐模块自检
#   ./devflow.sh up        # 同上（默认）
#   ./devflow.sh seed      # 只初始化示例数据（幂等，可反复执行）
#   ./devflow.sh health    # 只做健康检查（服务需已启动）
#   ./devflow.sh check     # 只做模块自检（服务需已启动）
#
# 任一环失败会点名模块/阶段并以非零码退出；重跑 up 时已完成环节跳过，
# 从断掉的地方接着跑。自检结论留档在 .devflow/archive/。
set -euo pipefail
cd "$(dirname "$0")"

PYTHON=.venv/bin/python
if [ ! -x "$PYTHON" ]; then
  echo "[devflow] 未找到虚拟环境，创建 .venv 并安装依赖..."
  python3 -m venv .venv
  .venv/bin/pip install -q -r requirements.txt
fi

exec "$PYTHON" -m app.devflow "${@:-up}"
