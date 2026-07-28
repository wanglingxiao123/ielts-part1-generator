"""The narrow object-storage interface state_store is written against, plus two backends.

Keeping the surface this small is what makes the state machine testable: every branch in
state_store -- including the crash-recovery paths that are the whole point of design.md §9.2
-- runs against InMemoryObjectStore with no AWS involved. The S3 backend then has no logic
of its own to get wrong.

boto3 is imported inside S3ObjectStore.__init__, never at module import. Nothing in this
package may require AWS to be importable.
"""

from __future__ import annotations

import copy
from typing import Dict, List, Optional


class ObjectNotFound(KeyError):
    """No object at that key."""


class PreconditionFailed(RuntimeError):
    """Conditional write lost: the key already exists (HTTP 412)."""


class ConditionalWriteUnsupported(RuntimeError):
    """The backend cannot do create-if-absent, so the transition mutex is unavailable."""


class InMemoryObjectStore:
    """Dict-backed store with S3's conditional-write semantics.

    Also records a call log, which lets a test assert *what* happened rather than only the
    end state -- e.g. that the manifest was copied after every other object, which is the
    property the completeness sentinel depends on.
    """

    def __init__(self, conditional_put_supported: bool = True) -> None:
        self._objects: Dict[str, bytes] = {}
        self._metadata: Dict[str, Dict[str, str]] = {}
        self.conditional_put_supported = conditional_put_supported
        self.calls: List[tuple] = []

    def put(
        self,
        key: str,
        body: bytes,
        if_none_match: bool = False,
        metadata: Optional[Dict[str, str]] = None,
    ) -> None:
        self.calls.append(("put", key))
        if if_none_match:
            if not self.conditional_put_supported:
                raise ConditionalWriteUnsupported("backend has no create-if-absent")
            if key in self._objects:
                raise PreconditionFailed(key)
        self._objects[key] = bytes(body)
        # Lowercased like S3 does, so a test cannot pass on a casing S3 would not return.
        self._metadata[key] = {str(k).lower(): str(v) for k, v in (metadata or {}).items()}

    def get(self, key: str) -> bytes:
        self.calls.append(("get", key))
        if key not in self._objects:
            raise ObjectNotFound(key)
        return self._objects[key]

    def head(self, key: str) -> bool:
        self.calls.append(("head", key))
        return key in self._objects

    def head_metadata(self, key: str) -> Optional[Dict[str, str]]:
        """User metadata for an existing key, or None if absent.

        One round trip that answers both "does the clip exist" and "is it the clip this SSML
        would produce". The alternative -- head() plus a manifest lookup -- cannot answer the
        second question after a crash, because the manifest is written last and so is exactly
        what a crashed run is missing (design.md §4.5).
        """
        self.calls.append(("head", key))
        if key not in self._objects:
            return None
        return dict(self._metadata.get(key, {}))

    def list_keys(self, prefix: str) -> List[str]:
        self.calls.append(("list", prefix))
        return sorted(k for k in self._objects if k.startswith(prefix))

    def copy(self, src: str, dst: str) -> None:
        self.calls.append(("copy", src, dst))
        if src not in self._objects:
            raise ObjectNotFound(src)
        self._objects[dst] = self._objects[src]
        # S3's CopyObject carries user metadata across by default; mirroring that here keeps a
        # transitioned material's clips recognisable to the idempotency check.
        self._metadata[dst] = dict(self._metadata.get(src, {}))

    def delete(self, keys: List[str]) -> None:
        for key in keys:
            self.calls.append(("delete", key))
            self._objects.pop(key, None)
            self._metadata.pop(key, None)

    def presign(self, key: str, ttl_seconds: int) -> str:
        self.calls.append(("presign", key))
        if key not in self._objects:
            raise ObjectNotFound(key)
        return "memory://{0}?ttl={1}".format(key, ttl_seconds)

    # Test helpers -------------------------------------------------------------
    def snapshot(self) -> Dict[str, bytes]:
        return copy.deepcopy(self._objects)

    def restore(self, snapshot: Dict[str, bytes]) -> None:
        self._objects = copy.deepcopy(snapshot)

    def clear_calls(self) -> None:
        self.calls = []


class S3ObjectStore:
    """boto3-backed implementation. Imported lazily; constructing it is the only AWS touch."""

    def __init__(self, bucket: str, *, client=None, conditional_put_supported: bool = True):
        if client is None:
            import boto3  # noqa: PLC0415 - deliberately lazy: no AWS at import time

            client = boto3.client("s3")
        self._client = client
        self.bucket = bucket
        # 待实测 (assumptions.s3-conditional-put): if PutObject IfNoneMatch is not honoured in
        # the target region, set this False and read design.md §9.2's weaker fallback.
        self.conditional_put_supported = conditional_put_supported

    def put(
        self,
        key: str,
        body: bytes,
        if_none_match: bool = False,
        metadata: Optional[Dict[str, str]] = None,
    ) -> None:
        kwargs = {"Bucket": self.bucket, "Key": key, "Body": body}
        if if_none_match:
            if not self.conditional_put_supported:
                raise ConditionalWriteUnsupported("conditional PutObject disabled")
            kwargs["IfNoneMatch"] = "*"
        if metadata:
            kwargs["Metadata"] = {str(k): str(v) for k, v in metadata.items()}
        try:
            self._client.put_object(**kwargs)
        except Exception as exc:  # noqa: BLE001 - re-raised as our own error taxonomy
            if _is_precondition_failed(exc):
                raise PreconditionFailed(key)
            raise

    def get(self, key: str) -> bytes:
        try:
            return self._client.get_object(Bucket=self.bucket, Key=key)["Body"].read()
        except Exception as exc:  # noqa: BLE001
            if _is_not_found(exc):
                raise ObjectNotFound(key)
            raise

    def head(self, key: str) -> bool:
        try:
            self._client.head_object(Bucket=self.bucket, Key=key)
            return True
        except Exception as exc:  # noqa: BLE001
            if _is_not_found(exc):
                return False
            raise

    def head_metadata(self, key: str) -> Optional[Dict[str, str]]:
        """User metadata, or None if the object is absent. See InMemoryObjectStore."""
        try:
            response = self._client.head_object(Bucket=self.bucket, Key=key)
        except Exception as exc:  # noqa: BLE001
            if _is_not_found(exc):
                return None
            raise
        # boto3 lowercases user-metadata keys; the in-memory store does the same so the two
        # backends cannot disagree about a key's casing.
        return {str(k).lower(): str(v) for k, v in (response.get("Metadata") or {}).items()}

    def list_keys(self, prefix: str) -> List[str]:
        keys: List[str] = []
        token: Optional[str] = None
        while True:
            kwargs = {"Bucket": self.bucket, "Prefix": prefix}
            if token:
                kwargs["ContinuationToken"] = token
            page = self._client.list_objects_v2(**kwargs)
            keys.extend(obj["Key"] for obj in page.get("Contents", []))
            if not page.get("IsTruncated"):
                break
            token = page.get("NextContinuationToken")
        return sorted(keys)

    def copy(self, src: str, dst: str) -> None:
        # Server-side copy: the bytes never come down to the runtime.
        self._client.copy_object(
            Bucket=self.bucket, Key=dst, CopySource={"Bucket": self.bucket, "Key": src}
        )

    def delete(self, keys: List[str]) -> None:
        for start in range(0, len(keys), 1000):  # DeleteObjects caps at 1000 per call
            batch = keys[start : start + 1000]
            self._client.delete_objects(
                Bucket=self.bucket, Delete={"Objects": [{"Key": k} for k in batch]}
            )

    def presign(self, key: str, ttl_seconds: int) -> str:
        return self._client.generate_presigned_url(
            "get_object", Params={"Bucket": self.bucket, "Key": key}, ExpiresIn=ttl_seconds
        )


def _error_code(exc: Exception) -> str:
    response = getattr(exc, "response", None)
    if isinstance(response, dict):
        return str(response.get("Error", {}).get("Code", ""))
    return ""


def _is_not_found(exc: Exception) -> bool:
    return _error_code(exc) in {"404", "NoSuchKey", "NotFound"}


def _is_precondition_failed(exc: Exception) -> bool:
    return _error_code(exc) in {"412", "PreconditionFailed"}
