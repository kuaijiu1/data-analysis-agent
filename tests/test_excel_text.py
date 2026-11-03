import io
import json
import os
import tempfile
import unittest
import zipfile
import xml.etree.ElementTree as ET
from unittest.mock import patch

import test_flow
from openpyxl import Workbook
from openpyxl.drawing.image import Image as ExcelImage
from PIL import Image

from app import analysis, store
from app.context import model_context
from app.text_search import retrieve_text
from app.excel_content import drawing_text


class ExcelTextTests(unittest.TestCase):
    setUp = test_flow.FlowTests.setUp
    _upload = test_flow.FlowTests._upload
    _await_status = test_flow.FlowTests._await_status

    def import_book(self, book, sheets=None):
        output = io.BytesIO()
        book.save(output)
        item_id = self._upload("文字资料.xlsx", output.getvalue())
        item = self._await_status(item_id, "awaiting_sheets")
        response = self.client.post(f"/api/datasets/{item_id}/import", json={"sheets": sheets or item["sheets"]})
        self.assertEqual(response.status_code, 200, response.text)
        return self._await_status(item_id, "ready")

    def test_narrative_keeps_first_sentence_and_source_positions(self):
        book = Workbook()
        sheet = book.active
        sheet.title = "售后政策"
        sheet["A1"] = "收到商品后7天内可申请退货。"
        sheet["A4"] = "订单编号00123，退款将在3个工作日内到账。"
        item = self.import_book(book)
        table = item["tables"][0]
        self.assertEqual(table["layout"]["mode"], "text")
        self.assertEqual(table["rows"], 2)
        self.assertIn("7天", table["preview"][0]["文字内容"])
        self.assertIn("售后政策!A4", table["preview"][1]["原始位置"])
        self.assertIn("00123", table["preview"][1]["文字内容"])
        self.assertIn("7天", json.dumps(model_context(item), ensure_ascii=False))

    def test_title_multilevel_headers_and_vertical_merged_categories(self):
        book = Workbook()
        sheet = book.active
        sheet.title = "经营数据"
        sheet.append(["十月经营情况", None, None])
        sheet.merge_cells("A1:C1")
        sheet.append(["地区", "销售", None])
        sheet.merge_cells("A2:A3")
        sheet.merge_cells("B2:C2")
        sheet.append([None, "金额", "数量"])
        sheet.append(["华东", 120, 3])
        sheet.append([None, 80, 2])
        sheet.merge_cells("A4:A5")
        item = self.import_book(book)
        table = item["tables"][0]
        self.assertEqual([c["name"] for c in table["columns"]], ["地区", "销售_金额", "销售_数量"])
        self.assertEqual(table["layout"]["header_rows"], [2, 3])
        self.assertEqual(table["preview"][1]["地区"], "华东")
        self.assertIn("十月经营情况", item["tables"][1]["preview"][0]["文字内容"])

    def test_text_cells_in_regular_table_reach_planner_and_query_summary(self):
        book = Workbook()
        sheet = book.active
        sheet.append(["商品", "售后说明"])
        sheet.append(["毛衣", "未拆标签可退货，期限为7天"])
        item = self.import_book(book)

        class TextModel(test_flow.FakeOpenAI):
            requests = []

            def create(self, **kwargs):
                self.requests.append(kwargs)
                if "text" in kwargs:
                    return test_flow.FakeResponse(json.dumps({"sql": 'SELECT "商品", "售后说明" FROM "sheet_1" WHERE contains("售后说明", \'退货\')', "chart": "none", "reason": "读取实际说明"}))
                self_test.assertIn("未拆标签可退货", kwargs["input"])
                return test_flow.FakeResponse("毛衣未拆标签可在7天内退货。")

        self_test = self
        with patch.object(analysis, "OpenAI", TextModel), patch.dict(os.environ, {"MODEL_PROVIDER": "openai", "OPENAI_API_KEY": "test"}):
            response = self.client.post(f"/api/datasets/{item['id']}/ask", json={"question": "毛衣的退货期限是多少？"})
        self.assertEqual(response.status_code, 200, response.text)
        self.assertIn("未拆标签", TextModel.requests[0]["input"])
        self.assertEqual(response.json()["chart"], "none")

    def test_numeric_text_code_keeps_leading_zeros(self):
        book = Workbook()
        book.active.append(["商品编号", "名称", "金额"])
        book.active.append(["00123", "毛衣", 50])
        item = self.import_book(book)
        row = item["tables"][0]["preview"][0]
        self.assertEqual(row["商品编号"], "00123")
        self.assertEqual(row["金额"], 50)

    def test_long_text_summary_is_bounded_but_keeps_relevant_passage(self):
        original = [{"原始位置": "政策!A1", "文字内容": "普通说明。" * 5000 + "退货期限为7天。"}]
        rows, limited = analysis._model_rows(original, "退货期限是多少？", budget=8000)
        self.assertTrue(limited)
        self.assertIn("退货期限为7天", rows[0]["文字内容"])
        self.assertLess(len(json.dumps(rows, ensure_ascii=False)), 8100)
        self.assertGreater(len(original[0]["文字内容"]), 20000)

    def test_retrieval_finds_text_after_preview_and_does_not_execute_question(self):
        book = Workbook()
        sheet = book.active
        for i in range(80):
            sheet.append([f"普通记录{i}"])
        sheet.append(["退款处理：款项将在三天内到账。"])
        item = self.import_book(book)
        evidence = retrieve_text(item, "退款处理是什么？'; DROP TABLE sheet_1; --")
        self.assertIn("款项将在三天内到账", json.dumps(evidence, ensure_ascii=False))
        rows, _ = analysis._query(item["id"], 'SELECT COUNT(*) AS n FROM "sheet_1"')
        self.assertEqual(rows[0]["n"], 81)

    def test_embedded_image_ocr_and_shape_text_have_locations(self):
        book = Workbook()
        sheet = book.active
        sheet.title = "图片说明"
        sheet["A1"] = "售后处理说明"
        image = io.BytesIO()
        Image.new("RGB", (200, 80), "white").save(image, format="PNG")
        image.seek(0)
        sheet.add_image(ExcelImage(image), "D4")
        with patch("app.excel_content._ocr_image", return_value=[{"text": "退货期限为7天", "confidence": .95}]):
            item = self.import_book(book)
        texts = item["tables"][1]["preview"]
        self.assertEqual(texts[0]["来源"], "图片OCR")
        self.assertIn("图片说明!D4", texts[0]["原始位置"])
        self.assertIn("7天", texts[0]["文字内容"])
        self.assertEqual(float(texts[0]["识别置信度"]), .95)

    def test_ocr_failure_is_visible_and_does_not_discard_cells(self):
        book = Workbook()
        sheet = book.active
        sheet["A1"] = "需要保留的正文"
        image = io.BytesIO()
        Image.new("RGB", (100, 80), "white").save(image, format="PNG")
        image.seek(0)
        sheet.add_image(ExcelImage(image), "B4")
        with patch("app.excel_content._ocr_image", side_effect=RuntimeError("OCR不可用")):
            item = self.import_book(book)
        self.assertIn("需要保留的正文", item["tables"][0]["preview"][0]["文字内容"])
        self.assertIn("OCR不可用", " ".join(item["import_warnings"]))

    def test_image_only_sheet_imports_without_cell_text(self):
        book = Workbook()
        image = io.BytesIO()
        Image.new("RGB", (100, 80), "white").save(image, format="PNG")
        image.seek(0)
        book.active.add_image(ExcelImage(image), "A1")
        with patch("app.excel_content._ocr_image", return_value=[{"text": "图片中的政策", "confidence": .99}]):
            item = self.import_book(book)
        self.assertEqual(item["tables"][0]["rows"], 0)
        self.assertEqual(item["tables"][1]["preview"][0]["文字内容"], "图片中的政策")

    def test_drawing_text_box_is_extracted_without_ocr(self):
        from types import SimpleNamespace
        from pathlib import Path
        data = io.BytesIO()
        with zipfile.ZipFile(data, "w") as archive:
            archive.writestr("xl/drawings/drawing1.xml", '<xdr:wsDr xmlns:xdr="http://schemas.openxmlformats.org/drawingml/2006/spreadsheetDrawing" xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main"><xdr:oneCellAnchor><xdr:from><xdr:col>1</xdr:col><xdr:row>2</xdr:row></xdr:from><xdr:sp><xdr:txBody><a:p><a:r><a:t>售后审批说明</a:t></a:r></a:p></xdr:txBody></xdr:sp></xdr:oneCellAnchor></xdr:wsDr>')
        with tempfile.TemporaryDirectory() as folder, zipfile.ZipFile(data) as archive:
            target = Path(folder) / "text.csv"
            count, images, notes = drawing_text(archive, ["xl/drawings/drawing1.xml"], SimpleNamespace(title="说明"), target, Path(folder), "shape-test")
            self.assertEqual((count, images, notes), (1, 0, []))
            self.assertIn("售后审批说明", target.read_text(encoding="utf-8"))
            self.assertIn("说明!B3", target.read_text(encoding="utf-8"))

    def test_reprocess_creates_owned_copy_and_preserves_original(self):
        book = Workbook()
        book.active["A1"] = "原文件第一句"
        original = self.import_book(book)
        response = self.client.post(f"/api/datasets/{original['id']}/reprocess")
        self.assertEqual(response.status_code, 200, response.text)
        copy = self._await_status(response.json()["id"], "ready")
        self.assertNotEqual(original["id"], copy["id"])
        self.assertEqual(store.db_path(original["id"]).exists(), True)
        self.assertEqual(copy["tables"][0]["preview"], original["tables"][0]["preview"])
        outsider = test_flow.TestClient(test_flow.app)
        outsider.post("/api/auth/register", json={"email": "text-outsider@example.com", "password": "test-password-123"})
        self.assertEqual(outsider.post(f"/api/datasets/{original['id']}/reprocess").status_code, 404)


if __name__ == "__main__":
    unittest.main()
