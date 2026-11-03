import datetime as dt
import codecs
import decimal
import math
import shutil
import time
from pathlib import Path

import duckdb
from openpyxl import load_workbook

from . import store, tasks
from .quality import profile_tables
from .excel_content import inspect_sheet, sheet_to_csv, drawing_text
from .tabular import create_table, json_value as number_json_value, DATE_TEXT, date_expression, numeric_text_type
from .quality import _identifier


def _csv_encoding(path: Path, requested: str) -> str:
    if requested != "auto":
        return requested
    with path.open("rb") as source:
        sample = source.read(262_144)
    if sample.startswith((b"\xff\xfe", b"\xfe\xff")):
        return "utf-16"
    if sample.startswith(b"\xef\xbb\xbf"):
        return "utf-8-sig"
    try:
        codecs.getincrementaldecoder("utf-8")().decode(sample, final=False)
        return "utf-8"
    except UnicodeDecodeError:
        return "gb18030"


def _transcode(path: Path, target: Path, encoding: str, item_id: str) -> None:
    with path.open("r", encoding=encoding, errors="strict", newline="") as source:
        with target.open("w", encoding="utf-8", newline="") as output:
            last_update = time.monotonic()
            while True:
                tasks.check(item_id)
                block = source.read(1024 * 1024)
                if not block:
                    break
                output.write(block)
                if time.monotonic() - last_update > 1:
                    percent = min(100, round(source.buffer.tell() / max(path.stat().st_size, 1) * 100))
                    store.update(item_id, progress=f"正在转换 CSV 编码：{percent}%")
                    last_update = time.monotonic()


def _normalise_headers(values: tuple) -> list[str]:
    names = []
    seen = set()
    for index, value in enumerate(values, 1):
        base = str(value).strip() if value is not None else ""
        base = base or f"列{index}"
        name = base
        suffix = 2
        while name.casefold() in seen:
            name = f"{base}_{suffix}"
            suffix += 1
        names.append(name)
        seen.add(name.casefold())
    return names


def _cell(value):
    if isinstance(value, (dt.datetime, dt.date, dt.time)):
        return value.isoformat()
    return "" if value is None else value


def _table_info(connection, name: str, label: str) -> dict:
    columns = [
        {"name": row[0], "type": row[1]}
        for row in connection.execute(f'DESCRIBE "{name}"').fetchall()
    ]
    rows = connection.execute(f'SELECT COUNT(*) FROM "{name}"').fetchone()[0]
    result = connection.execute(f'SELECT * FROM "{name}" LIMIT 5')
    fields = [column[0] for column in result.description]
    preview = [
        dict(zip(fields, (_json_value(value) for value in row)))
        for row in result.fetchall()
    ]
    for column in columns:
        values = [row[column["name"]] for row in preview if row[column["name"]]]
        if column["type"] == "VARCHAR" and values and all(isinstance(v, str) and DATE_TEXT.fullmatch(v) for v in values):
            col = _identifier(column["name"])
            invalid = connection.execute(f'SELECT COUNT(*) FROM "{name}" WHERE {col} IS NOT NULL AND {date_expression(col)} IS NULL').fetchone()[0]
            if not invalid:
                column["semantic_type"] = "date_text"
        elif column["type"] == "VARCHAR":
            numeric_type = numeric_text_type(connection, name, column["name"], values)
            if numeric_type:
                column.update(semantic_type="numeric_text", numeric_type=numeric_type)
    return {"name": name, "label": label, "rows": rows, "columns": columns, "preview": preview}


def _json_value(value, warnings=None):
    if isinstance(value, float) and not math.isfinite(value):
        warning = "查询产生了无效数值（NaN 或 Infinity，可能由除以零或数值溢出引起），已显示为空值，不能当作 0。"
        if warnings is not None and warning not in warnings:
            warnings.append(warning)
        return None
    if isinstance(value, dict):
        return {key: _json_value(entry, warnings) for key, entry in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_value(entry, warnings) for entry in value]
    if isinstance(value, decimal.Decimal):
        return number_json_value(value)
    if isinstance(value, (dt.datetime, dt.date, dt.time)):
        return str(value)
    if isinstance(value, bytes):
        return value.hex()
    return value


def _create_table(connection, name: str, csv_path: Path, text_columns: list[str] | None = None, item_id=None) -> None:
    create_table(connection, name, csv_path, item_id=item_id, text_columns=text_columns)


def scan_or_import(item_id: str) -> None:
    item = store.get(item_id)
    try:
        tasks.check(item_id)
        if item["extension"] == ".csv":
            import_file(item_id, [])
            return
        store.update(item_id, status="scanning", progress="正在读取工作表列表")
        workbook = load_workbook(
            store.upload_path(item_id, item["extension"]),
            read_only=True,
            data_only=True,
            keep_links=False,
        )
        try:
            sheets = list(workbook.sheetnames)
        finally:
            workbook.close()
        tasks.complete(item_id, lambda: store.update(item_id, status="awaiting_sheets", sheets=sheets, progress="请选择工作表"))
    except Exception as exc:
        _import_failure(item_id, exc, "读取失败")


def _import_failure(item_id: str, exc: Exception, progress: str):
    try:
        tasks.check(item_id)
    except tasks.Cancelled:
        store.update(item_id, status="cancelled", error=None, progress="导入已取消，原文件已保留", profile_status="cancelled", tables=[])
    else:
        store.update(item_id, status="error", error=str(exc), progress=progress)


def import_file(item_id: str, selected_sheets: list[str]) -> None:
    item = store.get(item_id)
    source = store.upload_path(item_id, item["extension"])
    target = store.db_path(item_id)
    temp_folder = store.TEMP_DIR / item_id
    temp_folder.mkdir(parents=True, exist_ok=True)
    connection = None
    try:
        tasks.check(item_id)
        store.update(item_id, status="importing", error=None, progress="正在准备数据", import_warnings=[], content_version=4)
        if target.exists():
            target.unlink()
        connection = duckdb.connect(str(target))
        tasks.attach(item_id, connection)
        connection.execute("SET memory_limit = '2GB'")
        connection.execute("SET threads = 4")
        connection.execute(f"SET temp_directory = '{str(temp_folder).replace(chr(39), chr(39) * 2)}'")
        tables = []
        if item["extension"] == ".csv":
            encoding = _csv_encoding(source, item["encoding"])
            csv_path = temp_folder / "normalised.csv"
            store.update(item_id, progress=f"正在转换 CSV 编码：{encoding}")
            _transcode(source, csv_path, encoding, item_id)
            store.update(item_id, progress="正在导入 CSV")
            _create_table(connection, "data", csv_path, item_id=item_id)
            tables.append(_table_info(connection, "data", item["name"]))
        else:
            if not selected_sheets:
                raise ValueError("至少选择一个工作表")
            workbook = load_workbook(source, read_only=True, data_only=True, keep_links=False)
            try:
                for index, sheet_name in enumerate(selected_sheets, 1):
                    tasks.check(item_id)
                    if sheet_name not in workbook.sheetnames:
                        raise ValueError(f"工作表不存在：{sheet_name}")
                    store.update(item_id, progress=f"正在读取工作表：{sheet_name}")
                    worksheet = workbook[sheet_name]
                    merges, drawings, notes, regions = inspect_sheet(workbook._archive, worksheet, item_id)
                    special_images = any(name.startswith("xl/cellimages") or name.startswith("xl/richData/") for name in workbook._archive.namelist())
                    if special_images:
                        notes.append("检测到单元格内嵌图片或富数据：目前仅识别浮动图片，其他图片请在 Excel 中转换为浮动图片后重新读取。")
                    for part, region in enumerate(regions, 1):
                        table_name = f"sheet_{index}" if part == 1 else f"sheet_{index}_part_{part}"
                        csv_path = temp_folder / f"{table_name}.csv"
                        supplement = temp_folder / f"{table_name}_text.csv"
                        layout = sheet_to_csv(region, csv_path, supplement, item_id, merges, _normalise_headers, _cell)
                        notes.extend(layout.get("warnings", []))
                        image_rows, image_count = 0, 0
                        if part == 1:
                            image_rows, image_count, image_notes = drawing_text(
                                workbook._archive, drawings, worksheet, supplement, temp_folder, item_id)
                            notes.extend(image_notes)
                        if not layout["rows"] and not layout["extra_rows"] and not image_rows and not image_count and not special_images:
                            notes.append(f"{sheet_name} · {region.source_range} 是空白区域，已跳过。")
                            continue
                        store.update(item_id, progress=f"正在导入 {sheet_name} · {region.source_range}")
                        _create_table(connection, table_name, csv_path, layout["text_columns"], item_id=item_id)
                        label = sheet_name if len(regions) == 1 else f"{sheet_name} · {region.source_range}"
                        info = _table_info(connection, table_name, label)
                        info["layout"] = {**layout, "images": image_count, "source_sheet": sheet_name}
                        tables.append(info)
                        if layout["extra_rows"] + image_rows:
                            text_table = f"{table_name}_text"
                            connection.execute(f'CREATE TABLE "{text_table}" AS SELECT * FROM read_csv(?, header=true, all_varchar=true)', [str(supplement)])
                            text_info = _table_info(connection, text_table, f"{label} · 说明与图片文字")
                            text_info["layout"] = {"mode": "supplement", "source_sheet": sheet_name}
                            tables.append(text_info)
                    warnings = store.get(item_id).get("import_warnings", [])
                    store.update(item_id, import_warnings=(warnings + notes)[:100])
            finally:
                workbook.close()
        if not tables:
            raise ValueError("所选工作表没有可读取的数据，请检查是否选错了空白工作表")
        try:
            store.update(item_id, profile_status="running", profile_error=None)
            tables = profile_tables(connection, tables, lambda message: store.update(item_id, progress=message))
            store.update(item_id, profile_status="ready", profile_error=None,
                         profile_updated_at=dt.datetime.now(dt.timezone.utc).isoformat())
        except Exception as exc:
            tasks.check(item_id)
            store.update(item_id, profile_status="error", profile_error=str(exc))
        tasks.check(item_id)
        tasks.detach(item_id, connection)
        connection.close()
        connection = None
        tasks.complete(item_id, lambda: store.update(item_id, status="ready", progress="可以开始提问", tables=tables))
    except Exception as exc:
        _import_failure(item_id, exc, "导入失败")
    finally:
        if connection is not None:
            tasks.detach(item_id, connection)
            connection.close()
        shutil.rmtree(store._safe_path(temp_folder), ignore_errors=True)
        if store.get(item_id).get("status") == "cancelled":
            target.unlink(missing_ok=True)
            Path(str(target) + ".wal").unlink(missing_ok=True)
