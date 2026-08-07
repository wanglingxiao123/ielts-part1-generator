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
    "ANSWER_ONLY_KEYS",
    "BLUEPRINT_ONLY_KEYS",
    "BLUEPRINT_JSON_FIELDS",
    "FEASIBILITY_ITEM_COUNT",
    "FEASIBILITY_PLAN_VERSION",
    "BlindnessViolation",
    "MissingPlanViolation",
    "assert_answer_blind",
    "assert_blind",
    "assert_carries_plan",
    "assert_no_plan_on_disk",
    "assert_reference_text_blind",
    "blueprint_key_hits",
    "plan_files_on_disk",
    "sweep_plan_files_on_disk",
]


class BlindnessViolation(RuntimeError):
    """Raised when an audit prompt carries generator-side planning information."""


class MissingPlanViolation(RuntimeError):
    """Raised when a non-blind prompt does not carry the usable plan it is supposed to judge."""


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
#
# The v2 additions below are only ever added, never swapped in: `question_type_coverage` stays
# because archived prompts and records still carry it, so dropping it would stop catching the leak
# it was added for. Each of the four was counted against material.schema.json, audit.schema.json and
# the whole audit pool before being added -- 0 occurrences each, so none can repeat `confirmed`'s
# false-positive problem. All four are snake_case identifiers that cannot occur in English prose,
# which is why they need no quoting the way `target` and `distractor` do.
BLUEPRINT_ONLY_KEYS = (
    "blueprint",
    "form_group",
    "question_type_coverage",
    "completion_layout_coverage",
    "item_form",
    "indirect_confirmation",
    "narration_mode",
    "split_after",
    "response_form",
    "answer_category",
    "narrator_window_id",
    '"target"',
    '"distractor"',
)

# Serialised-JSON forms. These cannot appear in English prose, so they are safe to apply to
# human-written skill text that discusses the blueprint in order to forbid reading it.
BLUEPRINT_JSON_FIELDS = (
    '"blueprint"',
    '"form_group"',
    '"question_type_coverage"',
    '"completion_layout_coverage"',
    '"item_form"',
    '"indirect_confirmation"',
    '"narration_mode"',
    '"split_after"',
    '"response_form"',
    '"answer_category"',
    '"narrator_window_id"',
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


# The question auditor's own forbidden set, and a SEPARATE tuple rather than a relaxation of
# BLUEPRINT_ONLY_KEYS above. The reason is recorded in question_package.schema.json's own description
# and is worth restating where the code is: a question face legitimately carries `response_form`,
# `answer_category` and `narrator_window_id`, all three of which are in BLUEPRINT_ONLY_KEYS -- so
# `assert_blind` cannot pass a question-audit payload. Removing them from that tuple to make one guard
# serve both callers would silently reopen the material-audit leak the tuple was built for, and the
# symptom on that side is a score that comes out too high with no error anywhere.
#
# What is forbidden here is the ANSWER, not the plan's shape. Each entry names a field that exists only
# in the two blocks a question auditor must never receive:
#   canonical / alternatives / counting_rule -- answer_key
#   quote / turn_index / paraphrase_relation / carrier_entity / evidence_entity /
#     proposition_relation / proposition_alignment_result -- evidence
#   answer_key / evidence / blueprint -- the block names themselves, and the plan whose
#     `items[].target` IS the answer
#
# Four of them are matched in quoted JSON-field form only, for the reason `"target"` and `"distractor"`
# are on the material side: `canonical`, `alternatives`, `evidence` and above all `quote` are ordinary
# English words that a Part 1 dialogue can legitimately contain -- a removals company quoting a price
# is a textbook Part 1 scenario -- and a guard that rejects valid materials trains people to disable
# it, taking the real check with it. A leaked answer block serialises as `"canonical": "Anna Woods"`,
# so the quoted form still catches the thing this guard exists for.
#
# The rest are snake_case identifiers that cannot occur in prose and so need no quoting. Counted
# against the real inputs before being added: 0 hits across /tmp/qgen_real/material.json, the same
# run's question_face, and skills/shared/tests/fixtures/material_valid.json -- so no entry can repeat
# `confirmed`'s fires-on-every-call problem.
#
# `target` is deliberately NOT here at all: a carrier can say "our target date" in either form, and
# `blueprint` already catches a serialised plan.
ANSWER_ONLY_KEYS = (
    "answer_key",
    '"canonical"',
    '"alternatives"',
    "counting_rule",
    '"evidence"',
    '"quote"',
    "turn_index",
    "paraphrase_relation",
    "carrier_entity",
    "evidence_entity",
    "proposition_relation",
    "proposition_alignment_result",
    "blueprint",
)


def assert_answer_blind(payload: str, label: str = "payload") -> None:
    """Fail the call if orchestrator-assembled question-audit text carries answers or evidence.

    The wire guard for the question audit, and the counterpart of :func:`assert_blind` rather than a
    reuse of it -- see ``ANSWER_ONLY_KEYS`` above for why one tuple cannot serve both.

    Raising rather than stripping, for the same reason as the material side and more sharply: the
    auditor's whole product is the answer it rebuilt without a key. An audit that saw the key does not
    fail, it *agrees* -- every reconstruction matches, the uniqueness check passes, the status comes out
    clean, and the document has exactly the shape of a genuine review. Stripping the offending text
    would keep the batch running while quietly changing what was audited, and nobody would learn the
    guard had fired.

    Applied to the payload only. The skill's own markdown says "never accepts a supplied answer, ...
    a quotation table", so the words appear in the reference text by necessity; that tier is
    :func:`assert_reference_text_blind`'s.
    """
    hits = blueprint_key_hits(payload, ANSWER_ONLY_KEYS)
    if hits:
        raise BlindnessViolation(
            "question audit %s leaked answer-only keys: %s" % (label, list(hits))
        )


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
            # Recursive: `file_read` takes any absolute path, so a plan one directory down is exactly
            # as readable as one at the top level. A non-recursive glob made `/tmp/x/blueprint.json`
            # invisible to the guard while leaving it readable by the agent.
            entries = sorted(directory.rglob("*.json"))
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


def sweep_plan_files_on_disk(since: float = None) -> List[str]:
    """Delete scratch plan files and return what was removed. Called before an audit.

    **Deleting rather than raising, and the reason is a measured blast radius.** The first version of
    this raised ``BlindnessViolation``, which read as the right instinct: a leftover plan means an
    assumption about how the agent writes files is wrong, and hiding that keeps it wrong. But the
    cutoff is the *process* start, and AgentCore Runtime instances are long-lived, so one survivor
    file poisoned every subsequent material in that instance -- measured through a real CLI batch: one
    leftover file, all 3 slots failed, 9 generation attempts and 6 refill rounds spent, zero materials
    produced. A guard whose false-positive cost is the whole batch does not get to be strict.

    So the noise moves and the safety stays: the file is removed, and the caller reports what it
    removed. Nothing about the audit proceeds with a readable plan on disk either way, which is the
    property that actually matters.

    ``GenerationWorkspace`` makes this a backstop rather than the mechanism -- it deletes its own tree,
    so anything reaching here was written outside the directory the agent was told to use.
    """
    swept: List[str] = []
    for path in plan_files_on_disk(since):
        try:
            Path(path).unlink()
        except OSError:
            continue
        swept.append(path)
    return swept


def assert_no_plan_on_disk() -> None:
    """Sweep, then fail only if a plan file survived deletion.

    Reaching the raise means a readable plan is on disk and could not be removed -- a permissions or
    filesystem problem, not a stale file -- and continuing would hand the auditor the answers.
    """
    sweep_plan_files_on_disk()
    stale = plan_files_on_disk()
    if stale:
        raise BlindnessViolation(
            "generator plan data is on disk, could not be removed, and the audit agent could read "
            "it: %s" % ", ".join(stale[:5])
        )


# The plan shape a non-blind judge must be given. Both values are duplicated from the validator and
# named here rather than inlined, so a drift shows up as a failing test instead of as a guard that
# quietly accepts the wrong thing:
#   FEASIBILITY_PLAN_VERSION -- `validate_part1.BLUEPRINT_SCHEMA_VERSION` (also `V_KEY`'s only
#     accepted value; anything else reads as "version unknown", not as another version)
#   FEASIBILITY_ITEM_COUNT   -- `validate_part1.py`'s `len(items) != 10` check, and
#     `question_feasibility_preflight.ITEM_COUNT`
FEASIBILITY_PLAN_VERSION = 2
FEASIBILITY_ITEM_COUNT = 10

_PLAN_VERSION_KEY = "blueprint_schema_version"


def assert_carries_plan(blueprint: object, label: str = "feasibility payload") -> None:
    """Fail the call unless a usable v2 ten-item plan is actually being handed over.

    The mirror image of :func:`assert_blind`, and it exists because that mirror failure is invisible.
    A blindness leak at least changes the payload; a *missing* plan changes nothing observable. If the
    blueprint argument goes astray -- a default value survives, an upstream field is read under the
    wrong key, an empty dict is passed -- the judge sees only the script and answers anyway, with
    exactly the confidence it would have had with the plan. Its reply is the same shape, nothing
    raises, and the verdict is about a question nobody asked: "could ten items be written from *some*
    plan" instead of "from *this* one".

    **Takes the blueprint object, not the assembled payload string.** Counting ten of anything in
    serialised text means counting substrings, which turns a structural question into a textual one.
    Call this before assembling the message.

    **Deliberately not built on :func:`blueprint_key_hits`.** The first design of this guard reused
    it, and that guard could never fail: ``BLUEPRINT_ONLY_KEYS`` contains the bare word
    ``blueprint``, which the payload's own ``## blueprint.json`` heading always matches -- so ``{}``,
    an empty ``items`` list, and a v1 plan would all have been waved through. An assertion that
    cannot fail is worse than no assertion, because it is read as coverage.

    The four criteria are ordered, each subscripting what the previous one established. The version
    criterion demands ``== 2`` rather than restating the validator's three-branch reading of the
    field: the question here is "is this a v2 plan", not "how should a version value be interpreted",
    and that second question already has exactly one implementation.
    """
    if not isinstance(blueprint, dict) or not blueprint:
        raise MissingPlanViolation(
            "%s carries no plan to judge (%s); a non-blind judgment without the plan is a "
            "judgment of the script alone" % (label, type(blueprint).__name__)
        )

    version = blueprint.get(_PLAN_VERSION_KEY)
    # `bool` first: `True == 1` and, more to the point, `isinstance(True, int)` holds, so a bare
    # int check would accept `True` as a version number.
    if isinstance(version, bool) or not isinstance(version, int) \
            or version != FEASIBILITY_PLAN_VERSION:
        raise MissingPlanViolation(
            "%s carries a plan whose %s is %r, not %d; only v%d plans are judged"
            % (label, _PLAN_VERSION_KEY, version, FEASIBILITY_PLAN_VERSION,
               FEASIBILITY_PLAN_VERSION)
        )

    items = blueprint.get("items")
    if not isinstance(items, list) or len(items) != FEASIBILITY_ITEM_COUNT:
        raise MissingPlanViolation(
            "%s carries %s plan items, not %d; the judgment is about a specific ten"
            % (label, len(items) if isinstance(items, list) else "no", FEASIBILITY_ITEM_COUNT)
        )

    # Only that each item is an object. What is *inside* an item is `validate_part1.py`'s question,
    # and re-checking it here would be a second implementation of the item contract.
    bad = [index for index, item in enumerate(items, 1) if not isinstance(item, dict)]
    if bad:
        raise MissingPlanViolation(
            "%s carries plan items that are not objects at position(s) %s"
            % (label, bad[:5])
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
