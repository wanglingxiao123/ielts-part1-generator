#!/usr/bin/env python3
"""Aggregate the question-feasibility verdict for one finished Part 1 material set.

Runs after the material is validated and audited, BEFORE any question is generated. It is a
*verdict aggregator*, not a validator: it counts nothing itself. The deterministic numbers come
from `validate_part1.py --json`; the semantic conclusion comes from a non-blind feasibility
audit (stage 3B). This module only decides what the two of them together mean.

Why the split matters (§5.4): the question stage may never touch the audible Script (SR-021) and
may not pick different information points, so "these ten points cannot carry ten reliable
questions" has to be decided here -- afterwards the only remedy is a whole new material set.

Deliberately absent: any regex, any loop over items, any attempt to judge naturalness. Those
belong to `validate_part1.py` (mechanical) or to the feasibility audit (semantic).
"""

from __future__ import annotations

import validate_part1 as validator  # module reference -- values are read at call time, see _thresholds

# Three client-named exits. Matched verbatim by downstream reports; do not rename.
PASS = "PASS"
PASS_WITH_JUSTIFICATION = "PASS_WITH_JUSTIFICATION"
REGENERATE_MATERIAL = "REGENERATE_MATERIAL"
# Three states that are NOT exits. The exits above mean "decided"; these mean "cannot decide".
# Folding either into REGENERATE_MATERIAL would assert "this material is unfit" -- an expensive
# claim that burns one outer-quota candidate swap and a full regeneration, while hiding the real
# fault (a system-side problem, or an archived record that nobody needs to regenerate).
SEMANTICS_MISSING = "SEMANTICS_MISSING"
VALIDATION_INCOMPLETE = "VALIDATION_INCOMPLETE"
UNSUPPORTED_VERSION = "UNSUPPORTED_VERSION"

VERSION_KEY = "blueprint_schema_version"
NUMERIC_KEY = "qr027_numeric_answers"
SPELLED_KEY = "qr027_spelled_answers"
LARGEST_KEY = "qr027_largest_category"
COUNT_KEYS = (NUMERIC_KEY, SPELLED_KEY, LARGEST_KEY)

SUPPORTED_VERSION = 2
REQUIRED_SEMANTICS = ("feasible", "reasons", "category_semantics_ok")


def _thresholds() -> tuple:
    """Read the thresholds at call time, never binding them at import.

    `from validate_part1 import QR027_MAX_NUMERIC` would copy the *value* into this module's
    namespace, after which monkeypatching the source module has no effect -- measured. The AC7
    test that claims to prove single-source-of-truth would then pass while proving nothing, which
    is worse than having no test at all.
    """
    return (validator.QR027_MAX_NUMERIC,
            validator.QR027_MIN_SPELLED,
            validator.QR027_MAX_SAME_CATEGORY)


class Verdict:
    """One outcome plus the reasons behind it. JSON-friendly via `as_dict`."""

    def __init__(self, outcome: str, reasons: list, qr027: dict | None = None,
                 justification: str | None = None) -> None:
        self.outcome = outcome
        self.reasons = reasons
        self.qr027 = qr027 or {}
        self.justification = justification

    def as_dict(self) -> dict:
        return {
            "outcome": self.outcome,
            "reasons": list(self.reasons),
            "qr027": dict(self.qr027),
            "justification": self.justification,
        }

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"Verdict({self.outcome}, reasons={self.reasons!r})"


def _qr027_snapshot(metrics: object) -> dict:
    """Whatever qr027_* keys are present, for the delivery report. Never raises."""
    if not isinstance(metrics, dict):
        return {}
    return {key: value for key, value in metrics.items() if str(key).startswith("qr027_")}


def _semantics_problem(feasibility: object) -> str | None:
    """Return a reason string when the semantic conclusion is unusable, else None.

    `missing` (key absent -> upstream produced nothing) and `invalid` (key present but wrong type
    -> upstream produced the wrong thing) point at different places to look, so they stay
    distinguishable in the reason even though both land on one outcome.
    """
    if feasibility is None:
        return "semantics missing: no feasibility result supplied"
    if not isinstance(feasibility, dict):
        return f"semantics invalid: feasibility is {type(feasibility).__name__}, expected object"
    for key in REQUIRED_SEMANTICS:
        if key not in feasibility:
            return f"semantics missing: feasibility.{key}"
    # `isinstance(x, bool)` rather than a truthiness test: `"false"` is a non-empty string and so
    # truthy, meaning an upstream bug that serialises booleans as strings would flip the verdict
    # the wrong way -- an unfeasible material set would read as feasible.
    for key in ("feasible", "category_semantics_ok"):
        if not isinstance(feasibility[key], bool):
            return (f"semantics invalid: feasibility.{key} is "
                    f"{type(feasibility[key]).__name__}, expected boolean")
    if not isinstance(feasibility["reasons"], list):
        return (f"semantics invalid: feasibility.reasons is "
                f"{type(feasibility['reasons']).__name__}, expected array")
    return None


def _justification_of(feasibility: dict) -> str | None:
    """The recorded QR-027 exception text, or None when no usable exception was requested.

    Structure only -- whether the reason actually holds is reviewed at the question-audit stage
    (§5.4). Judging the quality of the prose here would be exactly the "regex decides naturalness"
    overreach §5.4 forbids; checking nothing would turn PASS_WITH_JUSTIFICATION into a way around
    QR-027. A malformed `qr027_exception` reads as "no exception requested": it is a *request*, and
    a request that does not say anything is not a request. The three required semantic keys are
    *conclusions*, which is why an unusable one blocks the verdict instead.
    """
    exception = feasibility.get("qr027_exception")
    if not isinstance(exception, dict):
        return None
    if exception.get("requested") is not True:
        return None
    justification = exception.get("justification")
    if not isinstance(justification, str) or not justification.strip():
        return None
    return justification.strip()


def preflight(validation: object, feasibility: object) -> Verdict:
    """Decide whether question generation may start. Never raises on malformed input.

    Both parameters are annotated `object` on purpose: both arrive from another system (a
    subprocess' JSON, a model's output), so "the shape is wrong" is a real runtime case, not a
    defensive hypothetical. Annotating them `dict` would tell the next reader the shape is already
    guaranteed by the caller, which is precisely what this function must not assume.

    Note what is NOT a parameter: the blueprint. Without it this aggregator has no *ability* to
    recount targets or re-judge naturalness, so the §5.4 boundary is enforced by the signature
    rather than by a comment asking nicely.

    The gate order is itself the design -- see design.md D4. In particular an archived v1 record
    read with `--allow-v1` comes back `ok: true`, zero errors, and zero qr027_* keys (measured),
    so the version gate is the ONLY thing standing between it and a bogus verdict. There is no
    second line of defence behind it.
    """
    # Gate 0 -- shape. Must come first: every later gate subscripts `validation`, and raising
    # TypeError/KeyError would push the decision back onto a caller who can only guess from a
    # traceback whether to regenerate or to page someone.
    if not isinstance(validation, dict):
        return Verdict(VALIDATION_INCOMPLETE,
                       [f"validation invalid: result is {type(validation).__name__}, expected object"])
    if "metrics" not in validation:
        return Verdict(VALIDATION_INCOMPLETE, ["validation missing: metrics"])
    metrics = validation["metrics"]
    if not isinstance(metrics, dict):
        return Verdict(VALIDATION_INCOMPLETE,
                       [f"validation invalid: metrics is {type(metrics).__name__}, expected object"])

    qr027 = _qr027_snapshot(metrics)

    # Gate 1 -- version. Only a version that was actually read and is not 2 may be
    # UNSUPPORTED_VERSION. `validate_part1.py:466` writes None when it could not recognise the
    # value (`3`, `"2"`), so None means "I failed to determine the version", not "the version is
    # some unsupported value". Reporting None as UNSUPPORTED_VERSION would file a NEW record with
    # a corrupt version number as "this is a historical archive" -- and then nobody fixes the
    # corrupt version number.
    if VERSION_KEY not in metrics:
        return Verdict(VALIDATION_INCOMPLETE, [f"validation missing: metrics.{VERSION_KEY}"],
                       qr027=qr027)
    version = metrics[VERSION_KEY]
    if version is None:
        return Verdict(VALIDATION_INCOMPLETE,
                       [f"validation invalid: metrics.{VERSION_KEY} is null -- the validator could "
                        f"not determine the version, so it was not read as an unsupported one"],
                       qr027=qr027)
    if version != SUPPORTED_VERSION:
        return Verdict(UNSUPPORTED_VERSION,
                       [f"blueprint_schema_version is {version!r}; new generation accepts only "
                        f"{SUPPORTED_VERSION}. A v1 record is display-only and is not regenerated."],
                       qr027=qr027)

    # Gate 2 -- deterministic errors. After the version gate, not before: an archived record
    # carries the "version is missing" error, and checking errors first would hand every archived
    # record a REGENERATE_MATERIAL instead of UNSUPPORTED_VERSION.
    errors = validation.get("errors")
    if isinstance(errors, list) and errors:
        return Verdict(REGENERATE_MATERIAL,
                       [f"deterministic validation failed: {error}" for error in errors],
                       qr027=qr027)

    # Gate 3 -- completeness of what this function is about to read. `qr027_metrics` always runs
    # for version 2 (validate_part1.py:588), so a missing key means the validation flow did not
    # finish: a system-side problem, not unfit material. Absent metrics mean "not measured", not
    # zero -- the same rule as deterministic/validate.py:65. Treating them as zero would read
    # "spelled 0 < 4" and regenerate a material set that was never measured.
    incomplete = []
    for key in COUNT_KEYS:
        if key not in metrics:
            incomplete.append(f"validation missing: metrics.{key}")
        elif isinstance(metrics[key], bool) or not isinstance(metrics[key], int):
            incomplete.append(f"validation invalid: metrics.{key} is "
                              f"{type(metrics[key]).__name__}, expected integer")
    if incomplete:
        return Verdict(VALIDATION_INCOMPLETE, incomplete, qr027=qr027)

    # Gate 4 -- semantics. Before QR-027 (AC4): otherwise a set that clears all three QR-027
    # thresholds but cannot carry ten questions would collect a PASS. Deterministic-all-green is
    # not permission to generate.
    problem = _semantics_problem(feasibility)
    if problem is not None:
        return Verdict(SEMANTICS_MISSING, [problem], qr027=qr027)
    if feasibility["feasible"] is False:
        reasons = [str(reason) for reason in feasibility["reasons"]]
        return Verdict(REGENERATE_MATERIAL,
                       ["feasibility audit says these ten points cannot carry reliable, unique, "
                        "natural questions"] + reasons,
                       qr027=qr027)
    if feasibility["category_semantics_ok"] is False:
        reasons = [str(reason) for reason in feasibility["reasons"]]
        return Verdict(REGENERATE_MATERIAL,
                       ["feasibility audit rejected the answer_category semantics"] + reasons,
                       qr027=qr027)

    # Gate 5 -- QR-027. Judged rule by rule rather than by reading the pre-computed
    # `qr027_within_limits`: a PASS_WITH_JUSTIFICATION goes into the delivery report, and the
    # report needs to say which rule missed, by how much, against what threshold. The composite
    # boolean cannot say that.
    max_numeric, min_spelled, max_same_category = _thresholds()
    breaches = []
    if metrics[NUMERIC_KEY] > max_numeric:
        breaches.append(f"QR-027 numeric answers: {metrics[NUMERIC_KEY]} exceeds the maximum of "
                        f"{max_numeric}")
    if metrics[SPELLED_KEY] < min_spelled:
        breaches.append(f"QR-027 spelled answers: {metrics[SPELLED_KEY]} is below the minimum of "
                        f"{min_spelled}")
    if metrics[LARGEST_KEY] >= max_same_category:
        breaches.append(f"QR-027 same answer_category: {metrics[LARGEST_KEY]} items share one "
                        f"category, and {max_same_category} or more is already a breach")
    if not breaches:
        return Verdict(PASS, ["deterministic checks clear, QR-027 within limits, feasibility "
                              "audit approves"], qr027=qr027)

    justification = _justification_of(feasibility)
    if justification is not None:
        return Verdict(PASS_WITH_JUSTIFICATION, breaches, qr027=qr027, justification=justification)
    return Verdict(REGENERATE_MATERIAL,
                   breaches + ["no specific justification was recorded for the QR-027 exception"],
                   qr027=qr027)
