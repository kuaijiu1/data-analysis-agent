"""Read literals first, then cast only columns verified to be safe numbers."""

import decimal
import re

from .quality import _identifier
from . import tasks

IDENTIFIER = re.compile(r"(?:^id$|id$|编号|编码|代码|电话|手机|邮编|账号|账户|证件|银行卡|主键|code$|phone|zip|uuid)", re.I)
NUMBER = re.compile(r"^[+-]?(?:\d+(?:\.\d*)?|\.\d+)$")
DATE_TEXT = re.compile(r"^\d{4}[-/]\d{1,2}[-/]\d{1,2}(?:[ T].*)?$")
NUMERIC_TYPE = re.compile(r"^(?:BIGINT|DECIMAL\(38,(?:[0-9]|1[0-8])\))$")


def date_expression(column):
    return f"coalesce(try_cast({column} AS TIMESTAMP), try_strptime({column}, ['%Y/%m/%d %H:%M', '%Y/%m/%d %H:%M:%S', '%Y/%m/%d', '%Y-%m-%d %H:%M', '%Y-%m-%d']))"


def numeric_text_type(connection, table, column, values):
    """Verify the entire column before offering a lossless numeric interpretation."""
    if IDENTIFIER.search(column) or not values or not all(isinstance(v, str) and NUMBER.fullmatch(v.strip()) for v in values):
        return None
    quoted, col = _identifier(table), _identifier(column)
    expr = f"trim({col})"
    valid, zeros, digits, scale, integers = connection.execute(
        f"SELECT bool_and(regexp_full_match({expr}, '[+-]?([0-9]+(\\.[0-9]*)?|\\.[0-9]+)')),"
        f"bool_or(regexp_matches({expr}, '^[+-]?0[0-9]')),"
        f"max(length(split_part(regexp_replace({expr}, '^[+-]', ''), '.', 1))),"
        f"max(CASE WHEN contains({expr}, '.') THEN length(split_part({expr}, '.', 2)) ELSE 0 END),"
        f"bool_and(NOT contains({expr}, '.')) FROM {quoted} WHERE {col} IS NOT NULL"
    ).fetchone()
    if not valid or zeros or digits + scale > 38 or scale > 18:
        return None
    if integers:
        largest = connection.execute(f"SELECT max(abs(try_cast({col} AS HUGEINT))) FROM {quoted}").fetchone()[0]
        if largest is None or largest > 9_007_199_254_740_991:
            return None
        return "BIGINT"
    return f"DECIMAL(38,{scale})"


def numeric_expression(column, metadata):
    """Only use a numeric type produced by the validated import pipeline."""
    target = metadata.get("numeric_type", "")
    if metadata.get("semantic_type") == "numeric_text" and NUMERIC_TYPE.fullmatch(target):
        return f"CAST(trim({column}) AS {target})"
    return column


def create_table(connection, name, path, item_id=None, text_columns=None):
    quoted = _identifier(name)
    connection.execute(f"CREATE TABLE {quoted} AS SELECT * FROM read_csv(?, header=true, all_varchar=true, nullstr='', strict_mode=true)", [str(path)])
    columns = [row[0] for row in connection.execute(f"DESCRIBE {quoted}").fetchall()]
    sample = connection.execute(f"SELECT * FROM {quoted} LIMIT 200").fetchall()
    forced = set(text_columns or [])
    for index, column in enumerate(columns):
        if item_id:
            tasks.check(item_id)
        if column in forced or IDENTIFIER.search(column):
            continue
        values = [row[index] for row in sample if row[index] is not None]
        target = numeric_text_type(connection, name, column, values)
        if target is None:
            continue
        col = _identifier(column)
        connection.execute(f"ALTER TABLE {quoted} ALTER COLUMN {col} TYPE {target} USING CAST({col} AS {target})")


def json_value(value):
    if isinstance(value, decimal.Decimal):
        number = float(value)
        if decimal.Decimal(str(number)) == value and abs(number) <= 9_007_199_254_740_991:
            return number
        return str(value)
    return value
