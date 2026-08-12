"""Bridge from the backend to ``audio_storage``. Construction only, no policy.

Everything about how a material is synthesised, keyed or routed lives in ``audio_storage``; this
module decides only *where* -- which bucket, which region -- and builds the two clients. Keeping
it this thin means the backend has no second opinion about S3 layout or cache keys, which is the
only way the review system's "state_store is the sole interface" rule survives contact with a
caller.

boto3 is not imported here. Both clients import it lazily inside their own constructors, so
``backend.audio`` stays importable in a unit test with no AWS present -- which is what lets the
selection tests run against InMemoryObjectStore.
"""

from __future__ import annotations

import os
from typing import Optional, Tuple

__all__ = [
    "bucket_name",
    "region_name",
    "build_state_store",
    "build_version_audio_store",
    "build_polly",
    "build_clients",
]

# No default bucket. A wrong-but-plausible default would silently write a real material into
# someone else's prefix; an unset variable fails on the first call with an actionable message.
BUCKET_ENV = "IELTS_AUDIO_BUCKET"
REGION_ENV = "IELTS_AUDIO_REGION"


class AudioNotConfigured(RuntimeError):
    """The bucket is not configured, so nothing can be published."""


def bucket_name() -> str:
    bucket = os.environ.get(BUCKET_ENV, "").strip()
    if not bucket:
        raise AudioNotConfigured(
            "set {0} to the materials bucket; refusing to guess a bucket name".format(BUCKET_ENV)
        )
    return bucket


def region_name() -> Optional[str]:
    return os.environ.get(REGION_ENV) or os.environ.get("AWS_REGION") or None


def build_state_store(store=None):
    """A StateStore over S3, or over whatever object store the caller supplies."""
    from audio_storage.state_store import StateStore

    if store is None:
        from audio_storage.object_store import S3ObjectStore

        store = S3ObjectStore(bucket_name())
    return StateStore(store), store


def build_version_audio_store(store=None):
    """A version-isolated audio store plus its backing object store.

    Runtime actions should use this for assessment versions instead of calling
    ``StateStore.locate`` with the logical material id.
    """
    from audio_storage.version_audio import VersionAudioStore

    if store is None:
        from audio_storage.object_store import S3ObjectStore

        store = S3ObjectStore(bucket_name())
    return VersionAudioStore(store), store


def build_polly(client=None):
    from audio_storage.synthesize import PollyClient

    return PollyClient(client=client, region_name=region_name())


def build_clients(store=None, polly_client=None) -> Tuple[object, object, object]:
    """(state_store, object_store, polly). One place that knows the full set."""
    state_store, backing = build_state_store(store)
    return state_store, backing, build_polly(polly_client)
