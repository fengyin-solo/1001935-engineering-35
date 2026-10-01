"""数据仓库：示例数据持久化在本地 JSON 文件，服务重启后仍然可用。

真实项目里这里会换成数据库访问层；当前实现只依赖标准库，保证克隆下来就能起。
首次启动（或数据文件缺失）时自动用示例数据初始化并落盘；运行期的登记与状态流转
由主程序在写请求后统一落盘（见 app.main 的持久化中间件）。
"""
from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from app.seed import SEED_ROWS

DEFAULT_STORE_PATH = Path(__file__).resolve().parent.parent / "data" / "store.json"


def default_store_path() -> Path:
    """数据文件位置：默认 backend/data/store.json，可用 WINDFARM_STORE_PATH 覆盖。"""
    return Path(os.environ.get("WINDFARM_STORE_PATH", str(DEFAULT_STORE_PATH)))


class Store:
    def __init__(self, path: Path | None = None) -> None:
        self._path = path or default_store_path()
        tables = self._read()
        if tables is None:
            # 数据文件缺失或已损坏：用示例数据重建并落盘，保证服务起得来、概览不是零
            tables = {name: [dict(row) for row in rows] for name, rows in SEED_ROWS.items()}
            self._tables = tables
            self.save()
        else:
            self._tables = tables

    @property
    def data_path(self) -> Path:
        return self._path

    def _read(self) -> dict[str, list[dict[str, Any]]] | None:
        if not self._path.exists():
            return None
        try:
            payload = json.loads(self._path.read_text(encoding="utf-8"))
            tables = payload.get("tables")
            if not isinstance(tables, dict):
                raise ValueError("数据文件缺少 tables 字段")
        except (OSError, json.JSONDecodeError, ValueError):
            # 损坏的文件留档备份后再重建，避免反复覆盖后无从排查
            backup = self._path.with_name(
                f"{self._path.stem}.broken-{datetime.now(timezone.utc):%Y%m%d%H%M%S}{self._path.suffix}"
            )
            try:
                self._path.replace(backup)
            except OSError:
                pass
            return None
        # 以种子模块为准补齐缺失的模块表；文件里多出的模块（历史遗留）原样保留
        merged = {name: [dict(row) for row in tables.get(name, [])] for name in SEED_ROWS}
        for name, rows in tables.items():
            if name not in merged and isinstance(rows, list):
                merged[name] = rows
        return merged

    def save(self) -> None:
        """把当前数据原子写回数据文件：先写临时文件再替换，避免写一半损坏。"""
        self._path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "updated_at": datetime.now(timezone.utc).isoformat(),
            "tables": self._tables,
        }
        tmp = self._path.with_name(f"{self._path.name}.tmp")
        tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(self._path)

    def seed(self) -> dict[str, int]:
        """把示例数据幂等灌入仓库：按模块内 id 去重，反复执行不会产生重复记录。

        返回各模块本次新增的条数；有新增时自动落盘。
        """
        added: dict[str, int] = {}
        for name, seed_rows in SEED_ROWS.items():
            table = self._tables.setdefault(name, [])
            existing_ids = {int(row.get("id", 0)) for row in table}
            count = 0
            for row in seed_rows:
                row_id = int(row.get("id", 0))
                if row_id not in existing_ids:
                    table.append(dict(row))
                    existing_ids.add(row_id)
                    count += 1
            added[name] = count
        if any(added.values()):
            self.save()
        return added

    def module_names(self) -> list[str]:
        return sorted(self._tables)

    def rows(self, module: str) -> list[dict[str, Any]]:
        return self._tables.setdefault(module, [])

    def find(self, module: str, entry_id: int) -> dict[str, Any] | None:
        for row in self.rows(module):
            if int(row.get("id", 0)) == entry_id:
                return row
        return None

    def overview(self) -> dict[str, object]:
        modules: list[dict[str, object]] = []
        for name in self.module_names():
            rows = self.rows(name)
            modules.append({
                "name": name,
                "created": len(rows),
                "pending": sum(1 for row in rows if row.get("pending")),
                "abnormal": sum(1 for row in rows if row.get("abnormal")),
            })
        cards = [
            {"label": "业务模块", "value": len(modules)},
            {"label": "今日新增", "value": sum(int(item["created"]) for item in modules)},
            {"label": "待处理", "value": sum(int(item["pending"]) for item in modules)},
            {"label": "异常量", "value": sum(int(item["abnormal"]) for item in modules)},
        ]
        return {"cards": cards, "modules": modules}


store = Store()
