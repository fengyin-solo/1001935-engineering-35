.PHONY: install backend frontend devflow seed health check

install:
	cd backend && python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
	cd frontend && npm install

backend:
	cd backend && ./run.sh

frontend:
	cd frontend && npm run dev

# 一键流程：示例数据初始化 → 健康检查 → 业务模块自检（失败可断点续跑）
devflow:
	cd backend && ./devflow.sh up

seed:
	cd backend && ./devflow.sh seed

health:
	cd backend && ./devflow.sh health

check:
	cd backend && ./devflow.sh check
