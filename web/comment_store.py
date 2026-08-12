"""Single-user material comments stored as one JSON document per material."""

from __future__ import annotations

import json
import re
import threading
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
QUESTION_COMMENT_STATUSES = frozenset({
    "open", "resolved", "no_change", "needs_replan", "needs_material",
})
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
        self._lock = threading.RLock()

    def list(self, material_id: str) -> Dict[str, Any]:
        material_id = _material_id(material_id)
        with self._lock:
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
        if comment["anchor"]["type"] == "question":
            comment.update({
                "version_id": _version_id(payload.get("version_id")),
                "status": "open",
            })
        with self._lock:
            document = self._document(material_id)
            document["comments"].append(comment)
            self.store.save(material_id, document)
            return document

    def delete(self, material_id: str, comment_id: str) -> Dict[str, Any]:
        material_id = _material_id(material_id)
        with self._lock:
            document = self._document(material_id)
            comments = document["comments"]
            found = next((row for row in comments if row.get("id") == comment_id), None)
            if found is None:
                raise CommentError("COMMENT_NOT_FOUND", "没有找到这条评论。", 404)
            if (found.get("anchor") or {}).get("type") == "question" \
                    and found.get("status") != "open":
                raise CommentError(
                    "COMMENT_READ_ONLY", "已处理的题目批注只能查看，不能删除。", 409)
            document["comments"] = [
                comment for comment in comments if comment.get("id") != comment_id
            ]
            self.store.save(material_id, document)
            return document

    def settle_revision(
        self,
        material_id: str,
        *,
        comment_ids: list[str],
        base_version_id: str,
        request_id: str,
        outcome: str,
        resolved_by_version_id: Optional[str] = None,
        reasons: Optional[list[Dict[str, Any]]] = None,
        from_statuses: Optional[list[str]] = None,
    ) -> Dict[str, Any]:
        """Idempotently settle snapshotted comments from the allowed prior states."""
        material_id = _material_id(material_id)
        if outcome not in {"resolved", "no_change", "needs_replan", "needs_material"}:
            raise CommentError("INVALID_COMMENT_STATUS", "批注处理状态无效。")
        wanted = {str(value) for value in comment_ids if str(value)}
        allowed_statuses = set(from_statuses or ["open"])
        reason_by_id = {
            str(row.get("comment_id")): row
            for row in (reasons or [])
            if isinstance(row, dict) and str(row.get("comment_id") or "")
        }
        with self._lock:
            document = self._document(material_id)
            changed = False
            now = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
            for comment in document["comments"]:
                if (comment.get("id") not in wanted
                        or (comment.get("anchor") or {}).get("type") != "question"
                        or comment.get("version_id") != base_version_id
                        or comment.get("status") not in allowed_statuses):
                    continue
                comment.update({
                    "status": outcome,
                    "revision_request_id": request_id,
                    "resolved_at": now,
                })
                if outcome == "resolved" and resolved_by_version_id:
                    comment["resolved_by_version_id"] = resolved_by_version_id
                reason = reason_by_id.get(str(comment.get("id")))
                if reason:
                    comment["decision_reason"] = str(reason.get("reason") or "")
                    comment["decision_references"] = [
                        str(value) for value in reason.get("references") or []
                        if isinstance(value, str)
                    ]
                changed = True
            if changed:
                self.store.save(material_id, document)
            return document

    def _document(self, material_id: str) -> Dict[str, Any]:
        found = self.store.load(material_id)
        comments = found.get("comments") if isinstance(found, dict) else None
        projected = []
        for row in comments if isinstance(comments, list) else []:
            if not isinstance(row, dict):
                continue
            comment = dict(row)
            if (comment.get("anchor") or {}).get("type") == "question":
                if not isinstance(comment.get("version_id"), str):
                    comment["version_id"] = "original"
                if comment.get("status") not in QUESTION_COMMENT_STATUSES:
                    comment["status"] = "open"
            projected.append(comment)
        return {
            "material_id": material_id,
            "comments": projected,
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


def _version_id(value: Any) -> str:
    version_id = str(value or "original").strip()
    if not MATERIAL_ID_RE.fullmatch(version_id):
        raise CommentError("INVALID_VERSION_ID", "题目版本 ID 无效。")
    return version_id


def build_comment_store() -> CommentStore:
    import os

    bucket = (os.environ.get("IELTS_AUDIO_BUCKET") or "").strip()
    if not bucket:
        return InMemoryCommentStore()
    from audio_storage.object_store import S3ObjectStore

    return S3CommentStore(S3ObjectStore(bucket))
