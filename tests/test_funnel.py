"""Paywall flow-tree: sibling forks are exclusive and sum to the parent."""
from __future__ import annotations

import importlib.util
import json
import sqlite3
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

_MOD_PATH = Path(__file__).resolve().parents[1] / "analytics" / "paywall_funnel.py"
_spec = importlib.util.spec_from_file_location("paywall_funnel", _MOD_PATH)
assert _spec and _spec.loader
_funnel = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_funnel)
compute_paywall_funnel = _funnel.compute_paywall_funnel


class FunnelTreeTests(unittest.TestCase):
    def setUp(self) -> None:
        self.conn = sqlite3.connect(":memory:")
        self.conn.executescript(
            """
            CREATE TABLE events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                app_name TEXT NOT NULL,
                event TEXT NOT NULL,
                user_id TEXT,
                device_id TEXT NOT NULL,
                app_version TEXT,
                os_version TEXT,
                locale TEXT,
                props TEXT,
                ts TEXT NOT NULL,
                created_at TEXT NOT NULL
            );
            """
        )

    def tearDown(self) -> None:
        self.conn.close()

    def _add(self, device: str, event: str, props: dict | None = None, hours_ago: float = 1, created: datetime | None = None) -> None:
        if created is None:
            created = datetime.now(timezone.utc) - timedelta(hours=hours_ago)
        elif created.tzinfo is not None:
            created = created.astimezone(timezone.utc).replace(tzinfo=None)
        self.conn.execute(
            """INSERT INTO events (app_name, event, device_id, props, ts, created_at)
               VALUES ('liveai', ?, ?, ?, ?, ?)""",
            (event, device, json.dumps(props or {}), created.isoformat(), created.strftime("%Y-%m-%d %H:%M:%S")),
        )

    def _tree(self, days: int = 7) -> dict:
        return compute_paywall_funnel(self.conn, "liveai", days)["tree"]

    def _child(self, node: dict, key: str) -> dict:
        kids = {c["key"]: c for c in node.get("children") or []}
        self.assertIn(key, kids, f"missing fork {key} in {[c['key'] for c in node.get('children') or []]}")
        return kids[key]

    def test_exclusive_forks_sum_to_parent(self) -> None:
        # saw items → clicked → success
        self._add("a", "paywall_view", {"products_loaded": True, "products_count": 4})
        self._add("a", "purchase_start")
        self._add("a", "purchase_success")
        # saw items → left
        self._add("b", "paywall_view", {"products_loaded": True, "products_count": 4})
        # 首帧 0 且无任何「加载结束后」的失败证据 → unknown（1.3.2 埋点答不了）
        self._add("c", "paywall_view", {"products_loaded": False, "products_count": 0})
        # unknown → clicked → failed (then cancelled; failed wins over cancel)
        self._add("d", "paywall_view", {})
        self._add("d", "purchase_start")
        self._add("d", "purchase_failed")
        self._add("d", "purchase_cancelled")
        # unknown → clicked → success (failed first, success wins)
        self._add("e", "paywall_view", {})
        self._add("e", "purchase_start")
        self._add("e", "purchase_failed")
        self._add("e", "purchase_success")
        # unknown → left
        self._add("f", "paywall_view", {})
        # no items → clicked → blocked（blocked 的理由就是商品不可用，铁证）
        self._add("g", "paywall_view", {"products_loaded": False, "products_count": 0})
        self._add("g", "purchase_blocked", {"reason": "product_unavailable"})
        # unknown → clicked → cancelled
        self._add("h", "paywall_view", {})
        self._add("h", "purchase_start")
        self._add("h", "purchase_cancelled")
        # purchase without paywall does not enter the tree
        self._add("ghost", "purchase_success")

        root = self._tree()
        self.assertEqual(root["key"], "paywall")
        self.assertEqual(root["devices"], 8)

        saw = self._child(root, "saw_items")
        no = self._child(root, "no_items")
        unk = self._child(root, "items_unknown")
        self.assertEqual(saw["devices"] + no["devices"] + unk["devices"], root["devices"])
        # start/success 视为已看到商品，不再只靠第一帧 products_count
        self.assertEqual(saw["devices"], 5)          # a b d e h
        self.assertEqual(no["devices"], 1)           # g（blocked: product_unavailable）
        self.assertEqual(unk["devices"], 2)          # c f

        saw_click = self._child(saw, "clicked")
        saw_left = self._child(saw, "left")
        self.assertEqual(saw_click["devices"] + saw_left["devices"], saw["devices"])
        self.assertEqual(self._child(saw_click, "success")["devices"], 2)
        self.assertEqual(self._child(saw_click, "failed")["devices"], 1)
        self.assertEqual(self._child(saw_click, "cancelled")["devices"], 1)
        self.assertEqual(saw_left["devices"], 1)

        no_click = self._child(no, "clicked")
        no_left = self._child(no, "left")
        self.assertEqual(no_click["devices"] + no_left["devices"], no["devices"])
        self.assertEqual(self._child(no_click, "blocked")["devices"], 1)
        self.assertEqual(no_left["devices"], 0)

        unk_click = self._child(unk, "clicked")
        unk_left = self._child(unk, "left")
        self.assertEqual(unk_click["devices"] + unk_left["devices"], unk["devices"])
        self.assertEqual(unk_left["devices"], 2)     # c f 都没点购买

        for parent in (saw_click, no_click, unk_click):
            self.assertEqual(
                sum(self._child(parent, k)["devices"] for k in ("success", "failed", "cancelled", "blocked", "pending")),
                parent["devices"],
            )

    def test_purchase_start_overrides_empty_first_snapshot(self) -> None:
        self._add("x", "paywall_view", {"products_loaded": False, "products_count": 0})
        self._add("x", "purchase_start")
        self._add("x", "purchase_success")
        root = self._tree()
        saw = self._child(root, "saw_items")
        no = self._child(root, "no_items")
        self.assertEqual(saw["devices"], 1)
        self.assertEqual(no["devices"], 0)
        self.assertEqual(self._child(self._child(saw, "clicked"), "success")["devices"], 1)

    # ---- 版本分档：同一个「首帧 products_count=0」在不同版本下含义不同 ----

    def test_1_3_3_paywall_ready_is_authoritative(self) -> None:
        """1.3.3+：paywall_ready 在加载结束后才发，直接采信，不受首帧影响。"""
        # 首帧 0（onAppear 早于加载），但 ready 报了 4 个 → 看到了
        self._add("ok", "paywall_view", {"products_loaded": False, "products_count": 0})
        self._add("ok", "paywall_ready", {"products_loaded": True, "products_count": 4})
        # ready 报 0 → 真的没看到
        self._add("bad", "paywall_view", {"products_loaded": False, "products_count": 0})
        self._add("bad", "paywall_ready", {"products_loaded": True, "products_count": 0})
        root = self._tree()
        self.assertEqual(self._child(root, "saw_items")["devices"], 1)
        self.assertEqual(self._child(root, "no_items")["devices"], 1)
        self.assertEqual(self._child(root, "items_unknown")["devices"], 0)

    def test_1_3_2_needs_post_load_evidence(self) -> None:
        """1.3.2：只有 paywall_view，首帧恒为 0，必须靠加载结束后的证据判定。"""
        # 四个商品一个都没拿到 → 确证没看到
        self._add("miss", "paywall_view", {"products_loaded": False, "products_count": 0})
        self._add("miss", "storekit_products_missing", {"all_missing": True, "loaded": 0})
        # 重试耗尽仍失败 → 确证没看到
        self._add("fail", "paywall_view", {"products_loaded": False, "products_count": 0})
        self._add("fail", "storekit_products_failed", {"kind": "fetch_timeout", "attempts": 3})
        # 只缺一档（studio 没过审）→ 其余档位有价格，算看到
        self._add("part", "paywall_view", {"products_loaded": False, "products_count": 0})
        self._add("part", "storekit_products_missing", {"all_missing": False, "loaded": 3})
        # 无任何失败证据 → 不猜，归 unknown
        self._add("quiet", "paywall_view", {"products_loaded": False, "products_count": 0})
        root = self._tree()
        self.assertEqual(self._child(root, "no_items")["devices"], 2)        # miss fail
        self.assertEqual(self._child(root, "saw_items")["devices"], 1)       # part
        self.assertEqual(self._child(root, "items_unknown")["devices"], 1)   # quiet

    def test_legacy_client_without_product_props_is_unknown(self) -> None:
        """≤1.3.1：既无 products_* 字段也无 storekit_* 事件，无从判断。"""
        self._add("old", "paywall_view", {})
        root = self._tree()
        self.assertEqual(self._child(root, "items_unknown")["devices"], 1)
        self.assertEqual(self._child(root, "no_items")["devices"], 0)

    def test_empty_window(self) -> None:
        root = self._tree()
        self.assertEqual(root["devices"], 0)
        self.assertEqual(len(root["children"]), 3)

    def test_today_is_beijing_calendar_not_rolling_24h(self) -> None:
        bj = timezone(timedelta(hours=8))
        today = datetime.now(bj).date()
        yest = today - timedelta(days=1)
        # 昨天北京 20:00 = UTC 12:00；滚动 24h 会算进来，日历「今天」不应算。
        yest_utc = datetime(yest.year, yest.month, yest.day, 12, 0, 0)
        self._add("old", "paywall_view", {}, created=yest_utc)
        self._add("old", "purchase_start", {}, created=yest_utc)
        self._add("old", "purchase_success", {}, created=yest_utc)
        self._add("new", "paywall_view", {})
        self._add("new", "purchase_start", {})
        self._add("new", "purchase_success", {})
        root = self._tree(days=1)
        self.assertEqual(root["devices"], 1)
        saw = self._child(root, "saw_items")
        self.assertEqual(self._child(self._child(saw, "clicked"), "success")["devices"], 1)


if __name__ == "__main__":
    unittest.main()
