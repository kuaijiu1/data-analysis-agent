"""Build editable charts from a small validated query form, with bound filter values."""

import re
from typing import Literal

from pydantic import BaseModel, Field

from . import store
from .analysis import _query, _validate_sql
from .dashboard import DashboardConflict, _number, load_dashboard, save_dashboard
from .tabular import date_expression, numeric_expression


class FilterRule(BaseModel):
    column: str = Field(min_length=1, max_length=512)
    op: Literal["eq", "ne", "gte", "lte", "contains"] = "eq"
    value: str = Field(max_length=500)


class AggregateQuery(BaseModel):
    table: str = Field(min_length=1, max_length=256)
    column: str = Field(default="", max_length=512)
    aggregate: Literal["sum", "avg", "count", "count_distinct", "min", "max"] = "sum"
    filters: list[FilterRule] = Field(default_factory=list, max_length=5)


class ChartQuery(AggregateQuery):
    dimension: str = Field(min_length=1, max_length=512)
    grain: Literal["", "day", "month", "year"] = ""
    limit: int = Field(default=20, ge=1, le=50)
    sort: Literal["value_desc", "label_asc"] = "value_desc"


class KPIEdit(BaseModel):
    source_index: int | None = Field(default=None, ge=0)
    title: str = Field(min_length=1, max_length=60)
    query: AggregateQuery | None = None


class ChartEdit(BaseModel):
    source_index: int | None = Field(default=None, ge=0)
    title: str = Field(min_length=1, max_length=60)
    kind: Literal["bar", "line", "area", "pie"] = "bar"
    query: ChartQuery | None = None


class DashboardEdit(BaseModel):
    revision: str = Field(min_length=1, max_length=80)
    title: str = Field(min_length=1, max_length=80)
    description: str = Field(default="", max_length=300)
    kpis: list[KPIEdit] = Field(default_factory=list, max_length=12)
    charts: list[ChartEdit] = Field(min_length=1, max_length=12)


def _quoted(value):
    return '"' + value.replace('"', '""') + '"'


def build_query(item: dict, query: AggregateQuery) -> tuple[str, list]:
    table = next((table for table in item["tables"] if table["name"] == query.table), None)
    if table is None:
        raise ValueError("选择的数据表不存在")
    columns = {column["name"]: column["type"] for column in table["columns"]}
    metadata = {column["name"]: column for column in table["columns"]}
    if query.aggregate == "count":
        value = "COUNT(*)"
    else:
        if query.column not in columns:
            raise ValueError("选择的统计字段不存在")
        field = _quoted(query.column)
        numeric = numeric_expression(field, metadata[query.column])
        if query.aggregate in {"sum", "avg", "min", "max"} and numeric == field and not re.match(r"^(U?(TINYINT|SMALLINT|INTEGER|BIGINT|HUGEINT)|FLOAT|DOUBLE|DECIMAL)", columns[query.column]):
            raise ValueError("求和、平均、最小和最大值需要选择数值字段")
        value = f"COUNT(DISTINCT {field})" if query.aggregate == "count_distinct" else f"{query.aggregate.upper()}({numeric})"
    clauses, parameters = [], []
    for rule in query.filters:
        if rule.column not in columns:
            raise ValueError("筛选字段不存在")
        col = _quoted(rule.column)
        if rule.op == "contains":
            clauses.append(f"contains(CAST({col} AS VARCHAR), ?)")
        else:
            col = numeric_expression(col, metadata[rule.column])
            operator = {"eq": "=", "ne": "<>", "gte": ">=", "lte": "<="}[rule.op]
            clauses.append(f"{col} {operator} ?")
        parameters.append(rule.value)
    where = " WHERE " + " AND ".join(clauses) if clauses else ""
    if isinstance(query, ChartQuery):
        if query.dimension not in columns:
            raise ValueError("分组字段不存在")
        dimension = _quoted(query.dimension)
        if query.grain:
            if not columns[query.dimension].startswith(("DATE", "TIMESTAMP")):
                dimension_metadata = metadata[query.dimension]
                if dimension_metadata.get("semantic_type") != "date_text":
                    raise ValueError("按日/月/年分组需要选择日期字段")
                dimension = date_expression(dimension)
            dimension = f"date_trunc('{query.grain}', {dimension})"
        order = "value DESC, label" if query.sort == "value_desc" else "label ASC"
        sql = f"SELECT {dimension} AS label, {value} AS value FROM {_quoted(query.table)}{where} GROUP BY 1 ORDER BY {order} LIMIT {query.limit}"
    else:
        sql = f"SELECT {value} AS value FROM {_quoted(query.table)}{where}"
    return _validate_sql(sql, {table["name"] for table in item["tables"]}), parameters


def edit_dashboard(item_id: str, payload: DashboardEdit) -> dict:
    item = store.get(item_id)
    if item["status"] != "ready":
        raise ValueError("数据集尚未准备好")
    if not payload.title.strip() or any(not edit.title.strip() for edit in [*payload.kpis, *payload.charts]):
        raise ValueError("看板、指标和图表的标题不能为空")
    current = load_dashboard(item_id)
    if current is None:
        raise ValueError("请先生成一份看板")
    if current["revision"] != payload.revision:
        raise DashboardConflict("看板已被更新，请重新打开编辑器后再保存")
    kpis, charts = [], []
    for edit in payload.kpis:
        if edit.query is not None:
            sql, parameters = build_query(item, edit.query)
            rows, _ = _query(item_id, sql, parameters)
            value = rows[0]["value"] if rows else None
            if _number(value) is None:
                raise ValueError(f"指标“{edit.title}”没有有效数值")
            kpi = {"value": value, "sql": sql, "parameters": parameters, "query": edit.query.model_dump()}
        elif edit.source_index is not None and edit.source_index < len(current["kpis"]):
            kpi = dict(current["kpis"][edit.source_index])
        else:
            raise ValueError("新指标需要选择统计字段")
        kpis.append({**kpi, "title": edit.title.strip()})
    for edit in payload.charts:
        if edit.query is not None:
            sql, parameters = build_query(item, edit.query)
            rows, truncated = _query(item_id, sql, parameters)
            points = [{"label": str(row["label"]), "value": _number(row["value"])} for row in rows[:50]]
            points = [point for point in points if point["value"] is not None]
            if not points:
                raise ValueError(f"图表“{edit.title}”没有符合筛选条件的数据")
            chart = {"sql": sql, "parameters": parameters, "query": edit.query.model_dump(), "rows": points, "truncated": truncated}
        elif edit.source_index is not None and edit.source_index < len(current["charts"]):
            chart = dict(current["charts"][edit.source_index])
        else:
            raise ValueError("新图表需要选择分组和统计字段")
        if edit.kind == "pie" and (any(point["value"] < 0 for point in chart["rows"]) or sum(point["value"] for point in chart["rows"]) <= 0):
            raise ValueError("饼图需要非负数值且总和大于零，请换成柱状图或折线图")
        charts.append({**chart, "title": edit.title.strip(), "kind": edit.kind})
    return save_dashboard(item_id, {**current, "title": payload.title.strip(), "description": payload.description.strip(),
                                   "kpis": kpis, "charts": charts, "insight": "", "warnings": [], "edited": True}, payload.revision)
