"""Offline severity and investigation regressions: mocked connections, no credentials or network.

SQLite evaluates the ordinary SELECTs against an in-memory fixture so missing SQL
predicates actually change results. Full-text matching/scoring is a small stub;
these tests do not claim to validate SereneDB's text search or query planner.
"""
import asyncio
import hashlib
import inspect
import json
import os
import sqlite3
import unittest
from datetime import datetime, timedelta
from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import MagicMock, patch


# Mock configuration reads during import, without loading .env or changing any
# environment values. All subsequent database connections are mocked in setUp.
_env_getitem = type(os.environ).__getitem__
with patch("dotenv.load_dotenv"), patch.object(
    type(os.environ), "__getitem__",
    lambda self, key: "unused-test-dsn" if key == "SERENEDB_DSN" else _env_getitem(self, key),
):
    import tools

import agent


class SeverityFilterTests(unittest.TestCase):
    def setUp(self):
        db = sqlite3.connect(":memory:")
        db.row_factory = sqlite3.Row
        # SQLite's DISTINCT aggregate syntax lacks SereneDB's separator argument.
        class StringAgg:
            def __init__(self):
                self.values = set()

            def step(self, value):
                if value is not None:
                    self.values.add(value)

            def finalize(self):
                return ",".join(sorted(self.values))

        db.create_aggregate("string_agg", 1, StringAgg)
        self.db = db
        self.addCleanup(db.close)
        db.execute("CREATE TABLE events_v (dataset TEXT, line_id INTEGER, ts TEXT, level TEXT, "
                   "severity INTEGER, component TEXT, team TEXT, source TEXT, template_id TEXT, "
                   "template TEXT, content TEXT, is_alert BOOLEAN, alert_type TEXT)")
        # E19 deliberately mixes severity levels. INFO deliberately contains
        # 'FATAL', while the actual FATAL line does not contain that word.
        events = [
            (1, "FATAL", 3, "E19", "data TLB error interrupt", False),
            (2, "INFO", 0, "Einfo", "FATAL is the configured threshold", False),
            (3, "WARNING", 1, "E19", "data TLB warning", False),
            (4, "ERROR", 2, "E19", "data TLB error", False),
            (5, "SEVERE", 3, "Ecritical", "machine check", False),
            (6, "CRITICAL", 3, "Ecritical", "service stopped", False),
            (7, "FAILURE", 3, "Ecritical", "hardware failure", False),
            (8, "INFO", 0, "Ealert", "labelled alert", True),
        ]
        db.executemany("INSERT INTO events_v VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)", [
            ("bgl", line, f"2005-06-05 00:00:{line:02d}", level, severity,
             "KERNEL", "compute-kernel", "node-a", template, template, content, alert, None)
            for line, level, severity, template, content, alert in events
        ])
        db.execute("ALTER TABLE events_v ADD COLUMN is_problem BOOLEAN GENERATED ALWAYS AS "
                   "(severity >= 1 OR COALESCE(is_alert, false))")
        db.execute("CREATE VIEW events_idx AS SELECT * FROM events_v")
        db.execute("CREATE VIEW component_team AS SELECT DISTINCT dataset, component, team FROM events_v")

        def execute(sql, params):
            # Only stub the SereneDB-specific full-text functions. All structured
            # filtering, grouping, ordering, joins and limits execute as written.
            sql = sql.replace("content @@ websearch_to_tsquery(%s)",
                              "instr(lower(content), lower(%s)) > 0")
            sql = sql.replace("BM25(events_idx.tableoid)", "1.0")
            sql = sql.replace("string_agg(DISTINCT component, ',')", "string_agg(DISTINCT component)")
            sql = sql.replace("string_agg(DISTINCT team, ',')", "string_agg(DISTINCT team)")
            sql = sql.replace("CAST(ts AS DATE)", "date(ts)")
            params = [value.isoformat(sep=" ") if isinstance(value, datetime) else value for value in params]
            rows = [dict(row) for row in db.execute(sql.replace("%s", "?"), params).fetchall()]
            # Real psycopg returns datetime objects; exercise Evidence's timestamp serialization too.
            for row in rows:
                if row.get("ts"):
                    row["ts"] = datetime.fromisoformat(row["ts"])
            cursor = MagicMock()
            cursor.fetchall.return_value = rows
            return cursor

        self.connection = MagicMock()
        self.connection.__enter__.return_value = self.connection
        self.connection.execute.side_effect = execute
        self.connect = self.enterContext(patch.object(tools.psycopg, "connect", return_value=self.connection))

    def test_level_filters_structured_field_not_content(self):
        result = tools.search_logs(dataset="bgl", level="fatal")
        self.assertEqual(result["data"]["total_matches"], 1)
        self.assertEqual([r["line_id"] for r in result["data"]["lines"]], [1])
        self.assertNotIn("FATAL", result["data"]["lines"][0]["content"])
        self.assertEqual(result["filters"]["level"], "fatal")
        self.assertIn("level=fatal", result["scope"])

    def test_empty_text_with_level_uses_structured_retrieval(self):
        result = tools.search_logs(text="", dataset="bgl", level="FATAL")
        self.assertEqual(result["data"]["total_matches"], 1)
        self.assertNotIn("@@", result["queries"][0]["sql"])
        self.assertNotIn("BM25", result["queries"][1]["sql"])

    def test_text_search_still_finds_info_containing_fatal(self):
        result = tools.search_logs("FATAL", "bgl")
        self.assertEqual([r["line_id"] for r in result["data"]["lines"]], [2])
        self.assertIn("BM25(events_idx.tableoid)", result["queries"][1]["sql"])
        self.assertEqual(result["filters"]["sort"], "relevance")
        self.assertEqual(result["data"]["lines"][0]["score"], 1.0)

    def test_text_and_level_are_combined(self):
        result = tools.search_logs("FATAL", dataset="bgl", level="FATAL")
        self.assertEqual(result["data"]["total_matches"], 0)
        self.assertEqual(result["data"]["lines"], [])

    def test_severity_only_has_no_full_text_or_bm25(self):
        result = tools.search_logs(dataset="bgl", severity=3)
        self.assertEqual([r["line_id"] for r in result["data"]["lines"]], [1, 5, 6, 7])
        self.assertEqual(result["filters"]["sort"], "time")
        self.assertIn("severity=3", result["scope"])
        for query in result["queries"]:
            self.assertNotIn("BM25", query["sql"])
            self.assertNotIn("@@", query["sql"])
            self.assertNotIn("events_idx", query["sql"])
        self.assertTrue(all(r["score"] is None for r in result["data"]["lines"]))
        self.assertEqual(set(result["data"]), {"total_matches", "matches_by_dataset", "lines"})

    def test_text_with_numeric_severity(self):
        result = tools.search_logs("data", dataset="bgl", severity=3)
        self.assertEqual([r["line_id"] for r in result["data"]["lines"]], [1])

    def test_count_level_excludes_info_containing_fatal(self):
        result = tools.count_problems(group_by="template", dataset="bgl", level="fatal")
        self.assertEqual(result["data"]["total_lines"], 1)
        self.assertEqual(result["data"]["groups"][0]["template"], "E19")
        self.assertEqual(result["data"]["groups"][0]["share_pct"], 100.0)

    def test_mixed_template_counts_only_matching_severity(self):
        result = tools.count_problems(group_by="template", dataset="bgl", severity=3)
        groups = {g["template"]: g for g in result["data"]["groups"]}
        self.assertEqual(result["data"]["total_lines"], 4)
        self.assertEqual(result["data"]["total_groups"], 2)
        self.assertEqual(groups["E19"]["lines"], 1)
        self.assertEqual(groups["E19"]["share_pct"], 25.0)
        self.assertEqual(groups["Ecritical"]["lines"], 3)
        self.assertEqual(groups["Ecritical"]["share_pct"], 75.0)
        self.assertEqual(result["data"]["groups"][0]["template"], "Ecritical")

    def test_template_ranking_includes_all_filtered_components_and_teams(self):
        self.db.execute("UPDATE events_v SET component='APP', team='job-runtime' WHERE line_id=5")
        self.db.execute("UPDATE events_v SET component='LOW', team='lower-severity-team' WHERE line_id=3")
        groups = {g["template"]: g for g in tools.count_problems(group_by="template", severity=3)["data"]["groups"]}
        self.assertEqual(set(groups["Ecritical"]["components"].split(",")), {"APP", "KERNEL"})
        self.assertEqual(set(groups["Ecritical"]["teams"].split(",")), {"job-runtime", "compute-kernel"})
        self.assertEqual(groups["E19"]["components"], "KERNEL")
        self.assertEqual(groups["E19"]["teams"], "compute-kernel")

    def test_count_queries_and_metadata_use_identical_filters(self):
        result = tools.count_problems(
            group_by="template", dataset="bgl", start="2005-06-05", end="2005-06-06",
            team="compute-kernel", component="KERNEL", level="FATAL", severity=3,
        )
        grouped, total = result["queries"]
        where = grouped["sql"].split(" WHERE ")[1].split(" GROUP BY ")[0]
        self.assertEqual(where, total["sql"].split(" WHERE ")[1])
        self.assertEqual(grouped["params"][:-1], total["params"])
        self.assertEqual(total["params"][-2:], ["FATAL", 3])
        self.assertEqual(result["filters"]["severity"], 3)
        self.assertEqual(result["filters"]["level"], "FATAL")
        self.assertIn("level=FATAL", result["scope"])
        self.assertIn("severity=3", result["scope"])

    def test_search_totals_and_rows_use_identical_filters(self):
        for text in (None, "data"):
            with self.subTest(text=text):
                result = tools.search_logs(text, dataset="bgl", level="FATAL", severity=3)
                totals, rows = result["queries"]
                self.assertEqual(totals["params"], rows["params"][:-1])
                for query in (totals, rows):
                    self.assertIn("upper(level) = upper(%s) AND severity = %s", query["sql"])
                self.assertEqual(result["data"]["total_matches"], len(result["data"]["lines"]))

    def test_both_filters_intersect_without_fallback(self):
        self.assertEqual(tools.search_logs(level="INFO", severity=3)["data"]["total_matches"], 0)
        result = tools.count_problems(level="INFO", severity=3)
        self.assertEqual(result["data"], {"total_lines": 0, "total_groups": 0, "groups": []})

    def test_invalid_severity_rejected_before_connection(self):
        for severity in (-1, 4, "3", 3.0, True, False, [], {}):
            for function in (tools.search_logs, tools.count_problems):
                with self.subTest(severity=severity, function=function.__name__):
                    with self.assertRaisesRegex(ValueError, "severity must be an integer"):
                        function(severity=severity)
        self.connect.assert_not_called()

    def test_all_valid_severities_including_zero(self):
        for severity, expected in ((0, 2), (1, 1), (2, 1), (3, 4)):
            with self.subTest(severity=severity):
                self.assertEqual(tools.search_logs(severity=severity)["data"]["total_matches"], expected)
                self.assertEqual(tools.count_problems(severity=severity, problems_only=False)
                                 ["data"]["total_lines"], expected)
        # The original problem-only default still excludes unlabelled INFO.
        self.assertEqual(tools.count_problems(severity=0)["data"]["total_lines"], 1)

    def test_existing_positional_calls_and_problem_defaults(self):
        result = tools.count_problems("template", "bgl", None, None, None, None, True, 25)
        self.assertEqual(result["data"]["total_lines"], 7)
        self.assertEqual(next(g["lines"] for g in result["data"]["groups"] if g["template"] == "E19"), 3)
        self.assertNotIn("severity", result["filters"])
        self.assertNotIn("level", result["filters"])
        self.assertEqual(tools.count_problems(problems_only=False)["data"]["total_lines"], 8)
        result = tools.search_logs("data", "bgl", None, None, None, None, None, False, "time", 20)
        self.assertEqual(result["data"]["total_matches"], 3)
        self.assertEqual([r["line_id"] for r in result["data"]["lines"]], [1, 3, 4])

    def test_truncation_keeps_full_filtered_totals_and_shares(self):
        result = tools.count_problems(group_by="template", severity=3, limit=1)
        self.assertEqual(result["data"]["total_lines"], 4)
        self.assertEqual(result["data"]["total_groups"], 2)
        self.assertEqual(result["data"]["groups"][0]["share_pct"], 75.0)
        self.assertTrue(result["queries"][0]["truncated"])
        result = tools.search_logs(severity=3, limit=1)
        self.assertEqual(result["data"]["total_matches"], 4)
        self.assertEqual(len(result["data"]["lines"]), 1)
        self.assertTrue(result["queries"][1]["truncated"])

    def test_structured_retrieval_retains_other_filters(self):
        result = tools.search_logs(severity=3, dataset="bgl", team="compute-kernel", component="KERNEL",
                                   start="2005-06-05 00:00:05", end="2005-06-05 00:00:07")
        self.assertEqual([r["line_id"] for r in result["data"]["lines"]], [5, 6])
        self.assertEqual(tools.search_logs(severity=3, team="other")["data"]["total_matches"], 0)

    def test_missing_search_criteria_rejected(self):
        with self.assertRaisesRegex(ValueError, "provide non-empty text"):
            tools.search_logs()
        self.connect.assert_not_called()

    def test_published_schemas_match_callable_parameters(self):
        definitions = {tool["name"]: tool["input_schema"] for tool in tools.claude_tools()}
        for name in ("count_problems", "search_logs"):
            spec = definitions[name]
            self.assertEqual(set(spec["properties"]), set(inspect.signature(tools.TOOLS[name][0]).parameters))
            self.assertEqual(spec["properties"]["severity"]["enum"], [0, 1, 2, 3])
            self.assertIn("level", spec["properties"])
        self.assertNotIn("text", definitions["search_logs"]["required"])
        # The compatible API uses this same registry; MCP registers these same
        # callables and derives its arguments from their signatures.
        compatible = {tool["function"]["name"]: tool["function"]["parameters"] for tool in agent._openai_tools()}
        self.assertEqual(compatible, definitions)
        self.assertEqual(tools.call_tool("count_problems", {"severity": 3})["data"]["total_lines"], 4)

    def test_mcp_advertises_new_optional_arguments(self):
        import mcp_server

        definitions = {tool.name: tool.input_schema for tool in asyncio.run(mcp_server.server.list_tools())}
        for name in ("count_problems", "search_logs"):
            self.assertIn("level", definitions[name]["properties"])
            self.assertIn("severity", definitions[name]["properties"])
            self.assertNotIn("severity", definitions[name].get("required", []))
        self.assertNotIn("text", definitions["search_logs"].get("required", []))
        self.assertIn("same_source_minutes", definitions["investigate_event"]["properties"])
        self.assertNotIn("same_source_minutes", definitions["investigate_event"].get("required", []))
        self.connect.assert_not_called()

    def test_engineer_ranking_instruction_contract(self):
        prompt = agent.system_prompt("engineer")
        self.assertIn('"highest-severity errors"', prompt)
        self.assertIn('count_problems(dataset="bgl", group_by="template", severity=3, limit=5)', prompt)
        self.assertIn("One successful aggregation is sufficient", prompt)
        self.assertIn("Do not call corpus_overview, template_history, search_logs", prompt)
        self.assertIn("executed ranking SQL and its parameters", prompt)
        self.assertIn("replace the investigation sections", prompt)
        self.assertIn("Omit all investigation headings", prompt)
        self.assertIn("Never add an extra row to the tool's limit argument", prompt)

    def test_ranking_agent_loop_uses_one_tool_and_two_model_calls(self):
        """Exercise both provider loops with scripted model replies, not a live-model prediction."""
        import json

        question = ('Find the five most frequent highest-severity errors in the BGL dataset. '
                    'Use severity 3, show exact matching log lines, components, teams and SQL evidence. '
                    'Do not count lower-severity messages.')
        args = {"dataset": "bgl", "group_by": "template", "severity": 3, "limit": 5}
        for provider in ("openai", "anthropic"):
            with self.subTest(provider=provider):
                session = agent.Session("engineer")
                client = MagicMock()
                if provider == "openai":
                    tool_call = SimpleNamespace(id="call-1", function=SimpleNamespace(
                        name="count_problems", arguments=json.dumps(args)))
                    messages = [SimpleNamespace(content=None, tool_calls=[tool_call]),
                                SimpleNamespace(content="Severity 3 only. Ecritical: 3 lines; E19: 1 line. [Q1]", tool_calls=[])]
                    client.chat.completions.create.side_effect = [
                        SimpleNamespace(choices=[SimpleNamespace(message=message, finish_reason="stop")], usage=None)
                        for message in messages
                    ]
                    client_factory, runner, create = "_openai_client", agent._run_openai, client.chat.completions.create
                else:
                    client.beta.messages.create.side_effect = [
                        SimpleNamespace(stop_reason="tool_use", usage=SimpleNamespace(), content=[
                            SimpleNamespace(type="tool_use", id="call-1", name="count_problems", input=args)]),
                        SimpleNamespace(stop_reason="end_turn", usage=SimpleNamespace(), content=[
                            SimpleNamespace(type="text", text="Severity 3 only. Ecritical: 3 lines; E19: 1 line. [Q1]")]),
                    ]
                    client_factory, runner, create = "_client", agent._run_anthropic, client.beta.messages.create
                with patch.object(agent, client_factory, return_value=client):
                    events = list(runner(session, question))
                calls = [event for event in events if event["type"] == "tool_call"]
                self.assertEqual(len(calls), 1)
                self.assertEqual(calls[0]["name"], "count_problems")
                self.assertEqual(calls[0]["input"], args)
                self.assertEqual(create.call_count, 2)
                envelope = next(event["envelope"] for event in events if event["type"] == "tool_result")
                self.assertEqual(envelope["evidence_id"], "Q1")
                self.assertEqual(envelope["data"]["total_lines"], 4)
                self.assertEqual(envelope["data"]["groups"][0]["lines"], 3)
                self.assertIn("severity = %s", envelope["queries"][0]["sql"])
                self.assertEqual(envelope["queries"][0]["params"], ["bgl", 3, 6])
                self.assertEqual(events[-1]["type"], "done")
                self.assertEqual(events[-1]["usage"]["model_calls"], 2)


class ProblemTrendTests(unittest.TestCase):
    """Execute trend SQL on offline data; never connect to SereneDB."""

    def setUp(self):
        SeverityFilterTests.setUp(self)

        def date_trunc(bucket, value):
            # Only weekly bucketing is needed here: weeks start on Monday.
            if bucket != "week":
                raise ValueError("this fixture supports weekly buckets only")
            timestamp = datetime.fromisoformat(value)
            monday = timestamp - timedelta(days=timestamp.weekday())
            return monday.replace(hour=0, minute=0, second=0, microsecond=0).isoformat(sep=" ")

        self.date_trunc = date_trunc
        self.db.create_function("date_trunc", 2, date_trunc)
        # Put two severity-3 lines on opposite sides of a Monday boundary.
        self.db.execute("UPDATE events_v SET ts='2005-06-06 00:00:00' WHERE line_id IN (6, 7)")

    def trend(self, **kwargs):
        return tools.problem_trend(dataset="bgl", component="KERNEL", **kwargs)

    @staticmethod
    def total(result):
        return sum(row["lines"] for row in result["data"]["series"])

    def test_severity_three_excludes_lower_severities_and_alerts(self):
        result = self.trend(severity=3)
        self.assertEqual([(r["period"], r["lines"]) for r in result["data"]["series"]],
                         [("2005-05-30 00:00:00", 2), ("2005-06-06 00:00:00", 2)])

    def test_severity_two_returns_only_errors(self):
        result = self.trend(severity=2)
        self.assertEqual(self.total(result), 1)
        self.assertEqual(result["data"]["series"][0]["period"], "2005-05-30 00:00:00")

    def test_omitted_severity_preserves_sql_evidence_and_problem_defaults(self):
        result = self.trend()
        self.assertEqual(result, self.trend(severity=None))
        self.assertEqual(self.total(result), 7)
        self.assertEqual(result["filters"], {
            "bucket": "week", "dataset": "bgl", "component": "KERNEL", "split_by": "team"})
        self.assertEqual(result["scope"],
                         "bgl, start of data → end of data, problem lines only (severity ≥ WARNING or labelled alert)")
        self.assertEqual(result["queries"], [{
            "purpose": "Problem lines per week and team",
            "sql": "SELECT dataset, date_trunc(%s, ts) AS period, team AS team, count(*) AS lines "
                   "FROM events_v WHERE TRUE AND (severity >= 1 OR COALESCE(is_alert, false)) "
                   "AND dataset = %s AND component = %s GROUP BY dataset, period, team "
                   "ORDER BY period, lines DESC LIMIT 501",
            "params": ["week", "bgl", "KERNEL"], "rows_returned": 2, "truncated": False}])
        self.assertEqual(result["notes"], ["Buckets with zero problem lines are omitted.", tools.LINES_NOT_INCIDENTS])
        self.assertEqual(set(result), {"tool", "scope", "filters", "queries", "data", "notes"})

    def test_invalid_severity_rejected_before_connection(self):
        for severity in (-1, 4, "3", 3.0, True, False, [], {}):
            with self.subTest(severity=severity):
                with self.assertRaisesRegex(ValueError, "severity must be an integer"):
                    self.trend(severity=severity)
        self.connect.assert_not_called()

    def test_zero_keeps_only_labelled_alerts(self):
        result = self.trend(severity=0)
        self.assertEqual(self.total(result), 1)
        self.assertEqual(self.total(self.trend(severity=0, template_id="Einfo")), 0)
        self.assertEqual(self.total(self.trend(severity=0, template_id="Ealert")), 1)
        self.assertEqual(result["filters"]["severity"], 0)
        self.assertIn("severity=0", result["scope"])

    def test_dataset_component_team_time_and_template_filters_intersect(self):
        for line, column, value in ((5, "dataset", "openstack"), (6, "component", "APP"),
                                    (7, "team", "other-team")):
            # All inserted distractors are inside the requested time window.
            self.db.execute(
                "INSERT INTO events_v (dataset, line_id, ts, severity, component, team, template_id) "
                "SELECT dataset, line_id + 100, '2005-06-05 00:00:01', severity, component, team, template_id "
                "FROM events_v WHERE line_id=?", (line,))
            self.db.execute(f"UPDATE events_v SET {column}=? WHERE line_id=?", (value, line + 100))
        result = self.trend(severity=3, team="compute-kernel", start="2005-06-05 00:00:01",
                            end="2005-06-06 00:00:00", template_id="E19")
        self.assertEqual(self.total(result), 1)  # Inclusive start includes line 1.
        self.assertEqual(self.total(self.trend(severity=3, team="compute-kernel",
                                              start="2005-06-05 00:00:01", end="2005-06-06 00:00:00")), 2)
        self.assertEqual(self.total(self.trend(severity=3, team="missing-team")), 0)

    def test_parameterized_sql_and_evidence_match_requested_scope(self):
        result = self.trend(severity=3, team="compute-kernel", start="2005-06-05", end="2005-06-07",
                            template_id="Ecritical", split_by="none")
        query = result["queries"][0]
        self.assertIn("AND severity = %s", query["sql"])
        self.assertNotIn("severity = 3", query["sql"])
        self.assertLess(query["sql"].index("severity = %s"), query["sql"].index("GROUP BY"))
        self.assertEqual(query["params"], ["week", "bgl", "2005-06-05", "2005-06-07",
                                           "compute-kernel", "KERNEL", 3, "Ecritical"])
        self.assertEqual(result["filters"]["severity"], 3)
        self.assertEqual(result["scope"],
                         "bgl, 2005-06-05 → 2005-06-07, problem lines only (severity ≥ WARNING or labelled alert), severity=3")
        self.assertEqual(self.total(result), 3)

    def test_tool_schemas_advertise_optional_severity(self):
        definitions = {tool["name"]: tool["input_schema"] for tool in tools.claude_tools()}
        spec = definitions["problem_trend"]
        self.assertEqual(spec["properties"]["severity"], tools._SEVERITY)
        self.assertEqual(spec["properties"]["severity"]["type"], "integer")
        self.assertEqual(spec["properties"]["severity"]["enum"], [0, 1, 2, 3])
        self.assertNotIn("severity", spec["required"])
        self.assertEqual(set(spec["properties"]), set(inspect.signature(tools.problem_trend).parameters))
        compatible = {tool["function"]["name"]: tool["function"]["parameters"] for tool in agent._openai_tools()}
        self.assertEqual(compatible["problem_trend"], spec)
        self.assertEqual(self.total(tools.call_tool("problem_trend", {"dataset": "bgl", "severity": 3})), 4)

    def test_mcp_discovery_exposes_optional_severity(self):
        import mcp_server

        definitions = {tool.name: tool.input_schema for tool in asyncio.run(mcp_server.server.list_tools())}
        spec = definitions["problem_trend"]
        self.assertIn("severity", spec["properties"])
        self.assertNotIn("severity", spec.get("required", []))
        self.connect.assert_not_called()

    def test_existing_positional_call_is_unchanged(self):
        result = tools.problem_trend("week", "bgl", None, None, "compute-kernel", "KERNEL", "E19", "none")
        self.assertEqual(result, self.trend(team="compute-kernel", template_id="E19", split_by="none"))
        self.assertEqual(self.total(result), 3)

    def test_complete_weekly_totals_match_component_ranking(self):
        self.db.execute("UPDATE events_v SET component='APP', team='job-runtime' WHERE line_id IN (4, 7)")
        for severity in (None, 0, 1, 2, 3):
            with self.subTest(severity=severity):
                filters = dict(dataset="bgl", start="2005-06-05", end="2005-06-07", severity=severity)
                ranking = tools.count_problems(group_by="component", **filters)
                trend = tools.problem_trend(split_by="component", **filters)
                self.assertFalse(ranking["queries"][0]["truncated"])
                self.assertFalse(trend["queries"][0]["truncated"])
                totals = {}
                for row in trend["data"]["series"]:
                    totals[row["component"]] = totals.get(row["component"], 0) + row["lines"]
                self.assertEqual(totals, {row["component"]: row["lines"] for row in ranking["data"]["groups"]})
                self.assertEqual(sum(totals.values()), ranking["data"]["total_lines"])

    def test_week_boundary_fixture(self):
        self.assertEqual(self.date_trunc("week", "2005-06-05 23:59:59.999999"), "2005-05-30 00:00:00")
        self.assertEqual(self.date_trunc("week", "2005-06-06 00:00:00"), "2005-06-06 00:00:00")

    def test_team_lead_instructions_require_matching_scope(self):
        prompt = agent.system_prompt("team_lead")
        for rule in ("same dataset", "requested severity filter", "requested time range",
                     "Do not compare differently filtered populations", "problem_trend with severity=3",
                     "Explicitly disclose scope mismatches", "requested result is", "unavailable"):
            self.assertIn(rule, prompt)


class TeamLeadTemplateTests(unittest.TestCase):
    """Synthetic component data and scripted replies; not a live-model guarantee."""

    def setUp(self):
        SeverityFilterTests.setUp(self)
        self.db.execute("DELETE FROM events_v")
        self.expected = {}
        self.next_line = 1
        for index, (component, team) in enumerate((
                ("KERNEL", "compute-kernel"), ("APP", "job-runtime"),
                ("DISCOVERY", "hardware-ops"), ("MONITOR", "hardware-ops"), ("HARDWARE", "hardware-ops"))):
            winner = "Eshared" if index % 2 == 0 else "E" + component
            runner_up = "E" + component if index % 2 == 0 else "Eshared"
            self.expected[component] = (team, winner, index + 2)
            self.add_lines(component, team, winner, index + 2)
            self.add_lines(component, team, runner_up, 1)
            # More lower-severity lines than any winner, including labelled alerts.
            self.add_lines(component, team, "Elower", 12, severity=2)
            self.add_lines(component, team, "Elower", 12, severity=1)
            self.add_lines(component, team, "Elower", 12, severity=0, alert=True)

    def add_lines(self, component, team, template, count, severity=3, alert=False,
                  dataset="bgl", ts="2005-06-05 12:00:00"):
        for _ in range(count):
            self.db.execute(
                "INSERT INTO events_v (dataset, line_id, ts, level, severity, component, team, source, "
                "template_id, template, content, is_alert) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (dataset, self.next_line, ts, "FATAL" if severity == 3 else "OTHER", severity,
                 component, team, "fixture-node", template, "fixture pattern " + template, "fixture message", alert))
            self.next_line += 1

    def templates(self, component, **kwargs):
        return tools.count_problems(group_by="template", dataset="bgl", component=component, severity=3, **kwargs)

    @staticmethod
    def fingerprint(value):
        return hashlib.sha256(json.dumps(value, sort_keys=True).encode()).hexdigest()

    def test_top_template_for_every_component_with_exact_severity_and_owner(self):
        for component, (team, template, count) in self.expected.items():
            with self.subTest(component=component):
                result = self.templates(component, limit=1)
                row = result["data"]["groups"][0]
                self.assertEqual((row["template"], row["lines"]), (template, count))
                self.assertEqual((row["team"], row["teams"], row["components"]), (team, team, component))
                self.assertEqual(row["example_template"], "fixture pattern " + template)
                self.assertEqual(result["data"]["total_lines"], count + 1)
                self.assertEqual(result["data"]["total_groups"], 2)

    def test_shared_template_ids_are_counted_per_component(self):
        for component, (_, winner, count) in self.expected.items():
            with self.subTest(component=component):
                groups = {row["template"]: row["lines"] for row in self.templates(component)["data"]["groups"]}
                self.assertEqual(groups["Eshared"], count if winner == "Eshared" else 1)
        global_groups = {row["template"]: row["lines"] for row in tools.count_problems(
            dataset="bgl", group_by="template", severity=3)["data"]["groups"]}
        self.assertEqual(global_groups["Eshared"], 14)

    def test_sql_evidence_preserves_component_severity_team_and_time(self):
        self.add_lines("DISCOVERY", "hardware-ops", "Eoutside", 20, dataset="openstack")
        self.add_lines("DISCOVERY", "hardware-ops", "Eoutside", 20, ts="2005-06-06 00:00:00")
        self.add_lines("DISCOVERY", "other-team", "Eoutside", 20)
        result = self.templates("DISCOVERY", team="hardware-ops", start="2005-06-05", end="2005-06-06", limit=1)
        grouped, total = result["queries"]
        expected_params = ["bgl", "2005-06-05", "2005-06-06", "hardware-ops", "DISCOVERY", 3]
        self.assertEqual(grouped["params"], expected_params + [2])
        self.assertEqual(total["params"], expected_params)
        for query in (grouped, total):
            self.assertIn("component = %s AND severity = %s", query["sql"])
            self.assertNotIn("severity = 3", query["sql"])
        self.assertEqual(grouped["sql"].split(" WHERE ")[1].split(" GROUP BY ")[0],
                         total["sql"].split(" WHERE ")[1])
        self.assertEqual(result["filters"]["component"], "DISCOVERY")
        self.assertEqual(result["filters"]["severity"], 3)
        self.assertIn("severity=3", result["scope"])
        self.assertEqual(result["data"]["groups"][0]["lines"], 4)
        self.assertEqual(result["data"]["groups"][0]["team"], "hardware-ops")

    def test_truncated_top_templates_keep_exact_counts_and_full_totals(self):
        result = self.templates("HARDWARE", limit=1)
        self.assertTrue(result["queries"][0]["truncated"])
        self.assertEqual(len(result["data"]["groups"]), 1)
        self.assertEqual(result["data"]["groups"][0]["lines"], 6)
        self.assertEqual(result["data"]["total_lines"], 7)
        self.assertIn("Showing the top 1 of 2 groups.", result["notes"])
        self.assertIn(tools.LINES_NOT_INCIDENTS, result["notes"])

    def test_empty_results_and_failed_queries_have_distinct_evidence(self):
        session = agent.Session("team_lead")
        args = dict(dataset="bgl", component="MISSING", group_by="template", severity=3, limit=1)
        events, _, failed = agent._execute(session, "count_problems", args)
        self.assertFalse(failed)
        self.assertEqual(events[-1]["envelope"]["data"]["groups"], [])
        with patch.object(tools.Evidence, "run", side_effect=RuntimeError("fixture query failure")):
            events, _, failed = agent._execute(session, "count_problems", args)
        self.assertTrue(failed)
        self.assertIn("fixture query failure", events[-1]["error"])
        self.assertNotIn("envelope", events[-1])

    def test_team_lead_template_coverage_and_disclosure_instructions(self):
        prompt = agent.system_prompt("team_lead")
        for rule in ('Retrieve the requested component ranking first', 'EVERY component', 'group_by="template"',
                     'component="DISCOVERY", severity=3, limit=1', 'single global template ranking is not',
                     'owning team, template ID, exact matching log-line count', 'example template text when available',
                     'evidence reference [Q#]', 'executed SQL evidence and parameters', 'Before finalizing',
                     'failed queries', 'empty results', 'queries never attempted', 'Do not invent missing counts',
                     'not separate incidents', 'Do not invent root causes', 'downtime', 'financial impact',
                     'business consequences', 'If top counts tie', 'uniquely most frequent'):
            self.assertIn(rule, prompt)

    def test_existing_severity_consistency_instructions_preserved(self):
        ProblemTrendTests.test_team_lead_instructions_require_matching_scope(self)

    def test_engineer_instructions_unchanged(self):
        # Compatibility snapshots taken before the Problem 2 instruction change.
        self.assertEqual(self.fingerprint(agent.system_prompt("engineer")),
                         "64017fd4ac4da1dafc1d998f346c7baff9fdffcf0dde6f3df0ec664679da680e")

    def test_manager_instructions_unchanged(self):
        self.assertEqual(self.fingerprint(agent.system_prompt("manager")),
                         "98ca4e8d62da79f078e0c35dbc49933e6ea37ca2f85606a89cf012a68114ed09")

    def test_mcp_tool_schemas_unchanged(self):
        import mcp_server

        schemas = {tool.name: tool.input_schema for tool in asyncio.run(mcp_server.server.list_tools())}
        self.assertEqual(self.fingerprint(schemas),
                         "c636719f6fdc8c1093c425235649c9a99d86c735537f5f855a4e41b827f891c8")
        self.connect.assert_not_called()

    def test_scripted_agent_covers_all_components_with_separate_sql_evidence(self):
        # These replies are scripted: they test execution and citations, not model planning.
        ranking_args = dict(dataset="bgl", group_by="component", severity=3, limit=5)
        components = list(reversed(self.expected))
        template_args = [dict(dataset="bgl", group_by="template", component=component, severity=3, limit=1)
                         for component in components]
        answer = "\n".join(
            f"{component}: {self.expected[component][1]}, {self.expected[component][2]} log lines [Q{index + 2}]"
            for index, component in enumerate(components))
        for provider in ("openai", "anthropic"):
            with self.subTest(provider=provider):
                session = agent.Session("team_lead")
                client = MagicMock()
                responses = []
                for step, arguments in enumerate(([ranking_args], template_args, [])):
                    if provider == "openai":
                        calls = [SimpleNamespace(id=f"call-{step}-{i}", function=SimpleNamespace(
                            name="count_problems", arguments=json.dumps(args))) for i, args in enumerate(arguments)]
                        message = SimpleNamespace(content=answer if not calls else None, tool_calls=calls)
                        responses.append(SimpleNamespace(choices=[SimpleNamespace(message=message, finish_reason="stop")],
                                                         usage=None))
                    else:
                        content = [SimpleNamespace(type="tool_use", id=f"call-{step}-{i}",
                                   name="count_problems", input=args) for i, args in enumerate(arguments)]
                        responses.append(SimpleNamespace(stop_reason="tool_use" if content else "end_turn",
                            usage=SimpleNamespace(), content=content or [SimpleNamespace(type="text", text=answer)]))
                if provider == "openai":
                    factory, runner = "_openai_client", agent._run_openai
                    client.chat.completions.create.side_effect = responses
                else:
                    factory, runner = "_client", agent._run_anthropic
                    client.beta.messages.create.side_effect = responses
                with patch.object(agent, factory, return_value=client):
                    events = list(runner(session, "Rank five severity-3 BGL components and give each top template."))
                calls = [event["input"] for event in events if event["type"] == "tool_call"]
                self.assertEqual(calls, [ranking_args] + template_args)
                envelopes = [event["envelope"] for event in events if event["type"] == "tool_result"]
                self.assertEqual([row["component"] for row in envelopes[0]["data"]["groups"]], components)
                for index, (component, envelope) in enumerate(zip(components, envelopes[1:])):
                    self.assertEqual(envelope["evidence_id"], f"Q{index + 2}")
                    self.assertEqual(envelope["filters"]["component"], component)
                    self.assertEqual(envelope["filters"]["severity"], 3)
                    self.assertEqual(envelope["data"]["groups"][0]["lines"], self.expected[component][2])
                    self.assertIn("component = %s AND severity = %s", envelope["queries"][0]["sql"])
                self.assertEqual(next(event["text"] for event in events if event["type"] == "answer"), answer)
                self.assertEqual(events[-1]["usage"]["model_calls"], 3)


class TeamLeadRecommendationTests(unittest.TestCase):
    """Instruction contracts and measured synthetic evidence, not proof of live reasoning."""

    def setUp(self):
        TeamLeadTemplateTests.setUp(self)

        def week_start(bucket, value):
            if bucket != "week":
                raise ValueError("this fixture supports weekly buckets only")
            timestamp = datetime.fromisoformat(value)
            return (timestamp - timedelta(days=timestamp.weekday())).replace(
                hour=0, minute=0, second=0, microsecond=0).isoformat(sep=" ")

        self.db.create_function("date_trunc", 2, week_start)
        execute = self.connection.execute.side_effect

        def history_compatible_execute(sql, params):
            # Extend the SQLite fixture for history's extra DISTINCT aggregates.
            for column in ("level", "alert_type"):
                sql = sql.replace(f"string_agg(DISTINCT {column}, ',')", f"string_agg(DISTINCT {column})")
            return execute(sql, params)

        self.connection.execute.side_effect = history_compatible_execute
        self.prompt = agent.system_prompt("team_lead")

    add_lines = TeamLeadTemplateTests.add_lines

    def test_observations_interpretations_unknowns_and_hypothesis_checks(self):
        for rule in ("OBSERVED FACTS:", "distinct sources, active days", "INTERPRETATIONS:",
                     "UNKNOWN unless established by evidence", "independent", "incident counts, downtime",
                     "operational consequences not established by logs", "label it as a hypothesis",
                     "cite the motivating observation", "check that could confirm", "or weaken it",
                     "cause remains unconfirmed", 'say "Cause unknown."'):
            self.assertIn(rule, self.prompt)

    def test_concentration_spikes_and_recurrence_do_not_prove_faults(self):
        for rule in ("high log-line count does not prove many independent failures",
                     "Many lines from one source do not prove one incident or",
                     "a hardware fault", "same template across sources does not prove a shared root cause",
                     "system-wide failure", "weekly spike does not prove an isolated incident",
                     "Recurrence across days does not prove a fault persisted", "continuously",
                     "Do not present these conclusions as established facts"):
            self.assertIn(rule, self.prompt)

    def test_required_component_and_template_columns_and_evidence(self):
        self.assertIn("Component | Owning team | Component lines | Top template | Template lines | Evidence", self.prompt)
        self.assertIn("BOTH total matching component lines and matching top-template lines", self.prompt)
        self.assertIn("never substitute a template count for a component total", self.prompt)
        self.assertIn("Cite the component and template evidence separately", self.prompt)
        self.assertIn("not hard-coded production counts", self.prompt)

    def test_priorities_and_actions_require_comparisons_checks_and_uncertainty(self):
        for rule in ("State the prioritization criterion", "cite matching evidence",
                     "why this team comes before other plausible candidates", "trade-off",
                     "Log volume does not establish business impact or operational",
                     "responsible team, specific component/template, concrete diagnostic check",
                     "what the result would help establish", "important remaining uncertainty",
                     "Do not invent infrastructure details, operational events"):
            self.assertIn(rule, self.prompt)

    def test_broader_history_scope_and_unavailable_evidence_are_explicit(self):
        for rule in ("template_history is broader history", "does not filter by severity, component or time range",
                     "unless independent matching", "severity-filtered count_problems evidence",
                     "never silently compare different populations", "Final completeness check before answering",
                     "If evidence is unavailable, disclose the specific limitation",
                     "rather than inventing a replacement"):
            self.assertIn(rule, self.prompt)

    def test_answer_structure_is_adaptive_and_complete(self):
        sections = agent.ROLES["team_lead"]["sections"]
        self.assertIn("omit irrelevant sections", sections)
        self.assertIn("not a claim that an official PDF mandates", sections)
        self.assertEqual([line for line in sections.splitlines() if line.startswith("## ")], [
            "## Executive summary", "## Dataset, period and filters", "## Component ranking and top templates",
            "## Trends and recurrence", "## Prioritized investigations and team actions", "## SQL evidence",
            "## Limitations and unknowns"])

    def test_component_totals_template_counts_and_references_are_separate(self):
        session = agent.Session("team_lead")
        common = dict(dataset="bgl", severity=3, component="KERNEL")
        component_events, _, _ = agent._execute(session, "count_problems", dict(common, group_by="component"))
        template_events, _, _ = agent._execute(session, "count_problems", dict(common, group_by="template", limit=1))
        component = component_events[-1]["envelope"]
        template = template_events[-1]["envelope"]
        self.assertEqual(component["data"]["groups"][0]["lines"], 3)
        self.assertEqual(template["data"]["groups"][0]["lines"], 2)
        self.assertEqual(template["data"]["total_lines"], 3)
        self.assertEqual((component["evidence_id"], template["evidence_id"]), ("Q1", "Q2"))
        for envelope in (component, template):
            self.assertEqual(envelope["filters"]["severity"], 3)
            self.assertEqual(envelope["filters"]["component"], "KERNEL")
            self.assertIn("component = %s AND severity = %s", envelope["queries"][0]["sql"])

    def test_one_source_weekly_spike_is_only_measured_logging_evidence(self):
        filters = dict(dataset="bgl", component="KERNEL", severity=3)
        sources = tools.count_problems(group_by="source", **filters)
        trend = tools.problem_trend(bucket="week", split_by="none", **filters)
        self.assertEqual([(row["source"], row["lines"]) for row in sources["data"]["groups"]],
                         [("fixture-node", 3)])
        self.assertEqual([(row["period"], row["lines"]) for row in trend["data"]["series"]],
                         [("2005-05-30 00:00:00", 3)])
        self.assertIn(tools.LINES_NOT_INCIDENTS, sources["notes"])
        self.assertIn(tools.LINES_NOT_INCIDENTS, trend["notes"])

    def test_history_spread_and_active_days_can_exceed_filtered_population(self):
        # Same template, another component, lower severity and a later week.
        self.add_lines("APP", "job-runtime", "Eshared", 5, severity=2, ts="2005-06-20 12:00:00")
        self.db.execute("UPDATE events_v SET source='other-source' WHERE ts='2005-06-20 12:00:00'")
        filtered = tools.count_problems(dataset="bgl", group_by="template", component="KERNEL", severity=3,
                                        start="2005-06-05", end="2005-06-06")
        shared = next(row for row in filtered["data"]["groups"] if row["template"] == "Eshared")
        history = tools.template_history("bgl", "Eshared")
        self.assertEqual((shared["lines"], shared["sources"]), (2, 1))
        self.assertEqual((history["data"]["summary"]["lines"], history["data"]["summary"]["sources"],
                          history["data"]["summary"]["active_days"]), (19, 2, 2))
        self.assertEqual(history["filters"], {"dataset": "bgl", "template_id": "Eshared"})
        self.assertIn("whole dataset", history["scope"])
        for query in history["queries"]:
            self.assertNotIn("severity = %s", query["sql"])
        # Two active dates separated by a gap do not measure continuous fault duration.
        weeks = tools.problem_trend(dataset="bgl", severity=3, component="KERNEL", bucket="week")
        self.assertEqual(sum(row["lines"] for row in weeks["data"]["series"]), 3)

    def test_different_priority_criteria_have_distinct_evidence(self):
        # KERNEL wins volume; APP has more measured sources and active weeks.
        self.add_lines("KERNEL", "compute-kernel", "Evolume", 20)
        self.add_lines("APP", "job-runtime", "EAPP", 1, ts="2005-06-13 12:00:00")
        self.db.execute("UPDATE events_v SET source='second-source' WHERE ts='2005-06-13 12:00:00'")
        filters = dict(dataset="bgl", severity=3)
        ranking = tools.count_problems(group_by="component", **filters)
        by_component = {row["component"]: row for row in ranking["data"]["groups"]}
        self.assertEqual(ranking["data"]["groups"][0]["component"], "KERNEL")
        self.assertGreater(by_component["KERNEL"]["lines"], by_component["APP"]["lines"])
        self.assertGreater(by_component["APP"]["sources"], by_component["KERNEL"]["sources"])
        trend = tools.problem_trend(bucket="week", split_by="component", **filters)
        active_weeks = {component: sum(row["component"] == component for row in trend["data"]["series"])
                        for component in ("KERNEL", "APP")}
        self.assertEqual(active_weeks, {"KERNEL": 1, "APP": 2})

    def test_unavailable_evidence_remains_distinct_from_zero(self):
        TeamLeadTemplateTests.test_empty_results_and_failed_queries_have_distinct_evidence(self)

    def test_problem_one_and_two_instruction_contracts_preserved(self):
        ProblemTrendTests.test_team_lead_instructions_require_matching_scope(self)
        TeamLeadTemplateTests.test_team_lead_template_coverage_and_disclosure_instructions(self)


class TeamLeadEvidenceBoundaryTests(unittest.TestCase):
    """Small measured fixtures and instruction contracts; live reasoning still needs review."""

    def setUp(self):
        TeamLeadRecommendationTests.setUp(self)
        self.db.execute("DELETE FROM events_v")
        self.next_line = 1
        self.add_lines("KERNEL", "compute-kernel", "EA", 2)
        self.add_lines("KERNEL", "compute-kernel", "EB", 2, ts="2005-06-13 12:00:00")
        self.add_lines("KERNEL", "compute-kernel", "EA", 1, ts="2005-06-27 12:00:00")
        for line, source in ((1, "source-a"), (2, "shared-source"), (3, "shared-source"),
                             (4, "source-b"), (5, "source-a")):
            self.db.execute("UPDATE events_v SET source=? WHERE line_id=?", (source, line))

    add_lines = TeamLeadTemplateTests.add_lines

    def test_component_and_template_source_counts_retain_their_scope(self):
        filters = dict(dataset="bgl", component="KERNEL", severity=3)
        component = tools.count_problems(group_by="component", **filters)
        templates = tools.count_problems(group_by="template", **filters)
        self.assertEqual(component["data"]["groups"][0]["sources"], 3)
        self.assertEqual({row["template"]: row["sources"] for row in templates["data"]["groups"]},
                         {"EA": 2, "EB": 2})
        self.assertEqual(component["filters"]["group_by"], "component")
        self.assertEqual(templates["filters"]["group_by"], "template")

    def test_distinct_source_counts_overlap_across_templates(self):
        component_sources = tools.count_problems(dataset="bgl", component="KERNEL", severity=3, group_by="source")
        self.assertEqual(component_sources["data"]["total_groups"], 3)
        templates = tools.count_problems(dataset="bgl", component="KERNEL", severity=3, group_by="template")
        self.assertEqual(sum(row["sources"] for row in templates["data"]["groups"]), 4)
        shared = next(row for row in component_sources["data"]["groups"] if row["source"] == "shared-source")
        self.assertEqual((shared["lines"], shared["templates"]), (2, 2))

    def test_component_and_template_weekly_trends_are_different(self):
        filters = dict(dataset="bgl", component="KERNEL", severity=3, bucket="week", split_by="none")
        component = tools.problem_trend(**filters)
        template = tools.problem_trend(template_id="EA", **filters)
        self.assertEqual([(row["period"], row["lines"]) for row in component["data"]["series"]], [
            ("2005-05-30 00:00:00", 2), ("2005-06-13 00:00:00", 2), ("2005-06-27 00:00:00", 1)])
        self.assertEqual([(row["period"], row["lines"]) for row in template["data"]["series"]], [
            ("2005-05-30 00:00:00", 2), ("2005-06-27 00:00:00", 1)])
        self.assertEqual(template["filters"]["template_id"], "EA")
        self.assertIn("template_id = %s", template["queries"][0]["sql"])

    def test_recurring_weeks_do_not_measure_continuous_fault_duration(self):
        history = tools.template_history("bgl", "EA")
        self.assertEqual(history["data"]["summary"]["active_days"], 2)
        self.assertEqual({row["day"] for row in history["data"]["busiest_days"]},
                         {"2005-06-05", "2005-06-27"})
        # Sparse observed dates are all the tool establishes; it does not measure fault duration.
        self.assertIn(tools.LINES_NOT_INCIDENTS, history["notes"])
        self.assertNotIn("fault_duration", history["data"]["summary"])

    def test_historical_share_is_an_observed_proportion(self):
        result = tools.count_problems(dataset="bgl", component="KERNEL", severity=3, group_by="template")
        ea = next(row for row in result["data"]["groups"] if row["template"] == "EA")
        self.assertEqual((ea["lines"], result["data"]["total_lines"], ea["share_pct"]), (3, 5, 60.0))
        self.assertNotIn("achievable_reduction", ea)
        self.assertIn("achievable reduction is unknown", self.prompt)

    def test_proposed_diagnostic_check_does_not_create_execution_evidence(self):
        # Scripted replies verify execution bookkeeping, not the model's choice of wording.
        args = dict(dataset="bgl", component="KERNEL", severity=3, group_by="template")
        call = SimpleNamespace(id="count-1", function=SimpleNamespace(
            name="count_problems", arguments=json.dumps(args)))
        proposal = ("Proposed check — not executed: select a representative event and run preceding_patterns "
                    "using its dataset and line ID. No preceding-pattern result is available.")
        client = MagicMock()
        client.chat.completions.create.side_effect = [
            SimpleNamespace(usage=None, choices=[SimpleNamespace(finish_reason="stop",
                message=SimpleNamespace(content=None, tool_calls=[call]))]),
            SimpleNamespace(usage=None, choices=[SimpleNamespace(finish_reason="stop",
                message=SimpleNamespace(content=proposal, tool_calls=[]))])]
        with patch.object(agent, "_openai_client", return_value=client):
            events = list(agent._run_openai(agent.Session("team_lead"), "Recommend a next check."))
        self.assertEqual([event["name"] for event in events if event["type"] == "tool_call"], ["count_problems"])
        envelopes = [event["envelope"] for event in events if event["type"] == "tool_result"]
        self.assertEqual([envelope["tool"] for envelope in envelopes], ["count_problems"])
        self.assertEqual([envelope["evidence_id"] for envelope in envelopes], ["Q1"])
        self.assertEqual(next(event["text"] for event in events if event["type"] == "answer"), proposal)

    def test_failed_tool_call_has_no_confirmed_result_envelope(self):
        with patch.object(tools.Evidence, "run", side_effect=RuntimeError("offline failure")):
            events, content, failed = agent._execute(agent.Session("team_lead"), "count_problems", {
                "dataset": "bgl", "component": "KERNEL", "severity": 3, "group_by": "template"})
        self.assertTrue(failed)
        self.assertIn("offline failure", content)
        self.assertIn("error", events[-1])
        self.assertNotIn("envelope", events[-1])

    def test_granularity_instruction_contract(self):
        for rule in ("BOTH query filters and the level of aggregation", "COMPONENT LEVEL", "TEMPLATE LEVEL",
                     "SOURCE LEVEL", "TIME-BUCKET LEVEL", "Never transfer component-level source counts",
                     "template-level measurements to the whole component", "do not add them as if sources were disjoint",
                     "weekly trends, active days and log-line totals", "source counts retain their grouping scope"):
            self.assertIn(rule, self.prompt)

    def test_recurrence_and_remediation_instruction_contract(self):
        for rule in ("Multiple active days or weeks prove only", "not continuous fault", "persistence, independent incidents",
                     "one shared root cause or a system-wide failure", "separate events remains unknown",
                     "percentages describe observed proportions, not predicted remediation benefits",
                     "Do not claim that", "investigation or remediation would eliminate that share",
                     "prioritize investigation by", "achievable reduction is unknown"):
            self.assertIn(rule, self.prompt)

    def test_execution_status_and_final_check_instruction_contract(self):
        for rule in ("EXECUTED ANALYSIS", "actually called successfully", "OBSERVED RESULT", "PROPOSED CHECK",
                     "HYPOTHESIS", "Never describe a proposed check as completed", "failed or unexecuted tool call",
                     "Proposed check — not executed", "do not require every recommended check to run",
                     "representative event", "dataset and line ID", "temporal proximity alone",
                     "component/template measurements are not mixed", "trends retain",
                     "filters and grouping", "predicted remediation benefits", "not presented as a confirmed finding"):
            self.assertIn(rule, self.prompt)


class TeamLeadEvidencePlanningTests(unittest.TestCase):
    """Evidence sufficiency and scripted completion; not proof of live-model planning."""

    def setUp(self):
        TeamLeadRecommendationTests.setUp(self)

    add_lines = TeamLeadTemplateTests.add_lines

    def test_preferred_plan_covers_ranking_templates_and_one_component_trend(self):
        for rule in ('Prefer A: count_problems(group_by="component")', 'requested dataset, severity, time range',
                     'ranking limit', 'for each ranked component, count_problems(group_by="template")',
                     'filtered to that component', 'one problem_trend(split_by="component")',
                     'requested bucket and matching filters', '7 tool calls', 'not a rigid limit',
                     'question genuinely requires more evidence', 'Independent calls may be batched'):
            self.assertIn(rule, self.prompt)

    def test_reuse_aggregation_and_avoid_unnecessary_event_tools(self):
        for rule in ('Reuse count_problems(group_by="template")', 'template ID, matching line count, example template text',
                     'owning team', 'distinct source count, SQL evidence and scope',
                     'Do not call search_logs merely for an example message', 'ownership already supplied',
                     'needs actual event-level', 'Do not automatically call template_history, search_logs, preceding_patterns',
                     'investigate_event unless needed', 'Proposed diagnostic checks do not need to be executed'):
            self.assertIn(rule, self.prompt)

    def test_finish_with_sufficient_evidence_and_disclose_limitations(self):
        for rule in ('stop calling tools and produce the final answer', 'citing executed SQL evidence',
                     'stating limitations', 'Do not continue optional exploration',
                     'leave sufficient model iterations for the final response', 'evidence is missing, failed',
                     'or truncated, disclose the specific limitation', 'unnecessary calls or fabricate'):
            self.assertIn(rule, self.prompt)

    def test_aggregations_supply_template_details_without_search(self):
        for component, (team, template, count) in self.expected.items():
            with self.subTest(component=component):
                result = tools.count_problems(dataset="bgl", severity=3, component=component,
                                               group_by="template", limit=1)
                row = result["data"]["groups"][0]
                self.assertEqual((row["template"], row["lines"], row["example_template"], row["team"], row["sources"]),
                                 (template, count, "fixture pattern " + template, team, 1))
                self.assertEqual(result["filters"]["component"], component)
                self.assertIn("severity=3", result["scope"])
                self.assertEqual(len(result["queries"]), 2)
                self.assertIn("component = %s AND severity = %s", result["queries"][0]["sql"])

    def run_scripted_plan(self, batched):
        filters = dict(dataset="bgl", severity=3, start="2005-06-05", end="2005-06-06")
        ranking = ("count_problems", dict(filters, group_by="component", limit=5))
        components = list(reversed(self.expected))
        remaining = [("count_problems", dict(filters, group_by="template", component=component, limit=1))
                     for component in components]
        remaining.append(("problem_trend", dict(filters, bucket="week", split_by="component")))
        batches = [[ranking], remaining] if batched else [[call] for call in [ranking] + remaining]
        # The final text is scripted; this checks that the loop has time to request and emit it.
        final_text = "Synthetic final summary: " + " ".join(f"[Q{index}]" for index in range(1, 8))
        for provider in ("openai", "anthropic"):
            with self.subTest(provider=provider, batched=batched):
                client = MagicMock()
                replies = []
                for step, batch in enumerate(batches + [[]]):
                    if provider == "openai":
                        calls = [SimpleNamespace(id=f"call-{step}-{index}", function=SimpleNamespace(
                            name=name, arguments=json.dumps(args))) for index, (name, args) in enumerate(batch)]
                        replies.append(SimpleNamespace(usage=None, choices=[SimpleNamespace(finish_reason="stop",
                            message=SimpleNamespace(content=None if calls else final_text, tool_calls=calls))]))
                    else:
                        blocks = [SimpleNamespace(type="tool_use", id=f"call-{step}-{index}", name=name, input=args)
                                  for index, (name, args) in enumerate(batch)]
                        replies.append(SimpleNamespace(usage=SimpleNamespace(),
                            stop_reason="tool_use" if blocks else "end_turn",
                            content=blocks or [SimpleNamespace(type="text", text=final_text)]))
                if provider == "openai":
                    factory, runner = "_openai_client", agent._run_openai
                    create = client.chat.completions.create
                else:
                    factory, runner = "_client", agent._run_anthropic
                    create = client.beta.messages.create
                create.side_effect = replies
                with patch.object(agent, factory, return_value=client):
                    events = list(runner(agent.Session("team_lead"),
                        "Rank five BGL severity-3 components, give their top templates and weekly trends, owners and priorities."))
                tool_calls = [event for event in events if event["type"] == "tool_call"]
                self.assertEqual([(event["name"], event["input"]) for event in tool_calls], [ranking] + remaining)
                self.assertEqual(len(tool_calls), 7)
                envelopes = [event["envelope"] for event in events if event["type"] == "tool_result"]
                self.assertEqual([row["component"] for row in envelopes[0]["data"]["groups"]], components)
                self.assertEqual([envelope["evidence_id"] for envelope in envelopes], [f"Q{i}" for i in range(1, 8)])
                for component, envelope in zip(components, envelopes[1:6]):
                    self.assertEqual(envelope["filters"]["component"], component)
                    self.assertEqual(envelope["data"]["groups"][0]["lines"], self.expected[component][2])
                for envelope in envelopes:
                    self.assertEqual(envelope["filters"]["severity"], 3)
                    self.assertEqual(envelope["filters"]["start"], filters["start"])
                    self.assertEqual(envelope["filters"]["end"], filters["end"])
                    self.assertIn("severity = %s", envelope["queries"][0]["sql"])
                self.assertEqual({row["component"] for row in envelopes[-1]["data"]["series"]}, set(components))
                self.assertFalse(envelopes[-1]["queries"][0]["truncated"])
                self.assertEqual([event["text"] for event in events if event["type"] == "answer"], [final_text])
                expected_model_calls = 3 if batched else 8
                self.assertEqual(create.call_count, expected_model_calls)
                self.assertEqual(events[-1]["usage"]["model_calls"], expected_model_calls)
                self.assertLess(expected_model_calls, agent.MAX_STEPS)

    def test_seven_sequential_calls_then_final_answer_for_both_providers(self):
        self.run_scripted_plan(batched=False)

    def test_seven_batched_calls_then_final_answer_for_both_providers(self):
        self.run_scripted_plan(batched=True)


class TeamLeadSynthesisTests(unittest.TestCase):
    """Scripted provider boundaries and evidence preservation, not live-model reasoning tests."""

    def setUp(self):
        SeverityFilterTests.setUp(self)

    def run_scenario(self, provider, outcome="success", role="team_lead", early=False, no_evidence=False, mixed=False):
        session = agent.Session(role)
        session.evidence = 20  # Follow-up numbering must not restart at Q1.
        prior = [{"role": "user", "content": "Earlier context: use BGL."},
                 {"role": "assistant", "content": "Earlier response retained."}]
        if provider == "openai":
            prior.insert(0, {"role": "system", "content": agent.system_prompt(role)})
        session.messages.extend(deepcopy(prior))
        question = "Rank five severity-3 BGL components, their top templates and weekly trends, with SQL evidence."
        filters = {"dataset": "bgl", "severity": 3}
        specs = [("corpus_overview", {}),
                 ("count_problems", dict(filters, group_by="component", limit=5)),
                 ("count_problems", dict(dataset="bgl", group_by="component", team=""))]
        for component in ("KERNEL", "APP", "DISCOVERY", "MONITOR", "HARDWARE"):
            specs += [("count_problems", dict(filters, component=component, group_by="team")),
                      ("count_problems", dict(filters, component=component, group_by="template", limit=1))]
        if mixed:
            specs[7][1].pop("severity")  # A successful aggregation with the wrong severity scope.
        snapshots, executed, envelopes = [], [], []
        final_text = ("Observed ranking [Q22]. HARDWARE's top template and weekly trends were not retrieved. "
                      "Cause unknown. Proposed checks were not executed.")
        client = MagicMock()

        def reply(calls=None, text=None, finish=None, refusal=False):
            calls = calls or []
            if provider == "openai":
                return SimpleNamespace(usage=SimpleNamespace(prompt_tokens=10, completion_tokens=2), choices=[
                    SimpleNamespace(finish_reason=finish or "stop", message=SimpleNamespace(
                        content=text, tool_calls=[SimpleNamespace(id=call_id, function=SimpleNamespace(
                            name=name, arguments=json.dumps(args))) for call_id, name, args in calls],
                        refusal="declined" if refusal else None))])
            blocks = [SimpleNamespace(type="tool_use", id=call_id, name=name, input=args)
                      for call_id, name, args in calls]
            if text is not None:
                blocks.append(SimpleNamespace(type="text", text=text))
            return SimpleNamespace(usage=SimpleNamespace(input_tokens=10, output_tokens=2, cache_read_input_tokens=3),
                                   stop_reason=finish or ("tool_use" if calls else "end_turn"), content=blocks)

        def create(**kwargs):
            snapshots.append(deepcopy(kwargs))
            index = len(snapshots) - 1
            if early:
                return reply(text="Normal early final answer.")
            if index < 11 or role != "team_lead":
                name, args = specs[index]
                return reply(calls=[(f"call-{index}", name, args)])
            if outcome == "api_error":
                error = agent.openai.OpenAIError if provider == "openai" else agent.anthropic.AnthropicError
                raise error("synthetic provider failure")
            if outcome == "empty":
                return reply(text="")
            if outcome == "refusal":
                return reply(text="Unusable refusal text.", refusal=True,
                             finish="content_filter" if provider == "openai" else "refusal")
            if outcome == "unexpected_tool":
                return reply(calls=[("unexpected", "search_logs", {"severity": 3})])
            if outcome == "truncated":
                return reply(text=final_text, finish="length" if provider == "openai" else "max_tokens")
            if outcome == "unknown_citation":
                return reply(text="Invented result [Q999].")
            if outcome == "failed_citation":
                return reply(text="Unsupported result [Q23].")
            return reply(text=final_text)

        def call_tool(name, args):
            index = len(executed)
            executed.append((name, deepcopy(args)))
            if no_evidence or (mixed and index == 2):
                raise ValueError("synthetic failed call; not a confirmed finding")
            envelope = {
                "tool": name, "scope": "synthetic BGL scope; " + ("severity=3" if args.get("severity") == 3 else "mixed severities"),
                "filters": deepcopy(args), "queries": [{"purpose": "synthetic executed query",
                    "sql": "SELECT count(*) AS lines FROM events_v WHERE dataset = %s" +
                           (" AND severity = %s" if "severity" in args else ""),
                    "params": ["bgl"] + ([args["severity"]] if "severity" in args else []), "rows_returned": 1,
                    "truncated": mixed and index == 4}],
                "data": {"groups": [{"component": "KERNEL", "lines": 100}], "total_lines": 100},
                "notes": ["Synthetic truncated rows"] if mixed and index == 4 else [tools.LINES_NOT_INCIDENTS]}
            envelopes.append(deepcopy(envelope))
            return envelope

        if provider == "openai":
            factory, runner = "_openai_client", agent._run_openai
            client.chat.completions.create.side_effect = create
        else:
            factory, runner = "_client", agent._run_anthropic
            client.beta.messages.create.side_effect = create
        with patch.object(agent, factory, return_value=client), patch.object(tools, "call_tool", side_effect=call_tool):
            events = list(runner(session, question))
        self.connect.assert_not_called()
        return SimpleNamespace(session=session, events=events, requests=snapshots, executed=executed,
                               envelopes=envelopes, prior=prior, question=question, final_text=final_text)

    def assert_completed(self, result, calls=12, successful_responses=12):
        self.assertEqual(len(result.requests), calls)
        answers = [event for event in result.events if event["type"] == "answer"]
        self.assertEqual(len(answers), 1)
        self.assertEqual(sum(event["type"] == "done" for event in result.events), 1)
        self.assertEqual(result.events[-1]["type"], "done")
        usage = result.events[-1]["usage"]
        self.assertEqual(usage["model_calls"], calls)
        self.assertEqual(usage["input_tokens"], successful_responses * 10)
        self.assertEqual(usage["output_tokens"], successful_responses * 2)
        self.assertNotIn("Stopped after the maximum", answers[0]["text"])
        return answers[0]["text"]

    def test_twelfth_iteration_disables_tools_and_includes_usage_for_both_providers(self):
        for provider in ("openai", "anthropic"):
            with self.subTest(provider=provider):
                result = self.run_scenario(provider)
                text = self.assert_completed(result)
                self.assertEqual(agent.MAX_STEPS, 12)
                self.assertEqual(len(result.executed), 11)
                self.assertEqual(result.requests[-1]["tool_choice"], "none" if provider == "openai" else {"type": "none"})
                for request in result.requests[:-1]:
                    self.assertEqual(request.get("tool_choice", "auto"), "auto")
                self.assertIn("PARTIAL", text)
                self.assertIn(result.final_text, text)
                self.assertEqual(result.events[-1]["usage"]["cache_read_input_tokens"], 36 if provider == "anthropic" else 0)
                self.assertEqual(result.session.evidence, 31)

    def test_normal_early_answers_do_not_add_synthesis(self):
        for provider in ("openai", "anthropic"):
            with self.subTest(provider=provider):
                result = self.run_scenario(provider, early=True)
                self.assertEqual(self.assert_completed(result, calls=1, successful_responses=1), "Normal early final answer.")
                self.assertEqual(result.executed, [])
                self.assertNotEqual(result.requests[0].get("tool_choice"), "none")
                self.assertNotEqual(result.requests[0].get("tool_choice"), {"type": "none"})

    def test_history_question_role_sql_and_q_references_are_preserved(self):
        for provider in ("openai", "anthropic"):
            with self.subTest(provider=provider):
                result = self.run_scenario(provider, mixed=True)
                messages = result.requests[-1]["messages"]
                self.assertEqual(messages[:len(result.prior)], result.prior)
                self.assertIn({"role": "user", "content": result.question}, messages)
                role_text = (result.requests[-1]["system"][0]["text"] if provider == "anthropic"
                             else messages[0]["content"])
                self.assertEqual(role_text, agent.system_prompt("team_lead"))
                content = json.dumps(messages, default=lambda value: vars(value))
                self.assertIn("synthetic failed call; not a confirmed finding", content)
                instruction = messages[-1]["content"]
                self.assertNotIn("Q23", instruction)  # A failed call is not a successful citation.
                self.assertIn("Q22", instruction)
                self.assertIn("Q31", instruction)
                emitted = [event["envelope"] for event in result.events
                           if event["type"] == "tool_result" and "envelope" in event]
                successful_payloads = []
                for message in messages[:-1]:
                    if message["role"] == "tool" and not message["content"].startswith("ERROR:"):
                        successful_payloads.append(json.loads(message["content"]))
                    elif message["role"] == "user" and isinstance(message.get("content"), list):
                        successful_payloads.extend(json.loads(block["content"]) for block in message["content"]
                                                   if not block.get("is_error"))
                self.assertEqual(successful_payloads, emitted)
                self.assertTrue(any(envelope["queries"][0]["truncated"] for envelope in successful_payloads))
                self.assertTrue(any(envelope["tool"] == "count_problems" and "severity" not in envelope["filters"]
                                    for envelope in successful_payloads))
                self.assertEqual(result.session.messages[:-1], messages[:-1])  # Only the synthesized answer is persisted.

    def test_missing_template_trends_and_interpretation_constraints_reach_synthesis(self):
        # Payload checks establish available instructions/evidence, not the model's reasoning quality.
        for provider in ("openai", "anthropic"):
            with self.subTest(provider=provider):
                result = self.run_scenario(provider)
                instruction = result.requests[-1]["messages"][-1]["content"]
                for rule in ("only successfully", "existing executed SQL", "dataset, severity, time range",
                             "aggregation level", "Failed calls are limitations", "missing top template",
                             "missing requested weekly trends", "PARTIAL", "wrong-scope", "not independent incidents",
                             "observations from hypotheses", "proposed checks from executed checks",
                             "do not prove continuous faults", "does not predict remediation benefits"):
                    self.assertIn(rule, instruction)
                self.assertFalse(any(name == "problem_trend" for name, _ in result.executed))
                self.assertFalse(any(args.get("component") == "HARDWARE" and args.get("group_by") == "template"
                                     for _, args in result.executed))
                self.assertIn("HARDWARE's top template and weekly trends were not retrieved", self.assert_completed(result))

    def test_all_failed_calls_skip_unusable_synthesis_without_fabricated_findings(self):
        for provider in ("openai", "anthropic"):
            with self.subTest(provider=provider):
                result = self.run_scenario(provider, no_evidence=True)
                text = self.assert_completed(result, calls=11, successful_responses=11)
                self.assertIn("No successful tool evidence is available", text)
                self.assertIn("PARTIAL", text)
                self.assertNotIn("Observed ranking", text)
                self.assertEqual(len(result.executed), 11)

    def test_empty_synthesis_returns_a_clear_limitation(self):
        for provider in ("openai", "anthropic"):
            with self.subTest(provider=provider):
                self.assertIn("no final answer text", self.assert_completed(self.run_scenario(provider, outcome="empty")))

    def test_refusal_returns_a_limitation_and_done(self):
        for provider in ("openai", "anthropic"):
            with self.subTest(provider=provider):
                text = self.assert_completed(self.run_scenario(provider, outcome="refusal"))
                self.assertIn("declined final synthesis", text)
                self.assertNotIn("Unusable refusal text", text)

    def test_synthesis_api_error_is_counted_without_retry_or_tool_fallback(self):
        for provider in ("openai", "anthropic"):
            with self.subTest(provider=provider):
                result = self.run_scenario(provider, outcome="api_error")
                text = self.assert_completed(result, successful_responses=11)
                self.assertIn("final synthesis request failed", text)
                self.assertEqual(len(result.executed), 11)
                self.assertEqual(result.events[-1]["usage"]["cache_read_input_tokens"], 33 if provider == "anthropic" else 0)

    def test_unexpected_synthesis_tool_call_is_not_dispatched_or_persisted(self):
        for provider in ("openai", "anthropic"):
            with self.subTest(provider=provider):
                result = self.run_scenario(provider, outcome="unexpected_tool")
                self.assertIn("no additional tools were executed", self.assert_completed(result))
                self.assertEqual(len(result.executed), 11)
                self.assertFalse(any(name == "search_logs" for name, _ in result.executed))
                self.assertNotIn("tool_calls", result.session.messages[-1])
                self.assertIsInstance(result.session.messages[-1]["content"], str)

    def test_truncated_synthesis_is_explicitly_partial(self):
        for provider in ("openai", "anthropic"):
            with self.subTest(provider=provider):
                text = self.assert_completed(self.run_scenario(provider, outcome="truncated"))
                self.assertIn("PARTIAL", text)
                self.assertIn("cut off at the output limit", text)

    def test_unknown_or_failed_q_references_are_rejected(self):
        for provider in ("openai", "anthropic"):
            for outcome in ("unknown_citation", "failed_citation"):
                with self.subTest(provider=provider, outcome=outcome):
                    text = self.assert_completed(self.run_scenario(provider, outcome=outcome, mixed=True))
                    self.assertIn("referenced unavailable or failed evidence", text)
                    self.assertNotIn("Invented result", text)
                    self.assertNotIn("Unsupported result", text)

    def test_engineer_and_manager_keep_twelve_exploration_iterations(self):
        for provider in ("openai", "anthropic"):
            for role in ("engineer", "manager"):
                with self.subTest(provider=provider, role=role):
                    result = self.run_scenario(provider, role=role)
                    self.assertEqual(len(result.requests), 12)
                    self.assertEqual(len(result.executed), 12)
                    self.assertIn("Stopped after the maximum", result.events[-2]["text"])
                    self.assertEqual(result.events[-1]["usage"]["model_calls"], 12)
                    for request in result.requests:
                        self.assertEqual(request.get("tool_choice", "auto"), "auto")


class InvestigationTimingTests(unittest.TestCase):
    """Reuse the offline SQL fixture without inheriting/rerunning severity tests."""

    def setUp(self):
        SeverityFilterTests.setUp(self)
        self.add_event(1000, "2005-06-05 12:00:00", template="Etarget")

    def add_event(self, line, ts, source="node-target", template="Eother", level="ERROR", severity=2,
                  dataset="bgl", alert=False):
        self.db.execute("INSERT INTO events_v (dataset, line_id, ts, level, severity, component, team, source, "
                        "template_id, template, content, is_alert, alert_type) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                        (dataset, line, ts, level, severity, "KERNEL", "compute-kernel", source,
                         template, template, f"message {line}", alert, None))

    def investigate(self, **kwargs):
        return tools.investigate_event("bgl", 1000, **kwargs)

    def test_nine_day_gap_is_not_local_or_candidate_but_recurrence_remains_historical(self):
        self.add_event(999, "2005-05-27 12:00:00")
        self.add_event(1001, "2005-06-14 12:00:00", template="Etarget")
        result = self.investigate()
        self.assertEqual(result["data"]["same_source_problems"], [])
        self.assertIsNone(result["data"]["candidate_cause"])
        self.assertEqual(result["data"]["recurrence_in_dataset"]["lines"], 2)
        self.assertEqual(result["data"]["recurrence_in_dataset"]["active_days"], 2)
        self.assertIn("historical", result["data"]["recurrence_scope"])
        # Distant log-order context remains compatible, with an explicit signed gap.
        self.assertEqual(result["data"]["after"][0]["seconds_from_target"], 9 * 86400)
        self.assertIn("not necessarily chronological or local", " ".join(result["notes"]))

    def test_five_minutes_before_is_candidate_even_outside_line_window(self):
        self.add_event(4000, "2005-06-05 11:55:00")
        result = self.investigate()
        cause = result["data"]["candidate_cause"]
        self.assertEqual(cause["line_id"], 4000)
        self.assertEqual(cause["seconds_before_target"], 300)
        self.assertEqual(cause["seconds_from_target"], -300)
        self.assertTrue(cause["same_source_as_target"])
        self.assertEqual(result["data"]["same_source_problems"][0]["seconds_from_target"], -300)
        self.assertIn("300 seconds before", " ".join(result["data"]["suggested_checks"]))
        self.assertEqual(result["data"]["candidate_relationship"]["basis"], "same_source")

    def test_identical_timestamps_and_later_events_are_not_candidates(self):
        # Both have earlier line IDs, which must not override timestamp order.
        self.add_event(998, "2005-06-05 12:00:00")
        self.add_event(999, "2005-06-05 12:05:00")
        result = self.investigate()["data"]
        self.assertIsNone(result["candidate_cause"])
        gaps = {row["line_id"]: row["seconds_from_target"] for row in result["same_source_problems"]}
        self.assertEqual(gaps, {998: 0, 999: 300})

    def test_same_source_preferred_then_nearest_timestamp(self):
        self.add_event(996, "2005-06-05 11:40:00")
        self.add_event(997, "2005-06-05 11:55:00")
        self.add_event(999, "2005-06-05 11:59:00", source="other-node")
        self.assertEqual(self.investigate()["data"]["candidate_cause"]["line_id"], 997)

    def test_different_source_preceding_event_is_context_not_causal_candidate(self):
        self.add_event(999, "2005-06-05 11:55:00", source="other-node")
        result = self.investigate()
        preceding = result["data"]["preceding_event"]
        self.assertEqual(preceding["seconds_before_target"], 300)
        self.assertFalse(preceding["same_source_as_target"])
        self.assertIsNone(result["data"]["candidate_cause"])
        self.assertIsNone(result["data"]["candidate_relationship"])
        self.assertEqual(result["data"]["same_source_problems"], [])
        self.assertIn("No supported causal candidate has been identified", " ".join(result["data"]["suggested_checks"]))
        self.assertIn("hypothesis, not an established cause", " ".join(result["notes"]))

    def test_one_hour_boundary_included_and_older_excluded_on_all_sources(self):
        for source in ("node-target", "other-node"):
            with self.subTest(source=source):
                self.db.execute("DELETE FROM events_v WHERE line_id IN (998, 999)")
                self.add_event(998, "2005-06-05 10:59:59", source=source)
                self.add_event(999, "2005-06-05 11:00:00", source=source)
                data = self.investigate()["data"]
                preceding = data["preceding_event"]
                self.assertEqual(preceding["line_id"], 999)
                self.assertEqual(preceding["seconds_before_target"], 3600)
                self.assertEqual(data["candidate_cause"] is not None, source == "node-target")
                self.db.execute("DELETE FROM events_v WHERE line_id = 999")
                self.assertIsNone(self.investigate()["data"]["candidate_cause"])

    def test_custom_local_window_does_not_expand_candidate_hour(self):
        self.add_event(998, "2005-06-05 11:30:00")
        self.add_event(999, "2005-06-05 12:05:00")
        result = self.investigate(same_source_minutes=10)
        self.assertEqual([row["line_id"] for row in result["data"]["same_source_problems"]], [999])
        self.assertEqual(result["data"]["candidate_cause"]["line_id"], 998)
        self.assertEqual(result["filters"]["same_source_minutes"], 10)
        self.assertEqual(result["data"]["same_source_window"], {
            "start": "2005-06-05 11:50:00", "end": "2005-06-05 12:10:00", "minutes": 10})

    def test_invalid_local_window_rejected_without_database_connection(self):
        for value in (0, -1, 61, float("inf"), float("nan"), True, None, "60"):
            with self.subTest(value=value):
                with self.assertRaisesRegex(ValueError, "same_source_minutes"):
                    self.investigate(same_source_minutes=value)
        self.connect.assert_not_called()

    def test_only_problem_lines_from_requested_dataset_are_candidates(self):
        self.add_event(997, "2005-06-05 11:59:00", dataset="openstack")
        self.add_event(998, "2005-06-05 11:58:00", level="INFO", severity=0)
        self.assertIsNone(self.investigate()["data"]["candidate_cause"])
        self.add_event(999, "2005-06-05 11:55:00", level="INFO", severity=0, alert=True)
        self.assertEqual(self.investigate()["data"]["candidate_cause"]["line_id"], 999)

    def test_local_result_truncation_does_not_hide_nearest_candidate(self):
        for offset in range(1, 13):
            self.add_event(2000 + offset, f"2005-06-05 11:{offset:02d}:00")
        result = self.investigate()
        self.assertEqual(len(result["data"]["same_source_problems"]), 10)
        self.assertEqual(result["data"]["candidate_cause"]["line_id"], 2012)
        local_query = next(query for query in result["queries"] if "Other same-source" in query["purpose"])
        self.assertTrue(local_query["truncated"])

    def test_subsecond_order_and_actual_gaps_are_preserved(self):
        self.db.execute("UPDATE events_v SET ts='2005-06-05 12:00:00.500000' WHERE line_id=1000")
        self.add_event(998, "2005-06-05 12:00:00.250000")
        self.add_event(999, "2005-06-05 12:00:00.500000")
        result = self.investigate()
        cause = result["data"]["candidate_cause"]
        self.assertEqual(cause["line_id"], 998)
        self.assertEqual(cause["seconds_before_target"], 0.25)
        self.assertEqual(result["data"]["what_failed"]["ts"], "2005-06-05 12:00:00.500000")
        self.assertEqual(result["queries"][-1]["params"][2], "2005-06-05 12:00:00.500000")

    def test_unknown_source_is_not_treated_as_same_source(self):
        self.db.execute("UPDATE events_v SET source=NULL WHERE line_id=1000")
        self.add_event(998, "2005-06-05 11:55:00", source=None)
        self.add_event(999, "2005-06-05 11:59:00", source="known-node")
        result = self.investigate()["data"]
        self.assertEqual(result["preceding_event"]["line_id"], 999)
        self.assertFalse(result["preceding_event"]["same_source_as_target"])
        self.assertIsNone(result["candidate_cause"])
        self.assertEqual(result["same_source_problems"], [])

    def test_existing_positional_call_result_keys_and_published_arguments(self):
        result = tools.call_tool("investigate_event", {"dataset": "bgl", "line_id": 1000})
        self.assertEqual(result, tools.investigate_event("bgl", 1000, 10))
        self.assertTrue({"what_failed", "before", "after", "candidate_cause", "recurrence_in_dataset",
                         "same_source_problems", "suggested_checks"}.issubset(result["data"]))
        self.assertEqual(result["filters"]["same_source_minutes"], 60)
        schema = next(tool["input_schema"] for tool in tools.claude_tools() if tool["name"] == "investigate_event")
        self.assertEqual(set(schema["properties"]), set(inspect.signature(tools.investigate_event).parameters))
        self.assertEqual(schema["required"], ["dataset", "line_id"])

    def test_missing_target_remains_compatible(self):
        result = tools.investigate_event("bgl", 99999)
        self.assertEqual(result["data"], {})
        self.assertEqual(len(result["queries"]), 1)

    def test_different_source_explicit_link_is_supported_without_inventing_mechanism(self):
        self.db.execute("UPDATE events_v SET content='target request_id=req-42' WHERE line_id=1000")
        self.add_event(998, "2005-06-05 11:55:00", source="linked-node")
        self.db.execute("UPDATE events_v SET content='failed request_id=req-42' WHERE line_id=998")
        self.add_event(999, "2005-06-05 11:59:00", source="unrelated-node")
        result = self.investigate()["data"]
        self.assertEqual(result["preceding_event"]["line_id"], 999)
        self.assertEqual(result["candidate_cause"]["line_id"], 998)
        self.assertEqual(result["candidate_cause"]["seconds_before_target"], 300)
        self.assertFalse(result["candidate_cause"]["same_source_as_target"])
        self.assertEqual(result["candidate_relationship"]["basis"], "shared_explicit_identifier")
        self.assertEqual(result["candidate_relationship"]["identifiers"], "request_id=req-42")
        self.assertIn("not a causal mechanism", result["candidate_relationship"]["caveat"])
        self.add_event(997, "2005-06-05 11:50:00")
        result = self.investigate()["data"]
        self.assertEqual(result["candidate_cause"]["line_id"], 997)
        self.assertEqual(result["candidate_relationship"]["basis"], "same_source")

    def test_partial_ids_different_fields_and_placeholders_do_not_link_sources(self):
        self.add_event(999, "2005-06-05 11:55:00", source="other-node")
        for target, previous in (("request_id=req-42", "request_id=req-420"),
                                 ("job_id=job-42", "request_id=job-42"),
                                 ("request_id=null", "request_id=null"),
                                 ("request_id=req-42/path", "request_id=req-42/path"),
                                 ("process_id=123", "process_id=123")):
            with self.subTest(target=target, previous=previous):
                self.db.execute("UPDATE events_v SET content=? WHERE line_id=1000", (target,))
                self.db.execute("UPDATE events_v SET content=? WHERE line_id=999", (previous,))
                self.assertIsNone(self.investigate()["data"]["candidate_cause"])

    def test_linked_event_must_still_be_strictly_earlier_and_within_hour(self):
        self.db.execute("UPDATE events_v SET content='job_id=job-42' WHERE line_id=1000")
        for ts in ("2005-06-05 10:59:59", "2005-06-05 12:00:00", "2005-06-05 12:05:00"):
            with self.subTest(ts=ts):
                self.db.execute("DELETE FROM events_v WHERE line_id=999")
                self.add_event(999, ts, source="other-node")
                self.db.execute("UPDATE events_v SET content='job_id=job-42' WHERE line_id=999")
                self.assertIsNone(self.investigate()["data"]["candidate_cause"])

    def test_post_target_same_template_event_is_visible_without_causal_claim(self):
        self.add_event(1003, "2005-06-05 12:00:00.167000", source="other-node", template="Etarget")
        result = self.investigate()["data"]
        self.assertIsNone(result["candidate_cause"])
        self.assertIsNone(result["preceding_event"])
        self.assertEqual(len(result["nearby_same_template_events"]), 1)
        nearby = result["nearby_same_template_events"][0]
        self.assertEqual(nearby["line_id"], 1003)
        self.assertAlmostEqual(nearby["seconds_from_target"], 0.167)
        self.assertFalse(nearby["same_source_as_target"])
        self.assertIn("does not establish a shared cause", " ".join(result["suggested_checks"]))

    def test_no_preceding_event_reports_no_supported_candidate(self):
        data = self.investigate()["data"]
        self.assertIsNone(data["preceding_event"])
        self.assertIsNone(data["candidate_cause"])
        self.assertIsNone(data["candidate_relationship"])
        self.assertIn("No supported causal candidate has been identified", " ".join(data["suggested_checks"]))

    def test_live_17742_scenario_and_evidence_citation_regression(self):
        self.db.execute("UPDATE events_v SET component='APP', template_id='Eimage', "
                        "content='invalid or missing program image' WHERE line_id=1")
        self.db.execute("UPDATE events_v SET ts='2005-06-05 11:42:24.650000', source='other-node' WHERE line_id=1")
        self.db.execute("UPDATE events_v SET content='data TLB error interrupt' WHERE line_id=1000")
        self.add_event(1003, "2005-06-05 12:00:00.167000", source="third-node", template="Etarget")
        events, _, is_error = agent._execute(agent.Session("engineer"), "investigate_event", {"dataset": "bgl", "line_id": 1000})
        self.assertFalse(is_error)
        envelope = events[-1]["envelope"]
        self.assertEqual(envelope["evidence_id"], "Q1")
        data = envelope["data"]
        self.assertAlmostEqual(data["preceding_event"]["seconds_before_target"], 1055.35)
        self.assertIsNone(data["candidate_cause"])
        self.assertEqual(data["same_source_problems"], [])
        self.assertAlmostEqual(data["nearby_same_template_events"][0]["seconds_from_target"], 0.167)
        self.assertTrue(all(query["sql"] and "params" in query for query in envelope["queries"]))

    def test_engineer_instructions_distinguish_observations_from_causal_claims(self):
        prompt = agent.system_prompt("engineer")
        self.assertIn("Same source does not prove causality", prompt)
        self.assertIn("Nearby timestamps do not prove causality", prompt)
        self.assertIn("never claim a failure cascade without supporting evidence", prompt)
        self.assertIn("recurrence_in_dataset is historical", prompt)
        self.assertIn("Do not invent mechanisms connecting unrelated errors", prompt)
        self.assertIn("No supported causal candidate has been identified", prompt)
        self.assertIn("Highlight relevant nearby_same_template_events", prompt)


class NeighborInterpretationTests(unittest.TestCase):
    """Check both context tools against the same time/source cases."""

    def setUp(self):
        InvestigationTimingTests.setUp(self)

    add_event = InvestigationTimingTests.add_event

    def contexts(self):
        investigation = tools.investigate_event("bgl", 1000)
        nearby = tools.get_nearby_events("bgl", 1000)
        return (("investigate_event", investigation,
                 investigation["data"]["before"] + investigation["data"]["after"]),
                ("get_nearby_events", nearby,
                 [row for row in nearby["data"]["lines"] if row["line_id"] != 1000]))

    def test_identical_timestamps_do_not_imply_chronological_order(self):
        self.add_event(999, "2005-06-05 12:00:00")
        self.add_event(1001, "2005-06-05 12:00:00")
        for name, envelope, rows in self.contexts():
            with self.subTest(tool=name):
                self.assertEqual([row["seconds_from_target"] for row in rows], [0, 0])
                notes = " ".join(envelope["notes"])
                self.assertIn("log order", notes.replace("log-order", "log order"))
                self.assertIn("Identical timestamps do not establish ordering", notes)

    def test_lower_line_id_can_be_later_and_higher_line_id_can_be_earlier(self):
        self.add_event(999, "2005-06-05 12:05:00")
        self.add_event(1001, "2005-06-05 11:55:00")
        for name, envelope, rows in self.contexts():
            with self.subTest(tool=name):
                self.assertEqual({row["line_id"]: row["seconds_from_target"] for row in rows}, {999: 300, 1001: -300})
                self.assertIn("log order", " ".join(envelope["notes"]).replace("log-order", "log order"))
        nearby = tools.get_nearby_events("bgl", 1000)["data"]["lines"]
        self.assertEqual([(row["line_id"], row["position"]) for row in nearby],
                         [(999, "before"), (1000, "target"), (1001, "after")])

    def test_source_relationship_is_explicit_without_causal_claim(self):
        self.add_event(999, "2005-06-05 12:00:00", source="different-node")
        self.add_event(1001, "2005-06-05 12:00:00")
        for name, envelope, rows in self.contexts():
            with self.subTest(tool=name):
                self.assertEqual([(row["source"], row["same_source_as_target"]) for row in rows],
                                 [("different-node", False), ("node-target", True)])
                self.assertIn("do not prove causality", " ".join(envelope["notes"]))

    def test_millisecond_same_template_different_source_keeps_actual_gap(self):
        self.db.execute("UPDATE events_v SET ts='2005-06-05 12:00:00.650000' WHERE line_id=1000")
        self.add_event(999, "2005-06-05 12:00:00.483000", source="other-node", template="Etarget")
        self.add_event(1001, "2005-06-05 12:00:00.817000", source="third-node", template="Etarget")
        for name, envelope, rows in self.contexts():
            with self.subTest(tool=name):
                self.assertAlmostEqual(rows[0]["seconds_from_target"], -0.167)
                self.assertAlmostEqual(rows[1]["seconds_from_target"], 0.167)
                self.assertTrue(all(row["template_id"] == "Etarget" for row in rows))
                self.assertTrue(all(not row["same_source_as_target"] for row in rows))
                self.assertIn("do not prove causality", " ".join(envelope["notes"]))
        matching = tools.investigate_event("bgl", 1000)["data"]["nearby_same_template_events"]
        self.assertEqual([row["line_id"] for row in matching], [999, 1001])

    def test_distant_log_neighbors_keep_actual_gap_and_temporal_warning(self):
        self.add_event(999, "2005-05-27 12:00:00")
        self.add_event(1001, "2005-06-14 12:00:00")
        for name, envelope, rows in self.contexts():
            with self.subTest(tool=name):
                self.assertEqual([row["seconds_from_target"] for row in rows], [-777600, 777600])
                self.assertIn("not necessarily chronological or local in time", " ".join(envelope["notes"]))

    def test_time_window_keeps_reference_when_target_is_truncated_out(self):
        # More than 60 earlier records fill the sample before the target is reached.
        for line in range(2000, 2061):
            self.add_event(line, "2005-06-05 11:59:59.833000", source="other-node")
        result = tools.get_nearby_events("bgl", 1000, minutes=1)
        self.assertTrue(result["queries"][-1]["truncated"])
        self.assertEqual(len(result["data"]["lines"]), 60)
        self.assertFalse(any(row["line_id"] == 1000 for row in result["data"]["lines"]))
        for row in result["data"]["lines"]:
            self.assertAlmostEqual(row["seconds_from_target"], -0.167)
            self.assertFalse(row["same_source_as_target"])
        self.assertNotIn("not found", " ".join(result["notes"]))

    def test_nearby_evidence_and_existing_arguments_stay_compatible(self):
        self.add_event(999, "2005-06-05 11:59:59.833000", source="other-node")
        events, _, is_error = agent._execute(agent.Session("engineer"), "get_nearby_events",
                                           {"dataset": "bgl", "line_id": 1000, "before": 1, "after": 0})
        self.assertFalse(is_error)
        envelope = events[-1]["envelope"]
        self.assertEqual(envelope["evidence_id"], "Q1")
        self.assertEqual(envelope["queries"][0]["params"], ["bgl", 999, 1000])
        self.assertIn("ORDER BY line_id", envelope["queries"][0]["sql"])
        self.assertTrue({"tool", "scope", "filters", "queries", "data", "notes"}.issubset(envelope))
        self.assertEqual(set(envelope["data"]), {"lines"})
        self.assertEqual(tools.get_nearby_events("bgl", 1000, 1, 0), tools.call_tool(
            "get_nearby_events", {"dataset": "bgl", "line_id": 1000, "before": 1, "after": 0}))

    def test_time_mode_same_source_filter_and_missing_target_stay_compatible(self):
        self.add_event(999, "2005-06-05 11:59:59.833000", source="other-node")
        self.add_event(1001, "2005-06-05 12:00:00.167000")
        result = tools.get_nearby_events("bgl", 1000, 5, 5, 1, True)
        self.assertEqual([row["line_id"] for row in result["data"]["lines"]], [1000, 1001])
        self.assertTrue(all(row["same_source_as_target"] for row in result["data"]["lines"]))
        self.assertAlmostEqual(result["data"]["lines"][1]["seconds_from_target"], 0.167)
        missing = tools.get_nearby_events("bgl", 99999, minutes=1)
        self.assertEqual(missing["data"], {"lines": []})
        self.assertIn("not found", " ".join(missing["notes"]))

    def test_engineer_instructions_cover_neighbor_order_and_sources(self):
        prompt = agent.system_prompt("engineer")
        self.assertIn("get_nearby_events.position also describes log order only", prompt)
        self.assertIn("A lower line_id may have a later timestamp", prompt)
        self.assertIn("log adjacency does not imply temporal proximity", prompt)
        self.assertIn("same source as the failing line or from other sources", prompt)
        self.assertIn("do not claim they share a cause", prompt)


if __name__ == "__main__":
    unittest.main()
