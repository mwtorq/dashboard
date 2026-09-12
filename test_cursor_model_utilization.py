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

    def test_grok_is_cursor_models_pool(self):
        # Settings → Plan & Usage: "Cursor Models — Includes Cursor Grok and Composer"
        self.assertEqual(d._model_pool("grok-4.6"), "cursor")
        self.assertEqual(d._model_pool("cursor-grok"), "cursor")
        self.assertEqual(d._model_pool("grok-code"), "cursor")

    def test_tier_overrides_name(self):
        self.assertEqual(d._model_pool("claude-4.6-opus", tier=2), "cursor")
        self.assertEqual(d._model_pool("composer-2", tier=1), "other")
        self.assertEqual(d._model_pool("grok-4.6", tier=1), "other")

    def test_pool_labels_match_settings_pane(self):
        util = d._model_utilization({
            "individualUsage": {
                "plan": {"autoPercentUsed": 13, "apiPercentUsed": 100, "totalPercentUsed": 40}
            }
        }, {})
        by_id = {p["id"]: p for p in util["pools"]}
        self.assertEqual(by_id["cursor"]["label"], "Cursor Models")
        self.assertEqual(by_id["cursor"]["detail"], "Includes Cursor Grok and Composer")
        self.assertEqual(by_id["cursor"]["used_pct"], 13.0)
        self.assertEqual(by_id["other"]["label"], "Other Models")
        self.assertEqual(by_id["other"]["used_pct"], 100.0)


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

    def test_falls_back_to_included_spend_over_limit(self):
        # No percent fields and no display messages — still produce a total pool
        # from included spend ÷ plan limit so the dashboard section can render.
        summary = {
            "membershipType": "pro",
            "isUnlimited": False,
            "individualUsage": {
                "plan": {"enabled": True, "used": 2500, "limit": 10000, "remaining": 7500},
                "onDemand": {"enabled": False},
            },
        }
        util = d._model_utilization(
            summary, {}, included_used=25.0, included_limit=100.0,
            pool_spend={"cursor": 10.0, "other": 15.0})
        by_id = {p["id"]: p for p in util["pools"]}
        self.assertIn("total", by_id)
        self.assertEqual(by_id["total"]["used_pct"], 25.0)
        self.assertEqual(by_id["total"]["allocated_pct"], 100.0)
        self.assertEqual(by_id["cursor"]["used_pct"], 10.0)
        self.assertEqual(by_id["other"]["used_pct"], 15.0)

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
        self.assertIn("Included in plan", html)
        self.assertIn("Cursor Models (Grok + Composer)", html)
        self.assertIn("function renderModelUtil()", html)
        self.assertIn("used of", html)
        self.assertIn("allocated", html)
        # Section must be visible in HTML even before JS runs.
        self.assertNotIn('id="modelUtil" style="display:none"', html)
        self.assertIn("pools-v3", html)
        self.assertIn("Never hide this section", html)
        self.assertIn("Includes Cursor Grok and Composer", html)
        # Placed above plan-allowance so it is not missed.
        self.assertLess(html.find('id="modelUtil"'), html.find('id="mtd"'))



class SessionContextMarkupTests(unittest.TestCase):
    def test_session_context_render_is_defensive(self):
        html = d.PAGE
        self.assertIn("const jira=refs.jira||[]", html)
        self.assertIn("s.subtitle", html)
        self.assertIn('id="chatSessions"', html)
        self.assertIn("renderSessions", html)
        self.assertIn("Chat titles / PR context look thin", html)
        self.assertIn("fold:'+(s.id", html)



class UntitledTitleJoinTests(unittest.TestCase):
    def test_fill_missing_title_from_bubble_text(self):
        sess = {"title": "(untitled)"}
        self.assertTrue(d._fill_missing_title(
            sess, "Fix the auth redirect in login flow\nmore"))
        self.assertEqual(sess["title"], "Fix the auth redirect in login flow")

    def test_billing_join_uses_local_chat_text_as_title(self):
        cid = "conv-1"
        events = [{
            "conversationId": cid,
            "timestamp": 1_700_000_000_000,
            "model": "gpt-5",
            "kind": "INCLUDED",
            "tokenUsage": {"totalCents": 12, "inputTokens": 10, "outputTokens": 5,
                           "cacheWriteTokens": 0, "cacheReadTokens": 0},
        }]
        local = {
            cid: {
                "title": "",
                "text": "Implement PR review checklist\nsecond line",
                "repository": "acme/app",
                "branch": "main",
                "subtitle": "",
            }
        }
        sessions, _, _ = d._sessions_from_billing(events, local, {}, {})
        self.assertEqual(len(sessions), 1)
        self.assertEqual(sessions[0]["title"], "Implement PR review checklist")
        self.assertEqual(sessions[0]["repository"], "acme/app")
        self.assertFalse(sessions[0].get("orphan_billed"))

    def test_empty_stub_still_counts_as_orphan(self):
        notes = d._billing_match_notes("abc-123", {"title": "", "text": ""}, [{}])
        self.assertTrue(notes.get("orphan_billed"))
        notes2 = d._billing_match_notes(
            "bc-cloud", {"title": "(untitled)", "text": ""}, [{}])
        self.assertTrue(notes2.get("orphan_billed"))
        self.assertTrue(notes2.get("cloud_agent"))


class ComposerDataMergeTests(unittest.TestCase):
    def test_merge_upgrades_empty_composer_data_name(self):
        import json, sqlite3, tempfile, os
        with tempfile.TemporaryDirectory() as td:
            dst_path = os.path.join(td, "dst.vscdb")
            src_path = os.path.join(td, "src.vscdb")
            for path, name in ((dst_path, ""), (src_path, "Real chat title")):
                con = sqlite3.connect(path)
                con.execute("CREATE TABLE cursorDiskKV (key TEXT PRIMARY KEY, value BLOB)")
                blob = json.dumps({"name": name, "lastUpdatedAt": 200 if name else 100})
                con.execute(
                    "INSERT INTO cursorDiskKV(key, value) VALUES (?, ?)",
                    ("composerData:cid-1", blob))
                con.commit(); con.close()
            dst = sqlite3.connect(dst_path)
            src = sqlite3.connect(src_path)
            added, updated = d._merge_cursor_disk_kv(dst, src)
            self.assertEqual(added, 0)
            self.assertEqual(updated, 1)
            raw = dst.execute(
                "SELECT value FROM cursorDiskKV WHERE key=?",
                ("composerData:cid-1",)).fetchone()[0]
            self.assertEqual(json.loads(raw)["name"], "Real chat title")
            dst.close(); src.close()



class ItemTableComposerIndexTests(unittest.TestCase):
    def test_header_meta_reads_itemtable_composer_headers(self):
        import json, os, sqlite3, tempfile
        with tempfile.TemporaryDirectory() as td:
            db = os.path.join(td, "state.vscdb")
            con = sqlite3.connect(db)
            con.row_factory = sqlite3.Row
            con.execute("CREATE TABLE ItemTable (key TEXT PRIMARY KEY, value BLOB)")
            con.execute("CREATE TABLE cursorDiskKV (key TEXT PRIMARY KEY, value BLOB)")
            # No SQL composerHeaders table on purpose — Cursor 3.0+ shape.
            con.execute(
                "INSERT INTO ItemTable(key, value) VALUES (?, ?)",
                ("composer.composerHeaders", json.dumps({
                    "allComposers": [
                        {
                            "composerId": "cid-auth",
                            "name": "Fix auth redirect",
                            "lastUpdatedAt": 1700000000000,
                            "workspaceIdentifier": {
                                "id": "ws1",
                                "uri": {"fsPath": "/work/acme/webapp"},
                            },
                        }
                    ]
                })),
            )
            con.commit()
            meta = d._header_meta(con)
            con.close()
            self.assertIn("cid-auth", meta)
            self.assertEqual(meta["cid-auth"]["title"], "Fix auth redirect")
            self.assertTrue(meta["cid-auth"]["repository"])

    def test_itemtable_merge_upgrades_named_composer_index(self):
        import json, os, sqlite3, tempfile
        with tempfile.TemporaryDirectory() as td:
            dst = os.path.join(td, "dst.vscdb")
            src = os.path.join(td, "src.vscdb")
            for path, name in ((dst, ""), (src, "Real chat title")):
                con = sqlite3.connect(path)
                con.execute("CREATE TABLE ItemTable (key TEXT PRIMARY KEY, value BLOB)")
                con.execute(
                    "INSERT INTO ItemTable(key, value) VALUES (?, ?)",
                    ("composer.composerHeaders", json.dumps({
                        "allComposers": [{"composerId": "c1", "name": name}]
                    })),
                )
                con.commit(); con.close()
            dcon = sqlite3.connect(dst)
            scon = sqlite3.connect(src)
            added, updated = d._merge_item_table(dcon, scon)
            self.assertEqual(added, 0)
            self.assertEqual(updated, 1)
            raw = dcon.execute(
                "SELECT value FROM ItemTable WHERE key=?",
                ("composer.composerHeaders",)).fetchone()[0]
            blob = json.loads(raw)
            self.assertEqual(blob["allComposers"][0]["name"], "Real chat title")
            dcon.close(); scon.close()

    def test_billing_uses_itemtable_title_without_bubbles(self):
        events = [{
            "conversationId": "cid-auth",
            "timestamp": 1_700_000_000_000,
            "model": "gpt-5",
            "kind": "INCLUDED",
            "tokenUsage": {"totalCents": 5, "inputTokens": 3, "outputTokens": 2,
                           "cacheWriteTokens": 0, "cacheReadTokens": 0},
        }]
        meta = {
            "cid-auth": {
                "title": "Fix auth redirect",
                "subtitle": "",
                "repository": "acme/webapp",
                "branch": "main",
                "workspace_path": "/work/acme/webapp",
                "tracked_repos": ["acme/webapp"],
            }
        }
        local = {
            "cid-auth": d._session_stub_from_meta("cid-auth", meta, {}),
        }
        sessions, _, _ = d._sessions_from_billing(events, local, meta, {})
        self.assertEqual(sessions[0]["title"], "Fix auth redirect")


if __name__ == "__main__":
    unittest.main()
