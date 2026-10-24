"""Authenticated, same-origin delivery of audio clips from the application's S3 bucket."""
from __future__ import annotations

import logging
import os
import re
from urllib.parse import unquote, urlencode, urlparse

from fastapi import HTTPException
from fastapi.responses import StreamingResponse

from audio_storage.state_store import STATES

LOG = logging.getLogger(__name__)
_SEGMENT = r"[A-Za-z0-9][A-Za-z0-9._-]{0,199}"
_CLIP = re.compile(
    r"^(?:" + "|".join(STATES) + r"|_assessment_audio)/"
    + _SEGMENT + "/" + _SEGMENT + r"/audio/turn_[0-9]{3,6}\.mp3$"
)
_RANGE = re.compile(r"^bytes=(?:[0-9]+-[0-9]*|-[0-9]+)$")


class AudioProxy:
    def __init__(self, bucket: str, region: str, client=None):
        self.bucket = bucket
        self.region = region
        self._client = client

    @classmethod
    def from_environment(cls):
        return cls(os.environ.get("IELTS_AUDIO_BUCKET", ""),
                   os.environ.get("AWS_REGION") or os.environ.get("AWS_DEFAULT_REGION", ""))

    @property
    def configured(self):
        return bool(self.bucket) and self.region in {"cn-north-1", "cn-northwest-1"}

    def rewrite_links(self, result):
        """Use only Runtime-produced clip URLs, discarding all AWS query credentials."""
        if not isinstance(result, dict) or not isinstance(result.get("urls"), dict):
            return result
        endpoint = "s3.%s.amazonaws.com.cn" % self.region
        urls = {}
        for turn, url in result["urls"].items():
            parsed = urlparse(url)
            if (parsed.scheme != "https" or parsed.username or parsed.password
                    or parsed.port not in (None, 443)):
                raise ValueError("Invalid audio source")
            key = unquote(parsed.path.lstrip("/"))
            if parsed.hostname == endpoint and key.startswith(self.bucket + "/"):
                key = key[len(self.bucket) + 1:]
            elif parsed.hostname != self.bucket + "." + endpoint:
                raise ValueError("Audio source is outside the configured bucket")
            if not _CLIP.fullmatch(key) or not key.endswith("turn_%03d.mp3" % int(turn)):
                raise ValueError("Invalid audio clip key")
            urls[str(turn)] = "/api/material-audio?" + urlencode({"key": key})
        return {**result, "urls": urls}

    def response(self, key: str, byte_range: str | None):
        if not self.configured:
            raise HTTPException(503, "音频读取未配置。")
        # This route cannot read user records, manifests, credentials, arbitrary buckets or URLs.
        if not _CLIP.fullmatch(key):
            raise HTTPException(404, "没有找到音频片段。")
        if byte_range and not _RANGE.fullmatch(byte_range):
            raise HTTPException(416, "不支持此音频读取范围。")
        if self._client is None:
            import boto3
            from botocore.config import Config
            self._client = boto3.client(
                "s3", region_name=self.region,
                config=Config(connect_timeout=5, read_timeout=30,
                              retries={"total_max_attempts": 2},
                              ignore_configured_endpoint_urls=True),
            )
        from botocore.exceptions import ClientError
        params = {"Bucket": self.bucket, "Key": key}
        if byte_range:
            params["Range"] = byte_range
        try:
            found = self._client.get_object(**params)
        except ClientError as exc:
            code = str(exc.response.get("Error", {}).get("Code") or "")
            LOG.warning("Audio S3 read failed: %s", code)
            status = 416 if code == "InvalidRange" else 404 if code in {"NoSuchKey", "404"} else 502
            raise HTTPException(status, "暂时无法读取音频片段。") from None
        body = found["Body"]
        headers = {
            "Accept-Ranges": "bytes", "Cache-Control": "private, no-store",
            "Content-Length": str(found["ContentLength"]),
        }
        if found.get("ContentRange"):
            headers["Content-Range"] = found["ContentRange"]

        def chunks():
            try:
                while True:
                    chunk = body.read(65536)
                    if not chunk:
                        break
                    yield chunk
            finally:
                body.close()

        return StreamingResponse(
            chunks(), media_type="audio/mpeg", headers=headers,
            status_code=206 if found.get("ContentRange") else 200,
        )
