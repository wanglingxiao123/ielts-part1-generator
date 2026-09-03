"""离线 CLI：把一份题目包 JSON 转成 QTI 2.2.4 内容包。

    python3 -m qti_export path/to/20260808-booking-hotel-45425df4.json -o build/
    python3 -m qti_export input.json -o build/ --validate        # 附带 XSD 校验
    python3 -m qti_export input.json --material-id 20260808-booking-hotel-45425df4

材料 id 默认取文件名（去 ``.json``）：``_questions/`` 下的对象键就是 ``{material_id}.json``，
从 S3 同步下来的文件名即材料 id。``package.material_id`` **不能**顶替它 —— 那个字段实测常是
自由文本（"Test 1 Part 1"），既不唯一也不是合法标识符。

退出码：0 成功；1 门禁未通过 / 版式未实现；2 参数或环境问题（如 --validate 但无镜像）。
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from . import validate
from .service import ExportRejected, export_document, summarize


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="python3 -m qti_export", description="题目包 JSON → QTI 2.2.4 内容包")
    ap.add_argument("input", type=Path, help="题目包 JSON（_questions/{material_id}.json 的形状）")
    ap.add_argument("-o", "--out", type=Path, default=Path("build"), help="输出目录（默认 build）")
    ap.add_argument("--material-id", default="", help="材料 id；默认取输入文件名")
    ap.add_argument("--version", type=int, default=1, help="题目版本序号，进标识符（默认 1）")
    ap.add_argument("--validate", action="store_true", help="对本地 XSD 镜像校验 item 与 manifest")
    ap.add_argument("--no-zip", action="store_true", help="只展开产物目录，不打 zip")
    args = ap.parse_args(argv)

    raw = args.input.read_bytes()
    doc = json.loads(raw)
    material_id = args.material_id or args.input.name.removesuffix(".json")

    try:
        bundle = export_document(doc, material_id=material_id, version_ordinal=args.version, raw=raw)
    except ExportRejected as exc:
        print(f"不能导出 {material_id}：", file=sys.stderr)
        for reason in exc.reasons:
            print(f"  - {reason}", file=sys.stderr)
        return 1

    out: Path = args.out
    for arc, data in bundle.entries():
        path = out / arc
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        print(f"  + {path}")
    if not args.no_zip:
        zip_path = out / bundle.zip_filename
        zip_path.write_bytes(bundle.zip_bytes())
        print(f"  + {zip_path}")

    summary = summarize(bundle)
    print(
        f"{material_id} v{summary.version_ordinal}: {summary.questions} 道题，"
        f"接受集共 {summary.accept_entries} 条，自动拒绝集共 {summary.reject_entries} 条"
    )
    if summary.review:
        print(f"待人工确认 {len(summary.review)} 项 → {out / 'review.txt'}", file=sys.stderr)

    if args.validate:
        if not validate.available():
            print(validate.describe(), file=sys.stderr)
            return 2
        errors = validate.validate_bundle(bundle)
        if errors:
            print("XSD 校验失败：", file=sys.stderr)
            for line in errors:
                print(f"  {line}", file=sys.stderr)
            return 1
        print("XSD 校验通过（item + manifest）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
