.PHONY: install backend frontend pipeline

install:
	cd backend && python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
	cd frontend && npm install

backend:
	cd backend && ./run.sh

frontend:
	cd frontend && npm run dev

# 一条命令跑完：示例数据初始化 → 健康检查 → 模块自检 → 报告留档
pipeline:
	cd backend && ./pipeline.sh
