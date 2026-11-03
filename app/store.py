import json
import os
import shutil
import threading
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = Path(os.getenv("DATA_AGENT_DATA_DIR", str(ROOT / "data")))
UPLOAD_DIR = DATA_DIR / "uploads"
DB_DIR = DATA_DIR / "databases"
TEMP_DIR = DATA_DIR / "temp"
META_DIR = DATA_DIR / "metadata"
for directory in (UPLOAD_DIR, DB_DIR, TEMP_DIR, META_DIR):
    directory.mkdir(parents=True, exist_ok=True)

_lock = threading.RLock()
_items = {}
for file in META_DIR.glob("*.json"):
    try:
        item = json.loads(file.read_text(encoding="utf-8"))
        if item.get("status") in {"scanning", "queued", "importing", "cancelling"}:
            item["status"] = "interrupted"
            item["progress"] = "任务被中断，可以重新导入"
        if item.get("profile_status") in {"queued", "running"}:
            item["profile_status"] = "interrupted"
            item["profile_error"] = "统计被中断，请重新生成概览"
        if item.get("status") == "uploading":
            item_id = item["id"]
            if len(item_id) != 32 or any(char not in "0123456789abcdef" for char in item_id) or item.get("extension") not in {".csv", ".xlsx"}:
                continue
            path = (UPLOAD_DIR / f"{item_id}{item['extension']}").resolve()
            if not path.is_relative_to(UPLOAD_DIR.resolve()):
                continue
            received = min(item.get("received", 0), path.stat().st_size if path.exists() else 0)
            if path.exists() and path.stat().st_size > received:
                with path.open("r+b") as output:
                    output.truncate(received)
            item["received"] = received
        _items[item["id"]] = item
    except (ValueError, KeyError, OSError):
        pass


def create(name: str, size: int, extension: str, encoding: str, owner_id: str) -> dict:
    with _lock:
        item_id = uuid.uuid4().hex
        item = {
            "id": item_id,
            "owner_id": owner_id,
            "name": name,
            "size": size,
            "extension": extension,
            "encoding": encoding,
            "received": 0,
            "status": "uploading",
            "progress": "等待上传",
            "sheets": [],
            "tables": [],
            "error": None,
        }
        _items[item_id] = item
        _save(item)
        return dict(item)


def _save(item: dict) -> None:
    target = META_DIR / f"{item['id']}.json"
    temporary = target.with_suffix(".json.tmp")
    temporary.write_text(json.dumps(item, ensure_ascii=False), encoding="utf-8")
    temporary.replace(target)


def get(item_id: str) -> dict | None:
    with _lock:
        item = _items.get(item_id)
        return dict(item) if item else None


def update(item_id: str, **changes) -> dict:
    with _lock:
        item = _items[item_id]
        item.update(changes)
        _save(item)
        return dict(item)


def all_items(owner_id: str) -> list[dict]:
    with _lock:
        return [dict(item) for item in reversed(list(_items.values())) if item.get("owner_id") == owner_id]


def save_dictionary(item_id: str, dictionary: dict) -> dict:
    with _lock:
        item = _items[item_id]
        item["dictionary"] = dictionary
        item["dictionary_version"] = item.get("dictionary_version", 0) + 1
        _save(item)
        return dict(item)


def queue_profile(item_id: str) -> bool:
    with _lock:
        item = _items[item_id]
        if item.get("profile_status") in {"queued", "running"}:
            return False
        item.update(profile_status="queued", profile_error=None)
        _save(item)
        return True


def claim_unowned(owner_id: str) -> None:
    """Assign pre-login datasets to the first account or a migrated legacy account."""
    with _lock:
        for item in _items.values():
            if not item.get("owner_id"):
                item["owner_id"] = owner_id
                _save(item)


def upload_path(item_id: str, extension: str) -> Path:
    return UPLOAD_DIR / f"{item_id}{extension}"


def db_path(item_id: str) -> Path:
    return DB_DIR / f"{item_id}.duckdb"


def public(item: dict, summary: bool = False) -> dict:
    excluded = {"encoding", "owner_id"}
    if summary:
        excluded |= {"tables", "dictionary"}
    result = {key: value for key, value in item.items() if key not in excluded}
    result["storage_bytes"] = storage_bytes(item)
    return result


def _dataset_paths(item: dict) -> list[Path]:
    item_id = item["id"]
    if len(item_id) != 32 or any(char not in "0123456789abcdef" for char in item_id):
        raise ValueError("无效数据集编号")
    return [upload_path(item_id, item["extension"]), db_path(item_id),
            Path(str(db_path(item_id)) + ".wal"), META_DIR / f"{item_id}.json",
            DATA_DIR / "dashboards" / f"{item_id}.json", DATA_DIR / "history" / f"{item_id}.sqlite",
            TEMP_DIR / item_id]


def _safe_path(path: Path) -> Path:
    target, root = path.resolve(), DATA_DIR.resolve()
    if target == root or not target.is_relative_to(root):
        raise ValueError("文件路径超出项目数据目录")
    return target


def storage_bytes(item: dict) -> int:
    total = 0
    for path in _dataset_paths(item):
        try:
            if path.is_dir():
                total += sum(file.stat().st_size for file in path.rglob("*") if file.is_file())
            elif path.is_file():
                total += path.stat().st_size
        except FileNotFoundError:
            pass
    return total


def check_disk_space(size: int) -> None:
    # Original file, normalized CSV, database and a reserve; XLSX expansion can exceed this estimate.
    if shutil.disk_usage(DATA_DIR).free < size * 3 + 256 * 1024 ** 2:
        raise ValueError("磁盘剩余空间不足，请先删除不需要的数据集再上传")


def remove_dataset(item_id: str) -> int:
    with _lock:
        item = _items[item_id]
        paths = [_safe_path(path) for path in _dataset_paths(item)]
        released = storage_bytes(item)
        # Keep the metadata until other files have been removed, so cleanup can be retried on failure.
        metadata = _safe_path(META_DIR / f"{item_id}.json")
        for path in paths:
            if path == metadata:
                continue
            if path.is_dir():
                shutil.rmtree(path)
            else:
                path.unlink(missing_ok=True)
        metadata.unlink(missing_ok=True)
        _items.pop(item_id)
        return released
