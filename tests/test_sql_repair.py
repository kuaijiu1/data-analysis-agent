import json
import os
import unittest
from unittest.mock import patch

import test_flow
import duckdb

from app import analysis


def model_sequence(*outputs):
    class SequenceModel(test_flow.FakeOpenAI):
        requests = []

        def create(self, **kwargs):
            index = len(self.requests)
            self.requests.append(kwargs)
            if index >= len(outputs):
                raise AssertionError("发生了预期之外的模型调用")
            value = outputs[index]
            if isinstance(value, Exception):
                raise value
            return test_flow.FakeResponse(json.dumps(value, ensure_ascii=False) if isinstance(value, dict) else value)

    return SequenceModel


def plan(sql):
    return {"sql": sql, "chart": "none", "reason": "按实际销售字段统计"}


class SQLRepairTests(unittest.TestCase):
    setUp = test_flow.FlowTests.setUp
    _upload = test_flow.FlowTests._upload
    _await_status = test_flow.FlowTests._await_status

    def dataset(self):
        item_id = self._upload("repair.csv", "地区,销售额\n华东,120\n华北,80\n华东,30\n".encode())
        self._await_status(item_id, "ready")
        return item_id

    def ask(self, item_id, model):
        with patch.object(analysis, "OpenAI", model), patch.dict(os.environ, {"MODEL_PROVIDER": "openai", "OPENAI_API_KEY": "test"}):
            return self.client.post(f"/api/datasets/{item_id}/ask", json={"question": "华东销售总额是多少？"})

    def test_valid_query_needs_no_repair_call(self):
        item_id = self.dataset()
        model = model_sequence(plan('SELECT SUM("销售额") AS value FROM data'), "总销售额230。")
        response = self.ask(item_id, model)
        self.assertEqual(response.status_code, 200, response.text)
        self.assertIsNone(response.json()["sql_repair"])
        self.assertEqual(len(model.requests), 2)

    def test_binder_error_is_repaired_and_final_sql_saved_to_history(self):
        item_id = self.dataset()
        original = 'SELECT SUM("销售金额") AS value FROM data WHERE "地区"=\'华东\''
        corrected = 'SELECT SUM("销售额") AS value FROM data WHERE "地区"=\'华东\''
        model = model_sequence(plan(original), {"sql": corrected, "reason": "字段应为销售额，保留华东筛选"}, "华东销售额150。")
        response = self.ask(item_id, model)
        self.assertEqual(response.status_code, 200, response.text)
        record = response.json()
        self.assertEqual(record["rows"], [{"value": 150}])
        self.assertEqual(record["sql_repair"]["original_sql"], original)
        self.assertEqual(record["sql_repair"]["corrected_sql"], record["sql"])
        self.assertIn("已自动修正查询", record["answer"])
        repair_request = model.requests[1]
        self.assertEqual(repair_request["text"]["format"]["name"], "sql_repair")
        feedback = json.loads(repair_request["input"])
        self.assertEqual(feedback["error"]["type"], "BinderException")
        self.assertNotIn("sample_rows", feedback["context"]["tables"][0])
        self.assertIn("只读权限", repair_request["instructions"])
        saved = self.client.get(f"/api/datasets/{item_id}/history/{record['history_id']}")
        self.assertEqual(saved.status_code, 200)
        self.assertEqual(saved.json()["sql"], record["sql"])
        self.assertEqual(saved.json()["sql_repair"], record["sql_repair"])
        summary = json.loads(model.requests[2]["input"])
        self.assertEqual(summary["sql"], record["sql"])
        self.assertEqual(summary["rows"], [{"value": 150}])

    def test_syntax_function_and_conversion_errors_can_be_repaired(self):
        item_id = self.dataset()
        originals = [
            ('SELECT SUM( FROM data', ("ParseError", "ParserException")),
            ('SELECT "地区 FROM data', ("TokenError",)),
            ('SELECT nonexistent_total("销售额") FROM data', ("CatalogException",)),
            ('SELECT CAST("地区" AS INTEGER) FROM data', ("ConversionException",)),
            ('SELECT strptime("地区", \'%Y-%m-%d\') FROM data', ("InvalidInputException",)),
        ]
        for original, kinds in originals:
            with self.subTest(original=original):
                model = model_sequence(plan(original), {"sql": 'SELECT SUM("销售额") AS value FROM data', "reason": "修正用法"}, "总销售额230。")
                response = self.ask(item_id, model)
                self.assertEqual(response.status_code, 200, response.text)
                self.assertIn(response.json()["sql_repair"]["error_type"], kinds)
                self.assertEqual(len(model.requests), 3)
                if kinds in [("ConversionException",), ("InvalidInputException",)]:
                    feedback = json.loads(model.requests[1]["input"])
                    self.assertNotIn("华东", feedback["error"]["message"])

    def test_failed_or_unchanged_correction_stops_without_history(self):
        item_id = self.dataset()
        original = 'SELECT SUM("缺失字段") FROM data'
        for corrected in [original, 'SELECT SUM("另一个缺失字段") FROM data']:
            with self.subTest(corrected=corrected):
                model = model_sequence(plan(original), {"sql": corrected, "reason": "尝试修正"})
                with patch.object(analysis, "_query", wraps=analysis._query) as query:
                    response = self.ask(item_id, model)
                self.assertEqual(response.status_code, 400, response.text)
                self.assertIn("修正一次", response.json()["detail"])
                self.assertEqual(len(model.requests), 2)
                self.assertLessEqual(query.call_count, 2)
        self.assertEqual(self.client.get(f"/api/datasets/{item_id}/history").json()["total"], 0)

    def test_malformed_or_failed_repair_response_does_not_retry_model(self):
        item_id = self.dataset()
        for output in ["", '{"sql": "SELECT 1"}', RuntimeError("模拟连接错误")]:
            with self.subTest(output=str(output)):
                model = model_sequence(plan('SELECT "缺失字段" FROM data'), output)
                response = self.ask(item_id, model)
                self.assertEqual(response.status_code, 400, response.text)
                self.assertIn("自动修正未能返回有效结果", response.json()["detail"])
                self.assertEqual(len(model.requests), 2)

    def test_unsafe_correction_is_rejected_before_execution(self):
        item_id = self.dataset()
        for corrected in ["DROP TABLE data", "SELECT * FROM other_user_table", "SELECT * FROM read_csv('secret.csv')", "SELECT 1; SELECT 2"]:
            with self.subTest(corrected=corrected):
                model = model_sequence(plan('SELECT "缺失字段" FROM data'), {"sql": corrected, "reason": "修正"})
                with patch.object(analysis, "_query", wraps=analysis._query) as query:
                    response = self.ask(item_id, model)
                self.assertEqual(response.status_code, 400, response.text)
                self.assertIn("只读权限检查", response.json()["detail"])
                self.assertEqual(query.call_count, 1)
                self.assertEqual(analysis._query(item_id, "SELECT COUNT(*) AS n FROM data")[0], [{"n": 3}])

    def test_initial_unsafe_query_does_not_trigger_repair(self):
        item_id = self.dataset()
        for sql in ["DROP TABLE data", "SELECT * FROM other_user_table", "SELECT * FROM read_csv('secret.csv')"]:
            with self.subTest(sql=sql):
                model = model_sequence(plan(sql))
                response = self.ask(item_id, model)
                self.assertEqual(response.status_code, 400)
                self.assertEqual(len(model.requests), 1)

    def test_resource_and_io_errors_do_not_trigger_repair(self):
        item_id = self.dataset()
        for error in [duckdb.InterruptException("timeout"), duckdb.OutOfMemoryException("memory"), duckdb.IOException("disk")]:
            with self.subTest(error=type(error).__name__):
                model = model_sequence(plan("SELECT 1"))
                with patch.object(analysis, "_query", side_effect=error) as query:
                    response = self.ask(item_id, model)
                self.assertEqual(response.status_code, 502, response.text)
                self.assertEqual(len(model.requests), 1)
                self.assertEqual(query.call_count, 1)

    def test_dashboard_shared_budget_is_consumed_even_if_repair_has_bad_shape(self):
        item_id = self.dataset()
        dashboard_plan = {"title": "销售", "description": "地区统计", "kpis": [
            {"title": "销售额", "sql": 'SELECT SUM("错误金额") AS value FROM data'},
            {"title": "另一指标", "sql": 'SELECT SUM("仍错误") AS value FROM data'}],
            "charts": [{"title": "地区分布", "kind": "bar", "sql": 'SELECT "不存在" AS label, SUM("销售额") AS value FROM data GROUP BY 1'},
                       {"title": "可用图", "kind": "bar", "sql": 'SELECT "地区" AS label, SUM("销售额") AS value FROM data GROUP BY 1'}]}
        model = model_sequence(dashboard_plan,
                               {"sql": 'SELECT SUM("销售额") AS value FROM data', "reason": "金额字段修正"},
                               {"sql": 'SELECT "地区" AS label, SUM("销售额") AS value FROM data GROUP BY 1', "reason": "第二次修正返回多列"},
                               "华东销售额最高。")
        with patch("app.dashboard.OpenAI", model), patch.dict(os.environ, {"MODEL_PROVIDER": "openai", "OPENAI_API_KEY": "test"}):
            response = self.client.post(f"/api/datasets/{item_id}/dashboard", json={"focus": "销售"})
        self.assertEqual(response.status_code, 200, response.text)
        board = response.json()
        self.assertEqual(board["kpis"][1]["value"], 230)
        self.assertEqual(len(board["charts"]), 1)
        self.assertEqual(len(board["sql_repairs"]), 1)
        self.assertTrue(any("次数已用完" in warning for warning in board["warnings"]))
        self.assertEqual(len([r for r in model.requests if r.get("text", {}).get("format", {}).get("name") == "sql_repair"]), 2)
        saved = self.client.get(f"/api/datasets/{item_id}/dashboard").json()
        self.assertEqual(saved["sql_repairs"], board["sql_repairs"])

    def test_dashboard_chart_repair_uses_corrected_sql_and_preserves_old_on_failure(self):
        item_id = self.dataset()
        dashboard_plan = {"title": "销售", "description": "地区统计", "kpis": [],
                          "charts": [{"title": "地区销售", "kind": "bar", "sql": 'SELECT "错误地区" AS label, SUM("销售额") AS value FROM data GROUP BY 1'}]}
        corrected = 'SELECT "地区" AS label, SUM("销售额") AS value FROM data GROUP BY 1'
        model = model_sequence(dashboard_plan, {"sql": corrected, "reason": "修正地区字段"}, "华东销售额最高。")
        with patch("app.dashboard.OpenAI", model), patch.dict(os.environ, {"MODEL_PROVIDER": "openai", "OPENAI_API_KEY": "test"}):
            response = self.client.post(f"/api/datasets/{item_id}/dashboard", json={"focus": "销售"})
        self.assertEqual(response.status_code, 200, response.text)
        original = response.json()
        self.assertEqual(len(original["sql_repairs"]), 1)
        self.assertEqual(original["charts"][0]["sql"], original["sql_repairs"][0]["corrected_sql"])
        model = model_sequence(dashboard_plan, {"sql": 'SELECT "另一个错误地区" FROM data', "reason": "修正失败"})
        with patch("app.dashboard.OpenAI", model), patch.dict(os.environ, {"MODEL_PROVIDER": "openai", "OPENAI_API_KEY": "test"}):
            failure = self.client.post(f"/api/datasets/{item_id}/dashboard", json={"focus": "销售"})
        self.assertEqual(failure.status_code, 400, failure.text)
        saved = self.client.get(f"/api/datasets/{item_id}/dashboard").json()
        self.assertEqual(saved["revision"], original["revision"])
