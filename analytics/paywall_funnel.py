"""Live.AI 付费页互斥流程树。纯计算，不依赖 Flask。

节点按设备去重，兄弟分叉互斥。同一设备多次打开只算一次；购买结果优先
成功 > 失败 > 取消 > 点了但商品不可用 > 点了尚无结果。

「看到商品」按客户端版本分三档判定，见 _item_state：

- **1.3.3+**：有 paywall_ready（loadProducts 结束后才发），props 即真实状态，直接采信。
- **1.3.2**：只有 paywall_view，而它打在 .onAppear，**必然早于**商品加载完成——
  首帧 products_count 恒为 0，单凭它判「没看到」会把每一次正常曝光都算成失败。
  改用「加载结束后才发」的证据当判据：storekit_products_missing(all_missing) /
  storekit_products_failed / purchase_blocked(product_unavailable)。
  三者都没有时归入 items_unknown——1.3.2 的埋点答不了这个问题，不猜。
- **≤1.3.1**：既无 products_* 字段也无 storekit_* 事件，无从判断 → items_unknown。
"""
from __future__ import annotations

import json
import sqlite3
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from typing import Any

FUNNEL_EVENTS = (
    "paywall_view",
    "paywall_ready",
    "purchase_start",
    "purchase_success",
    "purchase_failed",
    "purchase_cancelled",
    "purchase_blocked",
    "storekit_products_missing",
    "storekit_products_failed",
)
TZ_BJ = timezone(timedelta(hours=8))
ITEM_KEYS = ("saw_items", "no_items", "items_unknown")
OUTCOME_KEYS = ("success", "failed", "cancelled", "blocked", "pending")


def _parse_props(raw: str | None) -> dict[str, Any]:
    if not raw:
        return {}
    try:
        val = json.loads(raw)
    except (TypeError, ValueError):
        return {}
    return val if isinstance(val, dict) else {}


def _saw_products(props: dict[str, Any]) -> bool:
    count = props.get("products_count")
    try:
        return count is not None and int(count) > 0
    except (TypeError, ValueError):
        return False


def _known_product_state(props: dict[str, Any]) -> bool:
    return props.get("products_loaded") is not None or props.get("products_count") is not None


def _item_state(slot: dict[str, Any]) -> str:
    """这台设备到底有没有看到价格。按可用证据的可靠度分档。"""
    # 能发出 purchase_start / success，当时手里一定有 StoreKit Product。
    if slot["start"] or slot["success"]:
        return "saw_items"

    # ① 1.3.3+：paywall_ready 在 loadProducts 结束后才发，是权威判据
    if slot["ready"]:
        if any(_saw_products(p) for p in slot["ready"]):
            return "saw_items"
        if any(_known_product_state(p) for p in slot["ready"]):
            return "no_items"
        return "items_unknown"

    # ② 1.3.2：paywall_view 打在 .onAppear，同步执行，**必然早于** .task 里的加载完成，
    #    所以首帧 products_count 恒为 0——这是时序产物，不是失败。
    #    真正的判据是 storekit_* 那两条「加载结束后才发」的事件：
    #      - 完全没有 → 那一轮加载拿到了全部 4 个商品（1.3.2 只要缺就会发 missing）
    #      - all_missing → 一个都没拿到，付费页只剩兜底价，这才是真的没看到
    #      - 部分缺失 → 其余档位照常显示价格，算看到
    if any(_known_product_state(p) for p in slot["views"]):
        # 正面证据优先：只要有一屏确实有价格，这台设备就算看到过
        if any(_saw_products(p) for p in slot["views"]) or slot["loaded_some"]:
            return "saw_items"
        if slot["load_failed"] or slot["all_missing"] or slot["blocked_unavailable"]:
            return "no_items"
        # 首帧 0、但没有任何「加载结束后」的失败证据。**这里不能猜**：
        # 既可能是加载随后成功了（只是首帧没赶上），也可能是用户没等加载完就离开
        # （.task 随 view 销毁被取消，两种情况都不会留下 storekit_* 事件）。
        # 猜「看到」会掩盖真实故障，猜「没看到」会把每次正常曝光算成失败——
        # 1.3.2 的埋点本就答不了这个问题，如实归入 unknown。
        return "items_unknown"

    # ③ ≤1.3.1：没有任何可用证据
    return "items_unknown"


def _purchase_fork(slot: dict[str, Any]) -> tuple[str, str]:
    clicked = slot["start"] or slot["blocked"]
    if not clicked:
        return "left", "left"
    if slot["success"]:
        return "clicked", "success"
    if slot["failed"]:
        return "clicked", "failed"
    if slot["cancelled"]:
        return "clicked", "cancelled"
    if slot["blocked"] and not slot["start"]:
        return "clicked", "blocked"
    return "clicked", "pending"


def _zero() -> dict[str, int]:
    return {"devices": 0, "events": 0}


def _new_tree() -> dict[str, Any]:
    def outcomes() -> dict[str, dict[str, int]]:
        return {k: _zero() for k in OUTCOME_KEYS}

    def clicks() -> dict[str, Any]:
        return {
            "clicked": {**_zero(), "outcomes": outcomes()},
            "left": _zero(),
        }

    return {
        "paywall": _zero(),
        "items": {
            key: {**_zero(), "missing_events": 0, "clicks": clicks()}
            for key in ITEM_KEYS
        },
    }


def _new_device_slot() -> dict[str, Any]:
    return {
        "views": [],
        "ready": [],
        "view_n": 0,
        "start_n": 0,
        "success_n": 0,
        "failed_n": 0,
        "cancelled_n": 0,
        "blocked_n": 0,
        "missing_n": 0,
        # 加载**结束后**才会置位的两个判据（1.3.2 起才有这两个事件）
        "all_missing": False,   # 某次加载四个商品一个都没拿到
        "load_failed": False,   # 重试耗尽仍失败（超时 / 网络错误）
        # 用户点了购买却被挡下，理由是商品不可用——「确实没看到价格」的最强证据
        "blocked_unavailable": False,
        # 某次加载结束时至少拿到了一个商品 → 那一屏是有价格的（正面证据）
        "loaded_some": False,
        "start": False,
        "success": False,
        "failed": False,
        "cancelled": False,
        "blocked": False,
    }


def _accumulate_device(slot: dict[str, Any], event: str, props_raw: str | None) -> None:
    props = _parse_props(props_raw)
    if event == "paywall_view":
        slot["views"].append(props)
        slot["view_n"] += 1
    elif event == "paywall_ready":
        slot["ready"].append(props)
    elif event == "purchase_start":
        slot["start"] = True
        slot["start_n"] += 1
    elif event == "purchase_success":
        slot["success"] = True
        slot["success_n"] += 1
    elif event == "purchase_failed":
        slot["failed"] = True
        slot["failed_n"] += 1
    elif event == "purchase_cancelled":
        slot["cancelled"] = True
        slot["cancelled_n"] += 1
    elif event == "purchase_blocked":
        slot["blocked"] = True
        slot["blocked_n"] += 1
        if props.get("reason") == "product_unavailable":
            slot["blocked_unavailable"] = True
    elif event == "storekit_products_missing":
        slot["missing_n"] += 1
        # 部分缺失（某档没过审）不算「没看到」——其余档位照常显示价格，
        # 而且这条事件是加载**结束后**才发的，等于确证「那一屏有价格」
        if props.get("all_missing") is True:
            slot["all_missing"] = True
        else:
            slot["loaded_some"] = True
    elif event == "storekit_products_failed":
        slot["load_failed"] = True


def classify_devices(rows: list[tuple[str, str, str | None]]) -> dict[str, Any]:
    by_dev: dict[str, dict[str, Any]] = {}
    for device_id, event, props_raw in rows:
        if not device_id:
            continue
        slot = by_dev.setdefault(device_id, _new_device_slot())
        _accumulate_device(slot, event, props_raw)

    tree = _new_tree()
    for slot in by_dev.values():
        if slot["view_n"] == 0:
            continue
        item = _item_state(slot)
        click, outcome = _purchase_fork(slot)
        tree["paywall"]["devices"] += 1
        tree["paywall"]["events"] += slot["view_n"]
        branch = tree["items"][item]
        branch["devices"] += 1
        # 事件数：1.3.3 有 paywall_ready 可以逐次数准；1.3.2 的 paywall_view 首帧
        # 恒为 0，逐次数会把正常曝光计进 no_items，所以整台设备按 view_n 归一档。
        if slot["ready"]:
            if item == "saw_items":
                n_saw = sum(1 for p in slot["ready"] if _saw_products(p))
                branch["events"] += n_saw if n_saw else slot["view_n"]
            elif item == "no_items":
                branch["events"] += sum(
                    1 for p in slot["ready"] if _known_product_state(p) and not _saw_products(p)
                ) or slot["view_n"]
            else:
                branch["events"] += slot["view_n"]
        else:
            branch["events"] += slot["view_n"]
        branch["missing_events"] += slot["missing_n"]
        clk = branch["clicks"][click]
        clk["devices"] += 1
        if click == "clicked":
            clk["events"] += slot["start_n"] + slot["blocked_n"]
            out = clk["outcomes"][outcome]
            out["devices"] += 1
            out["events"] += {
                "success": slot["success_n"],
                "failed": slot["failed_n"],
                "cancelled": slot["cancelled_n"],
                "blocked": slot["blocked_n"],
                "pending": slot["start_n"],
            }[outcome]
    return tree


def _counts_node(
    key: str,
    counts: dict[str, Any],
    children: list[dict[str, Any]] | None = None,
    extra: dict[str, Any] | None = None,
) -> dict[str, Any]:
    node: dict[str, Any] = {
        "key": key,
        "devices": int(counts.get("devices") or 0),
        "events": int(counts.get("events") or 0),
    }
    if extra:
        node.update(extra)
    if children is not None:
        node["children"] = children
    return node


def tree_to_json(tree: dict[str, Any]) -> dict[str, Any]:
    item_children = []
    for item_key in ITEM_KEYS:
        branch = tree["items"][item_key]
        clicked = branch["clicks"]["clicked"]
        left = branch["clicks"]["left"]
        outcomes = [_counts_node(k, clicked["outcomes"][k]) for k in OUTCOME_KEYS]
        extra = {}
        if branch.get("missing_events"):
            extra["storekit_missing_events"] = int(branch["missing_events"])
        item_children.append(_counts_node(
            item_key,
            branch,
            [
                _counts_node("clicked", clicked, outcomes),
                _counts_node("left", left),
            ],
            extra or None,
        ))
    return _counts_node("paywall", tree["paywall"], item_children)


def compute_paywall_funnel(conn: sqlite3.Connection, app_name: str, days: int) -> dict[str, Any]:
    # 日界必须是北京日历日，不能用 datetime('now','-N days') 滚动窗口。
    # 后者会把昨晚的购买算进「今天」，和利润卡片（Asia/Shanghai 0 点）对不上。
    placeholders = ",".join("?" for _ in FUNNEL_EVENTS)
    bj_day = "strftime('%Y-%m-%d', created_at, '+8 hours')"
    today = datetime.now(TZ_BJ).date()
    since_bj = (today - timedelta(days=days - 1)).isoformat()
    rows = conn.execute(
        f"""SELECT device_id, event, props, {bj_day} AS date
              FROM events
             WHERE app_name = ?
               AND {bj_day} >= ?
               AND event IN ({placeholders})""",
        (app_name, since_bj, *FUNNEL_EVENTS),
    ).fetchall()

    period_rows = [(r[0], r[1], r[2]) for r in rows]
    by_date: dict[str, list[tuple[str, str, str | None]]] = defaultdict(list)
    for r in rows:
        by_date[str(r[3])].append((r[0], r[1], r[2]))

    dates = [(today - timedelta(days=i)).isoformat() for i in range(days - 1, -1, -1)]
    return {
        "app": app_name,
        "days": days,
        "tree": tree_to_json(classify_devices(period_rows)),
        "daily": [
            {"date": d, "tree": tree_to_json(classify_devices(by_date.get(d, [])))}
            for d in dates
        ],
    }
