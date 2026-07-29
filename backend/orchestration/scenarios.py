"""Scenario catalogue: the backend is the single source of truth (prd.md R10).

The frontend fetches this through ``action: "list_scenarios"`` and keeps no local copy. A local
copy drifts, and a drifted id means a user ticks a scenario the backend cannot resolve -- which
surfaces as a failed batch rather than a validation message.
"""

from __future__ import annotations

import re
from typing import Any, Dict, List, Optional

import yaml

from .. import paths

__all__ = ["Scenario", "ScenarioCatalogue", "load_catalogue", "title_for_key",
           "InvalidScenario"]

# Control characters (except tab/newline) plus prompt-injection markers. A custom scenario is
# free-form user text that lands in a system-prompted model call, so it is filtered rather than
# trusted -- but only for structural abuse, not for content.
_CONTROL_RE = re.compile(r"[\x00-\x08\x0b-\x1f\x7f]")
_INJECTION_PATTERNS = (
    "ignore previous",
    "ignore all previous",
    "disregard previous",
    "system:",
    "assistant:",
    "</system",
    "<|im_start|>",
    "<|im_end|>",
    "new instructions",
)


class InvalidScenario(ValueError):
    """A custom scenario failed input validation."""


class Scenario(object):
    __slots__ = ("id", "category", "title_zh", "prompt_hint", "default_count")

    def __init__(
        self,
        id: str,
        category: str,
        title_zh: str,
        prompt_hint: str,
        default_count: int = 2,
    ) -> None:
        self.id = id
        self.category = category
        self.title_zh = title_zh
        self.prompt_hint = prompt_hint
        self.default_count = default_count

    def as_dict(self) -> Dict[str, Any]:
        return {
            "id": self.id,
            "category": self.category,
            "title_zh": self.title_zh,
            "prompt_hint": self.prompt_hint,
            "default_count": self.default_count,
        }

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return "Scenario(%r)" % self.id


class ScenarioCatalogue(object):
    __slots__ = ("version", "default_count", "max_batch", "categories", "_by_id",
                 "custom_enabled", "custom_max_length")

    def __init__(self, raw: Dict[str, Any]) -> None:
        self.version = raw.get("version", 1)
        self.default_count = int(raw.get("default_count", 2))
        self.max_batch = int(raw.get("max_batch", 6))
        custom = raw.get("custom_scenario") or {}
        self.custom_enabled = bool(custom.get("enabled", False))
        self.custom_max_length = int(custom.get("max_length", 200))

        self.categories: List[Dict[str, Any]] = []
        self._by_id: Dict[str, Scenario] = {}
        for category in raw.get("categories") or []:
            if not isinstance(category, dict):
                continue
            entries = []
            for item in category.get("scenarios") or []:
                if not isinstance(item, dict):
                    continue
                scenario = Scenario(
                    id=str(item.get("id", "")),
                    category=str(category.get("id", "")),
                    title_zh=str(item.get("title_zh", "")),
                    prompt_hint=" ".join(str(item.get("prompt_hint", "")).split()),
                    default_count=int(item.get("default_count", self.default_count)),
                )
                if scenario.id:
                    self._by_id[scenario.id] = scenario
                    entries.append(scenario.as_dict())
            self.categories.append({
                "id": category.get("id"),
                "title_zh": category.get("title_zh"),
                "scenarios": entries,
            })

    def get(self, scenario_id: str) -> Optional[Scenario]:
        return self._by_id.get(scenario_id)

    def ids(self) -> List[str]:
        return list(self._by_id.keys())

    def as_dict(self) -> Dict[str, Any]:
        return {
            "version": self.version,
            "default_count": self.default_count,
            "max_batch": self.max_batch,
            "categories": self.categories,
            "custom_scenario": {
                "enabled": self.custom_enabled,
                "max_length": self.custom_max_length,
            },
        }

    def build_custom(self, text: str, scenario_id: str = "custom") -> Scenario:
        """Validate and pass through one user-written scenario.

        Rejects rather than sanitises. Silently stripping an injection attempt would produce a
        material from a prompt the user never sees and cannot debug.
        """
        if not self.custom_enabled:
            raise InvalidScenario("custom scenarios are disabled")
        if not isinstance(text, str) or not text.strip():
            raise InvalidScenario("custom scenario must be non-empty text")
        cleaned = " ".join(text.split())
        if len(cleaned) > self.custom_max_length:
            raise InvalidScenario(
                "custom scenario exceeds %d characters" % self.custom_max_length
            )
        if _CONTROL_RE.search(text):
            raise InvalidScenario("custom scenario contains control characters")
        lowered = cleaned.casefold()
        for pattern in _INJECTION_PATTERNS:
            if pattern in lowered:
                raise InvalidScenario("custom scenario contains a prompt-injection pattern")
        return Scenario(
            id=scenario_id,
            category="custom",
            title_zh="自定义场景",
            prompt_hint=cleaned,
            default_count=self.default_count,
        )


def load_catalogue(path: Optional[Any] = None) -> ScenarioCatalogue:
    raw = yaml.safe_load((path or paths.scenarios_path()).read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise ValueError("scenarios.yaml must contain a mapping")
    return ScenarioCatalogue(raw)


_titles: Optional[Dict[str, str]] = None


def title_for_key(scenario_key: str) -> str:
    """The Chinese topic for an S3 scenario_key, or "" when there is none.

    Used by the candidate card summary, which is built on a request already up against the
    15-minute wall -- so the catalogue is read once per process and cached. A custom scenario's
    key is a hash with no title, and "" is the honest answer: the summary then renders its
    feature list alone rather than inventing a topic.

    Never raises. A missing or malformed scenarios.yaml must not turn a generated material into
    a failed one over a display string.
    """
    global _titles
    if _titles is None:
        try:
            catalogue = load_catalogue()
            _titles = {
                str(entry.get("id")): str(entry.get("title_zh") or "")
                for category in catalogue.categories
                for entry in category["scenarios"]
            }
        except Exception:  # noqa: BLE001 - a display string is never worth failing a batch
            _titles = {}
    return _titles.get(str(scenario_key), "")
