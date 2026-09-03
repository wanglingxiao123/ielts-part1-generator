"""对 QTI 2.2.4 官方 XSD 校验导出产物。测试与 CLI 用；Web 运行时不依赖它。

为什么必须走本地镜像：libxml2（lxml / xmllint）不支持 HTTPS，而 QTI 2.2.4 的 ``xs:import``
全指向 ``https://``。不镜像就会 "failed to load external entity"，并把未解析的
``xml:lang`` / MathML / SSML 引用报成 parser error，校验根本跑不起来。

镜像目录的查找顺序：

1. 环境变量 ``QTI_XSD_MIRROR``
2. ``qti_export/schemas/mirror``（由 ``python3 -m qti_export.fetch_schemas`` 生成，不入库）

两处都没有时 ``available()`` 为 False，测试据此 skip 而不是 fail —— 缺镜像是环境问题，
不是产物问题；但 CI 若要把 XSD 校验当门禁，就应先跑 fetch_schemas。
"""
from __future__ import annotations

import os
from functools import lru_cache
from pathlib import Path
from typing import List, Optional
from urllib.parse import urlparse

HERE = Path(__file__).resolve().parent
DEFAULT_MIRROR = HERE / "schemas" / "mirror"

QTI_XSD_REL = "purl.imsglobal.org/spec/qti/v2p2/schema/xsd/imsqti_v2p2p4.xsd"
CP_XSD_REL = "www.imsglobal.org/xsd/qti/qtiv2p2/qtiv2p2_imscpv1p2_v1p0.xsd"


def mirror_dir() -> Path:
    configured = (os.environ.get("QTI_XSD_MIRROR") or "").strip()
    return Path(configured) if configured else DEFAULT_MIRROR


def available() -> bool:
    """本地镜像与 lxml 都在。"""
    try:
        import lxml.etree  # noqa: F401
    except ImportError:
        return False
    root = mirror_dir()
    return (root / QTI_XSD_REL).exists() and (root / CP_XSD_REL).exists()


class _MirrorResolver:
    """把 ``http(s)://host/path`` 重写到 ``<mirror>/host/path``。lxml 自定义 resolver。"""

    def __init__(self, root: Path) -> None:
        from lxml import etree

        self._root = root

        class Resolver(etree.Resolver):
            def resolve(self, url, pubid, context):  # noqa: ARG002 - lxml 回调签名
                parsed = urlparse(url or "")
                if parsed.scheme in ("http", "https"):
                    local = root / parsed.netloc / parsed.path.lstrip("/")
                    if local.exists():
                        return self.resolve_filename(str(local), context)
                return None

        self.resolver = Resolver()


@lru_cache(maxsize=4)
def _schema(rel: str):
    from lxml import etree

    root = mirror_dir()
    parser = etree.XMLParser()
    parser.resolvers.add(_MirrorResolver(root).resolver)
    doc = etree.parse(str(root / rel), parser)
    return etree.XMLSchema(doc)


def _validate(rel: str, xml: bytes) -> List[str]:
    from lxml import etree

    schema = _schema(rel)
    doc = etree.fromstring(xml)
    if schema.validate(doc):
        return []
    return [f"line {e.line}: {e.message}" for e in schema.error_log]


def validate_item(xml: bytes) -> List[str]:
    """assessmentItem 对 imsqti_v2p2p4.xsd。返回错误行；空列表即通过。"""
    return _validate(QTI_XSD_REL, xml)


def validate_manifest(xml: bytes) -> List[str]:
    """imsmanifest.xml 对 qtiv2p2_imscpv1p2_v1p0.xsd。"""
    return _validate(CP_XSD_REL, xml)


def validate_bundle(bundle) -> List[str]:
    """一次导出的两个 XML 都校验，错误行带上文件名前缀。"""
    errors = [f"{bundle.item_arcname}: {e}" for e in validate_item(bundle.item_xml)]
    errors += [f"imsmanifest.xml: {e}" for e in validate_manifest(bundle.manifest_xml)]
    return errors


def describe() -> Optional[str]:
    """给 CLI 打印的一句话：镜像在哪，或者为什么不可用。"""
    if available():
        return f"XSD 镜像：{mirror_dir()}"
    try:
        import lxml  # noqa: F401
    except ImportError:
        return "lxml 未安装，无法做 XSD 校验（pip install lxml）"
    return f"XSD 镜像缺失：{mirror_dir()}（先运行 python3 -m qti_export.fetch_schemas）"
