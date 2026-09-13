"""Tests for subscription included model utilization (allocated / used / remaining)."""
import datetime
import os
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
        self.assertFalse(notes.get("cloud_agent"))
        self.assertIn("not a bc-* cloud agent id", notes.get("billing_note") or "")
        notes2 = d._billing_match_notes(
            "bc-cloud", {"title": "(untitled)", "text": ""}, [{}])
        self.assertTrue(notes2.get("orphan_billed"))
        self.assertTrue(notes2.get("cloud_agent"))
        self.assertIn("cloud agent", (notes2.get("billing_note") or "").lower())


class OrphanUuidResolutionTests(unittest.TestCase):
    def test_request_id_is_not_treated_as_conversation_id(self):
        ev = {"requestId": "req-only-uuid", "timestamp": 1, "model": "gpt-5",
              "tokenUsage": {"totalCents": 1}}
        self.assertEqual(d._billing_event_cid(ev), "_unattributed")

    def test_cloud_agent_id_extracted_separately_from_conversation_id(self):
        ev = {
            "conversationId": "26f96495-6a2b-4ca9-a111-222233334444",
            "cloudAgentId": "bc-574a3af1-0292-44f5-b9f1-5197b5c6641b",
            "timestamp": 1_700_000_000_000,
            "model": "claude-4.5-sonnet",
            "kind": "INCLUDED",
            "tokenUsage": {"totalCents": 50, "inputTokens": 1, "outputTokens": 1,
                           "cacheWriteTokens": 0, "cacheReadTokens": 0},
        }
        self.assertEqual(
            d._billing_event_cid(ev), "26f96495-6a2b-4ca9-a111-222233334444")
        self.assertEqual(
            d._billing_event_cloud_agent_id(ev),
            "bc-574a3af1-0292-44f5-b9f1-5197b5c6641b")
        sessions, _, _ = d._sessions_from_billing([ev], {}, {}, {})
        self.assertEqual(len(sessions), 1)
        self.assertTrue(sessions[0].get("orphan_billed"))
        self.assertEqual(
            sessions[0].get("cloud_agent_id"),
            "bc-574a3af1-0292-44f5-b9f1-5197b5c6641b")
        self.assertTrue(sessions[0].get("cloud_agent"))
        self.assertIn("cloudAgentId", sessions[0].get("billing_note") or "")

    def test_enrich_matches_via_cloud_agent_id_not_only_bc_session_id(self):
        cid = "26f96495-6a2b-4ca9-a111-222233334444"
        bc = "bc-574a3af1-0292-44f5-b9f1-5197b5c6641b"
        sessions = [{
            "session_id": cid,
            "title": "(untitled)",
            "orphan_billed": True,
            "cloud_agent_id": bc,
            "cost_usd": 1.0,
            "billed": True,
        }]
        agents = [{
            "id": bc,
            "name": "Investigate orphan billing IDs",
            "repository": "mwtorq/dashboard",
            "branch": "main",
            "url": f"https://cursor.com/agents/{bc}",
        }]
        out, _, _, n = d.enrich_sessions_with_cloud_agents(sessions, {}, {}, agents)
        self.assertGreaterEqual(n, 1)
        matched = next(s for s in out if s["session_id"] == cid)
        self.assertEqual(matched["title"], "Investigate orphan billing IDs")
        self.assertFalse(matched.get("orphan_billed"))
        self.assertTrue(matched.get("cloud_agent"))
        self.assertEqual(matched.get("repository"), "mwtorq/dashboard")

    def test_plain_uuid_orphan_label_avoids_cloud_agent_claim(self):
        notes = d._billing_match_notes(
            "26f96495-6a2b-4ca9-a111-222233334444",
            {"title": "", "text": ""},
            [{"isHeadless": False}])
        self.assertTrue(notes.get("orphan_billed"))
        self.assertFalse(notes.get("cloud_agent"))
        note = notes.get("billing_note") or ""
        self.assertNotIn("likely a cloud agent", note)
        self.assertIn("not a bc-*", note)

    def test_headless_orphan_label(self):
        notes = d._billing_match_notes(
            "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee",
            {"title": ""},
            [{"isHeadless": True}])
        self.assertIn("headless", (notes.get("billing_note") or "").lower())

    def test_fuzzy_match_orphan_to_nearby_local_chat(self):
        orphan_id = "bbbbbbbb-1111-2222-3333-444444444444"
        local_id = "cccccccc-aaaa-bbbb-cccc-dddddddddddd"
        ts = "2026-01-15T12:00:00+00:00"
        sess = {
            "session_id": orphan_id,
            "title": "(untitled)",
            "orphan_billed": True,
            "cost_usd": 1.25,
            "billed": True,
            "top_model": "gpt-5",
        }
        priced = [{"started_at": ts, "model": "gpt-5", "cost_usd": 1.25}]
        local = {
            "session_id": local_id,
            "title": "Fix dashboard orphan labels",
            "text": "please fix orphan labeling",
            "repository": "mwtorq/dashboard",
            "cost_usd": 1.20,
            "top_model": "gpt-5",
            "billed": False,
        }
        local_priced = {local_id: [{"started_at": ts, "model": "gpt-5", "cost_usd": 1.2}]}
        n = d.resolve_orphan_sessions(
            [sess], {local_id: local},
            priced_all={orphan_id: priced},
            local_priced=local_priced, con=None, meta={})
        self.assertEqual(n, 1)
        self.assertFalse(sess.get("orphan_billed"))
        self.assertEqual(sess["title"], "Fix dashboard orphan labels")
        self.assertEqual(sess.get("title_source"), "fuzzy-local")
        self.assertEqual(sess.get("matched_local_id"), local_id)

    def test_embedded_billing_uuid_in_composer_data(self):
        import json, os, sqlite3, tempfile
        orphan_id = "dddddddd-1111-2222-3333-444444444444"
        local_id = "eeeeeeee-aaaa-bbbb-cccc-dddddddddddd"
        with tempfile.TemporaryDirectory() as td:
            db = os.path.join(td, "state.vscdb")
            con = sqlite3.connect(db)
            con.row_factory = sqlite3.Row
            con.execute("CREATE TABLE cursorDiskKV (key TEXT PRIMARY KEY, value BLOB)")
            con.execute(
                "INSERT INTO cursorDiskKV(key, value) VALUES (?, ?)",
                (f"composerData:{local_id}",
                 json.dumps({
                     "name": "Embedded reverse map chat",
                     "billingConversationId": orphan_id,
                     "lastUpdatedAt": 1700000000000,
                 })),
            )
            con.commit()
            sess = {
                "session_id": orphan_id,
                "title": "(untitled)",
                "orphan_billed": True,
                "cost_usd": 0.5,
                "billed": True,
            }
            local = {
                "session_id": local_id,
                "title": "Embedded reverse map chat",
                "text": "hello",
                "repository": "acme/app",
            }
            n = d.resolve_orphan_sessions(
                [sess], {local_id: local}, priced_all={}, local_priced={},
                con=con, meta={})
            con.close()
            self.assertEqual(n, 1)
            self.assertEqual(sess["title"], "Embedded reverse map chat")
            self.assertEqual(sess.get("title_source"), "embedded-id")
            self.assertFalse(sess.get("orphan_billed"))

    def test_agent_id_bc_prefix_counts_as_cloud_agent_id(self):
        ev = {"agentId": "bc-aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"}
        self.assertEqual(
            d._billing_event_cloud_agent_id(ev),
            "bc-aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee")
        self.assertEqual(d._billing_event_cloud_agent_id({"agentId": "local-uuid"}), "")

    def test_enrich_fuzzy_matches_orphan_uuid_to_cloud_agent_by_time(self):
        cid = "11111111-2222-3333-4444-555555555555"
        sessions = [{
            "session_id": cid,
            "title": "(untitled)",
            "orphan_billed": True,
            "cost_usd": 2.0,
            "billed": True,
            "days": {"2026-09-12": {"cost_usd": 2.0}},
        }]
        agents = [{
            "id": "bc-99999999-aaaa-bbbb-cccc-dddddddddddd",
            "name": "Time window agent",
            "repository": "mwtorq/dashboard",
            "branch": "main",
            "url": "https://cursor.com/agents/x",
            "created_at": "2026-09-12T01:00:00+00:00",
            "updated_at": "2026-09-12T23:00:00+00:00",
        }]
        out, _, _, n = d.enrich_sessions_with_cloud_agents(sessions, {}, {}, agents)
        self.assertGreaterEqual(n, 1)
        matched = next(s for s in out if s["session_id"] == cid)
        self.assertEqual(matched["title"], "Time window agent")
        self.assertFalse(matched.get("orphan_billed"))
        self.assertTrue(matched.get("cloud_agent"))
        self.assertIn("time", (matched.get("title_source") or ""))

    def test_normalize_bare_uuid_becomes_bc_prefix(self):
        agent = d._normalize_cloud_agent({
            "bcId": "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee",
            "name": "Cookie-listed agent",
            "repoUrl": "https://github.com/mwtorq/dashboard.git",
            "createdAt": "2026-09-11T12:00:00.000Z",
        })
        self.assertEqual(agent["id"], "bc-aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee")
        self.assertEqual(agent["name"], "Cookie-listed agent")
        self.assertEqual(agent["repository"], "mwtorq/dashboard")

    def test_cookie_composer_list_parsed_into_agents(self):
        calls = []

        def fake_api(cookie, method, path, body=None, timeout=60):
            calls.append(path)
            if path.endswith("/list"):
                return {
                    "composers": [{
                        "bcId": "bc-11111111-2222-3333-4444-555555555555",
                        "name": "From session cookie",
                        "repoUrl": "https://github.com/acme/app",
                        "createdAtMs": 1_725_000_000_000,
                        "updatedAtMs": 1_725_000_100_000,
                    }]
                }
            raise RuntimeError("unexpected path")

        orig = d._api
        d._api = fake_api
        try:
            agents = d._list_background_composers_cookie("WorkosCursorSessionToken=u::t")
        finally:
            d._api = orig
        self.assertEqual(len(agents), 1)
        self.assertEqual(agents[0]["name"], "From session cookie")
        self.assertTrue(agents[0]["id"].startswith("bc-"))
        self.assertTrue(any(p.endswith("/list") for p in calls))

    def test_fetch_cloud_agents_uses_cookie_when_no_api_key(self):
        d._CLOUD_AGENTS_CACHE = {"at": 0, "agents": None, "error": "", "source": ""}
        # Ensure no API key in env for this process.
        old_key = d.CLOUD_AGENTS_API_KEY
        d.CLOUD_AGENTS_API_KEY = ""
        env_keys = ["CLOUD_AGENTS_API_KEY", "CURSOR_API_KEY", "CURSOR_CLOUD_API_KEY",
                    "CURSOR_DASH_API_KEY"]
        saved = {k: os.environ.pop(k, None) for k in env_keys}

        def fake_list(cookie):
            return [{
                "id": "bc-cookie-1",
                "name": "Cookie agent",
                "repository": "acme/app",
                "branch": "main",
                "url": "https://cursor.com/agents/bc-cookie-1",
                "created_at": "2026-09-11T00:00:00+00:00",
                "updated_at": "2026-09-11T01:00:00+00:00",
                "model": "auto",
                "usage": {},
                "status": "FINISHED",
                "repo_url": "",
            }]

        orig_list = d._list_background_composers_cookie
        orig_load = d._load_cloud_agents_cache_file
        orig_save = d._save_cloud_agents_cache_file
        d._list_background_composers_cookie = fake_list
        d._load_cloud_agents_cache_file = lambda: {}
        d._save_cloud_agents_cache_file = lambda *a, **k: None
        try:
            agents = d.fetch_cloud_agents(force=True, cookie="WorkosCursorSessionToken=x")
        finally:
            d._list_background_composers_cookie = orig_list
            d._load_cloud_agents_cache_file = orig_load
            d._save_cloud_agents_cache_file = orig_save
            d.CLOUD_AGENTS_API_KEY = old_key
            for k, v in saved.items():
                if v is not None:
                    os.environ[k] = v
        self.assertEqual(len(agents), 1)
        self.assertEqual(agents[0]["name"], "Cookie agent")
        self.assertEqual(d._CLOUD_AGENTS_CACHE.get("source"), "cookie")


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



class AdaptiveComposerHeadersTests(unittest.TestCase):
    def test_reads_alternate_sql_column_names(self):
        import json, os, sqlite3, tempfile
        with tempfile.TemporaryDirectory() as td:
            db = os.path.join(td, "state.vscdb")
            con = sqlite3.connect(db)
            con.row_factory = sqlite3.Row
            # Simulate a Cursor build with snake_case columns.
            con.execute(
                "CREATE TABLE composerHeaders ("
                "composer_id TEXT PRIMARY KEY, workspace_id TEXT, created_at INT, "
                "updated_at INT, archived INT, subagent INT, data BLOB)"
            )
            con.execute(
                "CREATE TABLE ItemTable (key TEXT PRIMARY KEY, value BLOB)"
            )
            con.execute(
                "CREATE TABLE cursorDiskKV (key TEXT PRIMARY KEY, value BLOB)"
            )
            con.execute(
                "INSERT INTO composerHeaders VALUES (?,?,?,?,?,?,?)",
                ("cid-sql", "ws", 1, 2, 0, 0,
                 json.dumps({"name": "SQL header title", "subtitle": "from sql"})),
            )
            con.commit()
            meta = d._header_meta(con)
            con.close()
            self.assertEqual(meta["cid-sql"]["title"], "SQL header title")

    def test_fill_titles_from_bubbles_for_untitled_sessions(self):
        import json, os, sqlite3, tempfile
        with tempfile.TemporaryDirectory() as td:
            db = os.path.join(td, "state.vscdb")
            con = sqlite3.connect(db)
            con.row_factory = sqlite3.Row
            con.execute("CREATE TABLE ItemTable (key TEXT PRIMARY KEY, value BLOB)")
            con.execute("CREATE TABLE cursorDiskKV (key TEXT PRIMARY KEY, value BLOB)")
            con.execute(
                "INSERT INTO cursorDiskKV VALUES (?, ?)",
                ("bubbleId:cid-b1:u1",
                 json.dumps({"type": 1, "text": "Refactor the payment webhook handler"})),
            )
            con.commit()
            sessions = [{"session_id": "cid-b1", "title": d.UNTITLED_TITLE}]
            n = d._fill_titles_from_local_content(con, sessions, {})
            con.close()
            self.assertEqual(n, 1)
            self.assertEqual(sessions[0]["title"], "Refactor the payment webhook handler")
            self.assertEqual(sessions[0]["title_source"], "bubble")

    def test_sample_bubble_rows_keeps_middle_pr_mentions(self):
        """Long chats must not drop older PR links sitting between head and tail."""
        cap = 20
        rows = []
        for i in range(60):
            text = f"noise bubble {i}"
            if i == 25:
                text = "Opened https://github.com/acme/app/pull/10 for the first fix"
            if i == 40:
                text = "Follow-up https://github.com/acme/app/pull/11"
            rows.append((f"2026-01-01T00:{i:02d}:00Z", text, 2, f"b{i}"))
        kept = d._sample_bubble_rows(rows, cap=cap)
        self.assertLessEqual(len(kept), cap)
        joined = "\n".join(r[1] for r in kept)
        self.assertIn("pull/10", joined)
        self.assertIn("pull/11", joined)

    def test_sample_cid_bubble_raws_keeps_middle_pr(self):
        import json, os, sqlite3, tempfile
        with tempfile.TemporaryDirectory() as td:
            db = os.path.join(td, "state.vscdb")
            con = sqlite3.connect(db)
            con.row_factory = sqlite3.Row
            con.execute("CREATE TABLE cursorDiskKV (key TEXT PRIMARY KEY, value BLOB)")
            cid = "cid-multi-pr"
            cap = 20
            for i in range(60):
                text = f"filler {i}"
                if i == 30:
                    text = "See https://github.com/acme/app/pull/22 in the middle"
                blob = json.dumps({
                    "type": 2,
                    "createdAt": f"2026-01-01T01:{i:02d}:00.000Z",
                    "text": text,
                })
                con.execute(
                    "INSERT INTO cursorDiskKV VALUES (?, ?)",
                    (f"bubbleId:{cid}:b{i:04d}", blob),
                )
            con.commit()
            sampled = d._sample_cid_bubble_raws(con, cid, cap=cap)
            con.close()
            self.assertLessEqual(len(sampled), cap)
            joined = "\n".join(
                (r[1].decode() if isinstance(r[1], bytes) else r[1]) for r in sampled)
            self.assertIn("pull/22", joined)

    def test_pr_cost_entries_keeps_zero_cost_mentioned_prs(self):
        """Earlier PRs in a session must appear even when spend is on a later PR."""
        session = {"session_id": "s1", "cost_usd": 5.0, "pr_turn_costs": {}}
        refs = {"prs": [
            {"key": "acme/app#10", "repo": "acme/app", "number": 10, "created": True},
            {"key": "acme/app#11", "repo": "acme/app", "number": 11, "created": True},
        ]}
        turn_prs = {"s1": {2: {"acme/app#11": True}}}
        turn_cost = {"s1": {0: (1.0, 100), 1: (1.0, 100), 2: (3.0, 100)}}
        entries = d._pr_cost_entries(session, refs, turn_prs, turn_cost)
        keys = {p["key"] for p, _c in entries}
        self.assertEqual(keys, {"acme/app#10", "acme/app#11"})
        by_key = {p["key"]: c for p, c in entries}
        self.assertEqual(by_key["acme/app#10"], 0)
        self.assertGreater(by_key["acme/app#11"], 0)

    def test_pr_cost_entries_still_drops_zero_cost_inferred(self):
        session = {"session_id": "s1", "cost_usd": 5.0, "pr_turn_costs": {}}
        refs = {"prs": [
            {"key": "acme/app#99", "repo": "acme/app", "number": 99,
             "created": False, "inferred": True},
        ]}
        entries = d._pr_cost_entries(session, refs, {}, {})
        self.assertEqual(entries, [])

    def test_build_refs_keeps_multiple_prs_from_one_session(self):
        rows = [(
            "s1",
            "First https://github.com/acme/app/pull/10 then later "
            "https://github.com/acme/app/pull/11 and acme/app#12",
        )]
        refs = d._build_refs(rows, {}, set())
        keys = {p["key"] for p in refs["s1"]["prs"]}
        self.assertEqual(keys, {"acme/app#10", "acme/app#11", "acme/app#12"})

    def test_rollup_lists_zero_cost_mentioned_pr(self):
        sessions = [{
            "session_id": "s1", "title": "Multi PR chat", "cost_usd": 4.0,
            "on_demand_usd": 0, "total_tokens": 100, "turns": 2,
            "billed": True, "est": False, "repository": "acme/app",
        }]
        refs = {"s1": {"jira": [], "repos": [], "prs": [
            {"key": "acme/app#10", "repo": "acme/app", "number": 10, "created": True},
            {"key": "acme/app#11", "repo": "acme/app", "number": 11, "created": True},
        ]}}
        turn_prs = {"s1": {1: {"acme/app#11": True}}}
        turn_cost = {"s1": {0: (1.0, 50), 1: (3.0, 50)}}
        out = d.rollup(sessions, refs, turn_prs, turn_cost)
        keys = {p["key"] for p in out["prs"]}
        self.assertEqual(keys, {"acme/app#10", "acme/app#11"})

    def test_bubble_dict_pulls_prs_from_tool_payload(self):
        """PR URLs buried in tool JSON (not text/richText) must still become refs."""
        import json
        raw = json.dumps({
            "type": 2,
            "createdAt": "2026-01-01T00:00:00Z",
            "text": "",
            "toolFormerData": {
                "result": "Opened https://github.com/acme/app/pull/10 successfully",
            },
        })
        row = d._bubble_dict_from_raw("bubbleId:cid:tool1", raw)
        self.assertIsNotNone(row)
        self.assertIn("pull/10", row["text"])

    def test_hidden_tool_prs_reach_build_refs(self):
        """Earlier tool-only PRs must not be lost when later visible text has another PR."""
        import json
        blobs = [
            json.dumps({
                "type": 2, "createdAt": "2026-01-01T00:00:00Z", "text": "",
                "toolFormerData": {"result": "https://github.com/acme/app/pull/10"},
            }),
            json.dumps({
                "type": 2, "createdAt": "2026-01-01T00:05:00Z",
                "text": "Also filed https://github.com/acme/app/pull/11",
            }),
            json.dumps({
                "type": 2, "createdAt": "2026-01-01T00:10:00Z", "text": "done",
                "additionalData": {
                    "url": "https://api.github.com/repos/acme/app/pulls/12",
                },
            }),
        ]
        texts = []
        for i, raw in enumerate(blobs):
            row = d._bubble_dict_from_raw(f"bubbleId:cid:b{i}", raw)
            self.assertIsNotNone(row)
            texts.append(row["text"])
        refs = d._build_refs([("s1", "\n".join(texts))], {}, set())
        keys = {p["key"] for p in refs["s1"]["prs"]}
        self.assertEqual(keys, {"acme/app#10", "acme/app#11", "acme/app#12"})

    def test_truncate_prefer_pr_keeps_links_past_limit(self):
        filler = "x" * 12050
        text = filler + "\nSee https://github.com/acme/app/pull/99 at the end"
        clipped = d._truncate_prefer_pr(text, 12000)
        self.assertLessEqual(len(clipped), 12000)
        self.assertIn("pull/99", clipped)

    def test_git_activity_gate_keeps_bare_mentions_without_merge(self):
        sessions = [{
            "session_id": "s1", "cost_usd": 1.0,
            "repository": "acme/app",
        }]
        refs = {"s1": {"jira": [], "repos": [{"name": "acme/app", "role": "primary"}],
                       "prs": [
                           {"key": "acme/app#10", "repo": "acme/app", "number": 10,
                            "created": False, "bare": True},
                           {"key": "acme/app#11", "repo": "acme/app", "number": 11,
                            "created": True},
                       ]}}
        d._apply_git_activity_gate(sessions, refs, {}, [], {})
        keys = {p["key"] for p in refs["s1"]["prs"] if p.get("role") != "skipped"}
        self.assertEqual(keys, {"acme/app#10", "acme/app#11"})



class MultiPrCaptureAndDayScopeTests(unittest.TestCase):
    """Full-composer PR scan; clip keeps refs like Copilot (pre-#27)."""

    def test_full_scan_keeps_all_tool_prs_not_only_latest_two(self):
        import json, os, sqlite3, tempfile
        with tempfile.TemporaryDirectory() as td:
            db = os.path.join(td, "state.vscdb")
            con = sqlite3.connect(db)
            con.execute("CREATE TABLE cursorDiskKV (key TEXT PRIMARY KEY, value BLOB)")
            cid = "chat-many-prs"
            for i, n in enumerate([10, 11, 12, 13]):
                blob = json.dumps({
                    "type": 2,
                    "createdAt": f"2026-09-{10 + i:02d}T12:00:00.000Z",
                    "text": "",
                    "toolFormerData": {
                        "result": f"Opened https://github.com/acme/app/pull/{n}"
                    },
                })
                con.execute(
                    "INSERT INTO cursorDiskKV VALUES (?, ?)",
                    (f"bubbleId:{cid}:b{i}", blob),
                )
            for i in range(50, 400):
                con.execute(
                    "INSERT INTO cursorDiskKV VALUES (?, ?)",
                    (f"bubbleId:{cid}:n{i}",
                     json.dumps({"type": 2, "createdAt": f"2026-09-12T01:{i % 60:02d}:00Z",
                                 "text": f"noise {i}"})),
                )
            con.commit()
            texts = {}
            added, hints = d._inject_composer_pr_links(con, texts, [cid])
            con.close()
            self.assertGreaterEqual(added, 4)
            refs = d._build_refs([(cid, texts[cid])], {}, set())
            keys = {p["key"] for p in refs[cid]["prs"]}
            self.assertEqual(keys, {"acme/app#10", "acme/app#11", "acme/app#12", "acme/app#13"})
            self.assertIn(cid, hints)

    def test_clip_scopes_prs_by_github_local_days(self):
        """Yesterday/Today list different PRs from GitHub local create/merge days."""
        sess = {
            "session_id": "s1",
            "title": "multi-day agent",
            "first_day": "2026-09-11",
            "last_day": "2026-09-12",
            "days": {
                "2026-09-11": {
                    "cost_usd": 2.0, "requests": 1, "total_tokens": 10,
                    "input_tokens": 5, "output_tokens": 5,
                    "cache_read_tokens": 0, "cache_write_tokens": 0,
                    "measured_tokens": 10, "est_usd": 0.0, "on_demand_usd": 0.0,
                },
                "2026-09-12": {
                    "cost_usd": 5.0, "requests": 1, "total_tokens": 10,
                    "input_tokens": 5, "output_tokens": 5,
                    "cache_read_tokens": 0, "cache_write_tokens": 0,
                    "measured_tokens": 10, "est_usd": 0.0, "on_demand_usd": 0.0,
                },
            },
            "by_model_day": {},
            "refs": {
                "jira": [], "repos": [],
                "prs": [
                    {"key": "acme/app#20", "repo": "acme/app", "number": 20,
                     "created": True, "first_day": "2026-09-11",
                     "last_day": "2026-09-11", "days": ["2026-09-11"],
                     "day_source": "github"},
                    {"key": "acme/app#24", "repo": "acme/app", "number": 24,
                     "created": True, "first_day": "2026-09-11",
                     "last_day": "2026-09-12",
                     "days": ["2026-09-11", "2026-09-12"],
                     "day_source": "github"},
                    {"key": "acme/app#25", "repo": "acme/app", "number": 25,
                     "created": True, "first_day": "2026-09-12",
                     "last_day": "2026-09-12", "days": ["2026-09-12"],
                     "day_source": "github"},
                    {"key": "acme/app#28", "repo": "acme/app", "number": 28,
                     "created": True, "first_day": "2026-09-12",
                     "last_day": "2026-09-12", "days": ["2026-09-12"],
                     "day_source": "github"},
                ],
            },
            "billed": True,
        }
        yesterday = d._clip(sess, "2026-09-11", "2026-09-11")
        today = d._clip(sess, "2026-09-12", "2026-09-12")
        self.assertEqual(
            [p["number"] for p in yesterday["refs"]["prs"]], [20, 24])
        self.assertEqual(
            [p["number"] for p in today["refs"]["prs"]], [24, 25, 28])

    def test_github_days_override_today_remention_of_old_pr(self):
        """Re-mentioning #20 today must not put it on Today's badge list."""
        import os, time
        prev = os.environ.get("TZ")
        try:
            os.environ["TZ"] = "America/Chicago"
            time.tzset()
            fake = [
                {"number": 20, "createdAt": "2026-09-12T03:18:25Z",
                 "mergedAt": "2026-09-12T03:18:55Z"},
                {"number": 25, "createdAt": "2026-09-12T12:38:58Z",
                 "mergedAt": "2026-09-12T23:13:45Z"},
            ]
            prev_list = d._list_repo_prs_via_gh
            d._list_repo_prs_via_gh = lambda repo, limit=100: fake
            refs = {"s": {"jira": [], "repos": [], "prs": [
                {"key": "acme/app#20", "repo": "acme/app", "number": 20,
                 "created": True, "first_day": "2026-09-12",
                 "last_day": "2026-09-12", "days": ["2026-09-12"],
                 "day_source": "mention"},
                {"key": "acme/app#25", "repo": "acme/app", "number": 25,
                 "created": True},
            ]}}
            d._stamp_pr_github_dates(refs)
            d._stamp_pr_mention_days(
                refs,
                {"s": [{"turn_index": 0, "started_at": "2026-09-12T20:00:00-05:00"}]},
                {"s": {0: ["acme/app#20"]}},
                None)
            sess = {
                "session_id": "s", "first_day": "2026-09-12", "last_day": "2026-09-12",
                "days": {"2026-09-12": {
                    "cost_usd": 1, "requests": 1, "total_tokens": 1,
                    "input_tokens": 1, "output_tokens": 0, "cache_read_tokens": 0,
                    "cache_write_tokens": 0, "measured_tokens": 1, "est_usd": 0,
                    "on_demand_usd": 0}},
                "by_model_day": {}, "refs": refs["s"], "billed": True,
            }
            today = d._clip(sess, "2026-09-12", "2026-09-12")
            self.assertEqual([p["number"] for p in today["refs"]["prs"]], [25])
            d._list_repo_prs_via_gh = prev_list
        finally:
            if prev is None:
                os.environ.pop("TZ", None)
            else:
                os.environ["TZ"] = prev
            time.tzset()

    def test_local_day_uses_machine_timezone_not_utc_date(self):
        """UTC early-morning timestamps must follow machine local calendar day."""
        import os, time
        prev = os.environ.get("TZ")
        try:
            os.environ["TZ"] = "America/Chicago"
            time.tzset()
            # 03:18 UTC on Sep 12 is still Sep 11 in America/Chicago.
            self.assertEqual(d._local_day("2026-09-12T03:18:25Z"), "2026-09-11")
            os.environ["TZ"] = "UTC"
            time.tzset()
            self.assertEqual(d._local_day("2026-09-12T03:18:25Z"), "2026-09-12")
        finally:
            if prev is None:
                os.environ.pop("TZ", None)
            else:
                os.environ["TZ"] = prev
            time.tzset()

    def test_enrich_cloud_agent_prs_from_github_footer(self):
        """PRs with bc-* in body attach even when bubbles missed them."""
        fake = [
            {"number": 20, "url": "https://github.com/acme/app/pull/20",
             "createdAt": "2026-09-12T03:18:25Z",
             "mergedAt": "2026-09-12T03:18:55Z",
             "body": "footer bc-aaaa"},
            {"number": 24, "url": "https://github.com/acme/app/pull/24",
             "createdAt": "2026-09-12T03:53:46Z",
             "mergedAt": "2026-09-12T12:24:19Z",
             "body": "footer bc-aaaa"},
            {"number": 25, "url": "https://github.com/acme/app/pull/25",
             "createdAt": "2026-09-12T12:38:58Z",
             "mergedAt": "2026-09-12T23:13:45Z",
             "body": "footer bc-aaaa"},
            {"number": 99, "url": "https://github.com/acme/app/pull/99",
             "createdAt": "2026-09-12T15:00:00Z", "mergedAt": None,
             "body": "other agent bc-bbbb"},
        ]
        prev = d._list_repo_prs_via_gh
        try:
            d._list_repo_prs_via_gh = lambda repo, limit=100: fake
            sess = {
                "session_id": "bc-aaaa",
                "cloud_agent": True,
                "cloud_agent_id": "bc-aaaa",
                "repository": "acme/app",
                "refs": {"jira": [], "prs": [], "repos": []},
            }
            refs = {"bc-aaaa": sess["refs"]}
            added = d._enrich_cloud_agent_prs([sess], refs)
            self.assertEqual(added, 3)
            nums = sorted(p["number"] for p in refs["bc-aaaa"]["prs"])
            self.assertEqual(nums, [20, 24, 25])
            by_n = {p["number"]: p for p in refs["bc-aaaa"]["prs"]}
            # Stamps come from `_local_day` of create/merge — assert fields exist.
            self.assertTrue(by_n[20].get("first_day"))
            self.assertTrue(by_n[24].get("last_day"))
            self.assertGreaterEqual(by_n[24]["last_day"], by_n[24]["first_day"])
        finally:
            d._list_repo_prs_via_gh = prev



    def test_stamp_falls_back_when_gh_list_empty(self):
        """If `gh pr list` returns nothing, per-PR timestamps still stamp all PRs."""
        import os, time
        prev_tz = os.environ.get("TZ")
        prev_list = d._list_repo_prs_via_gh
        prev_ts = d._github_pr_timestamps
        try:
            os.environ["TZ"] = "America/Chicago"
            time.tzset()
            d._list_repo_prs_via_gh = lambda repo, limit=100: []
            def fake_ts(repo, number):
                table = {
                    20: ("2026-09-12T03:18:25Z", "2026-09-12T03:18:55Z"),
                    24: ("2026-09-12T03:53:46Z", "2026-09-12T12:24:19Z"),
                    25: ("2026-09-12T12:38:58Z", "2026-09-12T23:13:45Z"),
                    28: ("2026-09-12T23:47:55Z", "2026-09-12T23:48:35Z"),
                }
                return table.get(int(number), ("", ""))
            d._github_pr_timestamps = fake_ts
            refs = {"s": {"prs": [
                {"key": "acme/app#20", "number": 20,
                 "first_day": "2026-09-12", "last_day": "2026-09-12",
                 "days": ["2026-09-12"], "day_source": "mention"},
                {"key": "acme/app#24", "number": 24},
                {"key": "acme/app#25", "number": 25},
                {"key": "acme/app#28", "number": 28},
            ]}}
            self.assertEqual(d._stamp_pr_github_dates(refs), 4)
            sess = {
                "session_id": "s", "first_day": "2026-09-11", "last_day": "2026-09-12",
                "days": {
                    "2026-09-11": {"cost_usd": 1, "requests": 1, "total_tokens": 1,
                        "input_tokens": 1, "output_tokens": 0, "cache_read_tokens": 0,
                        "cache_write_tokens": 0, "measured_tokens": 1, "est_usd": 0,
                        "on_demand_usd": 0},
                    "2026-09-12": {"cost_usd": 1, "requests": 1, "total_tokens": 1,
                        "input_tokens": 1, "output_tokens": 0, "cache_read_tokens": 0,
                        "cache_write_tokens": 0, "measured_tokens": 1, "est_usd": 0,
                        "on_demand_usd": 0},
                },
                "by_model_day": {}, "refs": refs["s"], "billed": True,
            }
            y = [p["number"] for p in d._clip(sess, "2026-09-11", "2026-09-11")["refs"]["prs"]]
            today = [p["number"] for p in d._clip(sess, "2026-09-12", "2026-09-12")["refs"]["prs"]]
            self.assertEqual(y, [20, 24])
            self.assertEqual(today, [24, 25, 28])
        finally:
            d._list_repo_prs_via_gh = prev_list
            d._github_pr_timestamps = prev_ts
            if prev_tz is None:
                os.environ.pop("TZ", None)
            else:
                os.environ["TZ"] = prev_tz
            time.tzset()

    def test_list_repo_prs_falls_back_to_rest_when_gh_empty(self):
        """Empty/failed `gh pr list` must still return REST catalog for enrichment."""
        prev_gh = d._gh_json
        prev_rest = d._list_repo_prs_via_rest
        try:
            d._gh_json = lambda args, timeout=60: []
            d._list_repo_prs_via_rest = lambda repo, limit=100: [
                {"number": 14, "body": "bc-aaaa", "createdAt": "2026-09-12T01:17:11Z",
                 "mergedAt": "2026-09-12T01:27:38Z", "url": "https://github.com/acme/app/pull/14"},
            ]
            out = d._list_repo_prs_via_gh("acme/app")
            self.assertEqual([p["number"] for p in out], [14])
        finally:
            d._gh_json = prev_gh
            d._list_repo_prs_via_rest = prev_rest

    def test_enrich_via_rest_fills_yesterday_prs_14_through_23(self):
        """When bubbles only mention #20, REST enrichment still attaches #14–#23
        onto Yesterday (local Chicago create/merge of early Sep 12 UTC)."""
        import os, time
        prev_tz = os.environ.get("TZ")
        prev_gh = d._gh_json
        prev_rest = d._list_repo_prs_via_rest
        try:
            os.environ["TZ"] = "America/Chicago"
            time.tzset()
            # Simulate broken/empty gh pr list — enrichment must use REST.
            d._gh_json = lambda args, timeout=60: []
            agent = "bc-a9f74d1c-2262-4bd5-a3d1-6bc4ad5499b3"
            # Real create/merge times for #14–#24 from mwtorq/dashboard.
            times = {
                14: ("2026-09-12T01:17:11Z", "2026-09-12T01:27:38Z"),
                15: ("2026-09-12T01:35:25Z", "2026-09-12T01:36:45Z"),
                16: ("2026-09-12T01:44:45Z", "2026-09-12T01:45:15Z"),
                17: ("2026-09-12T02:12:32Z", "2026-09-12T02:12:59Z"),
                18: ("2026-09-12T02:19:26Z", "2026-09-12T03:06:59Z"),
                19: ("2026-09-12T03:06:37Z", "2026-09-12T03:07:12Z"),
                20: ("2026-09-12T03:18:25Z", "2026-09-12T03:18:55Z"),
                21: ("2026-09-12T03:26:53Z", "2026-09-12T03:27:24Z"),
                22: ("2026-09-12T03:32:46Z", "2026-09-12T03:33:03Z"),
                23: ("2026-09-12T03:45:42Z", "2026-09-12T03:46:48Z"),
                24: ("2026-09-12T03:53:46Z", "2026-09-12T12:24:19Z"),
                25: ("2026-09-12T12:38:58Z", "2026-09-12T23:13:45Z"),
            }
            fake_rest = []
            for num, (c, m) in times.items():
                fake_rest.append({
                    "number": num,
                    "url": f"https://github.com/acme/app/pull/{num}",
                    "createdAt": c,
                    "mergedAt": m,
                    "body": f"Cloud agent footer {agent}",
                    "title": f"PR {num}",
                    "state": "closed",
                    "headRefName": f"cursor/x-{num}",
                })
            d._list_repo_prs_via_rest = lambda repo, limit=100: list(fake_rest)
            # Only #20 was scraped from chat — the Yesterday-only symptom.
            refs = {"bc-sess": {"jira": [], "repos": [], "prs": [
                {"key": "acme/app#20", "repo": "acme/app", "number": 20,
                 "created": True, "first_day": "2026-09-11",
                 "last_day": "2026-09-11", "days": ["2026-09-11"],
                 "day_source": "mention"},
            ]}}
            sess = {
                "session_id": "bc-sess",
                "cloud_agent": True,
                # Agent id only on cloud_url (common billed-row shape).
                "cloud_url": f"https://cursor.com/agents/{agent}",
                "repository": "acme/app",
                "first_day": "2026-09-11",
                "last_day": "2026-09-12",
                "days": {
                    "2026-09-11": {"cost_usd": 2, "requests": 1, "total_tokens": 10,
                        "input_tokens": 5, "output_tokens": 5, "cache_read_tokens": 0,
                        "cache_write_tokens": 0, "measured_tokens": 10, "est_usd": 0,
                        "on_demand_usd": 0},
                    "2026-09-12": {"cost_usd": 5, "requests": 1, "total_tokens": 10,
                        "input_tokens": 5, "output_tokens": 5, "cache_read_tokens": 0,
                        "cache_write_tokens": 0, "measured_tokens": 10, "est_usd": 0,
                        "on_demand_usd": 0},
                },
                "by_model_day": {},
                "refs": refs["bc-sess"],
                "billed": True,
            }
            added = d._enrich_cloud_agent_prs([sess], refs)
            self.assertGreaterEqual(added, 11)  # 14-19,21-25 at least
            d._stamp_pr_github_dates(refs)
            sess["refs"] = refs["bc-sess"]
            yesterday = [p["number"] for p in
                         d._clip(sess, "2026-09-11", "2026-09-11")["refs"]["prs"]]
            today = [p["number"] for p in
                     d._clip(sess, "2026-09-12", "2026-09-12")["refs"]["prs"]]
            self.assertEqual(yesterday, list(range(14, 25)))  # 14-23 + 24
            self.assertEqual(today, [24, 25])
        finally:
            d._gh_json = prev_gh
            d._list_repo_prs_via_rest = prev_rest
            if prev_tz is None:
                os.environ.pop("TZ", None)
            else:
                os.environ["TZ"] = prev_tz
            time.tzset()


if __name__ == "__main__":
    unittest.main()
