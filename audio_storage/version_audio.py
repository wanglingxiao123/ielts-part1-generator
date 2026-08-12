"""Version-isolated audio for immutable assessment versions.

This namespace is deliberately separate from ``StateStore``. Revised material must never call
``StateStore.locate(material_id)`` because that lookup can only find the originally published
material and would make a missing revised-version manifest play the old recording.
"""

from __future__ import annotations

import json
import re
from typing import Any, Dict, Optional

from . import synthesize
from .object_store import ObjectNotFound

NEEDS_SYNTHESIS = "needs_synthesis"
SYNTHESIZING = "synthesizing"
READY = "ready"
FAILED = "failed"

VERSION_AUDIO_ROOT = "_assessment_audio"
MANIFEST_NAME = "audio/manifest.json"
STATUS_NAME = "status.json"

_SAFE_SEGMENT = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,199}$")


class VersionAudioError(RuntimeError):
    """A version audio request violates the storage or lifecycle contract."""


class VersionAudioNotReady(VersionAudioError):
    """The selected assessment version has no complete recording."""


def _segment(value: str, label: str) -> str:
    value = str(value or "")
    if not _SAFE_SEGMENT.fullmatch(value):
        raise VersionAudioError("{0} is not a safe storage key segment".format(label))
    return value


def version_key(material_id: str, assessment_version_id: str) -> str:
    """Stable logical key stored with an assessment version's audio metadata."""
    return "{0}/{1}".format(
        _segment(material_id, "material_id"),
        _segment(assessment_version_id, "assessment_version_id"),
    )


def version_prefix(material_id: str, assessment_version_id: str) -> str:
    """S3 prefix unique to one logical material and immutable assessment version."""
    material = _segment(material_id, "material_id")
    version = _segment(assessment_version_id, "assessment_version_id")
    return "{0}/{1}/{2}/".format(VERSION_AUDIO_ROOT, material, version)


class VersionAudioStore:
    """Status, manifest and presigning for assessment-version audio only."""

    def __init__(self, store) -> None:
        self._store = store

    def initialize(self, material_id: str, assessment_version_id: str) -> Dict[str, Any]:
        status = self._status_payload(material_id, assessment_version_id, NEEDS_SYNTHESIS)
        self._put_status(material_id, assessment_version_id, status)
        return status

    def status(self, material_id: str, assessment_version_id: str) -> Dict[str, Any]:
        prefix = version_prefix(material_id, assessment_version_id)
        try:
            stored = json.loads(self._store.get(prefix + STATUS_NAME))
        except ObjectNotFound:
            stored = self._status_payload(
                material_id, assessment_version_id, NEEDS_SYNTHESIS
            )
        except (TypeError, ValueError) as exc:
            raise VersionAudioError("version audio status is invalid: {0}".format(exc))

        state = stored.get("status")
        if state not in {NEEDS_SYNTHESIS, SYNTHESIZING, READY, FAILED}:
            raise VersionAudioError("unknown version audio status {0!r}".format(state))

        # A ready marker without its completeness sentinel is not ready. Most importantly,
        # this path does not ask StateStore for the original material's manifest.
        if state == READY and not self._store.head(prefix + MANIFEST_NAME):
            stored = dict(stored)
            stored["status"] = NEEDS_SYNTHESIS
            stored.pop("manifest", None)
        return stored

    def manifest(self, material_id: str, assessment_version_id: str) -> dict:
        prefix = version_prefix(material_id, assessment_version_id)
        try:
            return json.loads(self._store.get(prefix + MANIFEST_NAME))
        except ObjectNotFound:
            raise VersionAudioNotReady(
                "assessment version {0} has no audio manifest".format(
                    assessment_version_id
                )
            )
        except (TypeError, ValueError) as exc:
            raise VersionAudioError("version audio manifest is invalid: {0}".format(exc))

    def presign(
        self,
        material_id: str,
        assessment_version_id: str,
        *,
        ttl_seconds: int = 3600,
    ) -> Dict[int, str]:
        current = self.status(material_id, assessment_version_id)
        if current["status"] != READY:
            raise VersionAudioNotReady(
                "assessment version {0} audio is {1}".format(
                    assessment_version_id, current["status"]
                )
            )
        manifest = self.manifest(material_id, assessment_version_id)
        prefix = version_prefix(material_id, assessment_version_id)
        urls: Dict[int, str] = {}
        for clip in manifest.get("clips", []):
            try:
                turn_index = int(clip["turn_index"])
                relative_key = str(clip["key"])
            except (KeyError, TypeError, ValueError):
                raise VersionAudioError("version audio manifest has a malformed clip")
            if not relative_key.startswith("audio/") or ".." in relative_key.split("/"):
                raise VersionAudioError("version audio manifest has an unsafe clip key")
            try:
                urls[turn_index] = self._store.presign(
                    prefix + relative_key, int(ttl_seconds)
                )
            except ObjectNotFound:
                raise VersionAudioNotReady(
                    "assessment version {0} is missing audio for turn {1}".format(
                        assessment_version_id, turn_index
                    )
                )
        return urls

    def synthesize(
        self,
        material_id: str,
        assessment_version_id: str,
        material: dict,
        blueprint: dict,
        *,
        polly,
        scenario_key: str,
        on_event=None,
        synthesized_at: Optional[str] = None,
    ):
        """Generate this version only; the original state prefix is never consulted."""
        prefix = version_prefix(material_id, assessment_version_id)
        self._put_status(
            material_id,
            assessment_version_id,
            self._status_payload(material_id, assessment_version_id, SYNTHESIZING),
        )
        try:
            result = synthesize._synthesize_material_at_prefix(
                material,
                material_id=material_id,
                scenario_key=scenario_key,
                store=self._store,
                polly=polly,
                destination_prefix=prefix,
                blueprint=blueprint,
                synthesized_at=synthesized_at,
                on_event=on_event,
            )
        except BaseException as exc:
            self._put_status(
                material_id,
                assessment_version_id,
                self._status_payload(
                    material_id,
                    assessment_version_id,
                    FAILED,
                    error="{0}: {1}".format(type(exc).__name__, str(exc)[:300]),
                ),
            )
            raise

        if not result.ok:
            self._put_status(
                material_id,
                assessment_version_id,
                self._status_payload(
                    material_id,
                    assessment_version_id,
                    FAILED,
                    error="turns {0} failed to synthesise".format(
                        sorted(result.failed)
                    ),
                ),
            )
            return result

        # Clips are written first, manifest is the completeness sentinel, and ready is last.
        self._store.put(
            prefix + MANIFEST_NAME,
            json.dumps(
                result.manifest, ensure_ascii=False, indent=2, sort_keys=False
            ).encode("utf-8"),
        )
        self._put_status(
            material_id,
            assessment_version_id,
            self._status_payload(
                material_id,
                assessment_version_id,
                READY,
            ),
        )
        return result

    def preview(
        self,
        material_id: str,
        assessment_version_id: str,
        material: dict,
        blueprint: dict,
        **kwargs,
    ):
        """Backend-callable alias: preview synthesis has the same isolated lifecycle."""
        return self.synthesize(
            material_id,
            assessment_version_id,
            material,
            blueprint,
            **kwargs,
        )

    def _status_payload(
        self,
        material_id: str,
        assessment_version_id: str,
        status: str,
        **extra: Any,
    ) -> Dict[str, Any]:
        payload: Dict[str, Any] = {
            "material_id": _segment(material_id, "material_id"),
            "assessment_version_id": _segment(
                assessment_version_id, "assessment_version_id"
            ),
            "version_key": version_key(material_id, assessment_version_id),
            "status": status,
        }
        payload.update(extra)
        return payload

    def _put_status(
        self,
        material_id: str,
        assessment_version_id: str,
        payload: Dict[str, Any],
    ) -> None:
        self._store.put(
            version_prefix(material_id, assessment_version_id) + STATUS_NAME,
            json.dumps(
                payload, ensure_ascii=False, indent=2, sort_keys=False
            ).encode("utf-8"),
        )


def version_audio_status(
    store, material_id: str, assessment_version_id: str
) -> Dict[str, Any]:
    return VersionAudioStore(store).status(material_id, assessment_version_id)


def initialize_version_audio(
    store, material_id: str, assessment_version_id: str
) -> Dict[str, Any]:
    return VersionAudioStore(store).initialize(material_id, assessment_version_id)


def synthesize_version_audio(
    store,
    polly,
    material_id: str,
    assessment_version_id: str,
    material: dict,
    blueprint: dict,
    *,
    scenario_key: str,
    on_event=None,
    synthesized_at: Optional[str] = None,
):
    return VersionAudioStore(store).synthesize(
        material_id,
        assessment_version_id,
        material,
        blueprint,
        polly=polly,
        scenario_key=scenario_key,
        on_event=on_event,
        synthesized_at=synthesized_at,
    )


def preview_version_audio(
    store,
    polly,
    material_id: str,
    assessment_version_id: str,
    material: dict,
    blueprint: dict,
    *,
    scenario_key: str,
    on_event=None,
    synthesized_at: Optional[str] = None,
):
    return synthesize_version_audio(
        store,
        polly,
        material_id,
        assessment_version_id,
        material,
        blueprint,
        scenario_key=scenario_key,
        on_event=on_event,
        synthesized_at=synthesized_at,
    )


def presign_version_audio(
    store,
    material_id: str,
    assessment_version_id: str,
    *,
    ttl_seconds: int = 3600,
) -> Dict[int, str]:
    return VersionAudioStore(store).presign(
        material_id, assessment_version_id, ttl_seconds=ttl_seconds
    )
