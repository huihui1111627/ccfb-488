"""纯函数级单元测试（不需要起服务）：python -m bid_system.tests.test_utils"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from bid_system.collector.scheduler import cron_matches
from bid_system.utils import (normalize_code, normalize_title, parse_budget,
                              parse_deadline, project_key)


class TestDedup(unittest.TestCase):
    def test_title_normalization_strips_platform_noise(self):
        a = normalize_title("【更正公告】某市第一人民医院 医疗设备采购项目")
        b = normalize_title("某市第一人民医院医疗设备采购项目招标公告")
        self.assertEqual(a, b)

    def test_project_key_code_priority(self):
        k1 = project_key("HX-2026-005", "滨江省", "政务云")
        k2 = project_key("hx 2026 005", "任意地区", "完全不同的标题")
        self.assertEqual(k1, k2)
        self.assertTrue(k1.startswith("code:"))

    def test_project_key_fingerprint_without_code(self):
        k1 = project_key(None, "华信市", "城区排水管网检测服务  招标公告")
        k2 = project_key(None, "华信市", "城区排水管网检测服务")
        self.assertEqual(k1, k2)
        k3 = project_key(None, "滨江省", "城区排水管网检测服务")
        self.assertNotEqual(k1, k3)

    def test_normalize_code(self):
        self.assertEqual(normalize_code(" hx-2026_001 "), normalize_code("HX2026001"))


class TestBudgetParsing(unittest.TestCase):
    def test_yuan_with_commas(self):
        self.assertEqual(parse_budget("预算金额：人民币1,250,000.00元"), 1_250_000.0)

    def test_wanyuan(self):
        self.assertEqual(parse_budget("本项目预算352.5万元，资金已落实"), 3_525_000.0)

    def test_control_price(self):
        self.assertEqual(parse_budget("最高限价：880,000元"), 880_000.0)

    def test_no_amount(self):
        self.assertIsNone(parse_budget("本公告不含金额信息"))


class TestDeadlineParsing(unittest.TestCase):
    def test_full_datetime(self):
        text = "投标截止时间：2026-12-28 14:00，逾期不予受理"
        self.assertEqual(parse_deadline(text), "2026-12-28 14:00")

    def test_chinese_format_with_keyword(self):
        text = "投标人应于2027年1月8日10点00分前完成递交"
        self.assertEqual(parse_deadline(text), "2027-01-08 10:00")

    def test_date_only(self):
        self.assertEqual(parse_deadline("开标时间：2026/12/20"), "2026-12-20 00:00")

    def test_none(self):
        self.assertIsNone(parse_deadline("没有时间"))


class TestCron(unittest.TestCase):
    def test_step_minutes(self):
        from datetime import datetime
        self.assertTrue(cron_matches("*/30 * * * *", datetime(2026, 11, 13, 10, 30)))
        self.assertFalse(cron_matches("*/30 * * * *", datetime(2026, 11, 13, 10, 17)))

    def test_weekday_9am(self):
        from datetime import datetime
        friday_9 = datetime(2026, 11, 13, 9, 0)   # 周五
        sat_9 = datetime(2026, 11, 14, 9, 0)
        self.assertTrue(cron_matches("0 9 * * 1-5", friday_9))
        self.assertFalse(cron_matches("0 9 * * 1-5", sat_9))


if __name__ == "__main__":
    unittest.main(verbosity=2)
