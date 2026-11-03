import asyncio
import shutil
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request, Response, Query
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel, Field

from . import auth, store, tasks, history
from .analysis import analyse, model_config
from .dashboard import generate_dashboard, load_dashboard
from .ingest import import_file, scan_or_import
from .context import clean_dictionary
from .quality import refresh_profiles
from .board_edit import DashboardEdit, edit_dashboard
from .dashboard import DashboardConflict

MAX_FILE_SIZE = 1024 * 1024 * 1024
CHUNK_SIZE = 8 * 1024 * 1024
executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="import")
upload_locks: dict[str, asyncio.Lock] = {}
app = FastAPI(title="本地数据分析 Agent")
WEB_DIR = Path(__file__).resolve().parent.parent / "web"


@app.exception_handler(tasks.BusyError)
async def busy_error(request: Request, exc: tasks.BusyError):
    return JSONResponse({"detail": str(exc)}, status_code=409)


@app.exception_handler(DashboardConflict)
async def dashboard_conflict(request: Request, exc: DashboardConflict):
    return JSONResponse({"detail": str(exc)}, status_code=409)


class UploadStart(BaseModel):
    name: str
    size: int = Field(gt=0, le=MAX_FILE_SIZE)
    encoding: str = "auto"


class ImportRequest(BaseModel):
    sheets: list[str] = Field(default_factory=list)


class Question(BaseModel):
    question: str = Field(min_length=2, max_length=2000)
    parent_id: str | None = Field(default=None, pattern=r"^[0-9a-f]{32}$")


class DashboardRequest(BaseModel):
    focus: str = Field(default="", max_length=300)


class FieldDefinition(BaseModel):
    table: str = Field(min_length=1, max_length=256)
    column: str = Field(min_length=1, max_length=512)
    description: str = Field(default="", max_length=500)


class MetricDefinition(BaseModel):
    name: str = Field(min_length=1, max_length=80)
    definition: str = Field(min_length=1, max_length=1500)


class DictionaryRequest(BaseModel):
    description: str = Field(default="", max_length=2000)
    fields: list[FieldDefinition] = Field(default_factory=list, max_length=1000)
    metrics: list[MetricDefinition] = Field(default_factory=list, max_length=20)


class Credentials(BaseModel):
    email: str = Field(min_length=3, max_length=254)
    password: str = Field(min_length=8, max_length=128)


class LegacyMigration(Credentials):
    username: str = Field(min_length=1, max_length=40)


@app.middleware("http")
async def require_login(request: Request, call_next):
    path = request.url.path
    if path.startswith("/api/"):
        origin = request.headers.get("origin")
        expected_origin = f"{request.url.scheme}://{request.headers.get('host')}"
        if request.method not in {"GET", "HEAD", "OPTIONS"} and origin and origin != expected_origin:
            return JSONResponse({"detail": "请求来源不正确"}, status_code=403)
        if not path.startswith("/api/auth/"):
            user = auth.session_user(request.cookies.get(auth.COOKIE_NAME))
            if user is None:
                return JSONResponse({"detail": "请先登录"}, status_code=401)
            request.state.user_id = user["id"]
    return await call_next(request)


@app.get("/api/auth/status")
def auth_status(request: Request):
    user = auth.session_user(request.cookies.get(auth.COOKIE_NAME))
    return {"authenticated": user is not None, "email": user["email"] if user else None,
            "legacy_available": auth.legacy_available()}


@app.post("/api/auth/register")
def auth_register(payload: Credentials, response: Response):
    try:
        user = auth.register(payload.email, payload.password)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    except FileExistsError as exc:
        raise HTTPException(409, str(exc)) from exc
    response.set_cookie(auth.COOKIE_NAME, auth.new_session(user["id"]), max_age=auth.SESSION_SECONDS,
                        httponly=True, samesite="strict", secure=False, path="/")
    return {"email": user["email"]}


@app.post("/api/auth/login")
def auth_login(payload: Credentials, request: Request, response: Response):
    client_id = request.client.host if request.client else "local"
    try:
        user = auth.authenticate(payload.email, payload.password, client_id)
    except PermissionError as exc:
        raise HTTPException(429, str(exc)) from exc
    if user is None:
        raise HTTPException(401, "邮箱或密码错误")
    response.set_cookie(auth.COOKIE_NAME, auth.new_session(user["id"]), max_age=auth.SESSION_SECONDS,
                        httponly=True, samesite="strict", secure=False, path="/")
    return {"email": user["email"]}


@app.post("/api/auth/migrate")
def auth_migrate(payload: LegacyMigration, request: Request, response: Response):
    client_id = request.client.host if request.client else "local"
    try:
        user = auth.migrate_legacy(payload.email, payload.username, payload.password, client_id)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    except FileExistsError as exc:
        raise HTTPException(409, str(exc)) from exc
    except PermissionError as exc:
        raise HTTPException(429, str(exc)) from exc
    response.set_cookie(auth.COOKIE_NAME, auth.new_session(user["id"]), max_age=auth.SESSION_SECONDS,
                        httponly=True, samesite="strict", secure=False, path="/")
    return {"email": user["email"]}


@app.post("/api/auth/logout")
def auth_logout(request: Request, response: Response):
    auth.revoke_session(request.cookies.get(auth.COOKIE_NAME))
    response.delete_cookie(auth.COOKIE_NAME, path="/")
    return {"ok": True}


def _get(item_id: str, request: Request) -> dict:
    item = store.get(item_id)
    if not item or item.get("owner_id") != request.state.user_id:
        raise HTTPException(404, "数据集不存在")
    return item


@app.get("/")
def index():
    return FileResponse(WEB_DIR / "index.html")


@app.get("/app.js")
def script():
    return FileResponse(WEB_DIR / "app.js", media_type="text/javascript")


@app.get("/style.css")
def style():
    return FileResponse(WEB_DIR / "style.css", media_type="text/css")


@app.get("/dashboard.css")
def dashboard_style():
    return FileResponse(WEB_DIR / "dashboard.css", media_type="text/css")


@app.get("/auth.css")
def auth_style():
    return FileResponse(WEB_DIR / "auth.css", media_type="text/css")


@app.get("/quality.css")
def quality_style():
    return FileResponse(WEB_DIR / "quality.css", media_type="text/css")


@app.get("/quality.js")
def quality_script():
    return FileResponse(WEB_DIR / "quality.js", media_type="text/javascript")


@app.get("/workspace.js")
def workspace_script():
    return FileResponse(WEB_DIR / "workspace.js", media_type="text/javascript")


@app.get("/workspace.css")
def workspace_style():
    return FileResponse(WEB_DIR / "workspace.css", media_type="text/css")


@app.get("/api/health")
def health():
    config = model_config()
    return {"ok": True, "model_ready": bool(config["api_key"]), "provider": config["provider"], "model": config["model"]}


@app.get("/api/datasets")
def datasets(request: Request):
    return [store.public(item, summary=True) for item in store.all_items(request.state.user_id)]


@app.get("/api/datasets/{item_id}")
def dataset(item_id: str, request: Request):
    return store.public(_get(item_id, request))


@app.post("/api/uploads")
def start_upload(payload: UploadStart, request: Request):
    name = Path(payload.name).name.strip()
    extension = Path(name).suffix.lower()
    if extension not in {".csv", ".xlsx"}:
        raise HTTPException(400, "只支持 .csv 和 .xlsx 文件")
    if payload.encoding not in {"auto", "utf-8", "utf-8-sig", "gb18030", "utf-16"}:
        raise HTTPException(400, "不支持此 CSV 编码")
    try:
        store.check_disk_space(payload.size)
    except ValueError as exc:
        raise HTTPException(507, str(exc)) from exc
    item = store.create(name, payload.size, extension, payload.encoding, request.state.user_id)
    return {"id": item["id"], "chunk_size": CHUNK_SIZE}


@app.put("/api/uploads/{item_id}/chunk")
async def upload_chunk(item_id: str, request: Request, offset: int):
    lock = upload_locks.setdefault(item_id, asyncio.Lock())
    async with lock:
        item = _get(item_id, request)
        if item["status"] != "uploading":
            raise HTTPException(409, "上传已经结束")
        if offset != item["received"]:
            raise HTTPException(409, f"上传位置不匹配；服务端已收到 {item['received']} 字节")
        path = store.upload_path(item_id, item["extension"])
        if path.exists() and path.stat().st_size != offset:
            raise HTTPException(409, "上传文件状态不一致")
        count = 0
        try:
            with path.open("ab") as output:
                async for part in request.stream():
                    count += len(part)
                    if count > CHUNK_SIZE or offset + count > item["size"]:
                        raise HTTPException(413, "分片或文件超过大小限制")
                    output.write(part)
        except Exception:
            with path.open("r+b") as output:
                output.truncate(offset)
            raise
        if count == 0:
            raise HTTPException(400, "分片为空")
        received = offset + count
        store.update(item_id, received=received, progress=f"上传中：{received / item['size']:.0%}")
        return {"received": received}


@app.post("/api/uploads/{item_id}/complete")
def complete_upload(item_id: str, request: Request):
    item = _get(item_id, request)
    if item["status"] != "uploading":
        raise HTTPException(409, "上传已经结束")
    if item["received"] != item["size"]:
        raise HTTPException(409, "文件尚未上传完整")
    tasks.schedule(executor, item_id, scan_or_import,
                   on_queue=lambda: store.update(item_id, status="queued", progress="等待处理"))
    return {"ok": True}


@app.post("/api/datasets/{item_id}/import")
def begin_import(item_id: str, payload: ImportRequest, request: Request):
    item = _get(item_id, request)
    if item["status"] not in {"awaiting_sheets", "error", "interrupted", "cancelled"}:
        raise HTTPException(409, "当前状态无法开始导入")
    if item["received"] != item["size"]:
        raise HTTPException(409, "请先续传完整文件")
    if item["extension"] == ".xlsx":
        if not item.get("sheets") and item["status"] in {"cancelled", "error", "interrupted"}:
            tasks.schedule(executor, item_id, scan_or_import,
                           on_queue=lambda: store.update(item_id, status="queued", progress="等待读取工作表"))
            return {"ok": True}
        if not payload.sheets or any(sheet not in item["sheets"] for sheet in payload.sheets):
            raise HTTPException(400, "请选择有效工作表")
    else:
        payload.sheets = []
    tasks.schedule(executor, item_id, import_file, payload.sheets,
                   on_queue=lambda: store.update(item_id, status="queued", progress="等待导入", selected_sheets=payload.sheets))
    return {"ok": True}


@app.post("/api/datasets/{item_id}/ask")
def ask(item_id: str, payload: Question, request: Request):
    _get(item_id, request)
    try:
        with tasks.activity(item_id):
            return analyse(item_id, payload.question.strip(), payload.parent_id)
    except tasks.BusyError:
        raise
    except (ValueError, RuntimeError) as exc:
        raise HTTPException(400, str(exc)) from exc
    except Exception as exc:
        raise HTTPException(502, f"分析失败：{exc}") from exc


@app.post("/api/datasets/{item_id}/reprocess")
def reprocess_file(item_id: str, request: Request):
    item = _get(item_id, request)
    if item["extension"] not in {".xlsx", ".csv"} or item["status"] != "ready":
        raise HTTPException(400, "请选择已经导入完成的 Excel 或 CSV 数据集")
    with tasks.activity(item_id):
        source = store.upload_path(item_id, item["extension"])
        if not source.is_file():
            raise HTTPException(400, "原始文件不存在，请重新上传")
        try:
            store.check_disk_space(item["size"] + item["size"] // 3)
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc
        clone = store.create(f"{Path(item['name']).stem}（重新读取）{item['extension']}", item["size"], item["extension"], item.get("encoding", "auto"), request.state.user_id)
        try:
            shutil.copyfile(source, store.upload_path(clone["id"], item["extension"]))
            sheets = item.get("selected_sheets") or item.get("sheets", [])
            store.update(clone["id"], received=item["size"], sheets=item.get("sheets", []), selected_sheets=sheets)
            tasks.schedule(executor, clone["id"], import_file, sheets,
                           on_queue=lambda: store.update(clone["id"], status="queued", progress="等待重新读取文件"))
        except Exception as exc:
            store.update(clone["id"], status="error", error=str(exc), progress="重新读取失败，原数据集保留")
            raise HTTPException(400, f"重新读取失败：{exc}") from exc
    return {"id": clone["id"]}


@app.post("/api/datasets/{item_id}/profile")
def create_profile(item_id: str, request: Request):
    item = _get(item_id, request)
    if item["status"] != "ready":
        raise HTTPException(409, "数据集尚未准备好")
    def queue():
        if not store.queue_profile(item_id):
            raise tasks.BusyError("正在生成概览，请稍候")
    tasks.schedule(executor, item_id, refresh_profiles, on_queue=queue)
    return {"ok": True}


@app.get("/api/datasets/{item_id}/history")
def analysis_history(item_id: str, request: Request, offset: int = Query(default=0, ge=0), limit: int = Query(default=30, ge=1, le=100)):
    _get(item_id, request)
    with tasks.activity(item_id):
        return history.list_records(item_id, offset, limit)


@app.get("/api/datasets/{item_id}/history/{record_id}")
def analysis_record(item_id: str, record_id: str, request: Request):
    _get(item_id, request)
    with tasks.activity(item_id):
        record = history.get(item_id, record_id)
    if record is None:
        raise HTTPException(404, "历史记录不存在")
    return record


@app.put("/api/datasets/{item_id}/dictionary")
def save_dictionary(item_id: str, payload: DictionaryRequest, request: Request):
    item = _get(item_id, request)
    if item["status"] != "ready":
        raise HTTPException(409, "数据集尚未准备好")
    try:
        dictionary = clean_dictionary(item, payload.model_dump())
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    with tasks.activity(item_id):
        saved = store.save_dictionary(item_id, dictionary)
    return {"dictionary": saved["dictionary"], "dictionary_version": saved["dictionary_version"]}


@app.get("/api/datasets/{item_id}/dashboard")
def dashboard(item_id: str, request: Request):
    _get(item_id, request)
    with tasks.activity(item_id):
        result = load_dashboard(item_id)
    if result is None:
        raise HTTPException(404, "尚未生成数据看板")
    return result


@app.post("/api/datasets/{item_id}/dashboard")
def create_dashboard(item_id: str, payload: DashboardRequest, request: Request):
    _get(item_id, request)
    try:
        with tasks.activity(item_id):
            return generate_dashboard(item_id, payload.focus.strip())
    except tasks.BusyError:
        raise
    except (ValueError, RuntimeError) as exc:
        raise HTTPException(400, str(exc)) from exc
    except Exception as exc:
        raise HTTPException(502, f"看板生成失败：{exc}") from exc


@app.post("/api/datasets/{item_id}/cancel")
def cancel_import(item_id: str, request: Request):
    _get(item_id, request)
    def mark(active):
        item = _get(item_id, request)
        if item["status"] not in {"queued", "scanning", "importing", "awaiting_sheets", "cancelling"}:
            raise tasks.BusyError("当前没有可取消的导入任务")
        store.update(item_id, status="cancelling" if active else "cancelled", error=None,
                     progress="正在取消导入…" if active else "导入已取消，原文件已保留")
    tasks.cancel(item_id, on_cancel=mark)
    return {"ok": True}


@app.put("/api/datasets/{item_id}/dashboard")
def update_dashboard(item_id: str, payload: DashboardEdit, request: Request):
    _get(item_id, request)
    try:
        with tasks.activity(item_id):
            return edit_dashboard(item_id, payload)
    except (DashboardConflict, tasks.BusyError):
        raise
    except Exception as exc:
        raise HTTPException(400, f"看板修改未保存：{exc}") from exc


@app.delete("/api/datasets/{item_id}")
async def delete_dataset(item_id: str, request: Request):
    lock = upload_locks.setdefault(item_id, asyncio.Lock())
    async with lock:
        _get(item_id, request)
        with tasks.deletion(item_id):
            try:
                released = await asyncio.to_thread(store.remove_dataset, item_id)
            except OSError as exc:
                store.update(item_id, status="error", progress="删除未完成，请重试删除", error="文件可能正在被其他程序占用")
                raise HTTPException(409, "部分文件无法删除，请关闭占用文件的程序后重试") from exc
    upload_locks.pop(item_id, None)
    return {"ok": True, "released_bytes": released}
