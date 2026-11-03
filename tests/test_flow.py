import io
import json
import os
import base64
import hashlib
import tempfile
import time
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

_test_data = tempfile.TemporaryDirectory()
os.environ["DATA_AGENT_DATA_DIR"] = _test_data.name

from fastapi.testclient import TestClient  # noqa: E402
from openpyxl import Workbook  # noqa: E402

from app import analysis, store, tasks  # noqa: E402
from app.main import app  # noqa: E402
from app.context import model_context  # noqa: E402


class FakeResponse:
    def __init__(self, output_text):
        self.output_text = output_text


class FakeOpenAI:
    requests = []
    def __init__(self, *args, **kwargs):
        self.responses = self

    def create(self, **kwargs):
        self.requests.append(kwargs)
        if "text" in kwargs:
            if kwargs["text"]["format"]["name"] == "dashboard_plan":
                return FakeResponse(json.dumps({
                    "title": "销售数据看板",
                    "description": "按地区汇总",
                    "kpis": [{"title": "销售总额", "sql": 'SELECT SUM("销售额") AS value FROM "data"'}],
                    "charts": [{"title": "地区销售额", "kind": "bar", "sql": 'SELECT "地区" AS label, SUM("销售额") AS value FROM "data" GROUP BY "地区" ORDER BY value DESC'}],
                }))
            return FakeResponse(json.dumps({"sql": 'SELECT "地区", SUM("销售额") AS "总额" FROM "data" GROUP BY "地区" ORDER BY "总额" DESC', "chart": "bar", "reason": "按地区汇总"}))
        return FakeResponse("华东销售额最高。")


class FlowTests(unittest.TestCase):
    def setUp(self):
        # Reuse one event loop per test and close it afterward. A fresh portal
        # for every tiny upload chunk can exhaust Windows socket resources.
        self.client = self.enterContext(TestClient(app))
        credentials = {"email": "tester@example.com", "password": "test-password-123"}
        response = self.client.post("/api/auth/register", json=credentials)
        if response.status_code == 409:
            response = self.client.post("/api/auth/login", json=credentials)
        self.assertEqual(response.status_code, 200, response.text)

    def _upload(self, name: str, content: bytes):
        start = self.client.post("/api/uploads", json={"name": name, "size": len(content), "encoding": "auto"})
        self.assertEqual(start.status_code, 200, start.text)
        item_id = start.json()["id"]
        for offset in range(0, len(content), 17):
            response = self.client.put(f"/api/uploads/{item_id}/chunk?offset={offset}", content=content[offset:offset + 17])
            self.assertEqual(response.status_code, 200, response.text)
        complete = self.client.post(f"/api/uploads/{item_id}/complete")
        self.assertEqual(complete.status_code, 200, complete.text)
        return item_id

    def _await_status(self, item_id: str, wanted: str):
        for _ in range(100):
            item = self.client.get(f"/api/datasets/{item_id}").json()
            if item["status"] == wanted:
                return item
            if item["status"] == "error":
                self.fail(item["error"])
            time.sleep(.05)
        self.fail(f"超时，最终状态：{item['status']}")

    def test_csv_upload_import_and_agent(self):
        content = "地区,销售额\n华东,120\n华北,80\n华东,30\n".encode("utf-8")
        item_id = self._upload("sales.csv", content)
        item = self._await_status(item_id, "ready")
        self.assertEqual(item["tables"][0]["rows"], 3)
        self.assertEqual(item["tables"][0]["preview"][0]["地区"], "华东")
        self.assertEqual(item["profile_status"], "ready", item.get("profile_error"))
        self.assertEqual(item["tables"][0]["profile"]["columns"][1]["max"], 120)
        with patch.object(analysis, "OpenAI", FakeOpenAI), patch.dict(os.environ, {"MODEL_PROVIDER": "openai", "OPENAI_API_KEY": "test"}):
            response = self.client.post(f"/api/datasets/{item_id}/ask", json={"question": "哪个地区销售额最高？"})
        self.assertEqual(response.status_code, 200, response.text)
        result = response.json()
        self.assertEqual(result["rows"][0]["总额"], 150)
        self.assertEqual(result["chart"], "bar")
        with patch("app.dashboard.OpenAI", FakeOpenAI), patch.dict(os.environ, {"MODEL_PROVIDER": "openai", "OPENAI_API_KEY": "test"}):
            dashboard = self.client.post(f"/api/datasets/{item_id}/dashboard", json={"focus": "销售概览"})
        self.assertEqual(dashboard.status_code, 200, dashboard.text)
        self.assertEqual(dashboard.json()["kpis"][1]["value"], 230)
        self.assertEqual(dashboard.json()["charts"][0]["rows"][0]["value"], 150)
        saved = self.client.get(f"/api/datasets/{item_id}/dashboard")
        self.assertEqual(saved.status_code, 200)
        self.assertEqual(saved.json()["title"], "销售数据看板")

    def test_excel_sheet_selection(self):
        workbook = Workbook()
        sheet = workbook.active
        sheet.title = "订单"
        sheet.append(["地区", "销售额"])
        sheet.append(["华南", 42])
        other = workbook.create_sheet("说明")
        other.append(["备注"])
        buffer = io.BytesIO()
        workbook.save(buffer)
        item_id = self._upload("book.xlsx", buffer.getvalue())
        item = self._await_status(item_id, "awaiting_sheets")
        self.assertEqual(item["sheets"], ["订单", "说明"])
        response = self.client.post(f"/api/datasets/{item_id}/import", json={"sheets": ["订单"]})
        self.assertEqual(response.status_code, 200, response.text)
        item = self._await_status(item_id, "ready")
        self.assertEqual(len(item["tables"]), 1)
        self.assertEqual(item["tables"][0]["rows"], 1)
        self.assertEqual(item["profile_status"], "ready", item.get("profile_error"))

    def test_limits_and_sql_boundary(self):
        over = self.client.post("/api/uploads", json={"name": "big.csv", "size": 1024 ** 3 + 1})
        self.assertEqual(over.status_code, 422)
        with self.assertRaises(ValueError):
            analysis._validate_sql("SELECT * FROM read_csv('secret.csv')", {"data"})
        with self.assertRaises(ValueError):
            analysis._validate_sql("DROP TABLE data", {"data"})

    def test_deepseek_configuration(self):
        with patch.dict(os.environ, {"MODEL_PROVIDER": "deepseek", "DEEPSEEK_API_KEY": "test-key", "DEEPSEEK_MODEL": "deepseek-flash"}):
            config = analysis.model_config()
        self.assertEqual(config["provider"], "DeepSeek")
        self.assertEqual(config["model"], "deepseek-flash")
        self.assertEqual(config["base_url"], "https://api.deepseek.com")

    def test_login_guards_all_data_apis_and_logout(self):
        outsider = TestClient(app)
        for method, path, kwargs in [
            ("get", "/api/health", {}),
            ("get", "/api/datasets", {}),
            ("post", "/api/uploads", {"json": {"name": "x.csv", "size": 1}}),
            ("put", "/api/uploads/unknown/chunk?offset=0", {"content": b"x"}),
            ("post", "/api/datasets/unknown/ask", {"json": {"question": "test"}}),
            ("get", "/api/datasets/unknown/dashboard", {}),
        ]:
            result = getattr(outsider, method)(path, **kwargs)
            self.assertEqual(result.status_code, 401, (method, path, result.text))
        self.assertEqual(outsider.post("/api/auth/login", json={"email": "tester@example.com", "password": "wrong-pass-123"}).status_code, 401)
        self.assertEqual(outsider.post("/api/auth/login", json={"email": "tester@example.com", "password": "test-password-123"}).status_code, 200)
        self.assertEqual(outsider.get("/api/datasets").status_code, 200)
        self.assertEqual(outsider.post("/api/auth/logout").status_code, 200)
        self.assertEqual(outsider.get("/api/datasets").status_code, 401)

    def test_rejects_cross_origin_mutations(self):
        result = self.client.post("/api/auth/logout", headers={"origin": "https://other.example"})
        self.assertEqual(result.status_code, 403)
        self.assertEqual(self.client.get("/api/datasets").status_code, 200)

    def test_login_page_and_auth_assets_are_served(self):
        page = self.client.get("/")
        self.assertEqual(page.status_code, 200)
        self.assertIn('id="authScreen"', page.text)
        self.assertIn('id="authEmail"', page.text)
        self.assertEqual(self.client.get("/auth.css").status_code, 200)
        self.assertEqual(self.client.get("/app.js").status_code, 200)
        self.assertEqual(self.client.get("/quality.js").status_code, 200)
        self.assertEqual(self.client.get("/quality.css").status_code, 200)
        self.assertEqual(self.client.get("/workspace.js").status_code, 200)
        self.assertEqual(self.client.get("/workspace.css").status_code, 200)
        self.assertEqual(self.client.get("/api/auth/status").status_code, 200)

    def test_email_registration_and_password_storage(self):
        with tempfile.TemporaryDirectory() as folder, patch.object(store, "DATA_DIR", Path(folder)), patch.object(store, "META_DIR", Path(folder) / "metadata"), patch.object(store, "_items", {}):
            (Path(folder) / "metadata").mkdir()
            old_item = store.create("before-login.csv", 1, ".csv", "auto", "")
            fresh = TestClient(app)
            self.assertFalse(fresh.get("/api/auth/status").json()["authenticated"])
            credentials = {"email": "User@Example.com", "password": "strong-password-123"}
            created = fresh.post("/api/auth/register", json=credentials)
            self.assertEqual(created.status_code, 200, created.text)
            self.assertEqual(fresh.get("/api/auth/status").json()["email"], "user@example.com")
            self.assertEqual(fresh.get(f"/api/datasets/{old_item['id']}").status_code, 200)
            saved = (Path(folder) / "users.json").read_text(encoding="utf-8")
            self.assertNotIn(credentials["password"], saved)
            self.assertEqual(fresh.post("/api/auth/register", json={"email": "user@example.com", "password": "another-password"}).status_code, 409)
            self.assertEqual(fresh.post("/api/auth/register", json={"email": "invalid", "password": "another-password"}).status_code, 400)
            fresh.post("/api/auth/logout")
            self.assertEqual(fresh.post("/api/auth/login", json=credentials).status_code, 200)

    def test_users_cannot_access_each_others_datasets(self):
        first_id = self._upload("private.csv", b"name,value\nA,1\n")
        self._await_status(first_id, "ready")
        other = TestClient(app)
        result = other.post("/api/auth/register", json={"email": "second@example.com", "password": "second-password-123"})
        if result.status_code == 409:
            result = other.post("/api/auth/login", json={"email": "second@example.com", "password": "second-password-123"})
        self.assertEqual(result.status_code, 200, result.text)
        self.assertNotIn(first_id, [item["id"] for item in other.get("/api/datasets").json()])
        second = other.post("/api/uploads", json={"name": "other.csv", "size": 1, "encoding": "auto"})
        self.assertEqual(second.status_code, 200)
        self.assertEqual(self.client.get(f"/api/datasets/{second.json()['id']}").status_code, 404)
        checks = [
            other.get(f"/api/datasets/{first_id}"),
            other.put(f"/api/uploads/{first_id}/chunk?offset=0", content=b"x"),
            other.post(f"/api/uploads/{first_id}/complete"),
            other.post(f"/api/datasets/{first_id}/import", json={"sheets": []}),
            other.post(f"/api/datasets/{first_id}/ask", json={"question": "show me"}),
            other.get(f"/api/datasets/{first_id}/dashboard"),
            other.post(f"/api/datasets/{first_id}/dashboard", json={"focus": ""}),
            other.post(f"/api/datasets/{first_id}/profile"),
            other.put(f"/api/datasets/{first_id}/dictionary", json={"description": "", "fields": [], "metrics": []}),
            other.get(f"/api/datasets/{first_id}/history"),
            other.get(f"/api/datasets/{first_id}/history/{'a' * 32}"),
            other.delete(f"/api/datasets/{first_id}"),
            other.post(f"/api/datasets/{first_id}/cancel"),
            other.put(f"/api/datasets/{first_id}/dashboard", json={"revision": "legacy", "title": "test", "charts": [{"title": "test", "source_index": 0}]}),
        ]
        self.assertTrue(all(response.status_code == 404 for response in checks), [response.status_code for response in checks])

    def test_dictionary_persists_and_reaches_question_and_dashboard(self):
        item_id = self._upload("definitions.csv", "地区,销售额\n华东,120\n华北,80\n".encode())
        self._await_status(item_id, "ready")
        dictionary = {
            "description": "门店经营数据，金额单位为元",
            "fields": [{"table": "data", "column": "销售额", "description": "已经扣除退款的实付金额"}],
            "metrics": [{"name": "净销售额", "definition": "对销售额求和，不要再次扣除退款"}],
        }
        saved = self.client.put(f"/api/datasets/{item_id}/dictionary", json=dictionary)
        self.assertEqual(saved.status_code, 200, saved.text)
        self.assertEqual(saved.json()["dictionary_version"], 1)
        reloaded = self.client.get(f"/api/datasets/{item_id}").json()
        self.assertEqual(reloaded["dictionary"], dictionary)
        persisted = json.loads((store.META_DIR / f"{item_id}.json").read_text(encoding="utf-8"))
        self.assertEqual(persisted["dictionary"], dictionary)
        self.assertNotIn("top_values", json.dumps(model_context(reloaded)))
        FakeOpenAI.requests.clear()
        with patch.object(analysis, "OpenAI", FakeOpenAI), patch("app.dashboard.OpenAI", FakeOpenAI), patch.dict(os.environ, {"MODEL_PROVIDER": "openai", "OPENAI_API_KEY": "test"}):
            answer = self.client.post(f"/api/datasets/{item_id}/ask", json={"question": "净销售额多少"})
            dashboard = self.client.post(f"/api/datasets/{item_id}/dashboard", json={"focus": "净销售额"})
        self.assertEqual(answer.status_code, 200, answer.text)
        self.assertEqual(dashboard.status_code, 200, dashboard.text)
        self.assertEqual(dashboard.json()["dictionary_version"], 1)
        plans = [request for request in FakeOpenAI.requests if "text" in request]
        self.assertEqual(len(plans), 2)
        for plan in plans:
            self.assertIn("已经扣除退款的实付金额", plan["input"])
            self.assertIn("不要再次扣除退款", plan["input"])
        for request in FakeOpenAI.requests:
            self.assertIn("不要再次扣除退款", request["input"])
        bad = {**dictionary, "fields": [{"table": "data", "column": "不存在", "description": "test"}]}
        self.assertEqual(self.client.put(f"/api/datasets/{item_id}/dictionary", json=bad).status_code, 400)
        self.assertEqual(store.get(item_id)["dictionary_version"], 1)
        duplicate = {**dictionary, "metrics": dictionary["metrics"] * 2}
        self.assertEqual(self.client.put(f"/api/datasets/{item_id}/dictionary", json=duplicate).status_code, 400)
        # Existing datasets can generate profiles without importing the file again.
        tables = [{key: value for key, value in table.items() if key != "profile"} for table in reloaded["tables"]]
        store.update(item_id, tables=tables, profile_status=None)
        result = self.client.post(f"/api/datasets/{item_id}/profile")
        self.assertEqual(result.status_code, 200)
        for _ in range(100):
            item = self.client.get(f"/api/datasets/{item_id}").json()
            if item.get("profile_status") in {"ready", "error"}:
                break
            time.sleep(.05)
        self.assertEqual(item["profile_status"], "ready", item.get("profile_error"))
        self.assertEqual(item["dictionary"], dictionary)

    def test_profile_failure_does_not_break_import(self):
        with patch("app.ingest.profile_tables", side_effect=RuntimeError("profile timeout")):
            item_id = self._upload("profile-failure.csv", b"name,value\nA,1\n")
            item = self._await_status(item_id, "ready")
        self.assertEqual(item["profile_status"], "error")
        self.assertEqual(item["tables"][0]["rows"], 1)
        self.assertIn("profile timeout", item["profile_error"])

    def test_history_followup_and_dataset_scope(self):
        item_id = self._upload("history.csv", "地区,销售额\n华东,120\n华北,80\n".encode())
        self._await_status(item_id, "ready")
        FakeOpenAI.requests.clear()
        with patch.object(analysis, "OpenAI", FakeOpenAI), patch.dict(os.environ, {"MODEL_PROVIDER": "openai", "OPENAI_API_KEY": "test"}):
            first = self.client.post(f"/api/datasets/{item_id}/ask", json={"question": "各地区销售额多少？"}).json()
            second = self.client.post(f"/api/datasets/{item_id}/ask", json={"question": "那华东呢？", "parent_id": first["history_id"]})
            self.assertEqual(second.status_code, 200, second.text)
            self.assertIn("各地区销售额多少？", FakeOpenAI.requests[-2]["input"])
            self.assertIn(first["sql"], FakeOpenAI.requests[-2]["input"].replace('\\"', '"'))
        records = self.client.get(f"/api/datasets/{item_id}/history?limit=1").json()
        self.assertEqual((records["total"], records["has_more"]), (2, True))
        loaded = self.client.get(f"/api/datasets/{item_id}/history/{second.json()['history_id']}").json()
        self.assertEqual(loaded["parent_id"], first["history_id"])
        self.assertEqual(loaded["rows"], second.json()["rows"])
        other_id = self._upload("another.csv", "地区,销售额\n华东,5\n".encode())
        self._await_status(other_id, "ready")
        with patch.dict(os.environ, {"MODEL_PROVIDER": "openai", "OPENAI_API_KEY": "test"}):
            result = self.client.post(f"/api/datasets/{other_id}/ask", json={"question": "那华东呢？", "parent_id": first["history_id"]})
        self.assertEqual(result.status_code, 400)

    def test_cancel_import_then_retry_and_block_deletion_during_work(self):
        from app.ingest import _transcode
        started, release = threading.Event(), threading.Event()
        def slow_transcode(path, target, encoding, item_id):
            started.set()
            while not release.wait(.01):
                tasks.check(item_id)
            tasks.check(item_id)
            return _transcode(path, target, encoding, item_id)
        with patch("app.ingest._transcode", slow_transcode):
            item_id = self._upload("cancel.csv", b"name,value\nA,1\n")
            self.assertTrue(started.wait(2))
            try:
                self.assertEqual(self.client.delete(f"/api/datasets/{item_id}").status_code, 409)
                self.assertEqual(self.client.post(f"/api/datasets/{item_id}/cancel").status_code, 200)
            finally:
                release.set()
            item = self._await_status(item_id, "cancelled")
        for _ in range(100):
            if tasks._states[item_id]["active"] == 0:
                break
            time.sleep(.01)
        self.assertTrue(store.upload_path(item_id, ".csv").exists())
        self.assertFalse(store.db_path(item_id).exists())
        self.assertFalse((store.TEMP_DIR / item_id).exists())
        self.assertEqual(self.client.post(f"/api/datasets/{item_id}/import", json={"sheets": []}).status_code, 200)
        self._await_status(item_id, "ready")

    def test_delete_cleans_only_the_owned_dataset_and_all_artifacts(self):
        item_id = self._upload("remove.csv", "地区,销售额\n华东,120\n".encode())
        self._await_status(item_id, "ready")
        other_id = self._upload("keep.csv", b"name,value\nB,2\n")
        self._await_status(other_id, "ready")
        with patch.object(analysis, "OpenAI", FakeOpenAI), patch("app.dashboard.OpenAI", FakeOpenAI), patch.dict(os.environ, {"MODEL_PROVIDER": "openai", "OPENAI_API_KEY": "test"}):
            self.assertEqual(self.client.post(f"/api/datasets/{item_id}/ask", json={"question": "销售额多少"}).status_code, 200)
            self.assertEqual(self.client.post(f"/api/datasets/{item_id}/dashboard", json={"focus": "销售"}).status_code, 200)
        temp = store.TEMP_DIR / item_id
        temp.mkdir(exist_ok=True); (temp / "leftover.csv").write_text("test")
        paths = store._dataset_paths(store.get(item_id))
        response = self.client.delete(f"/api/datasets/{item_id}")
        self.assertEqual(response.status_code, 200, response.text)
        self.assertGreater(response.json()["released_bytes"], 0)
        self.assertTrue(all(not path.exists() for path in paths))
        self.assertEqual(self.client.get(f"/api/datasets/{item_id}").status_code, 404)
        self.assertEqual(self.client.get(f"/api/datasets/{other_id}").status_code, 200)

    def test_dashboard_edit_recomputes_filters_and_checks_revision(self):
        item_id = self._upload("edit.csv", "地区,销售额,日期\n华东,120,2026-01-01\n华北,80,2026-02-01\n华东,30,2026-01-02\n".encode())
        self._await_status(item_id, "ready")
        with patch("app.dashboard.OpenAI", FakeOpenAI), patch.dict(os.environ, {"MODEL_PROVIDER": "openai", "OPENAI_API_KEY": "test"}):
            generated = self.client.post(f"/api/datasets/{item_id}/dashboard", json={"focus": "销售"}).json()
        query = {"table": "data", "column": "销售额", "aggregate": "sum", "dimension": "地区", "filters": [{"column": "地区", "op": "eq", "value": "华东"}]}
        payload = {"revision": generated["revision"], "title": "华东经营看板", "description": "筛选华东", "kpis": [{"title": "华东销售", "query": {key: value for key, value in query.items() if key != "dimension"}}], "charts": [{"source_index": 0, "title": "华东销售分布", "kind": "pie", "query": query}]}
        response = self.client.put(f"/api/datasets/{item_id}/dashboard", json=payload)
        self.assertEqual(response.status_code, 200, response.text)
        edited = response.json()
        self.assertEqual(edited["kpis"][0]["value"], 150)
        self.assertEqual(edited["charts"][0]["rows"], [{"label": "华东", "value": 150.0}])
        self.assertEqual(edited["charts"][0]["parameters"], ["华东"])
        self.assertNotEqual(edited["revision"], generated["revision"])
        self.assertEqual(self.client.put(f"/api/datasets/{item_id}/dashboard", json=payload).status_code, 409)
        payload["revision"] = edited["revision"]
        query["filters"][0]["value"] = "华东' OR 1=1 --"
        self.assertEqual(self.client.put(f"/api/datasets/{item_id}/dashboard", json=payload).status_code, 400)
        self.assertEqual(self.client.get(f"/api/datasets/{item_id}/dashboard").json()["revision"], edited["revision"])
        query.update(dimension="日期", grain="month", filters=[])
        payload["charts"][0]["kind"] = "area"
        payload["kpis"] = []
        dated = self.client.put(f"/api/datasets/{item_id}/dashboard", json=payload)
        self.assertEqual(dated.status_code, 200, dated.text)
        self.assertEqual(len(dated.json()["charts"][0]["rows"]), 2)

    def test_disk_preflight_and_partial_upload_resume(self):
        from collections import namedtuple
        usage = namedtuple("Usage", "total used free")(100, 99, 1)
        with patch("app.store.shutil.disk_usage", return_value=usage):
            self.assertEqual(self.client.post("/api/uploads", json={"name": "full.csv", "size": 20}).status_code, 507)
        content = b"name,value\nA,1\n"
        item_id = self.client.post("/api/uploads", json={"name": "resume.csv", "size": len(content)}).json()["id"]
        self.assertEqual(self.client.put(f"/api/uploads/{item_id}/chunk?offset=0", content=content[:5]).status_code, 200)
        self.assertEqual(self.client.put(f"/api/uploads/{item_id}/chunk?offset=0", content=b"wrong").status_code, 409)
        offset = self.client.get(f"/api/datasets/{item_id}").json()["received"]
        self.assertEqual(offset, 5)
        self.assertEqual(self.client.put(f"/api/uploads/{item_id}/chunk?offset={offset}", content=content[offset:]).status_code, 200)
        self.assertEqual(self.client.post(f"/api/uploads/{item_id}/complete").status_code, 200)
        self._await_status(item_id, "ready")

    def test_old_account_can_bind_email_and_keep_data(self):
        with tempfile.TemporaryDirectory() as folder, patch.object(store, "DATA_DIR", Path(folder)), patch.object(store, "META_DIR", Path(folder) / "metadata"), patch.object(store, "_items", {}):
            (Path(folder) / "metadata").mkdir()
            salt = b"legacy-test-salt"
            password = "legacy-password-123"
            legacy = {
                "username": "old-user", "salt": base64.b64encode(salt).decode("ascii"),
                "password_hash": base64.b64encode(hashlib.pbkdf2_hmac("sha256", password.encode(), salt, 1000)).decode("ascii"),
                "iterations": 1000,
            }
            (Path(folder) / "auth.json").write_text(json.dumps(legacy), encoding="utf-8")
            item = store.create("old.csv", 1, ".csv", "auto", "")
            fresh = TestClient(app)
            self.assertTrue(fresh.get("/api/auth/status").json()["legacy_available"])
            payload = {"email": "old@example.com", "username": "old-user", "password": password}
            self.assertEqual(fresh.post("/api/auth/migrate", json=payload).status_code, 200)
            self.assertEqual(fresh.get(f"/api/datasets/{item['id']}").status_code, 200)
            self.assertFalse((Path(folder) / "auth.json").exists())


if __name__ == "__main__":
    unittest.main()
