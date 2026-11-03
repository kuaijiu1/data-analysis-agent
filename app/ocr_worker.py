"""Isolated OCR worker, so cancellation and timeouts can stop image inference."""

import json
import sys
from pathlib import Path


def main():
    from PIL import Image
    from rapidocr_onnxruntime import RapidOCR

    path, output = map(Path, sys.argv[1:3])
    with Image.open(path) as image:
        if image.width * image.height > 20_000_000:
            raise ValueError("图片超过 2000 万像素")
        image.verify()
    engine = RapidOCR(intra_op_num_threads=2, inter_op_num_threads=1)
    result, _ = engine(str(path))
    lines = [{"text": str(line[1]), "confidence": round(float(line[2]), 4)} for line in (result or [])]
    output.write_text(json.dumps(lines, ensure_ascii=False), encoding="utf-8")


if __name__ == "__main__":
    try:
        main()
    except ImportError:
        Path(sys.argv[2]).write_text(json.dumps({"error": "缺少本机 OCR 依赖，请安装 requirements.txt 中的依赖。"}, ensure_ascii=False), encoding="utf-8")
        raise SystemExit(1)
    except Exception as exc:
        message = str(exc) if isinstance(exc, ValueError) else "图片格式不支持或 OCR 无法解码，请换成清晰的 PNG/JPEG 图片。"
        Path(sys.argv[2]).write_text(json.dumps({"error": message}, ensure_ascii=False), encoding="utf-8")
        raise SystemExit(1)
