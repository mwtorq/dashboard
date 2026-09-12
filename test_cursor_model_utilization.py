"""Tests for subscription included model utilization (allocated / used / remaining)."""
import datetime
import unittest

import cursor_dashboard as d


SAMPLE_SUMMARY = {
    "billingCycleStart": "2026-07-04T00:35:51.000Z",
    "billingCycleEnd": "2026-08-04T00:35:51.000Z",
    "membershipType": "ultra",
    "limitType": "user",
    "isUnlimited": False,
    "autoModelSelectedDisplayMessage": "You've used 98% of your included total usage",
    "namedModelSelectedDisplayMessage": "You've used 100% of your included API usage",
    "individualUsage": {
        "plan": {
            "enabled": True, "used": 40000, "limit": 40000, "remaining": 0,
            "autoPercentUsed": 98.109, "apiPercentUsed": 100, "totalPercentUsed": 98.5128,
        },
        "onDemand": {"enabled": True, "used": 1785, "limit": 35000, "remaining": 33215},
    },
    "teamUsage": {},
}

SAMPLE_PERIOD = {
    "planUsage": {
        "totalSpend": 40000, "includedSpend": 40000, "remaining": 0, "limit": 40000,
        "autoPercentUsed": 98.109, "apiPercentUsed": 100, "totalPercentUsed": 98.5128,
    },
    "displayMessage": "You've used 100% of your included usage",
    "autoModelSelectedDisplayMessage": "You've used 98% of your included total usage",
    "namedModelSelectedDisplayMessage": "You've used 100% of your included API usage",
}

SAMPLE_AGGS = [
    {
        "modelIntent": "composer-1.5",
        "inputTokens": "3200018",
        "outputTokens": "497699",
        "cacheReadTokens": "90338912",
        "totalCents": 2782.59541,
        "tier": 2,
    },
    {
        "modelIntent": "claude-4.6-opus-high-thinking",
        "inputTokens": "66",
        "outputTokens": "6074",
        "cacheWriteTokens": "120467",
        "cacheReadTokens": "1952379",
        "totalCents": 188.128825,
        "tier": 1,
    },
]


class PercentMessageTests(unittest.TestCase):
    def test_parses_integer(self):
        self.assertEqual(
            d._parse_percent_from_message(
                "You've used 98% of your included total usage"),
            98.0)

    def test_parses_hundred(self):
        self.assertEqual(
            d._parse_percent_from_message(
                "You've used 100% of your included API usage"),
            100.0)

    def test_missing(self):
        self.assertIsNone(d._parse_percent_from_message("unavailable"))
        self.assertIsNone(d._parse_percent_from_message(""))
        self.assertIsNone(d._parse_percent_from_message(None))


class ModelPoolTests(unittest.TestCase):
    def test_composer_and_auto_are_cursor_models(self):
        self.assertEqual(d._model_pool("auto"), "cursor")
        self.assertEqual(d._model_pool("composer-2"), "cursor")
        self.assertEqual(d._model_pool("composer-1.5"), "cursor")

    def test_named_models_are_other(self):
        self.assertEqual(d._model_pool("claude-4.6-opus"), "other")
        self.assertEqual(d._model_pool("gpt-5"), "other")
        self.assertEqual(d._model_pool("grok-4.6"), "other")

    def test_tier_overrides_name(self):
        self.assertEqual(d._model_pool("claude-4.6-opus", tier=2), "cursor")
        self.assertEqual(d._model_pool("composer-2", tier=1), "other")


class UtilizationTests(unittest.TestCase):
    def test_ultra_sample_allocated_used_remaining(self):
        util = d._model_utilization(SAMPLE_SUMMARY, SAMPLE_PERIOD)
        self.assertFalse(util["unlimited"])
        by_id = {p["id"]: p for p in util["pools"]}
        self.assertEqual(by_id["cursor"]["allocated_pct"], 100.0)
        self.assertEqual(by_id["cursor"]["used_pct"], 98.1)
        self.assertEqual(by_id["cursor"]["remaining_pct"], 1.9)
        self.assertEqual(by_id["other"]["used_pct"], 100.0)
        self.assertEqual(by_id["other"]["remaining_pct"], 0.0)
        self.assertEqual(by_id["total"]["used_pct"], 98.5)
        self.assertTrue(util["on_demand_enabled"])

    def test_over_allowance_not_clamped(self):
        summary = {
            "isUnlimited": False,
            "membershipType": "pro",
            "individualUsage": {
                "plan": {
                    "autoPercentUsed": 142.7,
                    "apiPercentUsed": 5,
                    "totalPercentUsed": 80,
                }
            },
        }
        util = d._model_utilization(summary, {})
        by_id = {p["id"]: p for p in util["pools"]}
        self.assertEqual(by_id["cursor"]["used_pct"], 142.7)
        self.assertEqual(by_id["cursor"]["remaining_pct"], 0.0)

    def test_unlimited_plan(self):
        util = d._model_utilization({"isUnlimited": True, "membershipType": "enterprise"}, {})
        self.assertTrue(util["unlimited"])
        self.assertTrue(all(p["unlimited"] for p in util["pools"]))
        self.assertIsNone(util["pools"][0]["allocated_pct"])

    def test_team_falls_back_to_display_messages(self):
        summary = {
            "membershipType": "team",
            "isUnlimited": False,
            "autoModelSelectedDisplayMessage":
                "You've used 42% of your included total usage",
            "namedModelSelectedDisplayMessage":
                "You've used 15% of your included API usage",
            "teamUsage": {"onDemand": {"enabled": True}},
        }
        util = d._model_utilization(summary, {})
        by_id = {p["id"]: p for p in util["pools"]}
        self.assertEqual(by_id["cursor"]["used_pct"], 42.0)
        self.assertEqual(by_id["other"]["used_pct"], 15.0)
        self.assertTrue(util["on_demand_enabled"])

    def test_zero_percent_is_kept(self):
        summary = {
            "individualUsage": {
                "plan": {
                    "autoPercentUsed": 0,
                    "apiPercentUsed": 0,
                    "totalPercentUsed": 0,
                }
            }
        }
        util = d._model_utilization(summary, {})
        by_id = {p["id"]: p for p in util["pools"]}
        self.assertEqual(by_id["cursor"]["used_pct"], 0.0)
        self.assertEqual(by_id["cursor"]["remaining_pct"], 100.0)


class AggregatedUsageTests(unittest.TestCase):
    def test_parses_forum_sample(self):
        rows = d._parse_aggregated_usage(SAMPLE_AGGS)
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[0]["model"], "composer-1.5")
        self.assertEqual(rows[0]["pool"], "cursor")
        self.assertAlmostEqual(rows[0]["cost_usd"], 27.825954, places=5)
        self.assertEqual(rows[1]["pool"], "other")
        spend = d._pool_spend(rows)
        self.assertGreater(spend["cursor"], spend["other"])
        self.assertAlmostEqual(spend["cursor"], 27.826, places=3)
        self.assertAlmostEqual(spend["other"], 1.8813, places=3)


class CycleMtdTests(unittest.TestCase):
    def test_cycle_mtd_exposes_pools_and_allocated_dollars(self):
        today = datetime.date(2026, 7, 20)
        data = {
            "billing_summary": SAMPLE_SUMMARY,
            "billing_period": SAMPLE_PERIOD,
            "billing_aggregations": d._parse_aggregated_usage(SAMPLE_AGGS),
            "sessions": [],
        }
        mtd = d._cycle_mtd(data, today)
        self.assertEqual(mtd["included_limit"], 400.0)
        self.assertEqual(mtd["included_usd"], 400.0)
        self.assertEqual(mtd["included_remaining"], 0.0)
        self.assertEqual(len(mtd["pools"]), 3)
        by_id = {p["id"]: p for p in mtd["pools"]}
        self.assertEqual(by_id["cursor"]["allocated_pct"], 100.0)
        self.assertGreater(by_id["cursor"]["metered_usd"], 0)
        self.assertEqual(len(mtd["cycle_models"]), 2)


class PageMarkupTests(unittest.TestCase):
    def test_dashboard_html_includes_utilization_section(self):
        html = d.PAGE
        self.assertIn('id="modelUtil"', html)
        self.assertIn("Included model utilization", html)
        self.assertIn("function renderModelUtil()", html)
        self.assertIn("used of", html)
        self.assertIn("allocated", html)


if __name__ == "__main__":
    unittest.main()
