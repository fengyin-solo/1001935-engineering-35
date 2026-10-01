"""风电场机组运维平台 后端服务入口。

启动：uvicorn app.main:app --host 127.0.0.1 --port 8000
健康检查：GET /api/health
模块自检：python -m app.devflow check（或 devflow.sh 一键流程）
"""
from __future__ import annotations

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.config import settings
from app.routers import ROUTERS
from app.seed import SEED_VERSION
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


@app.get("/api/health")
def health() -> dict[str, object]:
    """健康检查：确认服务已经监听、示例数据已经就绪。

    原有字段 ok/app/modules 的口径保持不变（自检结论按该口径留档）；
    新增的 seeded/pending/abnormal 等明细只供自检直接核对，不影响老调用方。
    """
    overview = store.overview()
    cards = {str(card["label"]): int(card["value"]) for card in overview["cards"]}  # type: ignore[arg-type]
    return {
        "ok": True,
        "app": settings.app_name,
        "modules": len(store.module_names()),
        "seeded": store.is_seeded(),
        "seed_version": store.seeded_version() or SEED_VERSION,
        "pending": cards.get("待处理", 0),
        "abnormal": cards.get("异常量", 0),
        "created": cards.get("今日新增", 0),
    }


@app.get("/api/overview")
def overview() -> dict[str, object]:
    """运营概览：把各业务模块的待处理量汇总成看板卡片。"""
    return store.overview()
