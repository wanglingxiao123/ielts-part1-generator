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
ASSESSMENT_AUDIO_PREFIX = "_assessment_audio/"
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
        self._lock = threading.RLock()

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
                "quality": {
                    "review": original.get("review"),
                    "cross_check": original.get("cross_check"),
                    "validation": original.get("validation"),
                },
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
            if item.get("operation") == "revise_material":
                stored_audio = self._read(
                    "%s%s/%s/status.json"
                    % (ASSESSMENT_AUDIO_PREFIX, material_id, str(item.get("id") or ""))
                )
                if isinstance(stored_audio, dict):
                    if (
                        stored_audio.get("status") == "ready"
                        and not self.store.head(
                            "%s%s/%s/audio/manifest.json"
                            % (
                                ASSESSMENT_AUDIO_PREFIX,
                                material_id,
                                str(item.get("id") or ""),
                            )
                        )
                    ):
                        stored_audio = dict(stored_audio, status="needs_synthesis")
                    item["audio"] = stored_audio
            item["ordinal"] = ordinal
            item["is_active"] = str(row["id"]) == active_id
            projected.append(item)
        revision = self._latest_request(material_id)
        running = revision if revision and revision.get("status") == "running" else None
        return {
            "material_id": material_id,
            "active_version_id": active_id or None,
            "versions": projected,
            "running_request": running,
            "revision_request": revision,
        }

    def load(self, material_id: str, version_id: str) -> Dict[str, Any]:
        document = self.list(material_id)
        for version in document["versions"]:
            if version["id"] == version_id:
                return version
        raise QuestionVersionError("QUESTION_VERSION_NOT_FOUND", "没有找到这个题目版本。", 404)

    def adopt(self, material_id: str, version_id: str, actor: str) -> Dict[str, Any]:
        material_id = _material_id(material_id)
        version = self.load(material_id, version_id)
        if version.get("operation") == "revise_material" and not all(
            isinstance(version.get(key), dict)
            for key in ("material", "blueprint", "package", "audio")
        ):
            raise QuestionVersionError(
                "ASSESSMENT_VERSION_INCOMPLETE",
                "这个材料版本不完整，不能采用。",
                409,
            )
        self._put("%s%s/active.json" % (QUESTION_VERSION_PREFIX, material_id), {
            "material_id": material_id,
            "version_id": version_id,
            "updated_at": _now(),
            "updated_by": actor,
        })
        return self.list(material_id)

    def reserve(
        self,
        material_id: str,
        base_version_id: str,
        comments: List[Dict[str, Any]],
        actor: str,
        *,
        operation: str = "revise_questions",
        source_request_id: Optional[str] = None,
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
                "stage": "queued",
                "operation": operation,
                "base_version_id": base_version_id,
                "source_comments": comments,
                "comment_count": len(comments),
                "actor": actor,
                "created_at": _now(),
            }
            if source_request_id:
                record["source_request_id"] = source_request_id
            running_key = _running_key(material_id)
            # Production runs one Web task, so the process lock arbitrates replacement of a
            # terminal/stale pointer. Do not delete it first: the task role intentionally lacks
            # DeleteObject, and S3 DeleteObjects reports per-key denial in its response rather than
            # necessarily raising. An absent pointer still uses create-only PUT so a concurrently
            # created live request cannot be overwritten.
            pointer_exists = self._read(running_key) is not None
            try:
                self._put(running_key, record, if_none_match=not pointer_exists)
            except Exception as exc:
                if type(exc).__name__ == "PreconditionFailed":
                    raise QuestionVersionError(
                        "QUESTION_REVISION_IN_PROGRESS",
                        "这套题目已有修改任务正在运行。", 409)
                raise
            try:
                self._put(_request_key(material_id, request_id), record)
            except Exception as exc:
                failed = dict(
                    record,
                    status="failed",
                    message="revision request record could not be stored",
                    completed_at=_now(),
                )
                try:
                    self._put(running_key, failed)
                except Exception:
                    # If the store itself is unavailable, preserving the running marker is safer
                    # than admitting a second request whose first reservation is ambiguous.
                    pass
                raise
            return record

    def load_request(self, material_id: str, request_id: str) -> Dict[str, Any]:
        material_id = _material_id(material_id)
        request_id = _record_id(request_id, "INVALID_REQUEST_ID")
        found = self._read(_request_key(material_id, request_id))
        if not isinstance(found, dict):
            raise QuestionVersionError(
                "QUESTION_REVISION_NOT_FOUND", "没有找到这次题目修改记录。", 404)
        return found

    def reserve_replan(
        self, material_id: str, source_request_id: str, actor: str
    ) -> Dict[str, Any]:
        """Reserve execution from a durable replan decision, without new open comments."""
        material_id = _material_id(material_id)
        source_request_id = _record_id(source_request_id, "INVALID_REQUEST_ID")
        with self._lock:
            source = self.replan_source(material_id, source_request_id)
            existing = self._replan_execution(material_id, source_request_id)
            if existing is not None:
                return existing
            return self.reserve(
                material_id,
                str(source["base_version_id"]),
                source["comments"],
                actor,
                operation="replan_questions",
                source_request_id=source_request_id,
            )

    def reserve_material_revision(
        self, material_id: str, source_request_id: str, actor: str
    ) -> Dict[str, Any]:
        """Reserve one execution for a durable revise-material decision."""
        material_id = _material_id(material_id)
        source_request_id = _record_id(source_request_id, "INVALID_REQUEST_ID")
        with self._lock:
            source = self.material_revision_source(material_id, source_request_id)
            existing = self._source_execution(
                material_id, source_request_id, "revise_material")
            if existing is not None:
                return existing
            return self.reserve(
                material_id,
                str(source["base_version_id"]),
                source["comments"],
                actor,
                operation="revise_material",
                source_request_id=source_request_id,
            )

    def material_revision_source(
        self, material_id: str, source_request_id: str
    ) -> Dict[str, Any]:
        """Validate a classified material-revision snapshot and its active baseline."""
        material_id = _material_id(material_id)
        source_request_id = _record_id(source_request_id, "INVALID_REQUEST_ID")
        source = self.load_request(material_id, source_request_id)
        if (
            source.get("request_id") != source_request_id
            or source.get("material_id") != material_id
            or source.get("operation") in {"replan_questions", "revise_material"}
            or source.get("status") != "needs_material_revision"
        ):
            raise QuestionVersionError(
                "MATERIAL_REVISION_NOT_AVAILABLE",
                "这次修改不需要修改材料，不能启动该操作。",
                409,
            )
        base_version_id = str(source.get("base_version_id") or "")
        versions = self.list(material_id)
        if not base_version_id or versions.get("active_version_id") != base_version_id:
            raise QuestionVersionError(
                "BASE_VERSION_NOT_ACTIVE", "只能基于当前采用版本修改材料。", 409)
        source_comments = source.get("source_comments")
        outcomes = source.get("comment_outcomes")
        if not isinstance(source_comments, list) or not source_comments \
                or not isinstance(outcomes, list) or not outcomes:
            raise QuestionVersionError(
                "MATERIAL_REVISION_SOURCE_MISSING",
                "原修改记录没有保留批注。",
                409,
            )
        dispositions: Dict[str, str] = {}
        for row in outcomes:
            comment_id = str(row.get("comment_id") or "") if isinstance(row, dict) else ""
            outcome = str(row.get("outcome") or "") if isinstance(row, dict) else ""
            if (
                not comment_id
                or comment_id in dispositions
                or outcome not in {
                    "question_only", "no_change", "replan_questions", "revise_material",
                }
            ):
                raise QuestionVersionError(
                    "MATERIAL_REVISION_SOURCE_INVALID",
                    "原修改记录的批注结论不完整。",
                    409,
                )
            dispositions[comment_id] = outcome
        comments: List[Dict[str, Any]] = []
        seen: set[str] = set()
        for row in source_comments:
            anchor = row.get("anchor") if isinstance(row, dict) else None
            comment_id = str(row.get("id") or "") if isinstance(row, dict) else ""
            if (
                not comment_id
                or comment_id in seen
                or comment_id not in dispositions
                or not isinstance(anchor, dict)
                or anchor.get("type") not in {"question", "turn"}
                or not str(row.get("text") or "").strip()
            ):
                raise QuestionVersionError(
                    "MATERIAL_REVISION_SOURCE_INVALID",
                    "原修改记录的批注不完整。",
                    409,
                )
            seen.add(comment_id)
            if dispositions[comment_id] != "no_change":
                comments.append(dict(row))
        if set(dispositions) != seen or "revise_material" not in dispositions.values():
            raise QuestionVersionError(
                "MATERIAL_REVISION_SOURCE_INVALID",
                "原修改记录的批注结论不完整。",
                409,
            )
        if not comments:
            raise QuestionVersionError(
                "MATERIAL_REVISION_SOURCE_MISSING",
                "原修改记录没有可用于修改材料的批注。",
                409,
            )
        return {
            "request": source,
            "base_version_id": base_version_id,
            "comments": comments,
        }

    def replan_source(
        self, material_id: str, source_request_id: str
    ) -> Dict[str, Any]:
        """Validate and project the immutable source snapshot before reserving execution."""
        material_id = _material_id(material_id)
        source_request_id = _record_id(source_request_id, "INVALID_REQUEST_ID")
        source = self.load_request(material_id, source_request_id)
        if (
            source.get("request_id") != source_request_id
            or source.get("material_id") != material_id
            or source.get("operation") == "replan_questions"
            or source.get("status") != "replan_questions"
        ):
            raise QuestionVersionError(
                "QUESTION_REPLAN_NOT_AVAILABLE",
                "这次修改不需要重新命题，不能启动该操作。", 409)
        base_version_id = str(source.get("base_version_id") or "")
        versions = self.list(material_id)
        if not base_version_id or versions.get("active_version_id") != base_version_id:
            raise QuestionVersionError(
                "BASE_VERSION_NOT_ACTIVE", "只能基于当前采用版本重新命题。", 409)
        source_comments = source.get("source_comments")
        outcomes = source.get("comment_outcomes")
        if (
            not isinstance(source_comments, list)
            or not source_comments
            or not isinstance(outcomes, list)
            or not outcomes
        ):
            raise QuestionVersionError(
                "QUESTION_REPLAN_SOURCE_MISSING", "原修改记录没有保留题目批注。", 409)
        dispositions: Dict[str, str] = {}
        replan_scopes: Dict[str, str] = {}
        for row in outcomes:
            if not isinstance(row, dict):
                raise QuestionVersionError(
                    "QUESTION_REPLAN_SOURCE_INVALID", "原修改记录的批注结论不完整。", 409)
            comment_id = str(row.get("comment_id") or "")
            outcome = str(row.get("outcome") or "")
            if (
                not comment_id
                or comment_id in dispositions
                or outcome not in {
                    "question_only", "no_change", "replan_questions", "revise_material",
                }
            ):
                raise QuestionVersionError(
                    "QUESTION_REPLAN_SOURCE_INVALID", "原修改记录的批注结论不完整。", 409)
            dispositions[comment_id] = outcome
            scope = row.get("replan_scope")
            if outcome == "replan_questions":
                # Source records created before replan scopes existed retain the old full-replan
                # behaviour so a durable confirmation does not change meaning after deployment.
                scope = "retarget" if scope is None else str(scope)
                if scope not in {"layout_only", "retarget"}:
                    raise QuestionVersionError(
                        "QUESTION_REPLAN_SOURCE_INVALID",
                        "原修改记录的重新命题范围不完整。", 409)
                replan_scopes[comment_id] = scope
        comment_ids: set[str] = set()
        for row in source_comments:
            anchor = row.get("anchor") if isinstance(row, dict) else None
            comment_id = str(row.get("id") or "") if isinstance(row, dict) else ""
            number = anchor.get("index") if isinstance(anchor, dict) else None
            if (
                not comment_id
                or comment_id in comment_ids
                or comment_id not in dispositions
                or not isinstance(anchor, dict)
                or anchor.get("type") != "question"
                or isinstance(number, bool)
                or not isinstance(number, int)
                or not 1 <= number <= 10
                or not str(row.get("text") or "").strip()
            ):
                raise QuestionVersionError(
                    "QUESTION_REPLAN_SOURCE_INVALID", "原修改记录的题目批注不完整。", 409)
            comment_ids.add(comment_id)
        if set(dispositions) != comment_ids or "replan_questions" not in dispositions.values():
            raise QuestionVersionError(
                "QUESTION_REPLAN_SOURCE_INVALID", "原修改记录的批注结论不完整。", 409)
        comments = []
        for row in source_comments:
            comment_id = str(row["id"])
            if dispositions[comment_id] not in {"question_only", "replan_questions"}:
                continue
            projected = dict(row)
            if dispositions[comment_id] == "replan_questions":
                projected["replan_scope"] = replan_scopes[comment_id]
            comments.append(projected)
        if not comments:
            raise QuestionVersionError(
                "QUESTION_REPLAN_SOURCE_MISSING",
                "原修改记录没有可用于重新命题的批注。", 409)
        return {
            "request": source,
            "base_version_id": base_version_id,
            "comments": comments,
        }

    def _replan_execution(
        self, material_id: str, source_request_id: str
    ) -> Optional[Dict[str, Any]]:
        """Return the one reusable execution for a confirmed source decision."""
        prefix = "%s%s/" % (QUESTION_REVISION_PREFIX, material_id)
        matches: List[Dict[str, Any]] = []
        for key in self.store.list_keys(prefix):
            if key == _running_key(material_id) or key.endswith(".claim"):
                continue
            found = self._read(key)
            if (
                isinstance(found, dict)
                and found.get("operation") == "replan_questions"
                and found.get("source_request_id") == source_request_id
                and found.get("status") != "failed"
                and not (
                    found.get("status") == "running"
                    and _is_stale(found)
                )
            ):
                matches.append(found)
        if not matches:
            return None
        matches.sort(
            key=lambda row: (
                str(row.get("created_at") or ""),
                str(row.get("request_id") or ""),
            ),
            reverse=True,
        )
        return matches[0]

    def _source_execution(
        self, material_id: str, source_request_id: str, operation: str
    ) -> Optional[Dict[str, Any]]:
        prefix = "%s%s/" % (QUESTION_REVISION_PREFIX, material_id)
        matches: List[Dict[str, Any]] = []
        for key in self.store.list_keys(prefix):
            if key == _running_key(material_id) or key.endswith(".claim"):
                continue
            found = self._read(key)
            if (
                isinstance(found, dict)
                and found.get("operation") == operation
                and found.get("source_request_id") == source_request_id
                and found.get("status") != "failed"
                and not (
                    found.get("status") == "running"
                    and _is_stale(found)
                )
            ):
                matches.append(found)
        if not matches:
            return None
        matches.sort(
            key=lambda row: (
                str(row.get("created_at") or ""),
                str(row.get("request_id") or ""),
            ),
            reverse=True,
        )
        return matches[0]

    def fail_request(self, material_id: str, record: Dict[str, Any], message: str) -> None:
        failed = dict(record)
        failed.update({"status": "failed", "message": message, "completed_at": _now()})
        self._put(_request_key(material_id, str(record["request_id"])), failed)
        self._put(_running_key(material_id), failed)

    def _running_request(self, material_id: str) -> Optional[Dict[str, Any]]:
        pointer = self._latest_request(material_id)
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

    def _latest_request(self, material_id: str) -> Optional[Dict[str, Any]]:
        pointer = self._read(_running_key(material_id))
        if not isinstance(pointer, dict):
            return None
        request_id = str(pointer.get("request_id") or "")
        version = (
            self._read(_version_key(material_id, request_id))
            if pointer.get("status") == "running" and request_id else None
        )
        if isinstance(version, dict):
            return dict(
                pointer,
                status="completed",
                stage="storing",
                version_id=request_id,
                completed_at=version.get("created_at")
                or pointer.get("updated_at") or pointer.get("created_at"),
                baseline_advisories=version.get("baseline_advisories") or [],
            )
        if pointer.get("status") == "running" and _is_stale(pointer):
            return None
        return pointer

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


def _record_id(value: Any, code: str) -> str:
    record_id = str(value or "").strip()
    if not MATERIAL_ID_RE.fullmatch(record_id):
        raise QuestionVersionError(code, "记录 ID 无效。")
    return record_id


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
