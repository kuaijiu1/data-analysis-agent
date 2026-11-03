import datetime as dt
import unittest

import duckdb

from app.quality import profile_table


def table_info(connection, name="data"):
    return {"name": name, "rows": connection.execute(f'SELECT COUNT(*) FROM "{name}"').fetchone()[0],
            "columns": [{"name": row[0], "type": row[1]} for row in connection.execute(f'DESCRIBE "{name}"').fetchall()]}


class QualityTests(unittest.TestCase):
    def test_missing_whitespace_numeric_dates_and_quoted_identifiers(self):
        with duckdb.connect() as connection:
            connection.execute('CREATE TABLE data ("x""y" VARCHAR, amount DOUBLE, day DATE)')
            connection.executemany("INSERT INTO data VALUES (?, ?, ?)", [
                (None, None, None), (" ", 5, dt.date(2026, 1, 1)),
                ("A", 0, dt.date(2026, 1, 2)), ("A", 10, None), ("\t", None, dt.date(2026, 1, 3)),
            ])
            result = profile_table(connection, table_info(connection))
        text, amount, day = result["columns"]
        self.assertEqual((text["null_count"], text["blank_count"], text["missing_percent"]), (1, 2, 60))
        self.assertEqual(text["top_values"], [{"value": "A", "count": 2, "truncated": False}])
        self.assertEqual((amount["min"], amount["max"], amount["missing_count"]), (0, 10, 2))
        self.assertEqual((day["min"], day["max"]), ("2026-01-01", "2026-01-03"))

    def test_full_counts_and_ranges_but_bounded_common_values(self):
        with duckdb.connect() as connection:
            connection.execute("CREATE TABLE data AS SELECT CASE WHEN i=10001 THEN NULL ELSE i END AS n, CASE WHEN i<10000 THEN 'sample' ELSE 'outside' END AS category FROM range(10002) t(i)")
            result = profile_table(connection, table_info(connection))
        self.assertEqual((result["row_count"], result["sample_rows"]), (10002, 10000))
        self.assertEqual(result["columns"][0]["null_count"], 1)
        self.assertEqual(result["columns"][0]["max"], 10000)
        self.assertEqual(result["columns"][1]["top_values"], [{"value": "sample", "count": 10000, "truncated": False}])

    def test_empty_table_and_long_values_remain_distinct(self):
        with duckdb.connect() as connection:
            connection.execute("CREATE TABLE data (text VARCHAR)")
            empty = profile_table(connection, table_info(connection))
            self.assertEqual(empty["columns"][0]["missing_percent"], 0)
            self.assertEqual(empty["columns"][0]["top_values"], [])
            connection.executemany("INSERT INTO data VALUES (?)", [("x" * 170 + "a",), ("x" * 170 + "b",)])
            result = profile_table(connection, table_info(connection))
        self.assertEqual(result["columns"][0]["sample_distinct"], 2)
        self.assertTrue(all(value["truncated"] and value["count"] == 1 for value in result["columns"][0]["top_values"]))
