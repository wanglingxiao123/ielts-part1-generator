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

import time
from pathlib import Path
from typing import Iterable, List, Sequence

__all__ = [
    "BLUEPRINT_ONLY_KEYS",
    "BLUEPRINT_JSON_FIELDS",
    "BlindnessViolation",
    "assert_blind",
    "assert_no_plan_on_disk",
    "assert_reference_text_blind",
    "blueprint_key_hits",
    "plan_files_on_disk",
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


# Where a generation agent's scratch files land. Same roots the purge covers; scanning the whole
# filesystem would be slow and would match this repository's own fixtures.
_SCRATCH_ROOTS = ("/tmp", "/var/tmp")

# Only files this process could have produced are considered. Measured why: a developer machine's
# /tmp held four blueprint files from runs days earlier, and a guard that fires on those fires on
# every run, gets diagnosed as noise, and is switched off -- taking the real check with it. An older
# file is also not this material's answer key, which is what the guard is protecting.
_PROCESS_STARTED_AT = time.time()


def plan_files_on_disk(since: float = None) -> List[str]:
    """Scratch JSON files written since this process started that contain blueprint data.

    Content-based rather than name-based. The generator picks its own filenames, and a plan written
    to ``draft2.json`` is exactly as readable as one written to ``blueprint.json`` -- an audit agent
    listing a directory sees both.

    ``since`` is injectable so tests can pin the window rather than depend on wall-clock ordering.
    """
    cutoff = _PROCESS_STARTED_AT if since is None else since
    found: List[str] = []
    for root in _SCRATCH_ROOTS:
        directory = Path(root)
        if not directory.is_dir():
            continue
        try:
            entries = sorted(directory.glob("*.json"))
        except OSError:
            continue
        for path in entries:
            try:
                if path.stat().st_mtime < cutoff:
                    continue
                text = path.read_text(encoding="utf-8", errors="ignore")
            except OSError:
                continue
            if blueprint_key_hits(text, BLUEPRINT_JSON_FIELDS):
                found.append(str(path))
    return found


def assert_no_plan_on_disk() -> None:
    """Fail before an audit call if any scratch file still holds the generator's plan.

    The second layer behind ``purge_plan_scratch``, and it exists because that one depends on parsing
    the agent's own shell commands. A path built by string interpolation, written through a Python
    heredoc, or produced by a command form the regex does not cover survives the purge -- and what
    survives is a file naming which turn carries which answer, at a path the audit agent can read
    with ``file_read``, which resolves against the process working directory and consults no sandbox.

    Raising rather than deleting here. A file the purge missed means the purge's assumption about how
    the agent writes files is wrong, and deleting it quietly would keep that wrong assumption in place
    for every later material. This is the same reasoning as ``assert_blind``: the whole failure mode
    is silent, so the guard has to be the thing that makes noise.
    """
    stale = plan_files_on_disk()
    if stale:
        raise BlindnessViolation(
            "generator plan data is still on disk and the audit agent could read it: %s"
            % ", ".join(stale[:5])
        )


def assert_reference_text_blind(text: str, label: str = "reference text") -> None:
    """Scan skill-sourced prompt text for serialised blueprint data.

    Looser than :func:`assert_blind` by design: the audit skill has to name the blueprint in
    order to forbid reading it, so prose mentions are expected and only JSON-field forms
    indicate real leaked data.
    """
    hits = blueprint_key_hits(text, BLUEPRINT_JSON_FIELDS)
    if hits:
        _fail(label, hits)
