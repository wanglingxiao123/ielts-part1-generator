"""递归镜像 QTI 2.2.4 的 XSD 依赖链到本地（49 个文件，约 3.5MB），供 ``qti_export.validate`` 用。

按 URL 路径镜像到 ``schemas/mirror/<host>/<path>``，这样 XSD 内部的相对 include
（MathML 的 common/ content/ presentation/ 等约 30 个文件）天然可解析。

用法：
    python3 -m qti_export.fetch_schemas            # 镜像到 qti_export/schemas/mirror
    QTI_XSD_MIRROR=/somewhere python3 -m qti_export.fetch_schemas

镜像目录不入版本库（.gitignore）。需要网络；在无外网的机器上可以把别处镜像好的目录
拷过来，或用 ``QTI_XSD_MIRROR`` 指向它。
"""
from __future__ import annotations

import re
import sys
import urllib.request
from pathlib import Path
from urllib.parse import urljoin, urlparse

from .validate import mirror_dir

ROOTS = [
    "https://purl.imsglobal.org/spec/qti/v2p2/schema/xsd/imsqti_v2p2p4.xsd",
    "https://purl.imsglobal.org/spec/qti/v2p2/schema/xsd/imsqtiv2p2p4_html5_v1p0.xsd",
    "https://www.imsglobal.org/xsd/qti/qtiv2p2/qtiv2p2_imscpv1p2_v1p0.xsd",
    "https://www.imsglobal.org/xsd/qti/qtiv2p2/imsqti_metadata_v2p2.xsd",
    "https://www.imsglobal.org/xsd/imsmd_loose_v1p3p2.xsd",
]

# schemaLocation="..."，同时覆盖 xs:import 与 xs:include
LOC_RE = re.compile(rb'schemaLocation\s*=\s*"([^"]+)"')

# xml.xsd 里指向 w3.org 的自引用注释块，抓不到也不需要
SKIP = ("http://www.w3.org/2001/xml.xsd", "http://www.w3.org/2009/01/xml.xsd")


def local_path(root: Path, url: str) -> Path:
    p = urlparse(url)
    return root / p.netloc / p.path.lstrip("/")


def fetch(root: Path, url: str, seen: set, stats: dict) -> None:
    if url in seen or url in SKIP:
        return
    seen.add(url)
    dest = local_path(root, url)
    if dest.exists():
        data = dest.read_bytes()
        stats["cached"] += 1
    else:
        try:
            with urllib.request.urlopen(url, timeout=60) as resp:
                data = resp.read()
        except Exception as exc:  # noqa: BLE001 - 网络失败要继续跑完其余依赖
            print(f"FAIL {url}\n     {exc}", file=sys.stderr)
            stats["failed"] += 1
            return
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(data)
        stats["downloaded"] += 1
        print(f"  + {dest.relative_to(root)}")
    for raw in LOC_RE.findall(data):
        child = urljoin(url, raw.decode("utf-8"))
        if urlparse(child).scheme in ("http", "https"):
            fetch(root, child, seen, stats)


def main() -> int:
    root = mirror_dir()
    seen: set = set()
    stats = {"downloaded": 0, "cached": 0, "failed": 0}
    for url in ROOTS:
        fetch(root, url, seen, stats)
    print(
        f"mirror={root} visited={len(seen)} downloaded={stats['downloaded']} "
        f"cached={stats['cached']} failed={stats['failed']}"
    )
    return 1 if stats["failed"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
