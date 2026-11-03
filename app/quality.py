"""Bounded data profiling performed by DuckDB, without an LLM call."""

import datetime as dt
import decimal
import math
import re
import threading

import duckdb

from . import store

SAMPLE_LIMIT = 10_000
PROFILE_VERSION = 1
RANGE_TYPES = re.compile(r"^(U?TINYINT|U?SMALLINT|U?INTEGER|U?BIGINT|U?HUGEINT|FLOAT|DOUBLE|DECIMAL|DATE|TIME|TIMESTAMP)")


def _identifier(value: str) -> str:
    return '"' + value.replace('"', '""') + '"'


def _json_value(value):
    if isinstance(value, (dt.datetime, dt.date, dt.time, decimal.Decimal)):
        return str(value)
    if isinstance(value, float) and not math.isfinite(value):
        return str(value)
    return value


def profile_table(connection, table: dict) -> dict:
    name = _identifier(table["name"])
    columns = table["columns"]
    stats = []
    # One full scan per batch, rather than one scan per field.
    for offset in range(0, len(columns), 32):
        batch = columns[offset:offset + 32]
        expressions = []
        for column in batch:
            col = _identifier(column["name"])
            text = column["type"].startswith(("VARCHAR", "CHAR", "TEXT"))
            blank = f"trim(CAST({col} AS VARCHAR), ' ' || chr(9) || chr(10) || chr(13)) = ''" if text else "false"
            expressions.extend([f"COUNT(*) - COUNT({col})", f"COUNT(*) FILTER (WHERE {blank})"])
            if RANGE_TYPES.match(column["type"]):
                expressions.extend([f"MIN({col})", f"MAX({col})"])
            else:
                expressions.extend(["NULL", "NULL"])
        values = connection.execute(f"SELECT {', '.join(expressions)} FROM {name}").fetchone()
        for index, column in enumerate(batch):
            nulls, blanks, minimum, maximum = values[index * 4:index * 4 + 4]
            missing = nulls + blanks
            stats.append({
                "name": column["name"], "type": column["type"],
                "null_count": nulls, "blank_count": blanks, "missing_count": missing,
                "missing_percent": round(missing / table["rows"] * 100, 2) if table["rows"] else 0,
                "min": _json_value(minimum), "max": _json_value(maximum),
            })
    for column in stats:
        col = _identifier(column["name"])
        sample = f"(SELECT {col} AS value FROM {name} LIMIT {SAMPLE_LIMIT})"
        valid = "value IS NOT NULL AND trim(CAST(value AS VARCHAR), ' ' || chr(9) || chr(10) || chr(13)) <> ''"
        column["sample_distinct"] = connection.execute(
            f"SELECT COUNT(DISTINCT value) FROM {sample} WHERE {valid}"
        ).fetchone()[0]
        common = connection.execute(
            f"SELECT left(CAST(value AS VARCHAR), 160), COUNT(*) AS n, length(CAST(value AS VARCHAR)) > 160 "
            f"FROM {sample} WHERE {valid} GROUP BY value ORDER BY n DESC, CAST(value AS VARCHAR) LIMIT 5"
        ).fetchall()
        column["top_values"] = [{"value": row[0], "count": row[1], "truncated": row[2]} for row in common]
    return {
        "version": PROFILE_VERSION, "row_count": table["rows"],
        "sample_rows": min(table["rows"], SAMPLE_LIMIT), "sample_method": "first_rows",
        "columns": stats,
    }


def profile_tables(connection, tables: list[dict], progress=None) -> list[dict]:
    timer = threading.Timer(120, connection.interrupt)
    timer.start()
    try:
        result = []
        for table in tables:
            if progress:
                progress(f"正在统计数据质量：{table['label']}")
            result.append({**table, "profile": profile_table(connection, table)})
        return result
    finally:
        timer.cancel()


def refresh_profiles(item_id: str) -> None:
    connection = None
    try:
        item = store.get(item_id)
        store.update(item_id, profile_status="running", profile_error=None)
        connection = duckdb.connect(str(store.db_path(item_id)), read_only=True)
        connection.execute("SET enable_external_access = false")
        connection.execute("SET memory_limit = '2GB'")
        tables = profile_tables(connection, item["tables"])
        store.update(item_id, tables=tables, profile_status="ready", profile_error=None,
                     profile_updated_at=dt.datetime.now(dt.timezone.utc).isoformat())
    except Exception as exc:
        store.update(item_id, profile_status="error", profile_error=str(exc))
    finally:
        if connection is not None:
            connection.close()
