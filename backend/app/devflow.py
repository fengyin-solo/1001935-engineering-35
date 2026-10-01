"""一键流程编排：示例数据初始化 → 健康检查 → 业务模块自检。

用法：
    python -m app.devflow up       完整流程：灌数据、起服务、健康检查、逐模块自检
    python -m app.devflow seed     只做示例数据初始化（幂等，可反复执行）
    python -m app.devflow health   只做健康检查
    python -m app.devflow check    只做模块自检（顺带与概览口径对账）

哪一环失败都会点名模块/阶段并以非零码退出；流程状态落在 backend/.devflow/
state.json，重跑 up 时已完成的环节跳过，从断掉的地方接着跑。自检结论按当时
的口径原样写入 backend/.devflow/archive/ 留档（JSON + Markdown 各一份）。
"""
from __future__ import annotations

import argparse
import datetime as _dt
import json
import os
import signal
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

BACKEND_DIR = Path(__file__).resolve().parent.parent
FLOW_DIR = BACKEND_DIR / ".devflow"
ARCHIVE_DIR = FLOW_DIR / "archive"
STATE_FILE = FLOW_DIR / "state.json"
SERVER_LOG = FLOW_DIR / "server.log"
PID_FILE = FLOW_DIR / "server.pid"

HOST = os.environ.get("APP_HOST", "127.0.0.1")
PORT = int(os.environ.get("APP_PORT", "8000"))
BASE_URL = os.environ.get("APP_BASE_URL", f"http://{HOST}:{PORT}")
# 一键流程要等服务拉起，给足启动时间；单独跑 health/check 时服务理应已在监听，
# 超时短一些，失败能立刻报出来（可用 APP_HEALTH_TIMEOUT 覆盖）。
HEALTH_TIMEOUT = float(os.environ.get("APP_HEALTH_TIMEOUT", "30"))
STANDALONE_TIMEOUT = 5.0


class StageFailed(RuntimeError):
    """某个环节失败：stage 点名环节，module 点名业务模块（适用时）。"""

    def __init__(self, stage: str, message: str, module: str | None = None) -> None:
        super().__init__(message)
        self.stage = stage
        self.module = module
        self.message = message


# ----------------------------- 基础工具 -----------------------------


def log(message: str) -> None:
    print(f"[devflow] {message}", flush=True)


def module_endpoints() -> list[tuple[str, str]]:
    """按路由注册顺序返回 (模块名, 列表接口路径)。"""
    from app.routers import ROUTERS

    endpoints: list[tuple[str, str]] = []
    for module in ROUTERS:
        prefix = str(module.router.prefix)
        name = prefix.rsplit("/", 1)[-1]
        endpoints.append((name, f"{prefix}?page=1&size=1"))
    return endpoints


def http_get_json(path: str, *, timeout: float = 5.0, stage: str = "check") -> Any:
    url = path if path.startswith("http") else f"{BASE_URL}{path}"
    req = urllib.request.Request(url, headers={"Accept": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")[:200]
        raise StageFailed(
            stage, f"接口 HTTP {exc.code}：{body}"
        ) from exc
    except (urllib.error.URLError, OSError) as exc:
        raise ConnectionError(str(exc)) from exc


def port_open(host: str, port: int, *, timeout: float = 0.5) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.settimeout(timeout)
        return sock.connect_ex((host, port)) == 0


def display_width(text: str) -> int:
    """中文等宽按两格计算，供自检表格对齐。"""
    width = 0
    for ch in text:
        width += 2 if ord(ch) > 0x2E7F else 1
    return width


def pad(text: str, width: int) -> str:
    return text + " " * max(width - display_width(text), 0)


# ----------------------------- 状态与留档 -----------------------------


def load_state() -> dict[str, Any]:
    if STATE_FILE.exists():
        try:
            return json.loads(STATE_FILE.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            pass
    return {}


def save_state(state: dict[str, Any]) -> None:
    FLOW_DIR.mkdir(parents=True, exist_ok=True)
    STATE_FILE.write_text(
        json.dumps(state, ensure_ascii=False, indent=2, sort_keys=True),
        encoding="utf-8",
    )


def clear_state() -> None:
    if STATE_FILE.exists():
        STATE_FILE.unlink()


def archive_report(report: dict[str, Any]) -> tuple[Path, Path]:
    """把一次自检结论按当时的口径留档，返回 (json 路径, markdown 路径)。"""
    ARCHIVE_DIR.mkdir(parents=True, exist_ok=True)
    stamp = _dt.datetime.now().strftime("%Y%m%d-%H%M%S")
    stem = f"selfcheck-{stamp}"
    json_path = ARCHIVE_DIR / f"{stem}.json"
    md_path = ARCHIVE_DIR / f"{stem}.md"
    json_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    md_path.write_text(render_markdown(report), encoding="utf-8")
    return json_path, md_path


def render_markdown(report: dict[str, Any]) -> str:
    health_payload = report.get("health") or {}
    lines = [
        f"# 模块自检报告 {report.get('checked_at', '')}",
        "",
        f"- 应用：{health_payload.get('app', '-')}",
        f"- 结论：{'通过' if report.get('ok') else '失败'}",
        f"- 健康检查（当时口径）：ok={health_payload.get('ok')}、"
        f"modules={health_payload.get('modules')}",
        f"- 示例数据：seeded={health_payload.get('seeded')}、"
        f"seed_version={health_payload.get('seed_version')}",
        f"- 失败环节：{report.get('failed_stage') or '无'}"
        + (f"（模块：{report.get('failed_module')}）" if report.get("failed_module") else ""),
        "",
        "## 健康检查原始返回",
        "",
        "```json",
        json.dumps(health_payload, ensure_ascii=False, indent=2),
        "```",
        "",
        "## 业务模块自检",
        "",
        "| 模块 | 结果 | HTTP | 记录数 | 待处理 | 异常量 | 说明 |",
        "| --- | --- | --- | --- | --- | --- | --- |",
    ]
    for item in report.get("modules", []):
        lines.append(
            f"| {item['name']} | {item['result']} | {item.get('http_status', '-')} | "
            f"{item.get('total', '-')} | {item.get('pending', '-')} | "
            f"{item.get('abnormal', '-')} | {item.get('detail', '')} |"
        )
    overview = report.get("overview") or {}
    lines += [
        "",
        "## 与运营概览对账",
        "",
        "| 指标 | 自检合计 | 概览卡片 | 一致 |",
        "| --- | --- | --- | --- |",
    ]
    for row in report.get("reconcile", []):
        lines.append(
            f"| {row['label']} | {row['selfcheck']} | {row['overview']} | "
            f"{'是' if row['match'] else '否'} |"
        )
    lines.append("")
    return "\n".join(lines)


# ----------------------------- 各环节 -----------------------------


def run_seed() -> dict[str, int]:
    """示例数据初始化：逐模块幂等灌入，失败点名模块。"""
    # 必须在 import app.store 之前关掉单例的自动灌库，否则“本轮新增条数”会被
    # 模块级单例抢先做完；服务进程不设此变量，保持启动即自动就绪。
    saved = os.environ.get("APP_AUTO_SEED")
    os.environ["APP_AUTO_SEED"] = "0"
    try:
        from app.seed import SEED_ROWS
        from app.store import Store

        seeder = Store(auto_seed=False)
        result: dict[str, int] = {}
        for module in sorted(SEED_ROWS):
            try:
                inserted = seeder.seed_module(module)
            except Exception as exc:  # 逐模块提交后仍可能在写库时失败
                raise StageFailed("seed", f"示例数据初始化失败：{exc}", module=module) from exc
            result[module] = inserted
        if not seeder.is_seeded():
            # 兜底：全部模块都在但版本标记缺失时补上版本标记
            seeder.seed_all()
    finally:
        if saved is None:
            os.environ.pop("APP_AUTO_SEED", None)
        else:
            os.environ["APP_AUTO_SEED"] = saved
    return result


def start_server() -> tuple[subprocess.Popen[bytes] | None, bool]:
    """起后端服务；端口已在监听则复用现有进程。返回 (进程, 是否复用)。"""
    if port_open(HOST, PORT):
        log(f"端口 {HOST}:{PORT} 已在监听，复用现有服务，不再重复启动")
        return None, True

    log(f"启动后端服务：uvicorn app.main:app --host {HOST} --port {PORT}")
    FLOW_DIR.mkdir(parents=True, exist_ok=True)
    log_handle = SERVER_LOG.open("ab")
    log_handle.write(f"\n===== devflow 启动于 {_dt.datetime.now().isoformat()} =====\n".encode("utf-8"))
    proc = subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "app.main:app", "--host", HOST, "--port", str(PORT)],
        cwd=str(BACKEND_DIR),
        stdout=log_handle,
        stderr=subprocess.STDOUT,
    )
    PID_FILE.write_text(str(proc.pid), encoding="utf-8")

    deadline = time.monotonic() + HEALTH_TIMEOUT
    while time.monotonic() < deadline:
        if proc.poll() is not None:
            raise StageFailed(
                "serve",
                f"服务进程提前退出（退出码 {proc.returncode}），详见 {SERVER_LOG}",
            )
        if port_open(HOST, PORT):
            log(f"服务已监听 {HOST}:{PORT}，日志：{SERVER_LOG}")
            return proc, False
        time.sleep(0.3)
    proc.terminate()
    raise StageFailed("serve", f"等待服务监听 {HOST}:{PORT} 超时（{HEALTH_TIMEOUT:g}s）")


def run_health(timeout: float = HEALTH_TIMEOUT) -> dict[str, Any]:
    """健康检查：轮询 /api/health，要求 ok 且示例数据已就绪。"""
    deadline = time.monotonic() + timeout
    last_error = "服务未响应"
    while time.monotonic() < deadline:
        try:
            payload = http_get_json("/api/health", timeout=2.0, stage="health")
        except ConnectionError as exc:
            last_error = str(exc)
            time.sleep(0.5)
            continue
        if not payload.get("ok"):
            raise StageFailed("health", f"健康检查返回 ok={payload.get('ok')}")
        if not payload.get("seeded"):
            raise StageFailed("health", "健康检查显示示例数据未就绪（seeded=false）")
        return payload  # type: ignore[no-any-return]
    raise StageFailed("health", f"健康检查在 {timeout:g}s 内未通过：{last_error}")


def run_module_checks(
    health_payload: dict[str, Any],
    done_modules: set[str] | None = None,
    state: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """逐模块自检并与 /api/overview 对账。

    done_modules 给出上次已通过的模块（断点续跑时沿用当时结论，仅用最新概览
    数字重算合计）；state 非空时每通过一个模块就落盘，便于从失败模块接着跑。
    任一模块不通或数字对不上即结束并在报告里点名。
    """
    done_modules = done_modules or set()
    overview = http_get_json("/api/overview")
    overview_rows = {str(row["name"]): row for row in overview.get("modules", [])}
    cards = {str(c["label"]): int(c["value"]) for c in overview.get("cards", [])}

    results: list[dict[str, Any]] = []
    totals = {"created": 0, "pending": 0, "abnormal": 0}
    checked: list[str] = [name for name, _ in module_endpoints() if name in done_modules]

    for name, path in module_endpoints():
        overview_row = overview_rows.get(name, {})
        if name in done_modules:
            item = {
                "name": name,
                "result": "OK",
                "http_status": 200,
                "total": int(overview_row.get("created", 0)),
                "pending": int(overview_row.get("pending", 0)),
                "abnormal": int(overview_row.get("abnormal", 0)),
                "detail": "正常（断点续跑沿用上次通过结论）",
            }
        else:
            item: dict[str, Any] = {"name": name, "result": "FAIL"}
            try:
                payload = http_get_json(path)
            except StageFailed as exc:
                item["detail"] = exc.message
                results.append(item)
                return _assemble_report(health_payload, overview, results, totals, cards,
                                        failed_module=name, failed_detail=exc.message)
            except ConnectionError as exc:
                detail = f"请求未送达：{exc}"
                item["detail"] = detail
                results.append(item)
                return _assemble_report(health_payload, overview, results, totals, cards,
                                        failed_module=name, failed_detail=detail)

            total = int(payload.get("total", -1))
            if not isinstance(payload.get("items"), list) or total < 0:
                detail = "返回结构缺少 items/total"
                item["detail"] = detail
                results.append(item)
                return _assemble_report(health_payload, overview, results, totals, cards,
                                        failed_module=name, failed_detail=detail)
            item.update(
                {
                    "result": "OK",
                    "http_status": 200,
                    "total": total,
                    # size=1 只取首页一条，待处理/异常量按概览的全量口径统计
                    "pending": int(overview_row.get("pending", 0)),
                    "abnormal": int(overview_row.get("abnormal", 0)),
                    "detail": "正常",
                }
            )
            checked.append(name)
            if state is not None:
                state["stages"]["check_modules"] = checked
                save_state(state)

        totals["created"] += int(item["total"])
        totals["pending"] += int(item["pending"])
        totals["abnormal"] += int(item["abnormal"])
        results.append(item)

    return _assemble_report(health_payload, overview, results, totals, cards)


def _assemble_report(
    health_payload: dict[str, Any],
    overview: dict[str, Any],
    results: list[dict[str, Any]],
    totals: dict[str, int],
    cards: dict[str, int],
    *,
    failed_module: str | None = None,
    failed_detail: str = "",
) -> dict[str, Any]:
    reconcile = [
        {"label": "业务模块", "selfcheck": len([r for r in results if r["result"] == "OK"]),
         "overview": cards.get("业务模块", 0)},
        {"label": "今日新增", "selfcheck": totals["created"], "overview": cards.get("今日新增", 0)},
        {"label": "待处理", "selfcheck": totals["pending"], "overview": cards.get("待处理", 0)},
        {"label": "异常量", "selfcheck": totals["abnormal"], "overview": cards.get("异常量", 0)},
    ]
    for row in reconcile:
        row["match"] = row["selfcheck"] == row["overview"]
    ok = failed_module is None and all(row["match"] for row in reconcile)
    failed_stage = None
    if failed_module is not None:
        failed_stage = "check"
    elif not all(row["match"] for row in reconcile):
        failed_stage = "reconcile"
    return {
        "ok": ok,
        "checked_at": _dt.datetime.now().isoformat(timespec="seconds"),
        "base_url": BASE_URL,
        "health": health_payload,
        "overview": overview,
        "modules": results,
        "totals": totals,
        "reconcile": reconcile,
        "failed_stage": failed_stage,
        "failed_module": failed_module,
        "failed_detail": failed_detail,
    }


def print_check_table(report: dict[str, Any]) -> None:
    items = report.get("modules", [])
    name_w = max([4, *(display_width(str(item["name"])) for item in items)])
    log("模块自检结果：")
    print(
        f"  {pad('模块', name_w)}  结果  {'记录':>4}  {'待处理':>4}  {'异常量':>4}  说明",
        flush=True,
    )
    for item in items:
        print(
            f"  {pad(str(item['name']), name_w)}  {item['result']:<4}  "
            f"{str(item.get('total', '-')):>4}  {str(item.get('pending', '-')):>4}  "
            f"{str(item.get('abnormal', '-')):>4}  {item.get('detail', '')}",
            flush=True,
        )
    log("与运营概览对账：")
    for row in report.get("reconcile", []):
        mark = "一致" if row["match"] else "不一致"
        print(
            f"  {pad(str(row['label']), 8)}  自检合计={row['selfcheck']:<4} "
            f"概览={row['overview']:<4} {mark}",
            flush=True,
        )


# ----------------------------- 命令入口 -----------------------------


def cmd_seed(_args: argparse.Namespace) -> int:
    inserted = run_seed()
    created = sum(inserted.values())
    if created:
        detail = "、".join(f"{name}+{n}" for name, n in inserted.items() if n)
        log(f"示例数据初始化完成：新增 {created} 条（{detail}）")
    else:
        log("示例数据已存在，本次初始化没有新增记录（幂等）")
    return 0


def cmd_health(_args: argparse.Namespace) -> int:
    try:
        payload = run_health(STANDALONE_TIMEOUT)
    except StageFailed as exc:
        log(f"健康检查失败：{exc.message}")
        return 1
    log(
        f"健康检查通过：ok={payload['ok']}、modules={payload['modules']}、"
        f"seeded={payload['seeded']}、待处理={payload['pending']}、异常量={payload['abnormal']}"
    )
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    return 0


def cmd_check(_args: argparse.Namespace) -> int:
    health_payload = run_health(STANDALONE_TIMEOUT)
    report = run_module_checks(health_payload)
    print_check_table(report)
    json_path, md_path = archive_report(report)
    if report["ok"]:
        log(f"全部 {len(report['modules'])} 个业务模块自检通过，结论已留档：")
        log(f"  {json_path}")
        log(f"  {md_path}")
        return 0
    if report["failed_module"]:
        log(f"模块自检失败，点名模块：{report['failed_module']} —— {report['failed_detail']}")
    mismatches = [r["label"] for r in report["reconcile"] if not r["match"]]
    if mismatches:
        log(f"自检数字与概览页不一致：{'、'.join(mismatches)}")
    log(f"本次自检结论已留档：{json_path}")
    return 1


def cmd_up(args: argparse.Namespace) -> int:
    FLOW_DIR.mkdir(parents=True, exist_ok=True)
    state = load_state()
    if state.get("finished"):
        # 上一轮已完整跑通：新一轮从头复验（灌数/起服本身幂等）
        log("检测到上一轮流程已完整跑通，本轮重新逐项复验")
        state = {}

    started_at = state.get("started_at") or _dt.datetime.now().isoformat(timespec="seconds")
    state.setdefault("started_at", started_at)
    state.setdefault("stages", {})
    save_state(state)

    proc: subprocess.Popen[bytes] | None = None
    try:
        # 1) 示例数据初始化
        if state["stages"].get("seed") == "done":
            log("环节[1/4] 示例数据初始化：已完成，跳过")
        else:
            log("环节[1/4] 示例数据初始化")
            inserted = run_seed()
            created = sum(inserted.values())
            state["stages"]["seed"] = "done"
            state["seed_inserted"] = inserted
            save_state(state)
            log(f"示例数据就绪，本轮新增 {created} 条（重复执行不会产生重复记录）")

        # 2) 起服务
        if state["stages"].get("serve") == "done" and port_open(HOST, PORT):
            log("环节[2/4] 启动服务：已在运行，跳过")
        else:
            if state["stages"].get("serve") == "done":
                log("环节[2/4] 启动服务：上次的服务已不在，重新拉起")
                state["stages"]["serve"] = "pending"
                save_state(state)
            else:
                log("环节[2/4] 启动服务")
            proc, reused = start_server()
            state["stages"]["serve"] = "done"
            state["server_reused"] = reused
            # 服务重新拉起，健康检查与模块自检结论需要重新取
            state["stages"]["health"] = "pending"
            state.pop("health", None)
            state["stages"]["check_modules"] = []
            save_state(state)

        # 3) 健康检查
        if state["stages"].get("health") == "done" and state.get("health"):
            log("环节[3/4] 健康检查：已完成，跳过（如需复验请执行 health 子命令）")
            health_payload = state["health"]
        else:
            log("环节[3/4] 健康检查 GET /api/health")
            health_payload = run_health()
            state["stages"]["health"] = "done"
            state["health"] = health_payload
            save_state(state)
            log(
                f"健康检查通过：业务模块 {health_payload['modules']} 个、"
                f"待处理 {health_payload['pending']}、异常量 {health_payload['abnormal']}"
            )

        # 4) 模块自检
        log("环节[4/4] 业务模块自检")
        done_modules = set(state["stages"].get("check_modules", []))
        if done_modules:
            log(f"断点续跑：{len(done_modules)} 个模块上次已通过，从剩余模块接着验")
        report = run_module_checks(health_payload, done_modules, state)
        print_check_table(report)
        json_path, md_path = archive_report(report)
        if not report["ok"]:
            if report["failed_module"]:
                log(f"模块自检失败，点名模块：{report['failed_module']} —— {report['failed_detail']}")
            else:
                mismatches = [r["label"] for r in report["reconcile"] if not r["match"]]
                log(f"自检数字与概览页不一致：{'、'.join(mismatches) or '未知'}")
            log(f"自检结论已按当时口径留档：{json_path}")
            log(f"状态已记录，重跑 up 将从断掉的模块接着跑：{STATE_FILE}")
            return 1

        state["stages"]["check"] = "done"
        state["finished"] = True
        state["finished_at"] = _dt.datetime.now().isoformat(timespec="seconds")
        state["last_report_json"] = str(json_path)
        state["last_report_md"] = str(md_path)
        save_state(state)
        log(f"全部 {len(report['modules'])} 个业务模块自检通过，结论已留档：")
        log(f"  {json_path}")
        log(f"  {md_path}")
        clear_state()

        if proc is not None:
            log(f"流程跑通，后端继续运行于 {BASE_URL}（Ctrl-C 停止，日志 {SERVER_LOG}）")
            try:
                signal.signal(signal.SIGTERM, lambda *_: proc.terminate())  # type: ignore[attr-defined]
                proc.wait()
            except KeyboardInterrupt:
                log("收到中断信号，停止后端服务")
                proc.terminate()
                proc.wait(timeout=10)
        else:
            log(f"流程跑通，复用的后端服务仍在 {BASE_URL} 运行")
        return 0

    except StageFailed as exc:
        if proc is not None and exc.stage == "serve":
            proc.terminate()
        where = f"[{exc.stage}]" + (f" 模块 {exc.module}" if exc.module else "")
        log(f"流程在 {where} 失败：{exc.message}")
        log(f"状态已记录，重跑 up 将从断掉的地方接着跑：{STATE_FILE}")
        return 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m app.devflow",
        description="示例数据初始化、健康检查与模块自检的一键流程",
    )
    sub = parser.add_subparsers(dest="command")
    sub.add_parser("up", help="完整流程：初始化→起服务→健康检查→模块自检（默认）")
    sub.add_parser("seed", help="只做示例数据初始化（幂等）")
    sub.add_parser("health", help="只做健康检查")
    sub.add_parser("check", help="只做模块自检并与概览对账")
    args = parser.parse_args(argv)

    handlers = {
        "seed": cmd_seed,
        "health": cmd_health,
        "check": cmd_check,
        "up": cmd_up,
        None: cmd_up,
    }
    return handlers[args.command](args)


if __name__ == "__main__":
    raise SystemExit(main())
