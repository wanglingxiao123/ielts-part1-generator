"""把已交付的题目包导出成 QTI 2.2.4 内容包（IMS Content Package + assessmentItem）。

对外只有三样东西：

    from qti_export import export_document, ExportRejected, ExportBundle

    bundle = export_document(stored_doc, material_id="20260808-booking-hotel-45425df4",
                             version_ordinal=1)
    bundle.item_xml       # assessmentItem，对 imsqti_v2p2p4.xsd 校验通过
    bundle.manifest_xml   # imsmanifest.xml，对 qtiv2p2_imscpv1p2_v1p0.xsd 校验通过
    bundle.zip_bytes()    # 交给下游导入端的 zip

``web/app.py`` 的 ``GET /api/material-qti/{material_id}`` 是它的 HTTP 外壳；
``python3 -m qti_export`` 是离线 CLI。规则与字段映射见 ``qti_export/README.md``。

只依赖标准库。XSD 校验（``qti_export.validate``）需要 lxml，只在测试与 CLI 里用，
Web 镜像不装它。
"""
from .emitter import ExportBundle, GateFailure, LayoutNotImplemented
from .service import ExportRejected, ExportSummary, export_document, normalize_document, summarize

__all__ = [
    "ExportBundle",
    "ExportRejected",
    "ExportSummary",
    "GateFailure",
    "LayoutNotImplemented",
    "export_document",
    "normalize_document",
    "summarize",
]
