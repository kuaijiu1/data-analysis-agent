"""Stream Excel tables and document-like sheets without discarding their text."""

import csv
import itertools
import json
import os
import posixpath
import re
import subprocess
import sys
import time
import xml.etree.ElementTree as ET
from pathlib import Path

from openpyxl.utils.cell import get_column_letter, range_boundaries, column_index_from_string

from . import store, tasks

S = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
R = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
XDR = "http://schemas.openxmlformats.org/drawingml/2006/spreadsheetDrawing"
A = "http://schemas.openxmlformats.org/drawingml/2006/main"
MAX_IMAGES = 100
MAX_IMAGE_BYTES = 16 * 1024 * 1024


def _relations(archive, part):
    path = posixpath.join(posixpath.dirname(part), "_rels", posixpath.basename(part) + ".rels")
    if path not in archive.namelist():
        return {}
    if archive.getinfo(path).file_size > 2 * 1024 * 1024:
        raise ValueError("工作表关联信息过大")
    result = {}
    for rel in ET.fromstring(archive.read(path)):
        if rel.get("TargetMode") == "External":
            continue
        target = rel.get("Target", "")
        target = posixpath.normpath(target.lstrip("/") if target.startswith("/") else posixpath.join(posixpath.dirname(part), target))
        if target.startswith("xl/"):
            result[rel.get("Id")] = target
    return result


def inspect_sheet(archive, worksheet, item_id):
    part = worksheet._worksheet_path.lstrip("/")
    merges, drawings, notes = [], [], []
    bands, occupied_columns, row_columns = [], set(), set()
    cell_has_value = cell_has_formula = cell_has_cache = False
    missing_formula_count, missing_formula_positions = 0, []
    with archive.open(part) as source:
        parser = ET.iterparse(source, events=("start", "end"))
        _, root = next(parser)
        cells = 0
        for event, node in parser:
            if event == "start":
                if node.tag == f"{{{S}}}c":
                    cell_has_value = cell_has_formula = cell_has_cache = False
                elif node.tag == f"{{{S}}}f":
                    # Shared/array formula followers may have an empty <f/>.
                    cell_has_formula = cell_has_value = True
                continue
            if node.tag == f"{{{S}}}v" and node.text is not None and node.text.strip():
                cell_has_cache = True
            if node.tag in {f"{{{S}}}v", f"{{{S}}}t", f"{{{S}}}f"} and node.text and node.text.strip():
                cell_has_value = True
            if node.tag == f"{{{S}}}c" and cell_has_value:
                if cell_has_formula and not cell_has_cache:
                    missing_formula_count += 1
                    if len(missing_formula_positions) < 12:
                        missing_formula_positions.append(node.get("r", "?"))
                ref = re.match(r"([A-Z]+)", node.get("r", ""))
                if ref:
                    row_columns.add(column_index_from_string(ref[1]))
            if node.tag == f"{{{S}}}mergeCell":
                if len(merges) < 100_000:
                    merges.append(range_boundaries(node.get("ref")))
                elif len(merges) == 100_000 and not notes:
                    notes.append("合并区域超过十万个，仅处理前十万个；请核对导入结果。")
            elif node.tag == f"{{{S}}}drawing":
                drawings.append(node.get(f"{{{R}}}id"))
            elif node.tag == f"{{{S}}}row":
                row_number = int(node.get("r", "1"))
                if row_columns:
                    if not bands or (row_number > bands[-1]["end"] + 1 and len(bands) < 512):
                        bands.append({"start": row_number, "end": row_number, "limits": {}})
                    band = bands[-1]
                    band["end"] = row_number
                    for col in row_columns:
                        band["limits"].setdefault(col, [row_number, row_number])[1] = row_number
                    occupied_columns.update(row_columns)
                row_columns = set()
                cells += 1
                if cells % 256 == 0:
                    tasks.check(item_id)
                root.clear()
            node.clear()
    relations = _relations(archive, part) if drawings else {}
    if missing_formula_count:
        positions = "、".join(missing_formula_positions)
        notes.append(f"{worksheet.title} 有 {missing_formula_count} 个公式没有可读取的计算结果（位置：{positions}"
                     f"{'等' if missing_formula_count > len(missing_formula_positions) else ''}）。这些单元格按空值导入；"
                     "公式也可能返回空文本，请在 Excel 中重新计算、保存并核对后，再重新读取文件。")
    return merges, [relations[key] for key in drawings if key in relations], notes, _regions(worksheet, bands, occupied_columns, item_id)


class SheetRegion:
    def __init__(self, sheet, first_col, last_col, first_row, last_row, force_text=False):
        self.sheet, self.title = sheet, sheet.title
        self.first_col, self.last_col = first_col, last_col
        self.first_row, self.last_row = first_row, last_row
        self.row_offset, self.column_offset = first_row - 1, first_col - 1
        self.force_text = force_text
        self.source_range = f"{get_column_letter(first_col)}{first_row}:{get_column_letter(last_col)}{last_row}"

    def iter_rows(self, values_only=True):
        return self.sheet.iter_rows(min_col=self.first_col, max_col=self.last_col,
                                    min_row=self.first_row, max_row=self.last_row, values_only=values_only)


def _regions(sheet, bands, occupied, item_id):
    if not occupied:
        return [SheetRegion(sheet, 1, 1, 1, 1)]
    columns = sorted(occupied)
    runs = []
    for col in columns:
        if not runs or col > runs[-1][-1] + 1:
            runs.append([col])
        else:
            runs[-1].append(col)
    # A separate table needs at least two occupied columns. Do not fragment prose.
    if len(runs) < 2 or any(len(run) < 2 for run in runs):
        runs = [columns]
    prefix = dict(enumerate(sheet.iter_rows(min_row=1, max_row=60, values_only=True), 1))
    result = []
    for run in runs:
        left, right = run[0], run[-1]
        relevant = [band for band in bands if any(left <= col <= right for col in band["limits"])]
        chunks = []
        for band in relevant:
            tasks.check(item_id)
            row = prefix.get(band["start"], ())
            values = [str(v).strip() for v in row[left - 1:right] if v is not None]
            footer = bool(chunks and any(re.match(r"^(?:Q\s*\d+|问题\s*\d*|注[：:]|说明[：:]|备注[：:])", v, re.I) for v in values))
            if not chunks or footer:
                chunks.append(([band], footer))
            else:
                chunks[-1][0].append(band)
        for group, footer in chunks:
            limits = [bounds for band in group for col, bounds in band["limits"].items() if left <= col <= right]
            result.append(SheetRegion(sheet, left, right, min(v[0] for v in limits), max(v[1] for v in limits), footer))
    return result


def _occupied(row):
    return [i for i, value in enumerate(row) if value is not None and str(value).strip()]


def _short_labels(row):
    values = [row[i] for i in _occupied(row)]
    return bool(values) and all(isinstance(v, str) and len(v.strip()) <= 60 and "\n" not in v for v in values)


def _header_plan(prefix, merges):
    populated = [(r, values) for r, values in prefix if _occupied(values)]
    if not populated:
        return [], 0
    maximum = max(len(_occupied(row)) for _, row in populated)
    if maximum == 1:
        first = populated[0]
        # Narrative sheets have no assumed header: their first sentence must survive.
        if len(populated) > 1 and _short_labels(first[1]) and all(
            all(isinstance(row[i], (int, float)) for i in _occupied(row)) for _, row in populated[1:]
        ):
            return [first], max(_occupied(first[1])) + 1
        return [], 0
    candidate = next(((r, row) for r, row in populated if len(_occupied(row)) >= max(2, maximum / 2) and _short_labels(row)), None)
    if candidate is None:
        return [], 0
    headers = [candidate]
    # Only join extra header levels when actual horizontal merges justify them.
    for r, row in populated[populated.index(candidate) + 1:]:
        previous = headers[-1][0]
        if len(headers) >= 3 or r != previous + 1 or not _short_labels(row):
            break
        if not any(c2 > c1 and r1 <= previous <= r2 for c1, r1, c2, r2 in merges):
            break
        headers.append((r, row))
    width = max(max(_occupied(row)) + 1 for _, row in populated)
    return headers, width


def sheet_to_csv(worksheet, target, supplement, item_id, merges, normalise, cell):
    row_offset = getattr(worksheet, "row_offset", 0)
    col_offset = getattr(worksheet, "column_offset", 0)
    merges = [(c1 - col_offset, r1, c2 - col_offset, r2) for c1, r1, c2, r2 in merges
              if c1 > col_offset and c2 <= getattr(worksheet, "last_col", 16384)]
    iterator = enumerate(worksheet.iter_rows(values_only=True), 1 + row_offset)
    prefix = list(itertools.islice(iterator, 40))
    headers, width = _header_plan(prefix, merges)
    if getattr(worksheet, "force_text", False):
        headers, width = [], 0
    mode = "table" if headers else "text"
    header_rows = [r for r, _ in headers]
    source_rows = dict(headers)
    names = []
    unnamed_columns = []
    if headers:
        # The XML scan already knows the occupied range of the entire region.
        # Do not drop columns whose first value occurs after the header sample.
        width = max(width, getattr(worksheet, "last_col", width + col_offset) - col_offset)
        for col in range(1, width + 1):
            pieces = []
            for r, row in headers:
                value = row[col - 1] if col <= len(row) else None
                if value is None:
                    for c1, r1, c2, r2 in merges:
                        if c1 <= col <= c2 and r1 <= r <= r2 and r1 in source_rows:
                            value = source_rows[r1][c1 - 1]
                            break
                if value is not None and str(value).strip() and str(value).strip() not in pieces:
                    pieces.append(str(value).strip())
            names.append("_".join(pieces))
            if not pieces:
                unnamed_columns.append(get_column_letter(col + col_offset))
        names = normalise(tuple(names))
    else:
        names = ["原始行号", "原始位置", "文字内容"]
    count, extra_count = 0, 0
    vertical = sorted((r1, r2, c1) for c1, r1, c2, r2 in merges if c1 == c2 and r2 > r1)
    pending, active = iter(vertical), []
    upcoming = next(pending, None)
    last_update = time.monotonic()
    with target.open("w", encoding="utf-8", newline="") as output, supplement.open("w", encoding="utf-8", newline="") as extras:
        writer, extra_writer = csv.writer(output), csv.writer(extras)
        writer.writerow(names)
        extra_writer.writerow(["来源", "原始位置", "文字内容", "识别置信度"])
        for row_number, raw in itertools.chain(prefix, iterator):
            if row_number % 256 == 0:
                tasks.check(item_id)
            occupied = _occupied(raw)
            if headers and row_number <= header_rows[-1]:
                if row_number not in header_rows:
                    for col in occupied:
                        extra_writer.writerow(["单元格说明", f"{worksheet.title}!{get_column_letter(col + 1 + col_offset)}{row_number}", str(raw[col]), ""])
                        extra_count += 1
                continue
            if not occupied:
                continue
            if mode == "text":
                first, last = occupied[0] + 1, occupied[-1] + 1
                position = f"{worksheet.title}!{get_column_letter(first + col_offset)}{row_number}:{get_column_letter(last + col_offset)}{row_number}"
                text = "；".join(f"{get_column_letter(col + 1 + col_offset)}: {cell(raw[col])}" for col in occupied)
                writer.writerow([row_number, position, text])
            else:
                row = list(raw[:width]) + [None] * max(0, width - len(raw))
                for col in occupied:
                    if col >= width:
                        extra_writer.writerow(["表外单元格", f"{worksheet.title}!{get_column_letter(col + 1 + col_offset)}{row_number}", str(raw[col]), ""])
                        extra_count += 1
                while upcoming is not None and upcoming[0] <= row_number:
                    start, end, col = upcoming
                    if start == row_number and start > header_rows[-1] and col <= width and isinstance(row[col - 1], str):
                        active.append((end, col, row[col - 1]))
                    upcoming = next(pending, None)
                active = [entry for entry in active if entry[0] >= row_number]
                for _, col, value in active:
                    if row[col - 1] is None:
                        row[col - 1] = value
                writer.writerow([cell(value) for value in row])
            count += 1
            if time.monotonic() - last_update > 2:
                store.update(item_id, progress=f"正在读取 {worksheet.title}：{count:,} 行")
                last_update = time.monotonic()
    text_columns = []
    if headers:
        for col, name in enumerate(names):
            if any(col < len(row) and isinstance(row[col], str) for r, row in prefix if r > header_rows[-1]):
                text_columns.append(name)
    return {"mode": mode, "header_rows": header_rows, "rows": count, "extra_rows": extra_count, "text_columns": text_columns,
            "merged_regions": len(merges), "source_range": getattr(worksheet, "source_range", ""),
            "warnings": ([f"{worksheet.title} · {getattr(worksheet, 'source_range', '')} 的 "
                          f"{'、'.join(unnamed_columns[:12])}{' 等' if len(unnamed_columns) > 12 else ''} "
                          "列缺少列名，已保留在原记录中并自动命名为“列N”，请核对列含义。"] if unnamed_columns else [])}


def _ocr_image(image_path: Path, item_id: str) -> list:
    output = image_path.with_suffix(".ocr.json")
    error = image_path.with_suffix(".ocr.log")
    with error.open("wb") as log:
        process = subprocess.Popen(
            [sys.executable, "-m", "app.ocr_worker", str(image_path), str(output)],
            cwd=store.ROOT, stdout=log, stderr=log, stdin=subprocess.DEVNULL,
            creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
            env={**os.environ, "PYTHONUTF8": "1"},
        )
        try:
            deadline = time.monotonic() + 60
            while process.poll() is None:
                tasks.check(item_id)
                if time.monotonic() > deadline:
                    raise RuntimeError("图片识别超过 60 秒")
                time.sleep(0.2)
            if not output.exists():
                raise RuntimeError("本机 OCR 识别失败，请检查 rapidocr-onnxruntime 是否正确安装")
            result = json.loads(output.read_text(encoding="utf-8"))
            if isinstance(result, dict) and result.get("error"):
                raise RuntimeError(result["error"])
            if process.returncode or not isinstance(result, list):
                raise RuntimeError("本机 OCR 返回无效结果")
            return result
        finally:
            if process.poll() is None:
                process.kill()
            process.wait()


def drawing_text(archive, drawings, worksheet, supplement, temp_folder, item_id):
    notes, count, images = [], 0, 0
    with supplement.open("a", encoding="utf-8", newline="") as output:
        writer = csv.writer(output)
        for drawing in drawings:
            if archive.getinfo(drawing).file_size > 8 * 1024 * 1024:
                notes.append(f"{worksheet.title} 的绘图信息过大，未读取。")
                continue
            root = ET.fromstring(archive.read(drawing))
            relations = _relations(archive, drawing)
            for index, anchor in enumerate(root, 1):
                tasks.check(item_id)
                origin = anchor.find(f"{{{XDR}}}from")
                row = int(origin.findtext(f"{{{XDR}}}row", "0")) + 1 if origin is not None else 1
                col = int(origin.findtext(f"{{{XDR}}}col", "0")) + 1 if origin is not None else 1
                position = f"{worksheet.title}!{get_column_letter(col)}{row}（对象 {index}）"
                shape_text = "\n".join(node.text for node in anchor.iter(f"{{{A}}}t") if node.text)
                if shape_text:
                    writer.writerow(["文本框", position, shape_text, ""])
                    count += 1
                for blip in anchor.iter(f"{{{A}}}blip"):
                    target = relations.get(blip.get(f"{{{R}}}embed"))
                    if not target:
                        notes.append(f"{position} 是外部链接图片，未读取。")
                        continue
                    images += 1
                    if images > MAX_IMAGES:
                        if images == MAX_IMAGES + 1:
                            notes.append(f"{worksheet.title} 超过 {MAX_IMAGES} 张图片，其余图片未识别。")
                        continue
                    if archive.getinfo(target).file_size > MAX_IMAGE_BYTES:
                        notes.append(f"{position} 图片超过 16MB，未识别。")
                        continue
                    path = temp_folder / f"ocr-{images}{Path(target).suffix}"
                    try:
                        store.update(item_id, progress=f"正在识别 {worksheet.title} 的第 {images} 张图片文字")
                        path.write_bytes(archive.read(target))
                        lines = _ocr_image(path, item_id)
                        if not lines:
                            notes.append(f"{position} 未识别到清晰文字。")
                        for line_number, line in enumerate(lines, 1):
                            writer.writerow(["图片OCR", f"{position} 第{line_number}行", line["text"], line["confidence"]])
                            count += 1
                    except tasks.Cancelled:
                        raise
                    except Exception as exc:
                        notes.append(f"{position}：{exc}")
    return count, images, notes
