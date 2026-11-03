"""Persistent analysis records, scoped to a dataset and its owner at the API boundary."""

import datetime as dt
import json
import sqlite3
import uuid
from contextlib import contextmanager

from . import store


def _path(item_id):
    if len(item_id) != 32 or any(char not in "0123456789abcdef" for char in item_id):
        raise ValueError("无效数据集编号")
    return store.DATA_DIR / "history" / f"{item_id}.sqlite"


@contextmanager
def _database(item_id, create=False):
    path = _path(item_id)
    if not create and not path.exists():
        yield None
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(path, timeout=10)
    connection.row_factory = sqlite3.Row
    try:
        if create:
            connection.execute("CREATE TABLE IF NOT EXISTS records (id TEXT PRIMARY KEY, created_at TEXT, parent_id TEXT, question TEXT, chart TEXT, row_count INTEGER, payload TEXT)")
        yield connection
        connection.commit()
    finally:
        connection.close()


def save(item_id, question, result, parent_id=None):
    record_id = uuid.uuid4().hex
    created_at = dt.datetime.now(dt.timezone.utc).isoformat()
    record = {**result, "history_id": record_id, "created_at": created_at,
              "question": question, "parent_id": parent_id}
    with _database(item_id, create=True) as connection:
        connection.execute("INSERT INTO records VALUES (?, ?, ?, ?, ?, ?, ?)",
                           (record_id, created_at, parent_id, question, result["chart"], len(result["rows"]), json.dumps(record, ensure_ascii=False)))
    return record


def get(item_id, record_id):
    with _database(item_id) as connection:
        if connection is None:
            return None
        row = connection.execute("SELECT payload FROM records WHERE id=?", (record_id,)).fetchone()
        return json.loads(row[0]) if row else None


def list_records(item_id, offset=0, limit=30):
    with _database(item_id) as connection:
        if connection is None:
            return {"items": [], "total": 0, "has_more": False}
        total = connection.execute("SELECT COUNT(*) FROM records").fetchone()[0]
        rows = connection.execute("SELECT id, created_at, parent_id, question, chart, row_count FROM records ORDER BY created_at DESC LIMIT ? OFFSET ?", (limit, offset)).fetchall()
        return {"items": [dict(row) for row in rows], "total": total, "has_more": offset + len(rows) < total}


def context(item_id, parent_id):
    chain = []
    while parent_id and len(chain) < 6:
        record = get(item_id, parent_id)
        if record is None:
            raise ValueError("追问对应的历史记录不存在，请重新选择或开始新问题")
        chain.append({"question": record["question"], "sql": record["sql"],
                      "answer": record["answer"][:1200], "rows": record["rows"][:10],
                      "dictionary_version": record.get("dictionary_version", 0)})
        parent_id = record.get("parent_id")
    return list(reversed(chain))
