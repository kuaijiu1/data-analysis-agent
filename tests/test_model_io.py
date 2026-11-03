import json
import unittest
from types import SimpleNamespace
from unittest.mock import Mock

from app.analysis import PLAN_SCHEMA
from app.model_io import request_json, request_text


class ModelOutputTests(unittest.TestCase):
    def test_empty_plan_retries_and_validates_shape(self):
        client = Mock()
        plan = {"sql": "SELECT 1", "chart": "none", "reason": "test"}
        client.responses.create.side_effect = [SimpleNamespace(output_text=""), SimpleNamespace(output_text=json.dumps(plan))]
        self.assertEqual(request_json(client, "test", "plan", "question", {"schema": PLAN_SCHEMA}), plan)
        self.assertEqual(client.responses.create.call_count, 2)

    def test_bad_schema_after_retry_has_clear_error(self):
        client = Mock()
        client.responses.create.return_value = SimpleNamespace(output_text='{"sql": "SELECT 1"}')
        with self.assertRaisesRegex(RuntimeError, "已自动重试一次"):
            request_json(client, "test", "plan", "question", {"schema": PLAN_SCHEMA})

    def test_empty_summary_retries_without_saving_empty_answer(self):
        client = Mock()
        client.responses.create.side_effect = [SimpleNamespace(output_text=" "), SimpleNamespace(output_text="退货期限为7天")]
        self.assertEqual(request_text(client, "test", "answer", "question"), "退货期限为7天")
