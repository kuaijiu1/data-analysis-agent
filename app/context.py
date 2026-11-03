"""User-authored business definitions shared by question and dashboard planning."""

from .quality import _identifier
from .tabular import numeric_expression


def clean_dictionary(item: dict, payload: dict) -> dict:
    allowed = {table["name"]: {column["name"] for column in table["columns"]} for table in item["tables"]}
    fields, metrics = [], []
    seen = set()
    for field in payload["fields"]:
        key = (field["table"], field["column"])
        if key[0] not in allowed or key[1] not in allowed[key[0]]:
            raise ValueError("字段说明包含不存在的表或字段")
        if key in seen:
            raise ValueError("同一字段不能重复填写说明")
        seen.add(key)
        description = field["description"].strip()
        if description:
            fields.append({"table": key[0], "column": key[1], "description": description})
    seen = set()
    for metric in payload["metrics"]:
        name, definition = metric["name"].strip(), metric["definition"].strip()
        if not name or not definition:
            raise ValueError("每个指标都需要名称和计算口径")
        if name.casefold() in seen:
            raise ValueError("指标名称不能重复")
        seen.add(name.casefold())
        metrics.append({"name": name, "definition": definition})
    return {"description": payload["description"].strip(), "fields": fields, "metrics": metrics}


def model_context(item: dict) -> dict:
    dictionary = item.get("dictionary") or {}
    descriptions = {(field["table"], field["column"]): field["description"] for field in dictionary.get("fields", [])}
    tables = []
    sample_budget = 16_000
    for table in item["tables"]:
        profiles = {column["name"]: column for column in table.get("profile", {}).get("columns", [])}
        columns = []
        for column in table["columns"]:
            entry = {"name": column["name"], "type": column["type"]}
            if column.get("semantic_type"):
                entry["semantic_type"] = column["semantic_type"]
            if column.get("semantic_type") == "numeric_text":
                entry["numeric_type"] = column["numeric_type"]
                entry["numeric_expression"] = numeric_expression(_identifier(column["name"]), column)
            description = descriptions.get((table["name"], column["name"]))
            if description:
                entry["description"] = description
            stats = profiles.get(column["name"])
            if stats:
                entry["missing_percent"] = stats["missing_percent"]
                if stats.get("min") is not None or stats.get("max") is not None:
                    entry["range"] = {"min": stats["min"], "max": stats["max"]}
            columns.append(entry)
        samples = []
        for row in table.get("preview", [])[:3]:
            sample = {}
            for name, value in list(row.items())[:16]:
                if sample_budget <= 0:
                    break
                if isinstance(value, str):
                    value = value[:min(1200, sample_budget)]
                    sample_budget -= len(value)
                sample[name] = value
            if sample:
                samples.append(sample)
        tables.append({"table": table["name"], "sheet": table["label"], "rows": table["rows"], "columns": columns,
                       "sample_rows": samples, "layout": table.get("layout", {})})
    return {"tables": tables, "business_description": dictionary.get("description", ""),
            "metrics": dictionary.get("metrics", []), "import_warnings": item.get("import_warnings", [])}
