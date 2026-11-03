"""Regressions for the five accuracy/reliability issues found in the local audit."""

import io
import json
import os
import unittest
import zipfile
import xml.etree.ElementTree as ET
from unittest.mock import patch

import test_flow
import test_excel_text
from openpyxl import Workbook

from app import analysis
from app.board_edit import AggregateQuery, FilterRule, build_query
from app.context import model_context
from app.dashboard import save_dashboard
from app.text_search import retrieve_text


class AuditFixTests(unittest.TestCase):
    setUp = test_flow.FlowTests.setUp
    _await_status = test_flow.FlowTests._await_status
    import_book = test_excel_text.ExcelTextTests.import_book

    def _upload(self, name, content):
        # Tiny-chunk recovery is already covered by test_flow. These fixtures
        # exercise imported content, including wide rows, using normal chunks.
        response = self.client.post("/api/uploads", json={"name": name, "size": len(content), "encoding": "auto"})
        self.assertEqual(response.status_code, 200, response.text)
        item_id = response.json()["id"]
        for offset in range(0, len(content), 65536):
            response = self.client.put(f"/api/uploads/{item_id}/chunk?offset={offset}", content=content[offset:offset + 65536])
            self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(self.client.post(f"/api/uploads/{item_id}/complete").status_code, 200)
        return item_id

    def test_uncached_formula_has_position_warning_and_keeps_other_cells(self):
        book = Workbook()
        book.active.title = "经营"
        book.active.append(["地区", "销售额", "成本", "利润"])
        book.active.append(["华东", 100, 60, "=B2-C2"])
        item = self.import_book(book)
        self.assertEqual(item["tables"][0]["preview"][0]["销售额"], 100)
        self.assertIsNone(item["tables"][0]["preview"][0]["利润"])
        warnings = " ".join(item["import_warnings"])
        self.assertIn("经营", warnings)
        self.assertIn("D2", warnings)
        self.assertIn("公式", warnings)
        self.assertIn("重新计算", warnings)
        self.assertIn("D2", " ".join(model_context(item)["import_warnings"]))

    def test_zero_formula_cache_is_not_missing_but_empty_shared_formula_is(self):
        book = Workbook()
        book.active.append(["名称", "结果"])
        book.active.append(["甲", "=1-1"])
        book.active.append(["乙", "=1-1"])
        output = io.BytesIO()
        book.save(output)
        updated = io.BytesIO()
        namespace = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
        with zipfile.ZipFile(output) as source, zipfile.ZipFile(updated, "w") as target:
            for part in source.infolist():
                content = source.read(part.filename)
                if part.filename == "xl/worksheets/sheet1.xml":
                    root = ET.fromstring(content)
                    cells = {cell.get("r"): cell for cell in root.iter(f"{{{namespace}}}c")}
                    cells["B2"].find(f"{{{namespace}}}v").text = "0"
                    formula = cells["B3"].find(f"{{{namespace}}}f")
                    formula.text = None
                    formula.set("t", "shared")
                    formula.set("si", "0")
                    content = ET.tostring(root)
                target.writestr(part, content)
        item_id = self._upload("cached.xlsx", updated.getvalue())
        item = self._await_status(item_id, "awaiting_sheets")
        self.client.post(f"/api/datasets/{item_id}/import", json={"sheets": item["sheets"]})
        item = self._await_status(item_id, "ready")
        self.assertEqual(item["tables"][0]["preview"][0]["结果"], 0)
        warnings = " ".join(item["import_warnings"])
        self.assertIn("B3", warnings)
        self.assertNotIn("B2", warnings)
        self.assertIn("1 个公式", warnings)

    def test_text_amounts_keep_literals_and_dashboard_sums_numerically(self):
        book = Workbook()
        book.active.append(["地区", "金额", "编号"])
        book.active.append(["华东", "100.50", "00123"])
        book.active.append(["华北", "20.00", "00124"])
        item = self.import_book(book)
        table = item["tables"][0]
        amount = next(c for c in table["columns"] if c["name"] == "金额")
        self.assertEqual(table["preview"][0]["金额"], "100.50")
        self.assertEqual(table["preview"][1]["金额"], "20.00")
        self.assertEqual(amount["semantic_type"], "numeric_text")
        self.assertNotIn("semantic_type", next(c for c in table["columns"] if c["name"] == "编号"))
        metadata = next(c for c in model_context(item)["tables"][0]["columns"] if c["name"] == "金额")
        self.assertIn("CAST", metadata["numeric_expression"])
        for aggregate, expected in [("sum", 120.5), ("min", 20), ("max", 100.5), ("avg", 60.25)]:
            sql, parameters = build_query(item, AggregateQuery(table=table["name"], column="金额", aggregate=aggregate))
            rows, _ = analysis._query(item["id"], sql, parameters)
            self.assertEqual(rows[0]["value"], expected)
        sql, parameters = build_query(item, AggregateQuery(table=table["name"], column="金额", filters=[FilterRule(column="金额", op="gte", value="30")]))
        self.assertEqual(analysis._query(item["id"], sql, parameters)[0][0]["value"], 100.5)
        board = save_dashboard(item["id"], {"title": "初始", "kpis": [], "charts": []})
        query = {"table": table["name"], "column": "金额", "aggregate": "sum"}
        payload = {"revision": board["revision"], "title": "金额统计", "kpis": [{"title": "总金额", "query": query}],
                   "charts": [{"title": "地区金额", "query": {**query, "dimension": "地区"}}]}
        response = self.client.put(f"/api/datasets/{item['id']}/dashboard", json=payload)
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["kpis"][0]["value"], 120.5)
        self.assertEqual(len(response.json()["charts"][0]["rows"]), 2)

    def test_text_number_validation_checks_late_values_and_protects_codes(self):
        book = Workbook()
        sheet = book.active
        sheet.append(["金额", "编码", "另一代码"])
        for index in range(205):
            sheet.append(["100.50", "00123", "9007199254740993"])
        sheet.append(["待核对", "00123", "9007199254740993"])
        item = self.import_book(book)
        self.assertTrue(all("numeric_type" not in c for c in item["tables"][0]["columns"]))
        with self.assertRaises(ValueError):
            build_query(item, AggregateQuery(table=item["tables"][0]["name"], column="金额"))

    def test_wide_table_evidence_contains_matching_column_before_budget(self):
        headers = [f"字段{i}" for i in range(1, 20)] + ["售后说明"]
        values = ["无关文字" * 600] * 19 + ["退款期限为七个工作日"]
        item_id = self._upload("wide.csv", (",".join(headers) + "\n" + ",".join(values) + "\n").encode())
        item = self._await_status(item_id, "ready")
        evidence = retrieve_text(item, "退款期限")
        row = evidence["matches"][0]["row"]
        self.assertIn("退款期限为七个工作日", row["售后说明"])
        self.assertEqual(next(iter(row)), "售后说明")
        self.assertLessEqual(len(row), 16)

    def test_nonfinite_results_return_json_warning_and_readable_history(self):
        item_id = self._upload("ratio.csv", b"name,value\nA,0\n")
        self._await_status(item_id, "ready")

        class RatioModel(test_flow.FakeOpenAI):
            def create(self, **kwargs):
                if "text" in kwargs:
                    return test_flow.FakeResponse(json.dumps({"sql": "SELECT 0.0/0.0 AS ratio, 1.0/0.0 AS overflow, [0.0/0.0] AS nested", "chart": "none", "reason": "计算比率"}))
                self_test.assertIn("NaN", kwargs["input"])
                return test_flow.FakeResponse("无法计算该比率。")

        self_test = self
        with patch.object(analysis, "OpenAI", RatioModel), patch.dict(os.environ, {"MODEL_PROVIDER": "openai", "OPENAI_API_KEY": "test"}):
            response = self.client.post(f"/api/datasets/{item_id}/ask", json={"question": "计算比率"})
        self.assertEqual(response.status_code, 200, response.text)
        result = response.json()
        self.assertEqual(result["rows"], [{"ratio": None, "overflow": None, "nested": [None]}])
        self.assertEqual(len(result["warnings"]), 1)
        self.assertIn("不能当作 0", result["answer"])
        saved = self.client.get(f"/api/datasets/{item_id}/history/{result['history_id']}")
        self.assertEqual(saved.status_code, 200, saved.text)
        self.assertEqual(saved.json()["rows"], result["rows"])
        warnings = []
        analysis._query(item_id, "SELECT NULL AS missing, 0 AS zero", warnings=warnings)
        self.assertEqual(warnings, [])

    def test_late_column_remains_on_original_row_with_missing_header_warning(self):
        book = Workbook()
        sheet = book.active
        sheet.title = "销售"
        sheet.append(["地区", "金额"])
        for number in range(2, 46):
            sheet.append([f"地区{number}", number])
        sheet["C45"] = 900
        item = self.import_book(book)
        table = item["tables"][0]
        self.assertEqual([c["name"] for c in table["columns"]], ["地区", "金额", "列3"])
        rows, _ = analysis._query(item["id"], f'SELECT "地区", "金额", "列3" FROM "{table["name"]}" WHERE "列3" IS NOT NULL')
        self.assertEqual(rows, [{"地区": "地区45", "金额": 45, "列3": 900}])
        self.assertEqual(len(item["tables"]), 1)
        self.assertIn("缺少列名", " ".join(item["import_warnings"]))
        self.assertEqual(item["content_version"], 4)
