"""Runtime blindness guard: the third of four defences from design.md §4.

The audit step must never see the generator's blueprint. Type isolation (defence 1) and the
CI grep (defence 2) protect the source; this module protects the wire. It scans the assembled
prompt immediately before the request leaves the process and raises rather than silently
continuing, because a leak's only symptom is a score that comes out too high -- there is no
error to notice and no way to spot it in the delivered artifact afterwards.

Two tiers, for a reason found during implementation. design.md §4 specifies one scan over the
"complete prompt string", but the audit skill's own instructions say *"If a generator's
blueprint, item list, or information-point annotation is available, do not read it"* -- the
word `blueprint` is in the human-maintained SKILL.md, so a single strict scan would reject
every audit call and the guard would get switched off within a day. So:

* the **payload** (everything the orchestrator assembles: material, metrics, any framing) gets
  the strict scan, since that is the only text a careless parameter addition can reach;
* the **reference text** (system prompt read verbatim from skill files) gets scanned for
  JSON-field forms only, which is what an actual leak of serialised blueprint data looks like.
"""

from __future__ import annotations

from typing import Iterable, List, Sequence

__all__ = [
    "BLUEPRINT_ONLY_KEYS",
    "BLUEPRINT_JSON_FIELDS",
    "BlindnessViolation",
    "assert_blind",
    "assert_reference_text_blind",
    "blueprint_key_hits",
]


class BlindnessViolation(RuntimeError):
    """Raised when an audit prompt carries generator-side planning information."""


# Every entry appears in blueprint.schema.json and in neither material.schema.json nor
# audit.schema.json -- verified against the three frozen schemas, not assumed. skill-contract's
# FORBIDDEN_KEYS already keeps question/answer data out of the material.
#
# `target` and `distractor` are matched in quoted JSON-field form only. A dialogue turn can
# legitimately contain those English words ("our target date"), and a guard that blocked valid
# materials would train people to disable it.
#
# `confirmed` was in the design's draft list but is NOT usable: audit.schema.json uses it as a
# `clarity` enum value, so it appears in the audit skill's own rubric and in legitimate audit
# output. Including it would fire on every call.
BLUEPRINT_ONLY_KEYS = (
    "blueprint",
    "form_group",
    "question_type_coverage",
    "item_form",
    "indirect_confirmation",
    "narration_mode",
    "split_after",
    '"target"',
    '"distractor"',
)

# Serialised-JSON forms. These cannot appear in English prose, so they are safe to apply to
# human-written skill text that discusses the blueprint in order to forbid reading it.
BLUEPRINT_JSON_FIELDS = (
    '"blueprint"',
    '"form_group"',
    '"question_type_coverage"',
    '"item_form"',
    '"indirect_confirmation"',
    '"narration_mode"',
    '"split_after"',
    '"target"',
    '"distractor"',
)


def blueprint_key_hits(text: str, keys: Iterable[str] = BLUEPRINT_ONLY_KEYS) -> List[str]:
    """Return the blueprint-only identifiers present in ``text``.

    Case-insensitive: a leak that arrives as "Form_Group" is still a leak.
    """
    haystack = (text or "").casefold()
    return sorted({key for key in keys if key.casefold() in haystack})


def _fail(label: str, hits: Sequence[str]) -> None:
    raise BlindnessViolation(
        "audit %s leaked blueprint-only keys: %s" % (label, list(hits))
    )


def assert_blind(payload: str, label: str = "payload") -> None:
    """Fail the call if orchestrator-assembled audit text carries blueprint information.

    Raising is the point. Stripping the offending text would keep the batch running while
    quietly changing what was audited, and nobody would learn the guard had fired.
    """
    hits = blueprint_key_hits(payload, BLUEPRINT_ONLY_KEYS)
    if hits:
        _fail(label, hits)


def assert_reference_text_blind(text: str, label: str = "reference text") -> None:
    """Scan skill-sourced prompt text for serialised blueprint data.

    Looser than :func:`assert_blind` by design: the audit skill has to name the blueprint in
    order to forbid reading it, so prose mentions are expected and only JSON-field forms
    indicate real leaked data.
    """
    hits = blueprint_key_hits(text, BLUEPRINT_JSON_FIELDS)
    if hits:
        _fail(label, hits)
