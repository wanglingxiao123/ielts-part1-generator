"""Immutable bundles and compare-and-swap pointers; no credentials at import time."""
from __future__ import annotations

import copy
import hashlib
import json
import os
import re
import threading
import time
import uuid
from pathlib import Path

SCHEMA = 1
PREFIX = "_prompt_config/"


class Conflict(ValueError):
    pass


def encode(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()


def digest(values):
    return hashlib.sha256(encode({"schema": SCHEMA, "values": values})).hexdigest()


class MemoryStore:
    def __init__(self):
        self.data = {}
        self.lock = threading.RLock()

    def read(self, key):
        with self.lock:
            return copy.deepcopy(self.data.get(key, (None, None)))

    def write(self, key, value, expected=None):
        with self.lock:
            if self.data.get(key, (None, None))[1] != expected:
                raise Conflict("配置已被更新，请刷新后重试。")
            token = uuid.uuid4().hex
            self.data[key] = (copy.deepcopy(value), token)
            return token


class FileStore:
    """Atomic shared local store for separate development Web/Runtime processes."""
    def __init__(self, directory):
        self.directory = Path(directory)

    def _locked(self):
        import fcntl
        from contextlib import contextmanager
        @contextmanager
        def lock():
            self.directory.mkdir(parents=True, exist_ok=True)
            with (self.directory / ".lock").open("a") as handle:
                fcntl.flock(handle, fcntl.LOCK_EX)
                yield
        return lock()

    def _read(self, key):
        path = self.directory / key
        if not path.exists():
            return None, None
        raw = path.read_bytes()
        return json.loads(raw), hashlib.sha256(raw).hexdigest()

    def read(self, key):
        with self._locked():
            return self._read(key)

    def write(self, key, value, expected=None):
        with self._locked():
            if self._read(key)[1] != expected:
                raise Conflict("配置已被更新，请刷新后重试。")
            path = self.directory / key
            path.parent.mkdir(parents=True, exist_ok=True)
            temporary = path.with_name(path.name + "." + uuid.uuid4().hex)
            raw = encode(value)
            try:
                with temporary.open("xb") as handle:
                    handle.write(raw)
                    handle.flush()
                    os.fsync(handle.fileno())
                os.replace(temporary, path)
            finally:
                temporary.unlink(missing_ok=True)
            return hashlib.sha256(raw).hexdigest()


class S3Store:
    def __init__(self, bucket, client=None):
        if client is None:
            import boto3
            from botocore.config import Config
            client = boto3.client("s3", config=Config(connect_timeout=5, read_timeout=15,
                                                      retries={"max_attempts": 3}))
        self.client, self.bucket = client, bucket

    def read(self, key):
        try:
            result = self.client.get_object(Bucket=self.bucket, Key=PREFIX + key)
        except self.client.exceptions.NoSuchKey:
            return None, None
        with result["Body"] as body:
            return json.loads(body.read()), result["ETag"]

    def write(self, key, value, expected=None):
        from botocore.exceptions import ClientError
        condition = {"IfMatch": expected} if expected else {"IfNoneMatch": "*"}
        try:
            response = self.client.put_object(Bucket=self.bucket, Key=PREFIX + key,
                                             Body=encode(value), ContentType="application/json",
                                             **condition)
        except ClientError as exc:
            if exc.response.get("ResponseMetadata", {}).get("HTTPStatusCode") in (409, 412):
                raise Conflict("配置已被更新，请刷新后重试。") from exc
            raise
        return response["ETag"]


class PromptStore:
    def __init__(self, storage=None):
        self._storage = storage
        self._lock = threading.Lock()

    @property
    def storage(self):
        with self._lock:
            if self._storage is None:
                bucket = os.environ.get("IELTS_AUDIO_BUCKET", "").strip()
                self._storage = S3Store(bucket) if bucket else FileStore(
                    os.environ.get("IELTS_PROMPT_DIRECTORY") or
                    Path(__file__).resolve().parents[1] / ".local" / "prompt-config")
        return self._storage

    def validate(self, values):
        from .catalogue import defaults
        if not isinstance(values, dict) or set(values) != set(defaults()):
            raise ValueError("提示词目录不匹配，请刷新页面。")
        for key, text in values.items():
            if not isinstance(text, str) or len(text) > 16000 or "\x00" in text:
                raise ValueError("提示词必须为不超过 16000 字符的文本。")
            if defaults()[key].strip() and not text.strip():
                raise ValueError("提示词不可为空。")
        return copy.deepcopy(values)

    def bundled(self):
        from .catalogue import defaults
        values = defaults()
        checksum = digest(values)
        return dict(schema=SCHEMA, release_id="bundled-" + checksum, digest=checksum,
                    values=values, actor="application", created_at=0)

    def resolve(self, release_id=None):
        if release_id is None:
            pointer, _ = self.storage.read("active.json")
            if pointer is None:
                return self.bundled()
            release_id = pointer["release_id"]
        if release_id == self.bundled()["release_id"]:
            return self.bundled()
        if not isinstance(release_id, str) or not re.fullmatch(r"[a-zA-Z0-9-]{1,100}", release_id):
            raise ValueError("无效的提示词版本。")
        bundle, _ = self.storage.read("releases/" + release_id + ".json")
        if bundle is None:
            raise ValueError("请求的提示词版本不存在，任务未使用其他版本替代。")
        if (bundle.get("schema") != SCHEMA or bundle.get("release_id") != release_id
                or digest(self.validate(bundle.get("values"))) != bundle.get("digest")):
            raise ValueError("提示词版本不兼容或内容校验失败。")
        return bundle

    def snapshot(self, values, actor, **extra):
        values = self.validate(values)
        bundle = dict(schema=SCHEMA, release_id=uuid.uuid4().hex, digest=digest(values),
                      values=values, actor=actor, created_at=time.time(), **extra)
        self.storage.write("releases/" + bundle["release_id"] + ".json", bundle)
        return bundle

    def save_draft(self, values, actor, expected):
        draft = dict(values=self.validate(values), actor=actor, updated_at=time.time())
        self.storage.write("draft.json", draft, expected)

    def publish(self, actor, expected, *, draft_etag=None, release_id=None):
        pointer, etag = self.storage.read("active.json")
        if etag != expected:
            raise Conflict("当前发布版本已改变，请刷新后重试。")
        if release_id is not None:
            values = self.resolve(release_id)["values"]
        else:
            draft, actual = self.storage.read("draft.json")
            if draft is None or actual != draft_etag:
                raise Conflict("草稿已改变，请重新检查后发布。")
            values = draft["values"]
        # Bundle first, then one CAS pointer: a losing writer leaves an inert orphan only.
        bundle = self.snapshot(values, actor, rollback_of=release_id,
                               previous=(pointer or {}).get("release_id"))
        self.storage.write("active.json", {"release_id": bundle["release_id"]}, expected)
        return bundle

    def state(self):
        from .catalogue import entries
        pointer, etag = self.storage.read("active.json")
        active = self.resolve(pointer["release_id"] if pointer else self.bundled()["release_id"])
        draft, draft_etag = self.storage.read("draft.json")
        history, item = [], active
        for _ in range(100):
            history.append({k: v for k, v in item.items() if k != "values"})
            if not item.get("previous"):
                break
            item = self.resolve(item["previous"])
        return dict(catalogue=entries(), active=active, active_etag=etag,
                    draft=draft, draft_etag=draft_etag, history=history)

    def pin(self, operation, *, default=False):
        """Durable idempotent binding, separate from mutable batch progress records."""
        key = "bindings/" + hashlib.sha256(operation.encode()).hexdigest() + ".json"
        ref, _ = self.storage.read(key)
        if ref is None:
            bundle = self.bundled() if default else self.resolve()
            # Persist even bundled defaults so a future image can read this exact configuration.
            if bundle["release_id"].startswith("bundled-"):
                try:
                    self.storage.write("releases/" + bundle["release_id"] + ".json", bundle)
                except Conflict:
                    pass
            ref = {k: bundle[k] for k in ("release_id", "digest")}
            try:
                self.storage.write(key, ref)
            except Conflict:
                ref, _ = self.storage.read(key)
        bundle = self.resolve(ref["release_id"])
        if bundle["digest"] != ref["digest"]:
            raise ValueError("任务的提示词摘要不匹配。")
        return ref
