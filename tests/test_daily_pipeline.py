#!/usr/bin/env python3
import csv
import json
import tempfile
import unittest
from pathlib import Path

import daily_pipeline as pipeline


CONFIG = {
    "store_scope": "sycm_offline_wireless",
    "wxt_channel": "万相台无界",
    "wxt_attribution_scope": "account_default_dynamic",
    "required_windows": ["1d"],
}


def window(end):
    return {"cur": {"start": end, "end": end}, "overview": {"payAmt": {"v": 100}}}


def snapshot(end, attributed):
    ly_end = f"{int(end[:4]) - 1}{end[4:]}"
    return {
        "meta": {"generatedAt": "test", "dataRange": {"start": end, "end": end, "days": 1}},
        "daily": {
            "dates": [end], "uv": [10], "pv": [20], "payAmt": [100], "payByrCnt": [2],
            "itmUv": [8], "cartByrCnt": [3], "rivalUv": [9], "rivalPayAmt": [90], "rivalPayByrCnt": [1],
        },
        "dailyLY": {
            "dates": [ly_end], "uv": [8], "pv": [16], "payAmt": [80], "payByrCnt": [1],
            "itmUv": [7], "cartByrCnt": [2],
        },
        "wxt": {
            "dates": [end], "charge": [20], "payAmt": [attributed], "click": [5],
            "impression": [100], "cart": [2], "ordCnt": [1],
        },
        "windows": {"1d": window(end)},
        "categories": {},
    }


def write_snapshot(path, payload):
    path.write_text("window.__DASH__ = " + json.dumps(payload, ensure_ascii=False) + ";\n", encoding="utf-8")


class DailyPipelineTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.connection = pipeline.connect(self.root / "test.db")

    def tearDown(self):
        self.connection.close()
        self.temp.cleanup()

    def test_snapshot_is_idempotent_and_wxt_restatement_updates_same_key(self):
        first = self.root / "snapshot-v1.js"
        second = self.root / "snapshot-v2.js"
        write_snapshot(first, snapshot("2026-01-01", 40))
        write_snapshot(second, snapshot("2026-01-01", 45))
        pipeline.ingest_snapshot(self.connection, first, CONFIG)
        pipeline.ingest_snapshot(self.connection, second, CONFIG)
        pipeline.ingest_snapshot(self.connection, second, CONFIG)
        self.assertEqual(self.connection.execute("SELECT COUNT(*) FROM store_daily_facts").fetchone()[0], 2)
        self.assertEqual(self.connection.execute("SELECT COUNT(*) FROM wxt_daily_facts").fetchone()[0], 1)
        self.assertEqual(self.connection.execute("SELECT COUNT(*) FROM wxt_daily_revisions").fetchone()[0], 2)
        self.assertEqual(self.connection.execute("SELECT payAmt FROM wxt_daily_facts").fetchone()[0], 45)

    def test_incremental_files_extend_facts_without_rewriting_history(self):
        initial = self.root / "snapshot.js"
        write_snapshot(initial, snapshot("2026-01-01", 40))
        pipeline.ingest_snapshot(self.connection, initial, CONFIG)
        store = self.root / "store_day_2026-01-02.csv"
        store.write_text(
            "date,scope,uv,pv,payment_amount,payment_buyers,product_visitors,cart_users\n"
            "2026-01-02,sycm_offline_wireless,12,24,120,3,9,4\n",
            encoding="utf-8",
        )
        wxt = self.root / "wxt_day_2026-01-02.csv"
        wxt.write_text(
            "date,channel,attribution_scope,ad_spend,attributed_payment_amount,clicks,impressions,cart_adds,total_orders\n"
            "2026-01-02,万相台无界,account_default_dynamic,25,50,6,120,3,2\n",
            encoding="utf-8",
        )
        detail = self.root / "window_1d_2026-01-02.json"
        detail.write_text(json.dumps(window("2026-01-02")), encoding="utf-8")
        pipeline.ingest_incremental_store(self.connection, store, CONFIG)
        pipeline.ingest_incremental_wxt(self.connection, wxt, CONFIG)
        pipeline.ingest_incremental_window(self.connection, detail, "1d")
        result = pipeline.build_snapshot(self.connection, CONFIG)
        self.assertEqual(result["daily"]["dates"], ["2026-01-01", "2026-01-02"])
        self.assertEqual(result["daily"]["payAmt"], [100, 120])
        self.assertEqual(result["wxt"]["payAmt"], [40, 50])
        self.assertEqual(result["windows"]["1d"]["cur"]["end"], "2026-01-02")
        self.assertEqual(result["monthly"]["payAmt"], [220])
        self.assertEqual(result["monthlyLy"]["payAmt"], [80])

    def test_monthly_ly_partial_month_uses_same_number_of_days(self):
        daily = {
            "dates": ["2025-11-01", "2025-11-02", "2025-11-03"],
            "uv": [10, 10, 10], "pv": [20, 20, 20], "payAmt": [100, 200, 300],
            "payByrCnt": [1, 2, 3], "itmUv": [8, 8, 8], "cartByrCnt": [2, 2, 2],
        }
        result = pipeline.monthly_from_daily(daily, ["2025-11"], 2)
        self.assertEqual(result["payAmt"], [300])
        self.assertEqual(result["payByrCnt"], [3])
        self.assertAlmostEqual(result["payRate"][0], 0.15)

    def test_product_master_ingest_and_window_items_enrichment(self):
        workbench = self.root / "tmall-workbench" / "product_master"
        workbench.mkdir(parents=True)
        master_csv = workbench / "product_master_snapshot_2026-01-01.csv"
        master_csv.write_text(
            "﻿product_id,product_name,category_id,category_name,image_url,list_date,product_status,source_status\n"
            "111,六神艾叶沐浴露,213205,,//img.example/a.jpg,,online,当前在线\n"
            "222,六神香氛沐浴露柚子玫瑰,213205,,//img.example/b.jpg,,online,当前在线\n",
            encoding="utf-8",
        )
        report = pipeline.ingest_product_master(self.connection, self.root / "tmall-workbench")
        self.assertEqual(report["counts"], {"inserted": 2})
        # 幂等：重复入库无变化
        repeat = pipeline.ingest_product_master(self.connection, self.root / "tmall-workbench")
        self.assertTrue(repeat["skipped_duplicate_file"])

        windows = {
            "7d": {
                "cur": {"start": "2026-01-01", "end": "2026-01-07"},
                "items": [
                    {"id": 111, "cate": 213205, "payAmt": 10, "uv": 5},
                    {"id": "222", "cate": 213205, "payAmt": 20, "uv": 6, "title": "已有标题", "img": "//img.example/old.jpg"},
                    {"id": 333, "cate": 213205, "payAmt": 30, "uv": 7},
                ],
            }
        }
        pipeline.enrich_window_items(self.connection, windows)
        items = windows["7d"]["items"]
        # 缺失才补：111 补 title/img；222 已有不覆盖；333 无主数据保持缺失
        self.assertEqual(items[0]["title"], "六神艾叶沐浴露")
        self.assertEqual(items[0]["img"], "//img.example/a.jpg")
        self.assertEqual(items[1]["title"], "已有标题")
        self.assertEqual(items[1]["img"], "//img.example/old.jpg")
        self.assertNotIn("title", items[2])
        # 数值指标不动
        self.assertEqual(items[0]["payAmt"], 10)


if __name__ == "__main__":
    unittest.main()
