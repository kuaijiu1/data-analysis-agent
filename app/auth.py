"""Email accounts and browser sessions for the local web app."""

import base64
import hashlib
import hmac
import json
import re
import secrets
import threading
import time
import uuid

from . import store


COOKIE_NAME = "data_agent_session"
SESSION_SECONDS = 7 * 24 * 60 * 60
PASSWORD_ITERATIONS = 600_000
EMAIL_PATTERN = re.compile(r"^[^\s@]+@(?:[^\s@.]+\.)+[^\s@.]+$")
_lock = threading.RLock()
_sessions: dict[str, tuple[float, str]] = {}
_failed: dict[str, list[float]] = {}


def _users_path():
    return store.DATA_DIR / "users.json"


def _legacy_path():
    return store.DATA_DIR / "auth.json"


def legacy_available() -> bool:
    return _legacy_path().exists()


def _users() -> list[dict]:
    path = _users_path()
    if not path.exists():
        return []
    return json.loads(path.read_text(encoding="utf-8"))["users"]


def _save_users(users: list[dict]) -> None:
    path = _users_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".json.tmp")
    temporary.write_text(json.dumps({"users": users}, ensure_ascii=False), encoding="utf-8")
    temporary.replace(path)


def _email(value: str) -> str:
    email = value.strip().casefold()
    if len(email) > 254 or not EMAIL_PATTERN.fullmatch(email):
        raise ValueError("请输入有效的邮箱地址")
    return email


def _hash(password: str, salt: bytes, iterations: int) -> str:
    digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, iterations)
    return base64.b64encode(digest).decode("ascii")


def _verify(record: dict, password: str) -> bool:
    salt = base64.b64decode(record["salt"])
    actual = _hash(password, salt, record["iterations"])
    return hmac.compare_digest(actual, record["password_hash"])


def _check_limit(client_id: str) -> list[float]:
    now = time.time()
    attempts = [when for when in _failed.get(client_id, []) if now - when < 15 * 60]
    if len(attempts) >= 5:
        raise PermissionError("登录尝试过多，请 15 分钟后再试")
    return attempts


def _record_failure(client_id: str, attempts: list[float]) -> None:
    attempts.append(time.time())
    _failed[client_id] = attempts


def register(email: str, password: str) -> dict:
    email = _email(email)
    if not 8 <= len(password) <= 128:
        raise ValueError("密码需为 8 到 128 个字符")
    with _lock:
        users = _users()
        if any(user["email"] == email for user in users):
            raise FileExistsError("该邮箱已注册，请直接登录")
        salt = secrets.token_bytes(16)
        user = {
            "id": uuid.uuid4().hex,
            "email": email,
            "salt": base64.b64encode(salt).decode("ascii"),
            "password_hash": _hash(password, salt, PASSWORD_ITERATIONS),
            "iterations": PASSWORD_ITERATIONS,
        }
        _save_users([*users, user])
        if not users and not legacy_available():
            store.claim_unowned(user["id"])
        return {"id": user["id"], "email": email}


def authenticate(email: str, password: str, client_id: str) -> dict | None:
    try:
        email = _email(email)
    except ValueError:
        return None
    with _lock:
        attempts = _check_limit(client_id)
        user = next((item for item in _users() if item["email"] == email), None)
        # Keep the password check timing similar when the email is unknown.
        dummy = {"salt": base64.b64encode(b"\0" * 16).decode("ascii"),
                 "password_hash": base64.b64encode(b"\0" * 32).decode("ascii"),
                 "iterations": PASSWORD_ITERATIONS}
        valid = _verify(user or dummy, password) and user is not None
        if valid:
            _failed.pop(client_id, None)
            return {"id": user["id"], "email": user["email"]}
        _record_failure(client_id, attempts)
        return None


def migrate_legacy(email: str, username: str, password: str, client_id: str) -> dict:
    email = _email(email)
    with _lock:
        attempts = _check_limit(client_id)
        path = _legacy_path()
        if not path.exists():
            raise ValueError("没有需要迁移的旧账号")
        legacy = json.loads(path.read_text(encoding="utf-8"))
        name_valid = hmac.compare_digest(username.strip().encode("utf-8"), legacy["username"].encode("utf-8"))
        valid = _verify(legacy, password) and name_valid
        if not valid:
            _record_failure(client_id, attempts)
            raise ValueError("旧账号用户名或密码错误")
        users = _users()
        existing = next((user for user in users if user["email"] == email), None)
        if existing:
            if existing["salt"] != legacy["salt"] or existing["password_hash"] != legacy["password_hash"]:
                raise FileExistsError("该邮箱已注册，请换一个邮箱")
            store.claim_unowned(existing["id"])
            path.unlink()
            _failed.pop(client_id, None)
            return {"id": existing["id"], "email": email}
        user = {"id": uuid.uuid4().hex, "email": email, "salt": legacy["salt"],
                "password_hash": legacy["password_hash"], "iterations": legacy["iterations"]}
        _save_users([*users, user])
        store.claim_unowned(user["id"])
        path.unlink()
        _failed.pop(client_id, None)
        return {"id": user["id"], "email": email}


def new_session(user_id: str) -> str:
    token = secrets.token_urlsafe(32)
    with _lock:
        if not any(user["id"] == user_id for user in _users()):
            raise RuntimeError("用户不存在")
        _sessions[hashlib.sha256(token.encode("ascii")).hexdigest()] = (
            time.time() + SESSION_SECONDS, user_id
        )
    return token


def session_user(token: str | None) -> dict | None:
    if not token:
        return None
    digest = hashlib.sha256(token.encode("utf-8")).hexdigest()
    with _lock:
        expiry, user_id = _sessions.get(digest, (0, ""))
        if expiry <= time.time():
            _sessions.pop(digest, None)
            return None
        user = next((item for item in _users() if item["id"] == user_id), None)
        if user is None:
            _sessions.pop(digest, None)
            return None
        return {"id": user_id, "email": user["email"]}


def revoke_session(token: str | None) -> None:
    if token:
        digest = hashlib.sha256(token.encode("utf-8")).hexdigest()
        with _lock:
            _sessions.pop(digest, None)
