"""把存储里的两种题目文档形状收敛成 emitter 的输入，并给出一次导出的结果对象。

存储里有两种形状：

* ``_questions/{material_id}.json`` —— Runtime 交付的原始题目包（``QuestionResult.as_dict()``）：
  顶层就是 ``ok`` / ``status`` / ``package`` / ``review`` / ``cross_check`` / ``validation`` /
  ``advisories``。这正是准入门禁读的形状。
* ``_question_versions/{material_id}/versions/{id}.json`` —— 批注修订产出的不可变版本：
  ``package`` 在顶层，而审核结论收在 ``quality`` 下（``label`` / ``review`` / ``cross_check`` /
  ``validation`` / ``status``），没有 ``ok`` 也没有 ``advisories``。

``normalize_document`` 把第二种翻成第一种，之后两者走同一条门禁。翻译而不是给门禁开旁路：
修订版本同样要满足「两侧答案一致、题号闭合、字数上限自洽」，否则导出去的包会把一个未经
互证的答案写进 ``correctResponse``。
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, List, Optional

from . import emitter
from .emitter import ExportBundle, GateFailure, LayoutNotImplemented

__all__ = [
    "ExportRejected",
    "ExportBundle",
    "export_document",
    "normalize_document",
]


class ExportRejected(ValueError):
    """这份题目包不能导出，``reasons`` 逐条说明为什么。"""

    def __init__(self, reasons: List[str]) -> None:
        self.reasons = list(reasons)
        super().__init__("；".join(self.reasons))


def normalize_document(document: Dict[str, Any]) -> Dict[str, Any]:
    """把版本文档（``package`` + ``quality``）翻成原始交付文档的形状；原始文档原样返回。

    判据是「有 ``quality`` 且顶层没有 ``review``」：原始文档的 ``review`` 在顶层，版本文档的
    在 ``quality`` 里。两者都没有时原样返回，交给门禁去报 G1。
    """
    if not isinstance(document, dict):
        return {}
    quality = document.get("quality")
    if not isinstance(quality, dict) or isinstance(document.get("review"), dict):
        return document
    # 版本只在通过完整质量检查后才会被写下（manual_question_revision.py 的 storing 阶段），
    # 所以 status=ready 就是「上游说可交付」。这里不替它把 ok 定成 True 以外的任何值来绕门禁：
    # G1 仍会逐项读 review / validation / cross_check。
    ready = str(document.get("status") or "") == "ready"
    return {
        "label": document.get("id") or quality.get("label"),
        "ok": ready,
        "status": quality.get("status"),
        "package": document.get("package"),
        "review": quality.get("review"),
        "cross_check": quality.get("cross_check"),
        "validation": quality.get("validation"),
        "advisories": list(document.get("baseline_advisories") or []),
    }


@dataclass
class ExportSummary:
    """给页面看的一眼概览，不含 XML。"""

    material_id: str
    version_ordinal: int
    item_identifier: str
    filename: str
    questions: int
    accept_entries: int
    reject_entries: int
    review: List[str]

    def as_dict(self) -> Dict[str, Any]:
        return {
            "material_id": self.material_id,
            "version_ordinal": self.version_ordinal,
            "item_identifier": self.item_identifier,
            "filename": self.filename,
            "questions": self.questions,
            "accept_entries": self.accept_entries,
            "reject_entries": self.reject_entries,
            "review": list(self.review),
        }


def summarize(bundle: ExportBundle) -> ExportSummary:
    mat = bundle.material
    return ExportSummary(
        material_id=mat.slug,
        version_ordinal=mat.version_ordinal,
        item_identifier=mat.item_identifier,
        filename=bundle.zip_filename,
        questions=len(mat.gaps),
        accept_entries=sum(len(g.accept) for g in mat.gaps),
        reject_entries=sum(len(g.reject) for g in mat.gaps),
        review=list(bundle.review),
    )


def export_document(
    document: Dict[str, Any],
    *,
    material_id: str,
    version_ordinal: int = 1,
    raw: Optional[bytes] = None,
) -> ExportBundle:
    """存储里的任一形状 → 内容包。失败一律抛 ``ExportRejected``，原因逐条列出。"""
    doc = normalize_document(document)
    if not isinstance(doc.get("package"), dict):
        raise ExportRejected(["这份文档里没有题目包（package）"])
    try:
        return emitter.render(doc, slug=material_id, version_ordinal=version_ordinal, raw=raw)
    except GateFailure as exc:
        raise ExportRejected(list(exc.failures)) from exc
    except LayoutNotImplemented as exc:
        raise ExportRejected([str(exc)]) from exc
    except ValueError as exc:
        raise ExportRejected([str(exc)]) from exc
