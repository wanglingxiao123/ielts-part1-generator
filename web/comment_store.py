"""Single-user material comments stored as one JSON document per material."""

from __future__ import annotations

import json
import re
import uuid
from datetime import datetime, timezone
from typing import Any, Dict, Optional

__all__ = [
    "CommentError",
    "CommentService",
    "CommentStore",
    "InMemoryCommentStore",
    "S3CommentStore",
    "build_comment_store",
]

COMMENT_PREFIX = "_comments/"
MAX_COMMENT_LENGTH = 4000
SEVERITIES = frozenset({"critical", "major", "minor"})
MATERIAL_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,199}$")


class CommentError(ValueError):
    def __init__(self, code: str, message: str, status: int = 400) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.status = status


def comment_key(material_id: str) -> str:
    return "%s%s.json" % (COMMENT_PREFIX, material_id)


class CommentStore:
    def load(self, material_id: str) -> Optional[Dict[str, Any]]:
        raise NotImplementedError

    def save(self, material_id: str, document: Dict[str, Any]) -> None:
        raise NotImplementedError


class InMemoryCommentStore(CommentStore):
    def __init__(self) -> None:
        self._documents: Dict[str, str] = {}

    def load(self, material_id: str) -> Optional[Dict[str, Any]]:
        raw = self._documents.get(material_id)
        return json.loads(raw) if raw is not None else None

    def save(self, material_id: str, document: Dict[str, Any]) -> None:
        self._documents[material_id] = json.dumps(document, ensure_ascii=False)


class S3CommentStore(CommentStore):
    def __init__(self, store: Any) -> None:
        self._store = store

    def load(self, material_id: str) -> Optional[Dict[str, Any]]:
        try:
            raw = self._store.get(comment_key(material_id))
        except Exception as exc:  # noqa: BLE001 - ObjectNotFound is owned by audio_storage
            if type(exc).__name__ == "ObjectNotFound":
                return None
            raise
        try:
            found = json.loads(raw.decode("utf-8") if isinstance(raw, bytes) else raw)
        except (ValueError, UnicodeDecodeError):
            return None
        return found if isinstance(found, dict) else None

    def save(self, material_id: str, document: Dict[str, Any]) -> None:
        body = json.dumps(document, ensure_ascii=False).encode("utf-8")
        self._store.put(comment_key(material_id), body)


class CommentService:
    """Validation and last-write-wins mutations over a narrow store."""

    def __init__(self, store: Optional[CommentStore] = None) -> None:
        self.store = store if store is not None else build_comment_store()

    def list(self, material_id: str) -> Dict[str, Any]:
        material_id = _material_id(material_id)
        return self._document(material_id)

    def create(self, material_id: str, payload: Any) -> Dict[str, Any]:
        material_id = _material_id(material_id)
        if not isinstance(payload, dict):
            raise CommentError("INVALID_COMMENT", "评论内容必须是 JSON 对象。")
        comment = {
            "id": str(uuid.uuid4()),
            "created_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
            "anchor": _anchor(payload.get("anchor")),
            "severity": _severity(payload.get("severity")),
            "text": _text(payload.get("text")),
        }
        document = self._document(material_id)
        document["comments"].append(comment)
        self.store.save(material_id, document)
        return document

    def delete(self, material_id: str, comment_id: str) -> Dict[str, Any]:
        material_id = _material_id(material_id)
        document = self._document(material_id)
        comments = document["comments"]
        remaining = [comment for comment in comments if comment.get("id") != comment_id]
        if len(remaining) == len(comments):
            raise CommentError("COMMENT_NOT_FOUND", "没有找到这条评论。", 404)
        document["comments"] = remaining
        self.store.save(material_id, document)
        return document

    def _document(self, material_id: str) -> Dict[str, Any]:
        found = self.store.load(material_id)
        comments = found.get("comments") if isinstance(found, dict) else None
        return {
            "material_id": material_id,
            "comments": comments if isinstance(comments, list) else [],
        }


def _material_id(value: Any) -> str:
    material_id = str(value or "").strip()
    if not MATERIAL_ID_RE.fullmatch(material_id):
        raise CommentError("INVALID_MATERIAL_ID", "材料 ID 无效。")
    return material_id


def _anchor(value: Any) -> Dict[str, Any]:
    if not isinstance(value, dict):
        raise CommentError("INVALID_ANCHOR", "请选择评论对应的题目或对话位置。")
    anchor_type = value.get("type")
    index = value.get("index")
    if isinstance(index, bool) or not isinstance(index, int):
        raise CommentError("INVALID_ANCHOR", "评论位置编号必须是整数。")
    if anchor_type == "question" and 1 <= index <= 10:
        return {"type": "question", "index": index}
    if anchor_type == "turn" and index >= 0:
        return {"type": "turn", "index": index}
    raise CommentError("INVALID_ANCHOR", "评论位置必须是 Q1-Q10 或有效的 Turn。")


def _severity(value: Any) -> str:
    if value not in SEVERITIES:
        raise CommentError("INVALID_SEVERITY", "请选择重要、一般或轻微。")
    return str(value)


def _text(value: Any) -> str:
    text = value.strip() if isinstance(value, str) else ""
    if not text:
        raise CommentError("INVALID_COMMENT", "评论内容不能为空。")
    if len(text) > MAX_COMMENT_LENGTH:
        raise CommentError("INVALID_COMMENT", "评论内容不能超过 4000 个字符。")
    return text


def build_comment_store() -> CommentStore:
    import os

    bucket = (os.environ.get("IELTS_AUDIO_BUCKET") or "").strip()
    if not bucket:
        return InMemoryCommentStore()
    from audio_storage.object_store import S3ObjectStore

    return S3CommentStore(S3ObjectStore(bucket))
