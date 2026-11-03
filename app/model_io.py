"""Validate model outputs and retry empty or malformed structured replies once."""

import json


def _valid(value, schema):
    kind = schema.get("type")
    if kind == "object":
        if not isinstance(value, dict) or any(key not in value for key in schema.get("required", [])):
            return False
        return all(_valid(value[key], child) for key, child in schema.get("properties", {}).items() if key in value)
    if kind == "array":
        return isinstance(value, list) and all(_valid(child, schema["items"]) for child in value)
    if kind == "string":
        return isinstance(value, str) and ("enum" not in schema or value in schema["enum"])
    return True


def request_json(client, model, instructions, input_text, output_format, *, max_attempts=2):
    schema = output_format["schema"]
    instructions += "只返回一个 JSON 对象，不要 Markdown 代码块；必须符合这个 JSON Schema：" + json.dumps(schema, ensure_ascii=False)
    for attempt in range(max_attempts):
        response = client.responses.create(model=model, instructions=instructions,
                                           input=input_text, text={"format": output_format})
        raw = (response.output_text or "").strip()
        if raw.startswith("```") and raw.endswith("```"):
            raw = raw.split("\n", 1)[-1].rsplit("```", 1)[0].strip()
        try:
            result = json.loads(raw)
            if _valid(result, schema):
                return result
        except (ValueError, TypeError):
            pass
        instructions += "上次返回为空或格式不正确，请重新生成完整 JSON，包含全部必填字段。"
    retry_notice = "已自动重试一次，" if max_attempts > 1 else ""
    raise RuntimeError(f"模型没有返回完整、有效的分析计划，{retry_notice}请稍后再试或缩小问题范围。")


def request_text(client, model, instructions, input_text):
    for _ in range(2):
        response = client.responses.create(model=model, instructions=instructions, input=input_text)
        if response.output_text and response.output_text.strip():
            return response.output_text.strip()
    raise RuntimeError("模型没有返回文字回答，已自动重试一次，请稍后重试。")
