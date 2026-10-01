"""数据仓库：启动时把示例数据落到 SQLite 文件，之后以文件为准。

设计目标：
- 只依赖标准库，克隆下来就能起；
- 示例数据幂等初始化（SEED_VERSION 不变、记录已在库里就不会重复写入）；
- 服务重启后数据仍在（默认落在 backend/data/ops_demo.sqlite3，可用
  环境变量 APP_DATA_DIR 覆盖目录）。

真实项目里这里会换成数据库访问层；当前保留与内存版一致的 rows/find/overview
读取接口，services 层的登记/状态流转改为调用 add_row/save_row 落盘。
"""
from __future__ import annotations

import json
import os
import sqlite3
from pathlib import Path
from typing import Any

from app.seed import SEED_ROWS, SEED_VERSION

_DEFAULT_DB_PATH = Path(__file__).resolve().parent.parent / "data" / "ops_demo.sqlite3"


def default_db_path() -> Path:
    """数据文件位置：APP_DATA_DIR 指定目录时落到该目录，否则落到 backend/data/。"""
    data_dir = os.environ.get("APP_DATA_DIR")
    if data_dir:
        return Path(data_dir) / "ops_demo.sqlite3"
    return _DEFAULT_DB_PATH


class Store:
    """以 SQLite 文件为底座的仓库；首次实例化时补齐示例数据。

    auto_seed=False 时只连库不灌数，供初始化命令做幂等/续跑控制。
    """

    def __init__(self, db_path: str | os.PathLike[str] | None = None, *, auto_seed: bool = True) -> None:
        self.db_path = Path(db_path) if db_path is not None else default_db_path()
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        # FastAPI 把同步接口放到工作线程执行，连接在 import 时的主线程创建，
        # 故关闭同线程校验；访问均为短事务，模块级单例配合串行请求足够安全。
        self._conn = sqlite3.connect(str(self.db_path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._init_schema()
        if auto_seed:
            self.seed_all()

    def _init_schema(self) -> None:
        self._conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS seed_meta (
                key   TEXT PRIMARY KEY,
                value TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS module_rows (
                module     TEXT NOT NULL,
                entry_id   INTEGER NOT NULL,
                data       TEXT NOT NULL,
                PRIMARY KEY (module, entry_id)
            );
            """
        )
        self._conn.commit()

    # ----- 示例数据初始化（幂等） -----

    def seeded_version(self) -> str | None:
        row = self._conn.execute(
            "SELECT value FROM seed_meta WHERE key = 'seed_version'"
        ).fetchone()
        return str(row["value"]) if row else None

    def is_seeded(self) -> bool:
        return self.seeded_version() == SEED_VERSION

    def seed_module(self, module: str) -> int:
        """初始化单个模块的示例数据；已有记录的模块跳过，返回新增条数。"""
        if module not in SEED_ROWS:
            raise KeyError(module)
        existing = {
            int(row["entry_id"])
            for row in self._conn.execute(
                "SELECT entry_id FROM module_rows WHERE module = ?", (module,)
            ).fetchall()
        }
        inserted = 0
        for row in SEED_ROWS[module]:
            entry_id = int(row["id"])
            if entry_id in existing:
                continue
            self._conn.execute(
                "INSERT OR IGNORE INTO module_rows (module, entry_id, data) VALUES (?, ?, ?)",
                (module, entry_id, json.dumps(dict(row), ensure_ascii=False)),
            )
            inserted += 1
        self._conn.commit()
        return inserted

    def seed_all(self) -> dict[str, int]:
        """初始化全部模块的示例数据。

        幂等保证：(module, entry_id) 为主键 + 已存在即跳过，重复执行不会产生
        重复记录；按模块逐个提交，中途失败可从缺数据的模块接着跑。
        """
        result: dict[str, int] = {}
        for module in sorted(SEED_ROWS):
            result[module] = self.seed_module(module)
        self._conn.execute(
            "INSERT INTO seed_meta (key, value) VALUES ('seed_version', ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (SEED_VERSION,),
        )
        self._conn.commit()
        return result

    # ----- 查询接口（与原内存版一致） -----

    def module_names(self) -> list[str]:
        rows = self._conn.execute(
            "SELECT DISTINCT module FROM module_rows ORDER BY module"
        ).fetchall()
        return [str(row["module"]) for row in rows]

    def rows(self, module: str) -> list[dict[str, Any]]:
        """读取模块全量记录，按 id 排序；空模块返回空列表。"""
        db_rows = self._conn.execute(
            "SELECT data FROM module_rows WHERE module = ? ORDER BY entry_id", (module,)
        ).fetchall()
        return [json.loads(str(row["data"])) for row in db_rows]

    def find(self, module: str, entry_id: int) -> dict[str, Any] | None:
        row = self._conn.execute(
            "SELECT data FROM module_rows WHERE module = ? AND entry_id = ?",
            (module, entry_id),
        ).fetchone()
        return json.loads(str(row["data"])) if row else None

    def add_row(self, module: str, row: dict[str, Any]) -> None:
        """新增一条业务记录（services 层登记入口时调用）。"""
        self._conn.execute(
            "INSERT OR REPLACE INTO module_rows (module, entry_id, data) VALUES (?, ?, ?)",
            (module, int(row["id"]), json.dumps(row, ensure_ascii=False)),
        )
        self._conn.commit()

    def save_row(self, module: str, row: dict[str, Any]) -> None:
        """把对单条记录的状态流转改动落盘（services 层执行动作后调用）。"""
        self._conn.execute(
            "UPDATE module_rows SET data = ? WHERE module = ? AND entry_id = ?",
            (json.dumps(row, ensure_ascii=False), module, int(row["id"])),
        )
        self._conn.commit()

    def overview(self) -> dict[str, object]:
        modules: list[dict[str, object]] = []
        for name in self.module_names():
            rows = self.rows(name)
            modules.append(
                {
                    "name": name,
                    "created": len(rows),
                    "pending": sum(1 for row in rows if row.get("pending")),
                    "abnormal": sum(1 for row in rows if row.get("abnormal")),
                }
            )
        cards = [
            {"label": "业务模块", "value": len(modules)},
            {"label": "今日新增", "value": sum(int(item["created"]) for item in modules)},
            {"label": "待处理", "value": sum(int(item["pending"]) for item in modules)},
            {"label": "异常量", "value": sum(int(item["abnormal"]) for item in modules)},
        ]
        return {"cards": cards, "modules": modules}


# 模块级单例：服务进程启动即自动把示例数据准备好；
# devflow 的 seed 命令会先设 APP_AUTO_SEED=0，由自己逐模块灌库并统计新增数。
store = Store(auto_seed=os.environ.get("APP_AUTO_SEED", "1") == "1")
