"""Offline integration checks. SQLite fixtures and scripted providers never query SereneDB.

SQLite adaptations exercise ordinary filters/grouping and evidence plumbing, not the
SereneDB implementations of regex extraction, quantiles, intervals or query planning.
"""
import asyncio
import inspect
import json
import math
import re
from datetime import datetime, timedelta
from types import SimpleNamespace
from unittest import TestCase
from unittest.mock import MagicMock, patch

import test_severity_filters as legacy

agent, tools = legacy.agent, legacy.tools

LEGACY = {"corpus_overview", "search_logs", "get_nearby_events", "preceding_patterns", "template_history",
          "investigate_event", "count_problems", "problem_trend", "recurring_problems", "compare_periods", "risk_ranking"}
NEW = {"get_traceback", "trace_instance", "extract_field", "filter_numeric", "count_incidents", "correlate_templates",
       "correlation_chains", "chain_profile"}


class NewToolsTests(TestCase):
    def setUp(self):
        legacy.SeverityFilterTests.setUp(self)
        self.db.execute("DELETE FROM events_v")
        self.next_line = 1

        class AnyValue:
            def __init__(self):
                self.value = None
            def step(self, value):
                if self.value is None:
                    self.value = value
            def finalize(self):
                return self.value

        class Quantile:
            def __init__(self):
                self.values = []
                self.q = 0.5
            def step(self, value, q):
                if value is not None:
                    self.values.append(value)
                self.q = q
            def finalize(self):
                values = sorted(self.values)
                if not values:
                    return None
                i = self.q * (len(values) - 1)
                lo = math.floor(i)
                hi = math.ceil(i)
                return values[lo] + (values[hi] - values[lo]) * (i - lo)

        def date_trunc(bucket, value):
            ts = datetime.fromisoformat(value)
            if bucket == "minute":
                ts = ts.replace(second=0, microsecond=0)
            elif bucket == "hour":
                ts = ts.replace(minute=0, second=0, microsecond=0)
            elif bucket == "day":
                ts = ts.replace(hour=0, minute=0, second=0, microsecond=0)
            elif bucket == "week":
                ts = (ts - timedelta(days=ts.weekday())).replace(hour=0, minute=0, second=0, microsecond=0)
            else:
                raise ValueError(bucket)
            return ts.isoformat(sep=" ")

        def extract(content, pattern, group):
            match = re.search(pattern, content or "")
            return match.group(group) if match else None

        self.db.create_aggregate("ANY_VALUE", 1, AnyValue)
        self.db.create_aggregate("QUANTILE_CONT", 2, Quantile)
        self.db.create_function("date_trunc", 2, date_trunc)
        self.db.create_function("regexp_extract", 3, extract)
        execute = self.connection.execute.side_effect

        def new_execute(sql, params):
            if "INTERVAL '" in sql:
                # Context interval SQL is inspected separately; SQLite is not SereneDB.
                cursor = MagicMock()
                cursor.fetchall.return_value = []
                return cursor
            sql = sql.replace("TRY_CAST(", "CAST(").replace(" ILIKE ", " LIKE ")
            sql = sql.replace("CAST(%s AS TIMESTAMP) + INTERVAL 2 SECOND", "datetime(%s, '+2 seconds')")
            return execute(sql, params)

        self.connection.execute.side_effect = new_execute

    def add(self, template="EA", source="node-a", ts="2005-06-05 12:00:00", content="fixture message",
            dataset="bgl", severity=3, component="KERNEL", alert=False):
        line = self.next_line
        self.next_line += 1
        self.db.execute("INSERT INTO events_v (dataset,line_id,ts,level,severity,component,team,source,template_id,template,content,is_alert) "
                        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                        (dataset,line,ts,"FATAL",severity,component,"compute-kernel",source,template,template,content,alert))
        return line

    def correlated(self):
        for day in range(1, 7):
            for template in ("EA", "EB"):
                for _ in range(day + 9):
                    self.add(template=template, ts=f"2005-06-{day:02d} 12:00:00")

    def test_all_nineteen_registrations_and_both_provider_schemas(self):
        self.assertEqual(set(tools.TOOLS), LEGACY | NEW)
        claude = {tool["name"]: tool["input_schema"] for tool in tools.claude_tools()}
        compatible = {tool["function"]["name"]: tool["function"]["parameters"] for tool in agent._openai_tools()}
        self.assertEqual(claude, compatible)
        for name in LEGACY | NEW:
            with self.subTest(tool=name):
                self.assertEqual(set(claude[name]["properties"]), set(inspect.signature(tools.TOOLS[name][0]).parameters))
                required = [key for key, param in inspect.signature(tools.TOOLS[name][0]).parameters.items()
                            if param.default is inspect.Parameter.empty]
                self.assertEqual(claude[name]["required"], required)
        for name in NEW:
            self.assertNotIn("severity", claude[name]["properties"])

    def test_mcp_discovers_all_nineteen_with_matching_callable_inputs(self):
        import mcp_server
        schemas = {tool.name: tool.input_schema for tool in asyncio.run(mcp_server.server.list_tools())}
        self.assertEqual(set(schemas), LEGACY | NEW)
        for name in NEW:
            self.assertEqual(set(schemas[name]["properties"]), set(inspect.signature(tools.TOOLS[name][0]).parameters))
        self.connect.assert_not_called()

    def test_traceback_is_bounded_and_does_not_assert_one_exception(self):
        line = self.add(content="Traceback (most recent call last)")
        self.add(content="ValueError: fixture")
        self.add(content="out of chronological window", ts="2005-06-04 00:00:00")
        result = tools.call_tool("get_traceback", {"dataset":"bgl", "line_id":line, "max_lines":1})
        self.assertEqual(len(result["data"]["lines"]), 1)
        self.assertTrue(result["data"]["truncated"])
        self.assertTrue(result["queries"][-1]["truncated"])
        self.assertIn("ts >= %s", result["queries"][-1]["sql"])
        self.assertIn("membership and incident count are unverified", " ".join(result["notes"]))
        self.assertNotIn("treat them as one incident", " ".join(result["notes"]))

    def test_instance_counts_and_sample_truncation_are_separate(self):
        for _ in range(3):
            self.add(content="instance fixture-12345678")
        self.add(content="different entity")
        result = tools.call_tool("trace_instance", {"dataset":"bgl", "instance_id":"fixture-12345678", "limit":1})
        self.assertEqual(result["data"]["summary"]["lines"], 3)
        self.assertEqual(len(result["data"]["lines"]), 1)
        self.assertTrue(result["data"]["truncated"])
        self.assertTrue(result["queries"][-1]["truncated"])
        self.assertEqual(result["filters"]["limit"], 1)

    def test_extract_numeric_distribution_coverage_and_time_filters(self):
        self.add(content="time: 0.2 status: 200")
        self.add(content="time: 1.5 status: 500")
        self.add(content="no extracted value")
        self.add(content="time: 99", ts="2005-07-01 00:00:00")
        result = tools.call_tool("extract_field", {"dataset":"bgl", "field":"latency", "start":"2005-06-01", "end":"2005-07-01"})
        self.assertEqual(result["data"]["lines_in_scope"], 3)
        self.assertEqual(result["data"]["lines_with_field"], 2)
        self.assertAlmostEqual(result["data"]["stats"]["p50"], .85)
        self.assertEqual(result["filters"]["end"], "2005-07-01")
        self.assertIn("all lines", result["scope"])

    def test_text_extraction_excludes_missing_values_and_reports_top_limit(self):
        self.add(content="req-00000000-0000-0000-0000-000000000001")
        self.add(content="req-00000000-0000-0000-0000-000000000002")
        self.add(content="no request identifier")
        result = tools.call_tool("extract_field", {"dataset":"bgl", "field":"req_id", "top":1})
        self.assertEqual(len(result["data"]["top_values"]), 1)
        self.assertEqual(result["data"]["top_values"][0]["n"], 1)
        self.assertTrue(result["queries"][-1]["truncated"])

    def test_numeric_filter_is_parameterized_with_full_count_and_truncated_sample(self):
        for value in (.2, 2., 3.):
            self.add(content=f"time: {value}")
        result = tools.call_tool("filter_numeric", {"dataset":"bgl", "field":"latency", "op":">", "value":1.,
                                                    "start":"2005-06-01", "end":"2005-07-01", "limit":1})
        self.assertEqual(result["data"]["matching_lines"], 2)
        self.assertEqual(len(result["data"]["lines"]), 1)
        self.assertTrue(result["queries"][-1]["truncated"])
        self.assertIn("v > %s", result["queries"][0]["sql"])
        self.assertEqual(result["queries"][0]["params"][-1], 1.)
        self.assertEqual(result["filters"]["start"], "2005-06-01")
        self.assertIn("2005-07-01", result["scope"])

    def test_estimated_episodes_are_grouped_by_dataset_and_template(self):
        self.add(); self.add()
        self.add(ts="2005-06-05 12:01:00")
        self.add(dataset="openstack")
        result = tools.call_tool("count_incidents", {"min_lines":1})
        rows = {row["dataset"]:row for row in result["data"]["templates"]}
        self.assertEqual((rows["bgl"]["lines"], rows["bgl"]["episodes"]), (3,2))
        self.assertEqual((rows["openstack"]["lines"], rows["openstack"]["episodes"]), (1,1))
        self.assertEqual(result["data"]["total_problem_lines"], 4)
        self.assertIn("not a verified incident", " ".join(result["notes"]))
        self.assertIn("No exact severity", " ".join(result["notes"]))
        self.assertIn("GROUP BY dataset, template_id", result["queries"][0]["sql"])

    def test_episode_top_limit_is_visible_in_query_evidence(self):
        self.add(template="EA"); self.add(template="EB")
        result = tools.count_incidents(dataset="bgl", min_lines=1, top=1)
        self.assertEqual(len(result["data"]["templates"]), 1)
        self.assertTrue(result["queries"][0]["truncated"])
        self.assertEqual(result["data"]["total_problem_lines"], 2)

    def test_correlation_and_chains_are_statistical_not_causal(self):
        self.correlated()
        pair = tools.call_tool("correlate_templates", {"dataset":"bgl"})
        self.assertEqual(len(pair["data"]["pairs"]), 1)
        self.assertEqual(pair["data"]["pairs"][0]["r"], 1.)
        self.assertIn("statistical association", " ".join(pair["notes"]))
        result = tools.call_tool("correlation_chains", {"dataset":"bgl"})
        self.assertEqual(len(result["data"]["confirmed_chains"]), 1)
        chain = result["data"]["confirmed_chains"][0]
        self.assertEqual(chain["episodes"], 6)
        self.assertIn("not verified incidents", " ".join(result["notes"]))
        self.assertIn("No exact severity", " ".join(result["notes"]))
        self.assertFalse(result["data"]["truncated"])

    def test_chain_profile_keeps_nested_sql_and_discloses_broad_scope(self):
        self.correlated()
        result = tools.call_tool("chain_profile", {"dataset":"bgl", "templates":["EA","EB"], "top":2})
        self.assertEqual(result["data"]["frequency"]["episodes"], 6)
        self.assertTrue(result["data"]["episode_dates_truncated"])
        self.assertEqual(len(result["data"]["frequency"]["episode_dates"]), 2)
        self.assertGreaterEqual(len(result["queries"]), 5)
        self.assertIn("not one confirmed event", " ".join(result["notes"]))
        self.assertIn("No severity, component or date-range filter", " ".join(result["notes"]))
        self.assertIn("whole-day sample", " ".join(result["notes"]))

    def test_chain_profile_context_lookahead_is_disclosed(self):
        inner = {"queries":[], "data":{"confirmed_chains":[{"chain_id":"CH-fixture", "size":2,
                 "templates":[{"template_id":"EA"},{"template_id":"EB"}], "order_proposal":["EA","EB"],
                 "episodes":2, "episode_dates":["2005-06-05","2005-06-06"],
                 "trend_first_half_lines":2, "trend_second_half_lines":2}]}}
        execute = self.connection.execute.side_effect
        def crowded_context(sql, params):
            if "INTERVAL '" in sql:
                cursor = MagicMock(); cursor.fetchall.return_value = [{"line_id":i} for i in range(61)]
                return cursor
            return execute(sql, params)
        with patch.object(tools,"correlation_chains",return_value=inner):
            self.connection.execute.side_effect = crowded_context
            result = tools.chain_profile("bgl", chain_id="CH-fixture")
        self.assertEqual(len(result["data"]["context_sample"]),60)
        self.assertTrue(result["data"]["context_truncated"])
        self.assertTrue(result["queries"][-1]["truncated"])

    def test_invalid_inputs_do_not_connect(self):
        cases = [("get_traceback",dict(dataset="bgl",line_id=1,max_lines=0)),
                 ("trace_instance",dict(dataset="bgl",instance_id="short")),
                 ("extract_field",dict(dataset="bgl",field="not-a-field")),
                 ("filter_numeric",dict(dataset="bgl",field="latency",op=">",value=float("nan"))),
                 ("count_incidents",dict(bucket="year")),
                 ("correlate_templates",dict(dataset="bgl",min_r=2)),
                 ("correlation_chains",dict(dataset="bgl",min_repeats=0)),
                 ("chain_profile",dict(dataset="bgl",templates=["EA","EB"],context_minutes=-1))]
        for name,args in cases:
            with self.subTest(tool=name):
                with self.assertRaises(ValueError):
                    tools.call_tool(name,args)
        self.connect.assert_not_called()

    def test_new_tools_reject_unsupported_severity_instead_of_silently_ignoring_it(self):
        for name in NEW:
            with self.subTest(tool=name):
                with self.assertRaises(TypeError):
                    tools.call_tool(name, {"severity":3})
        self.connect.assert_not_called()

    def test_agent_dispatch_preserves_q_sql_filters_and_provider_usage(self):
        self.add(); self.add()
        args = {"dataset":"bgl", "bucket":"minute", "min_lines":1}
        for provider in ("openai","anthropic"):
            with self.subTest(provider=provider):
                client = MagicMock(); session = agent.Session("team_lead")
                if provider == "openai":
                    call = SimpleNamespace(id="new-tool",function=SimpleNamespace(name="count_incidents",arguments=json.dumps(args)))
                    client.chat.completions.create.side_effect = [
                        SimpleNamespace(usage=None,choices=[SimpleNamespace(finish_reason="stop",message=SimpleNamespace(content=None,tool_calls=[call]))]),
                        SimpleNamespace(usage=None,choices=[SimpleNamespace(finish_reason="stop",message=SimpleNamespace(content="Estimated source/minute episodes [Q1]; not verified incidents.",tool_calls=[]))])]
                    factory,runner = "_openai_client",agent._run_openai
                else:
                    client.beta.messages.create.side_effect = [
                        SimpleNamespace(stop_reason="tool_use",usage=SimpleNamespace(),content=[SimpleNamespace(type="tool_use",id="new-tool",name="count_incidents",input=args)]),
                        SimpleNamespace(stop_reason="end_turn",usage=SimpleNamespace(),content=[SimpleNamespace(type="text",text="Estimated source/minute episodes [Q1]; not verified incidents.")])]
                    factory,runner = "_client",agent._run_anthropic
                with patch.object(agent,factory,return_value=client):
                    events = list(runner(session,"Estimate repeated logging episodes."))
                envelope = next(event["envelope"] for event in events if event["type"]=="tool_result")
                self.assertEqual(envelope["evidence_id"],"Q1")
                self.assertEqual(envelope["filters"]["dataset"],"bgl")
                self.assertEqual(envelope["data"]["templates"][0]["episodes"],1)
                self.assertIn("GROUP BY dataset, template_id",envelope["queries"][0]["sql"])
                self.assertEqual(envelope["queries"][0]["params"][0],"minute")
                self.assertEqual(events[-1]["usage"]["model_calls"],2)
