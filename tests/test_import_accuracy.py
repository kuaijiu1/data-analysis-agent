import csv
import decimal
import io
import tempfile
import unittest
from pathlib import Path

import duckdb
from openpyxl import Workbook
import test_flow
import test_excel_text

from app import store
from app.ingest import _csv_encoding


class ImportAccuracyTests(unittest.TestCase):
    setUp = test_flow.FlowTests.setUp
    _upload = test_flow.FlowTests._upload
    _await_status = test_flow.FlowTests._await_status
    import_book = test_excel_text.ExcelTextTests.import_book

    def test_side_by_side_tables_join_by_key_not_physical_row(self):
        book = Workbook()
        sheet = book.active
        sheet.title = "资料"
        for position, value in {"B1": "销售资料", "J1": "地区映射", "B2": "地区", "C2": "月份", "D2": "销售金额", "J2": "地区", "K2": "大区"}.items():
            sheet[position] = value
        for row, values in enumerate([("甲", 1, 100), ("乙", 2, 20), ("甲", 3, 50)], 3):
            for col, value in enumerate(values, 2):
                sheet.cell(row, col, value)
        sheet["J3"], sheet["K3"] = "乙", "南区"
        sheet["J4"], sheet["K4"] = "甲", "北区"
        sheet["B10"], sheet["C10"] = "Q1：", "请统计各大区销售总额"
        book.create_sheet("空白")
        item = self.import_book(book)
        tables = item["tables"]
        sales = next(t for t in tables if "销售金额" in [c["name"] for c in t["columns"]])
        mapping = next(t for t in tables if "大区" in [c["name"] for c in t["columns"]])
        self.assertEqual(sales["rows"], 3)
        self.assertEqual(mapping["rows"], 2)
        self.assertEqual(sales["layout"]["source_range"], "B1:D5")
        self.assertEqual(mapping["layout"]["source_range"], "J1:K4")
        self.assertEqual([c["name"] for c in sales["columns"]], ["地区", "月份", "销售金额"])
        connection = duckdb.connect(str(store.db_path(item["id"])), read_only=True)
        try:
            rows = connection.execute(f'SELECT "大区",sum("销售金额") FROM "{sales["name"]}" JOIN "{mapping["name"]}" USING ("地区") GROUP BY 1 ORDER BY 1').fetchall()
        finally:
            connection.close()
        self.assertEqual(rows, [("北区", 150), ("南区", 20)])
        self.assertTrue(any(t["layout"]["mode"] == "text" and "Q1" in str(t["preview"]) for t in tables))

    def test_csv_dates_codes_null_literals_and_decimal_precision(self):
        buffer = io.StringIO(newline="")
        writer = csv.writer(buffer)
        writer.writerow(["编号", "日期", "更新时间", "备注", "金额", "高精度"])
        writer.writerow(["00123", "2019/12/10", "2019/12/12 11:54", "NULL", "0.1", "0.123456789012345678"])
        writer.writerow(["9007199254740993", "2019/12/11", "2019/12/13 09:05", "多行\n说明,带逗号", "0.2", "0.000000000000000001"])
        item_id = self._upload("accuracy.csv", buffer.getvalue().encode("utf-8"))
        item = self._await_status(item_id, "ready")
        first, second = item["tables"][0]["preview"]
        self.assertEqual(first["编号"], "00123")
        self.assertEqual(second["编号"], "9007199254740993")
        self.assertEqual(first["日期"], "2019/12/10")
        self.assertEqual(first["更新时间"], "2019/12/12 11:54")
        self.assertEqual(first["备注"], "NULL")
        self.assertEqual(second["备注"], "多行\n说明,带逗号")
        self.assertEqual(first["高精度"], "0.123456789012345678")
        connection = duckdb.connect(str(store.db_path(item_id)), read_only=True)
        try:
            self.assertEqual(connection.execute('SELECT sum("金额") FROM data').fetchone()[0], decimal.Decimal("0.3"))
        finally:
            connection.close()
        column = next(c for c in item["tables"][0]["columns"] if c["name"] == "日期")
        self.assertEqual(column["semantic_type"], "date_text")

    def test_late_text_does_not_get_cast_to_null_or_fail_import(self):
        item_id = self._upload("late.csv", ("数值\n" + "123\n" * 205 + "待核对\n").encode())
        item = self._await_status(item_id, "ready")
        self.assertEqual(item["tables"][0]["columns"][0]["type"], "VARCHAR")
        self.assertEqual(item["tables"][0]["rows"], 206)

    def test_utf8_sample_boundary_is_not_misclassified_as_gbk(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "boundary.csv"
            beginning = "标题\n".encode("utf-8")
            path.write_bytes(beginning + b"x" * (262144 - len(beginning) - 1) + "中文\n".encode("utf-8"))
            self.assertEqual(_csv_encoding(path, "auto"), "utf-8")
