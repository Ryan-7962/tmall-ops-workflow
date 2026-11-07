#!/usr/bin/env python3
import datetime as dt
import json
import sqlite3
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import chat_bot
import daily_pipeline as pipeline


CONFIG = {
    "store_scope": "test_scope",
    "wxt_channel": "test_channel",
    "wxt_attribution_scope": "test_attribution",
    "dashboard_url": "https://dashboard.example.test",
    "chat_allowed_groups": ["经营分析群"],
}


class ChatBotTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.database = Path(self.temp.name) / "test.db"
        connection = pipeline.connect(self.database)
        source_id = connection.execute(
            "INSERT INTO source_files(path, file_name, sha256, size_bytes, source_type, ingested_at) VALUES (?,?,?,?,?,?)",
            ("fixture", "fixture", "fixture-hash", 1, "test", "test"),
        ).lastrowid
        store_rows = [
            ("2025-01-02", 10, 20, 1000, 1),
            ("2026-01-01", 20, 40, 1000, 2),
            ("2026-01-02", 30, 60, 1200, 3),
        ]
        for date, uv, pv, pay, buyers in store_rows:
            connection.execute(
                "INSERT INTO store_daily_facts(date, scope, uv, pv, payAmt, payByrCnt, source_file_id, row_hash, updated_at) VALUES (?,?,?,?,?,?,?,?,?)",
                (date, "test_scope", uv, pv, pay, buyers, source_id, date, "test"),
            )
        for date, charge, pay, orders, cart in [
            ("2026-01-01", 20, 40, 1, 2),
            ("2026-01-02", 30, 60, 2, 3),
        ]:
            connection.execute(
                "INSERT INTO wxt_daily_facts(date, channel, attribution_scope, charge, payAmt, ordCnt, cart, source_file_id, row_hash, updated_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
                (date, "test_channel", "test_attribution", charge, pay, orders, cart, source_id, date, "test"),
            )
        connection.commit()
        connection.close()
        self.connection = chat_bot.connect_readonly(self.database)

    def tearDown(self):
        self.connection.close()
        self.temp.cleanup()

    def answer(self, question):
        return chat_bot.answer_question(self.connection, question, CONFIG, dt.date(2026, 1, 3))

    def test_yesterday_answers_from_facts_with_yoy_and_dashboard(self):
        result = self.answer("昨天销售怎么样？")
        self.assertIn("2026-01-02", result)
        self.assertIn("1.2 千元", result)
        self.assertIn("GMV 同比 **+20.0%**", result)
        self.assertIn("https://dashboard.example.test", result)

    def test_missing_day_is_not_filled_with_zero(self):
        result = self.answer("2026-01-03 销售怎么样")
        self.assertIn("暂无 2026-01-03", result)
        self.assertIn("缺失没有填 0", result)
        self.assertNotIn("GMV **0.0 千元**", result)

    def test_wxt_seven_day_roi_and_partial_coverage(self):
        result = self.answer("万相台近7天ROI怎么样")
        self.assertIn("ROI **2.00**", result)
        self.assertIn("归因订单 **3**", result)
        self.assertIn("2/7 天事实", result)

    def test_category_with_missing_day_is_excluded_from_full_week_ranking(self):
        writable = pipeline.connect(self.database)
        for offset in range(7):
            date = f"2026-01-0{offset + 1}"
            if date != "2026-01-07":
                writable.execute(
                    "INSERT INTO category_daily_facts(date, category_l1, amt, source_file_id, row_hash, updated_at) VALUES (?,?,?,?,?,?)",
                    (date, "缺日类目", 1000, 1, f"a-{date}", "test"),
                )
            writable.execute(
                "INSERT INTO category_daily_facts(date, category_l1, amt, source_file_id, row_hash, updated_at) VALUES (?,?,?,?,?,?)",
                (date, "完整类目", 100, 1, f"b-{date}", "test"),
            )
        writable.commit()
        writable.close()
        result = self.answer("近7天一级类目TOP5")
        self.assertIn("完整类目：**0.7 千元**", result)
        self.assertNotIn("缺日类目：**6.0 千元**", result)
        self.assertIn("1 个缺日或成交缺失的类目未参与排名", result)

    def test_null_wxt_spend_blocks_total_and_roi(self):
        writable = pipeline.connect(self.database)
        writable.execute(
            "INSERT INTO wxt_daily_facts(date, channel, attribution_scope, charge, payAmt, ordCnt, cart, source_file_id, row_hash, updated_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
            ("2026-01-03", "test_channel", "test_attribution", None, 120, 3, 4, 1, "null-spend", "test"),
        )
        writable.commit()
        writable.close()
        result = self.answer("万相台近7天ROI怎么样")
        self.assertIn("消耗 **—**", result)
        self.assertIn("ROI **—**", result)
        self.assertIn("不按 0 计算", result)

    def test_chinese_explicit_date_is_single_day_not_period(self):
        result = self.answer("2026年1月7日销售怎么样")
        self.assertIn("暂无 2026-01-07", result)
        self.assertNotIn("近 7 天经营", result)

    def test_connection_is_read_only(self):
        with self.assertRaises(sqlite3.OperationalError):
            self.connection.execute("DELETE FROM store_daily_facts")

    def test_wecom_sender_rejects_unauthorized_status(self):
        calls = [
            subprocess.CompletedProcess([], 0, "wecom-cli 1.2.1", ""),
            subprocess.CompletedProcess([], 0, "unauthorized", ""),
        ]
        with mock.patch("chat_bot.subprocess.run", side_effect=calls) as runner:
            with self.assertRaises(RuntimeError):
                chat_bot.send_wecom("经营分析群", "test", CONFIG)
        self.assertEqual(runner.call_count, 2)

    def test_wecom_sender_refreshes_group_and_does_not_return_internal_id(self):
        calls = [
            subprocess.CompletedProcess([], 0, "wecom-cli 1.2.1", ""),
            subprocess.CompletedProcess([], 0, "authorized", ""),
            subprocess.CompletedProcess([], 0, json.dumps({"sessions": [{"chat_id": "internal-only", "chat_name": "经营分析群", "chat_type": "group"}]}), ""),
            subprocess.CompletedProcess([], 0, '{"errcode": 0}', ""),
        ]
        with mock.patch("chat_bot.subprocess.run", side_effect=calls) as runner:
            result = chat_bot.send_wecom("经营分析群", "**昨日经营**", CONFIG)
        self.assertEqual(result["chat_name"], "经营分析群")
        self.assertNotIn("internal-only", json.dumps(result))
        send_args = runner.call_args_list[3].args[0]
        payload = json.loads(send_args[-1])
        self.assertEqual(payload["chat_id"], "internal-only")
        self.assertEqual(payload["markdown"]["content"], "**昨日经营**")

    def test_wecom_sender_blocks_group_outside_allowlist(self):
        with mock.patch("chat_bot.subprocess.run") as runner:
            with self.assertRaises(ValueError):
                chat_bot.send_wecom("未授权群", "test", CONFIG)
        runner.assert_not_called()


if __name__ == "__main__":
    unittest.main()
