"""Small, read-only evidence search to help the planner locate narrative text."""

import re
import threading

import duckdb

from . import store
from .quality import _identifier


def _terms(question):
    quoted = re.findall(r'["“「](.*?)["”」]', question)
    chunks = re.findall(r"[\u4e00-\u9fff]{2,}|[a-zA-Z0-9][a-zA-Z0-9_-]+", question)
    words = []
    for chunk in chunks:
        parts = re.split(r"请问|请|帮我|告诉我|这个|文件|里面|中的|有什么|是什么|是多少|如何|怎么|哪些|相关|总结|一下|说明|内容|要求", chunk)
        words.extend(p for p in parts if len(p) >= 2)
    candidates = quoted + words
    for word in words:
        if len(word) > 4 and re.fullmatch(r"[\u4e00-\u9fff]+", word):
            candidates.extend(word[i:i + 2] for i in range(len(word) - 1))
    return list(dict.fromkeys(s.casefold() for s in candidates if 2 <= len(s) <= 80))[:12]


def retrieve_text(item, question):
    terms = _terms(question)
    if not terms:
        return {"matches": [], "limited": False}
    eligible = []
    for table in item["tables"]:
        narrative = table.get("layout", {}).get("mode") in {"text", "supplement"}
        columns = [c["name"] for c in table["columns"] if c["type"].startswith(("VARCHAR", "CHAR", "TEXT")) and
                   (narrative or re.search(r"备注|说明|内容|原因|政策|描述|评价|意见|问题|答复|回复|description|comment|note|text", c["name"], re.I))][:6]
        if columns:
            eligible.append((table, columns))
    if not eligible:
        return {"matches": [], "limited": False}
    matches, budget, limited = [], 12_000, False
    connection = duckdb.connect(str(store.db_path(item["id"])), read_only=True)
    timer = threading.Timer(3, connection.interrupt)
    try:
        connection.execute("SET enable_external_access = false")
        connection.execute("SET memory_limit = '256MB'")
        timer.start()
        for table, columns in eligible[:12]:
            if not columns or budget <= 0:
                continue
            predicates, parameters = [], []
            for col in columns:
                for term in terms:
                    predicates.append(f"contains(lower({_identifier(col)}), ?)")
                    parameters.append(term)
            # All row fields are bounded before crossing from DuckDB into Python.
            # Put searched fields first, so neither the 16-column limit nor the
            # character budget can discard the evidence that matched the row.
            selected = list(dict.fromkeys(columns + [c["name"] for c in table["columns"]]))[:16]
            fields = [f"left(CAST({_identifier(name)} AS VARCHAR), 32768) AS {_identifier(name)}" for name in selected]
            result = connection.execute(f"SELECT {', '.join(fields)} FROM {_identifier(table['name'])} WHERE {' OR '.join(predicates)} LIMIT 4", parameters)
            names = [col[0] for col in result.description]
            for values in result.fetchall():
                row = {}
                for name, value in zip(names, values):
                    if value is None or budget <= 0:
                        continue
                    text = str(value)
                    if len(text) > 1800:
                        indices = [text.casefold().find(term) for term in terms]
                        start = max(0, next((i for i in indices if i >= 0), 0) - 250)
                        text = ("…" if start else "") + text[start:start + 1800] + "…"
                    text = text[:budget]
                    row[name] = text
                    budget -= len(text)
                matches.append({"table": table["name"], "sheet": table["label"], "row": row})
        limited = len(eligible) > 12 or budget <= 0
    except duckdb.Error:
        limited = True
    finally:
        timer.cancel()
        connection.close()
    return {"matches": matches, "limited": limited, "notice": "相关片段仅用于定位资料，不代表已检索或阅读全文；仍需查询实际表。"}
