"""Per-dataset activity reservations and cooperative import cancellation."""

import threading
from contextlib import contextmanager


class BusyError(RuntimeError):
    pass


class Cancelled(RuntimeError):
    pass


_lock = threading.RLock()
_states = {}


def _state(item_id):
    return _states.setdefault(item_id, {"active": 0, "deleted": False, "cancel": threading.Event(), "connections": []})


def _acquire(item_id, exclusive=False):
    with _lock:
        state = _state(item_id)
        if state["deleted"]:
            raise BusyError("数据集正在删除或已删除")
        if exclusive and state["active"]:
            raise BusyError("数据集有任务正在运行，请稍后再试")
        if not state["active"]:
            state["cancel"].clear()
        state["active"] += 1


def _release(item_id):
    with _lock:
        _state(item_id)["active"] -= 1


@contextmanager
def activity(item_id):
    _acquire(item_id)
    try:
        yield
    finally:
        _release(item_id)


def schedule(executor, item_id, function, *args, on_queue=None):
    _acquire(item_id, exclusive=True)
    def run():
        try:
            return function(item_id, *args)
        finally:
            _release(item_id)
    try:
        if on_queue:
            on_queue()
        return executor.submit(run)
    except Exception:
        _release(item_id)
        raise


def check(item_id):
    with _lock:
        cancelled = _state(item_id)["cancel"].is_set()
    if cancelled:
        raise Cancelled("导入已取消")


def cancel(item_id, on_cancel=None):
    with _lock:
        state = _state(item_id)
        if on_cancel:
            on_cancel(bool(state["active"]))
        state["cancel"].set()
        for connection in state["connections"]:
            try:
                connection.interrupt()
            except Exception:
                pass
        return bool(state["active"])


def complete(item_id, callback):
    with _lock:
        check(item_id)
        callback()


def attach(item_id, connection):
    with _lock:
        _state(item_id)["connections"].append(connection)
    check(item_id)


def detach(item_id, connection):
    with _lock:
        connections = _state(item_id)["connections"]
        if connection in connections:
            connections.remove(connection)


@contextmanager
def deletion(item_id):
    with _lock:
        state = _state(item_id)
        if state["active"] or state["deleted"]:
            raise BusyError("任务仍在运行，请先取消导入或等待任务结束，再删除数据集")
        state["deleted"] = True
    try:
        yield
    except Exception:
        with _lock:
            state["deleted"] = False
        raise
