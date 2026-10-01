"""一条命令跑完的上线流程：示例数据初始化 → 服务健康检查 → 模块自检 → 报告留档。

用法：
    .venv/bin/python -m app.pipeline [--fresh] [--no-autostart] [--base-url URL]

断点续跑：模块自检的进度写在 data/pipeline-state.json；哪一环失败就指出环节或
模块名，修好以后重跑同一命令，会自动跳过已通过的模块、从断掉的地方继续。
自检报告按时间戳留档在 reports/，报告自带口径版本，历史报告不覆盖。
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from app import selfcheck
from app.store import store

BASE_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = BASE_DIR / "data"
REPORTS_DIR = BASE_DIR / "reports"
STATE_PATH = DATA_DIR / "pipeline-state.json"
SERVER_LOG_PATH = DATA_DIR / "pipeline-server.log"
DEFAULT_BASE_URL = os.environ.get("PIPELINE_BASE_URL", "http://127.0.0.1:8000")

STARTUP_TIMEOUT = 30  # 等待自动拉起的服务就绪的秒数

MODULE_LABELS = {
    "windfarm": "风电场站",
    "turbine": "风电机组",
    "blade": "叶片",
    "gearbox": "齿轮箱",
    "generator": "发电机",
    "pitch": "变桨系统",
    "yaw": "偏航系统",
    "metmast": "测风塔",
    "collector": "集电线路",
    "substation": "升压站",
    "forecast": "功率预测",
    "vibration": "振动监测",
    "defect": "缺陷登记",
    "maintjob": "检修任务",
    "spare": "备件领用",
    "patrol": "巡视检查",
    "accept": "验收确认",
    "settle": "电量结算",
}


class PipelineError(Exception):
    """流程环节失败：step 记录断在哪一环，便于下次从断点继续。"""

    def __init__(self, step: str, message: str) -> None:
        super().__init__(message)
        self.step = step


def label_of(name: str) -> str:
    return f"{MODULE_LABELS.get(name, name)}({name})"


def load_state() -> dict[str, Any]:
    try:
        state = json.loads(STATE_PATH.read_text(encoding="utf-8"))
        if isinstance(state.get("modules"), dict):
            return state
    except (OSError, json.JSONDecodeError):
        pass
    return {"modules": {}}


def save_state(state: dict[str, Any]) -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    state["updated_at"] = datetime.now().isoformat(timespec="seconds")
    STATE_PATH.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")


def clear_state() -> None:
    STATE_PATH.unlink(missing_ok=True)


def archive_report(report: dict[str, Any]) -> Path:
    """自检报告按时间戳留档，历史报告不覆盖；latest.json 始终指向最新一份。"""
    REPORTS_DIR.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    path = REPORTS_DIR / f"selfcheck-{stamp}.json"
    serial = 2
    while path.exists():
        path = REPORTS_DIR / f"selfcheck-{stamp}-{serial}.json"
        serial += 1
    content = json.dumps(report, ensure_ascii=False, indent=2)
    path.write_text(content, encoding="utf-8")
    (REPORTS_DIR / "latest.json").write_text(content, encoding="utf-8")
    return path


def service_running(base_url: str) -> bool:
    try:
        selfcheck.fetch_health(base_url)
    except selfcheck.CheckError:
        return False
    return True


def step_seed(base_url: str) -> None:
    """环节一：示例数据初始化。幂等，反复执行不会产生重复记录。"""
    print("[1/3] 示例数据初始化（幂等，反复执行不会产生重复记录）")
    if service_running(base_url):
        # 服务在跑：让运行中的实例自己灌数据，内存与磁盘同步更新
        result = selfcheck.post_json(base_url, "/api/admin/seed")
        added = result.get("added", {})
        total_added = result.get("total_added", 0)
        print(f"  服务已在运行，已同步运行实例的内存数据（本次新增 {total_added} 条）")
    else:
        # 服务没跑：先灌到数据文件，服务启动时自动加载
        added = store.seed()
        total_added = sum(added.values())
        total_rows = sum(len(store.rows(name)) for name in store.module_names())
        print(f"  数据文件已就绪：{store.data_path}（共 {total_rows} 条，本次新增 {total_added} 条）")
    fresh = {name: count for name, count in added.items() if count}
    for name in sorted(fresh):
        print(f"    {label_of(name)}: +{fresh[name]} 条")
    if not fresh:
        print("    各模块示例数据均已存在，无新增")


def step_health(base_url: str, *, autostart: bool) -> subprocess.Popen[bytes] | None:
    """环节二：服务健康检查。服务没起时按需自动拉起，返回拉起的进程句柄。"""
    print("[2/3] 服务健康检查")
    try:
        health = selfcheck.fetch_health(base_url)
        print(f"  服务已在运行：模块数 {health['modules']}，数据就绪 {health.get('data_ready')}")
        return None
    except selfcheck.CheckError:
        pass
    if not autostart:
        raise PipelineError(
            "health",
            f"服务未在 {base_url} 监听；请先启动（./run.sh），或去掉 --no-autostart 让流程自动拉起",
        )
    parsed = urlparse(base_url)
    host = parsed.hostname or "127.0.0.1"
    port = parsed.port or 80
    print(f"  服务未就绪，正在后台拉起 uvicorn（日志：{SERVER_LOG_PATH}）...")
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    log_file = SERVER_LOG_PATH.open("ab")
    proc = subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "app.main:app", "--host", host, "--port", str(port)],
        cwd=BASE_DIR,
        stdout=log_file,
        stderr=subprocess.STDOUT,
    )
    deadline = time.time() + STARTUP_TIMEOUT
    while time.time() < deadline:
        if proc.poll() is not None:
            raise PipelineError(
                "health",
                f"服务进程启动后退出（code={proc.returncode}），详见 {SERVER_LOG_PATH}",
            )
        try:
            health = selfcheck.fetch_health(base_url)
            print(f"  健康检查通过：模块数 {health['modules']}，数据就绪 {health.get('data_ready')}")
            return proc
        except selfcheck.CheckError:
            time.sleep(0.5)
    proc.terminate()
    raise PipelineError("health", f"等待服务就绪超时（{STARTUP_TIMEOUT}s），详见 {SERVER_LOG_PATH}")


def step_selfcheck(base_url: str, *, fresh: bool) -> tuple[dict[str, Any], str | None]:
    """环节三：逐模块自检。返回 (报告, 失败模块名)；全部通过时失败模块名为 None。"""
    print("[3/3] 模块自检（指标口径与运营概览页一致）")
    overview = selfcheck.fetch_overview(base_url)
    metrics_by_name = {item["name"]: item for item in overview.get("modules", [])}
    state = {"modules": {}} if fresh else load_state()
    done: dict[str, Any] = state.get("modules", {})
    results: list[dict[str, Any]] = []
    for name in sorted(metrics_by_name):
        metrics = metrics_by_name[name]
        cached = done.get(name)
        if cached and cached.get("ok"):
            # 断点续跑：已通过的模块直接采用，指标刷新为本次概览的数字，保证报告与概览页同步
            result = {
                **cached,
                "created": int(metrics.get("created", 0)),
                "pending": int(metrics.get("pending", 0)),
                "abnormal": int(metrics.get("abnormal", 0)),
            }
            results.append(result)
            print(f"  ↷ {label_of(name)}：此前已通过，断点续跑跳过（指标已同步）")
            continue
        result = selfcheck.check_module(base_url, name, metrics)
        results.append(result)
        state.setdefault("modules", {})[name] = result
        save_state(state)
        if result["ok"]:
            print(
                f"  ✓ {label_of(name)}：接口正常，"
                f"今日新增 {result['created']}、待处理 {result['pending']}、异常量 {result['abnormal']}"
            )
        else:
            print(f"  ✗ {label_of(name)}：{result['error']}")
            report = selfcheck.build_report(base_url, overview, results)
            return report, name
    report = selfcheck.build_report(base_url, overview, results)
    return report, None


def print_summary(report: dict[str, Any]) -> None:
    print("\n自检通过，以下指标与运营概览页一致：")
    for card in report.get("cards", []):
        print(f"  {card['label']}: {card['value']}")
    print("  模块明细（今日新增 / 待处理 / 异常量）：")
    for item in report.get("modules", []):
        print(f"    {label_of(item['name'])}: {item['created']} / {item['pending']} / {item['abnormal']}")


def stop_server(proc: subprocess.Popen[bytes]) -> None:
    proc.terminate()
    try:
        proc.wait(timeout=10)
    except subprocess.TimeoutExpired:
        proc.kill()
    print("  流程拉起的临时服务已停止（数据已落盘，重启后仍然可用）")


def parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="app.pipeline",
        description="示例数据初始化 → 服务健康检查 → 模块自检，一条命令跑完；失败可从断点续跑",
    )
    parser.add_argument("--base-url", default=DEFAULT_BASE_URL, help="后端服务地址，默认 %(default)s")
    parser.add_argument("--no-autostart", action="store_true", help="服务未启动时不自动拉起，直接报错")
    parser.add_argument("--fresh", action="store_true", help="忽略断点进度，全部模块重新自检")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    base_url = args.base_url.rstrip("/")
    server_proc: subprocess.Popen[bytes] | None = None
    try:
        step_seed(base_url)
        server_proc = step_health(base_url, autostart=not args.no_autostart)
        report, failed_module = step_selfcheck(base_url, fresh=args.fresh)
        report_path = archive_report(report)
        if failed_module is not None:
            error = next(
                (item["error"] for item in report["modules"] if item["name"] == failed_module),
                "未知原因",
            )
            print(f"\n自检失败：模块 {label_of(failed_module)} —— {error}")
            print(f"进度已记录到 {STATE_PATH}，修复后重跑同一命令将从断点继续")
            print(f"失败报告已留档：{report_path}")
            return 1
        clear_state()
        print_summary(report)
        print(f"\n自检报告已留档：{report_path}")
        return 0
    except PipelineError as exc:
        print(f"\n流程中断于环节 [{exc.step}]：{exc}")
        print("修复后重跑同一命令，会从断掉的环节继续")
        return 1
    except selfcheck.CheckError as exc:
        print(f"\n流程中断：{exc}")
        print("修复后重跑同一命令，会从断掉的环节继续")
        return 1
    finally:
        if server_proc is not None:
            stop_server(server_proc)


if __name__ == "__main__":
    sys.exit(main())
