"""Question-version projection and active-version pointer over shared object storage."""

from __future__ import annotations

import json
import threading
import uuid
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

QUESTION_PREFIX = "_questions/"
QUESTION_VERSION_PREFIX = "_question_versions/"
QUESTION_REVISION_PREFIX = "_question_revisions/"
MATERIAL_ID_RE = __import__("re").compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,199}$")
RUNNING_TTL_SECONDS = 2 * 60 * 60


class QuestionVersionError(ValueError):
    def __init__(self, code: str, message: str, status: int = 400) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.status = status


class QuestionVersionService:
    def __init__(self, store: Any) -> None:
        self.store = store
        self._lock = threading.Lock()

    def list(self, material_id: str) -> Dict[str, Any]:
        material_id = _material_id(material_id)
        versions: List[Dict[str, Any]] = []
        original = self._read("%s%s.json" % (QUESTION_PREFIX, material_id))
        original_package = original.get("package") if isinstance(original, dict) else None
        if isinstance(original_package, dict):
            versions.append({
                "id": "original",
                "material_id": material_id,
                "created_at": "",
                "based_on_version_id": None,
                "source_comment_ids": [],
                "status": "original",
                "package": original_package,
            })
        prefix = "%s%s/versions/" % (QUESTION_VERSION_PREFIX, material_id)
        for key in self.store.list_keys(prefix):
            found = self._read(key)
            if (isinstance(found, dict) and isinstance(found.get("package"), dict)
                    and found.get("id")):
                versions.append(found)
        versions.sort(key=lambda row: (
            0 if row.get("id") == "original" else 1,
            str(row.get("created_at") or ""),
            str(row.get("id") or ""),
        ))
        active_doc = self._read("%s%s/active.json" % (QUESTION_VERSION_PREFIX, material_id))
        active_id = str((active_doc or {}).get("version_id") or "original")
        ids = {str(row["id"]) for row in versions}
        if active_id not in ids:
            active_id = (
                "original" if "original" in ids
                else (str(versions[0]["id"]) if versions else "")
            )
        projected = []
        for ordinal, row in enumerate(versions, 1):
            item = dict(row)
            item["ordinal"] = ordinal
            item["is_active"] = str(row["id"]) == active_id
            projected.append(item)
        running = self._running_request(material_id)
        return {
            "material_id": material_id,
            "active_version_id": active_id or None,
            "versions": projected,
            "running_request": running,
        }

    def load(self, material_id: str, version_id: str) -> Dict[str, Any]:
        document = self.list(material_id)
        for version in document["versions"]:
            if version["id"] == version_id:
                return version
        raise QuestionVersionError("QUESTION_VERSION_NOT_FOUND", "没有找到这个题目版本。", 404)

    def adopt(self, material_id: str, version_id: str, actor: str) -> Dict[str, Any]:
        material_id = _material_id(material_id)
        self.load(material_id, version_id)
        self._put("%s%s/active.json" % (QUESTION_VERSION_PREFIX, material_id), {
            "material_id": material_id,
            "version_id": version_id,
            "updated_at": _now(),
            "updated_by": actor,
        })
        return self.list(material_id)

    def reserve(
        self, material_id: str, base_version_id: str, comments: List[Dict[str, Any]], actor: str
    ) -> Dict[str, Any]:
        material_id = _material_id(material_id)
        self.load(material_id, base_version_id)
        with self._lock:
            running = self._running_request(material_id)
            if running:
                raise QuestionVersionError(
                    "QUESTION_REVISION_IN_PROGRESS", "这套题目已有修改任务正在运行。", 409)
            request_id = str(uuid.uuid4())
            record = {
                "request_id": request_id,
                "material_id": material_id,
                "status": "running",
                "base_version_id": base_version_id,
                "source_comments": comments,
                "actor": actor,
                "created_at": _now(),
            }
            running_key = _running_key(material_id)
            # Remove only a terminal/stale pointer. The create-only put below is the actual
            # cross-process mutex; the Python lock merely avoids needless races inside one task.
            if self._read(running_key) is not None:
                self.store.delete([running_key])
            try:
                self._put(running_key, record, if_none_match=True)
            except Exception as exc:
                if type(exc).__name__ == "PreconditionFailed":
                    raise QuestionVersionError(
                        "QUESTION_REVISION_IN_PROGRESS",
                        "这套题目已有修改任务正在运行。", 409)
                raise
            try:
                self._put(_request_key(material_id, request_id), record)
            except Exception:
                self.store.delete([running_key])
                raise
            return record

    def fail_request(self, material_id: str, record: Dict[str, Any], message: str) -> None:
        failed = dict(record)
        failed.update({"status": "failed", "message": message, "completed_at": _now()})
        self._put(_request_key(material_id, str(record["request_id"])), failed)
        self._put(_running_key(material_id), failed)

    def _running_request(self, material_id: str) -> Optional[Dict[str, Any]]:
        pointer = self._read(_running_key(material_id))
        if isinstance(pointer, dict) and pointer.get("status") == "running":
            request_id = str(pointer.get("request_id") or "")
            # A durable version wins over a stale sidecar: version creation is the commit point.
            if request_id and self._read(_version_key(material_id, request_id)) is not None:
                return None
            if not _is_stale(pointer):
                return pointer

        # Compatibility with requests reserved by the first implementation, before running.json
        # existed. Ignore stale rows so a Web crash cannot disable revisions forever.
        prefix = "%s%s/" % (QUESTION_REVISION_PREFIX, material_id)
        running: List[Dict[str, Any]] = []
        for key in self.store.list_keys(prefix):
            if key == _running_key(material_id) or key.endswith(".claim"):
                continue
            found = self._read(key)
            if (isinstance(found, dict) and found.get("status") == "running"
                    and not _is_stale(found)):
                request_id = str(found.get("request_id") or "")
                if request_id and self._read(_version_key(material_id, request_id)) is not None:
                    continue
                running.append(found)
        if not running:
            return None
        running.sort(key=lambda row: str(row.get("created_at") or ""), reverse=True)
        return running[0]

    def _read(self, key: str) -> Optional[Dict[str, Any]]:
        try:
            raw = self.store.get(key)
        except Exception as exc:
            if type(exc).__name__ == "ObjectNotFound":
                return None
            raise
        found = json.loads(raw.decode("utf-8") if isinstance(raw, bytes) else raw)
        return found if isinstance(found, dict) else None

    def _put(
        self, key: str, document: Dict[str, Any], *, if_none_match: bool = False
    ) -> None:
        self.store.put(
            key,
            json.dumps(document, ensure_ascii=False).encode("utf-8"),
            if_none_match=if_none_match,
        )


def _material_id(value: Any) -> str:
    material_id = str(value or "").strip()
    if not MATERIAL_ID_RE.fullmatch(material_id):
        raise QuestionVersionError("INVALID_MATERIAL_ID", "材料 ID 无效。")
    return material_id


def _request_key(material_id: str, request_id: str) -> str:
    return "%s%s/%s.json" % (QUESTION_REVISION_PREFIX, material_id, request_id)


def _running_key(material_id: str) -> str:
    return "%s%s/running.json" % (QUESTION_REVISION_PREFIX, material_id)


def _version_key(material_id: str, version_id: str) -> str:
    return "%s%s/versions/%s.json" % (QUESTION_VERSION_PREFIX, material_id, version_id)


def _is_stale(record: Dict[str, Any]) -> bool:
    raw = record.get("created_at")
    if not isinstance(raw, str) or not raw:
        return True
    try:
        created = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return True
    return (datetime.now(timezone.utc) - created).total_seconds() > RUNNING_TTL_SECONDS


def _now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def build_question_version_service() -> Optional[QuestionVersionService]:
    import os

    bucket = (os.environ.get("IELTS_AUDIO_BUCKET") or "").strip()
    if not bucket:
        return None
    from audio_storage.object_store import S3ObjectStore

    return QuestionVersionService(S3ObjectStore(bucket))
