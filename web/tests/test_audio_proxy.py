import io
from urllib.parse import urlencode

import pytest
from botocore.exceptions import ClientError

from web.audio_proxy import AudioProxy
from .conftest import register

BUCKET = "test-audio"
REGION = "cn-northwest-1"
KEY = "pending/service-refund/material-1/audio/turn_000.mp3"
SOURCE = "https://test-audio.s3.cn-northwest-1.amazonaws.com.cn/"


class S3:
    def __init__(self):
        self.calls = []
        self.body = io.BytesIO(b"mp3-data")
        self.error = None

    def get_object(self, **kwargs):
        self.calls.append(kwargs)
        if self.error:
            raise ClientError({"Error": {"Code": self.error}}, "GetObject")
        result = {"Body": self.body, "ContentLength": 8}
        if kwargs.get("Range"):
            result["ContentRange"] = "bytes 0-7/8"
        return result


def test_real_tier_construction_initializes_proxy_from_deployment_environment(tier, monkeypatch):
    from fastapi.testclient import TestClient
    from web.app import WebTier
    monkeypatch.setenv("AWS_REGION", REGION)
    monkeypatch.setenv("IELTS_AUDIO_BUCKET", BUCKET)
    built = WebTier(
        tier.auth, tier.runtime, tier.static_dir, history=tier.history,
        slot_state=tier.slot_state, comments=tier.comments,
        question_versions=tier.question_versions, executions=tier.executions,
    )
    assert built.audio_proxy.configured
    assert built.audio_proxy.bucket == BUCKET
    built.audio_proxy._client = S3()
    with TestClient(built.app) as client:
        register(client)
        result = client.get("/api/material-audio", params={"key": KEY})
        assert result.status_code == 200
        assert result.content == b"mp3-data"


def test_presign_rpc_returns_same_origin_urls_without_aws_credentials(client, tier, runtime):
    register(client)
    tier.audio_proxy = AudioProxy(BUCKET, REGION, S3())
    runtime.json_response = {"urls": {"0": SOURCE + KEY + "?X-Amz-Security-Token=secret"}}
    response = client.post("/api/invocations", json={"action": "presign_audio"})
    assert response.status_code == 200
    assert response.json()["urls"]["0"] == "/api/material-audio?" + urlencode({"key": KEY})
    assert "secret" not in response.text and "amazonaws" not in response.text


@pytest.mark.parametrize("key", [
    KEY, "_assessment_audio/material-1/version-2/audio/turn_000.mp3",
])
@pytest.mark.parametrize("byte_range", [None, "bytes=0-7", "bytes=0-", "bytes=-8"])
def test_audio_uses_authenticated_s3_read_and_supports_ranges(client, tier, key, byte_range):
    register(client)
    storage = S3()
    tier.audio_proxy = AudioProxy(BUCKET, REGION, storage)
    response = client.get("/api/material-audio", params={"key": key},
                          headers={"Range": byte_range} if byte_range else {})
    assert response.status_code == (206 if byte_range else 200)
    assert response.content == b"mp3-data"
    assert response.headers["content-type"] == "audio/mpeg"
    assert response.headers["cache-control"] == "private, no-store"
    assert storage.calls[0] == {
        "Bucket": BUCKET, "Key": key, **({"Range": byte_range} if byte_range else {}),
    }
    assert storage.body.closed


def test_audio_requires_login_before_accessing_s3(client, tier):
    storage = S3()
    tier.audio_proxy = AudioProxy(BUCKET, REGION, storage)
    assert client.get("/api/material-audio", params={"key": KEY}).status_code == 401
    assert storage.calls == []


@pytest.mark.parametrize("key", [
    "web/users.json", "pending/scenario/material/material.json", "../web/users.json",
    "pending/../material/audio/turn_000.mp3", "https://example.com/file.mp3",
    "_assessment_audio/material/../audio/turn_000.mp3",
])
def test_audio_cannot_read_private_records_or_arbitrary_paths(client, tier, key):
    register(client)
    storage = S3()
    tier.audio_proxy = AudioProxy(BUCKET, REGION, storage)
    assert client.get("/api/material-audio", params={"key": key}).status_code == 404
    assert storage.calls == []


@pytest.mark.parametrize("url", [
    "https://other.s3.cn-northwest-1.amazonaws.com.cn/" + KEY,
    SOURCE + "web/users.json",
    "http://test-audio.s3.cn-northwest-1.amazonaws.com.cn/" + KEY,
    SOURCE + KEY.replace("turn_000", "turn_001"),
])
def test_rewrite_rejects_wrong_bucket_private_files_and_wrong_clip(url):
    with pytest.raises(ValueError):
        AudioProxy(BUCKET, REGION).rewrite_links({"urls": {"0": url}})


@pytest.mark.parametrize("code,status", [("NoSuchKey", 404), ("InvalidRange", 416),
                                        ("AccessDenied", 502)])
def test_storage_errors_are_bounded_and_do_not_expose_credentials(client, tier, code, status):
    register(client)
    storage = S3()
    storage.error = code
    tier.audio_proxy = AudioProxy(BUCKET, REGION, storage)
    response = client.get("/api/material-audio", params={"key": KEY})
    assert response.status_code == status


def test_multiple_ranges_are_rejected_without_s3_call(client, tier):
    register(client)
    storage = S3()
    tier.audio_proxy = AudioProxy(BUCKET, REGION, storage)
    response = client.get("/api/material-audio", params={"key": KEY},
                          headers={"Range": "bytes=0-7,12-15"})
    assert response.status_code == 416
    assert storage.calls == []
