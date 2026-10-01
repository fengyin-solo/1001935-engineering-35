"""模块自检：逐个业务模块验证接口可用性，指标口径与运营概览页保持一致。

自检报告自带口径版本与指标定义（spec），留档后即使将来口径调整，
历史报告仍按当时的口径解读。
"""
from __future__ import annotations

import json
import urllib.error
import urllib.request
from datetime import datetime, timezone
from typing import Any

SPEC_VERSION = 1
# 指标口径：与 GET /api/overview 返回的 cards/modules 一致
METRIC_SPEC = {
    "created": "模块当前记录总数，与列表接口 total 一致",
    "pending": "记录中 pending=true 的条数",
    "abnormal": "记录中 abnormal=true 的条数",
}

HTTP_TIMEOUT = 5


class CheckError(Exception):
    """自检请求失败：message 里带上出问题的接口路径。"""


def _request_json(base_url: str, path: str, *, method: str = "GET") -> Any:
    url = f"{base_url}{path}"
    data = b"{}" if method == "POST" else None
    request = urllib.request.Request(url, data=data, method=method)
    if data is not None:
        request.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(request, timeout=HTTP_TIMEOUT) as response:
            return json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        raise CheckError(f"接口 {path} 返回 {exc.code}") from exc
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
        raise CheckError(f"接口 {path} 不可用：{exc}") from exc


def get_json(base_url: str, path: str) -> Any:
    return _request_json(base_url, path, method="GET")


def post_json(base_url: str, path: str) -> Any:
    return _request_json(base_url, path, method="POST")


def fetch_health(base_url: str) -> dict[str, Any]:
    return get_json(base_url, "/api/health")


def fetch_overview(base_url: str) -> dict[str, Any]:
    return get_json(base_url, "/api/overview")


def check_module(base_url: str, name: str, metrics: dict[str, Any]) -> dict[str, Any]:
    """检查单个模块：列表接口必须可用，且记录总数与概览页的数字一致。"""
    result: dict[str, Any] = {
        "name": name,
        "ok": False,
        "created": int(metrics.get("created", 0)),
        "pending": int(metrics.get("pending", 0)),
        "abnormal": int(metrics.get("abnormal", 0)),
        "error": None,
    }
    try:
        page = get_json(base_url, f"/api/{name}?page=1&size=1")
    except CheckError as exc:
        result["error"] = str(exc)
        return result
    total = page.get("total") if isinstance(page, dict) else None
    if not isinstance(total, int):
        result["error"] = f"接口 /api/{name} 的返回缺少 total 字段"
        return result
    if total != result["created"]:
        result["error"] = f"列表总数 {total} 与概览页今日新增 {result['created']} 不一致"
        return result
    result["ok"] = True
    return result


def build_report(
    base_url: str,
    overview: dict[str, Any],
    module_results: list[dict[str, Any]],
) -> dict[str, Any]:
    """汇总自检报告：cards 与 modules 直接取自概览接口，保证与概览页同步。"""
    return {
        "spec": {
            "version": SPEC_VERSION,
            "source": "GET /api/overview",
            "metrics": METRIC_SPEC,
        },
        "base_url": base_url,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "ok": all(item["ok"] for item in module_results),
        "cards": overview.get("cards", []),
        "modules": module_results,
    }
