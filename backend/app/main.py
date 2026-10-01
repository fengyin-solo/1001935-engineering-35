"""风电场机组运维平台 后端服务入口。

启动：uvicorn app.main:app --host 127.0.0.1 --port 8000
健康检查：GET /api/health
一条龙流程（示例数据初始化 + 健康检查 + 模块自检）：./pipeline.sh
"""
from __future__ import annotations

from fastapi import FastAPI, Request, Response
from fastapi.middleware.cors import CORSMiddleware

from app.config import settings
from app.routers import ROUTERS
from app.store import store

app = FastAPI(title="风电场机组运维平台", version="1.0.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.allowed_origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

for module in ROUTERS:
    app.include_router(module.router)


@app.middleware("http")
async def persist_store_after_write(request: Request, call_next) -> Response:
    """写请求结束后把数据落盘，保证运行期的登记与流转在服务重启后仍然可用。"""
    response = await call_next(request)
    if request.method not in ("GET", "HEAD", "OPTIONS"):
        store.save()
    return response


@app.get("/api/health")
def health() -> dict[str, object]:
    """健康检查：确认服务已经监听、示例数据已经就绪。"""
    modules = store.module_names()
    seeded = sum(1 for name in modules if store.rows(name))
    return {
        "ok": True,
        "app": settings.app_name,
        "modules": len(modules),
        "seeded_modules": seeded,
        "data_ready": seeded == len(modules),
    }


@app.post("/api/admin/seed")
def reseed() -> dict[str, object]:
    """幂等灌入示例数据：已存在的记录按 id 跳过，反复调用不会产生重复。"""
    added = store.seed()
    return {"ok": True, "added": added, "total_added": sum(added.values())}


@app.get("/api/overview")
def overview() -> dict[str, object]:
    """运营概览：把各业务模块的待处理量汇总成看板卡片。"""
    return store.overview()
