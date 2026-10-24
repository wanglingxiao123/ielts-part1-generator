"""Exercise the deployment preset in isolated imports, including Polly request parameters."""
import json
import os
import subprocess
import sys

import pytest


@pytest.mark.parametrize("preset,engine,voices", [
    ("china-standard", "standard", {"Emma", "Amy", "Brian"}),
    ("british-neural", "neural", {"Brian", "Amy", "Arthur"}),
])
def test_preset_controls_voice_map_manifest_cache_and_sdk_call(preset, engine, voices):
    code = """
import io, json
from audio_storage import voice, manifest
from audio_storage.synthesize import PollyClient
class Client:
    def synthesize_speech(self, **kwargs):
        self.request = kwargs
        return {"AudioStream": io.BytesIO(b"audio")}
client = Client()
mapping = voice.resolve_voice_map("fixture-refund")
PollyClient(client=client).synthesize("<speak>Hello.</speak>", mapping["speaker1"])
print(json.dumps({"engine": voice.ENGINE, "voices": list(mapping.values()),
    "request": client.request, "distinct": mapping["speaker2"] != mapping["speaker3"],
    "cache_differs": manifest.cache_key("Brian", "standard", "24000", "hello")
        != manifest.cache_key("Brian", "neural", "24000", "hello")}))
"""
    result = subprocess.run(
        [sys.executable, "-c", code], env={**os.environ, "IELTS_POLLY_PRESET": preset},
        capture_output=True, text=True, check=True,
    )
    data = json.loads(result.stdout)
    assert data["engine"] == data["request"]["Engine"] == engine
    assert set(data["voices"]) == voices
    assert data["distinct"] and data["cache_differs"]


def test_unknown_preset_fails_instead_of_silent_fallback():
    result = subprocess.run(
        [sys.executable, "-c", "from audio_storage import voice"],
        env={**os.environ, "IELTS_POLLY_PRESET": "unknown"},
        capture_output=True, text=True,
    )
    assert result.returncode != 0
    assert "Unknown IELTS_POLLY_PRESET" in result.stderr
