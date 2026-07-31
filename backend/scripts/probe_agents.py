#!/usr/bin/env python
"""End-to-end probe for the two-agent shape, against the live model.

Answers the questions unit tests cannot, because they are behaviours rather than APIs:

1. does the generate agent activate a skill from its pool without being told which one?
2. does it actually run the validator with ``shell``, or claim to have?
3. does the audit agent produce ``compliance_review`` in the schema's shape?
4. does the audit agent stay blind -- no attempt to reach the generator's annotation?

    python backend/scripts/probe_agents.py --scenario booking-hotel

Every tool call the agents make is recorded and printed, so "it ran the validator" is observed
rather than inferred from the agent's own prose.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time
from pathlib import Path
from typing import Any, Dict, List

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from backend import agents as agents_module  # noqa: E402
from backend.orchestration.scenarios import load_catalogue  # noqa: E402
from backend.sandboxed_metrics import SandboxedMetrics  # noqa: E402
from backend.steps.call import extract_json  # noqa: E402


def _tool_calls(agent: Any) -> List[Dict[str, Any]]:
    """Every tool the agent invoked, read off its conversation history.

    The agent's own account of what it did is not evidence; the transcript is.

    Read from ``agent.messages``, not ``result.messages`` -- ``AgentResult`` has no ``messages``
    attribute, so the first version of this function silently reported zero tool calls for every run,
    including runs whose validator output was visible in the console. A probe that cannot fail loudly
    is worse than no probe.
    """
    calls: List[Dict[str, Any]] = []
    for message in getattr(agent, "messages", None) or []:
        for block in (message.get("content") or []) if isinstance(message, dict) else []:
            use = block.get("toolUse") if isinstance(block, dict) else None
            if use:
                calls.append({"name": use.get("name"), "input": use.get("input")})
    return calls


def _describe(calls: List[Dict[str, Any]]) -> None:
    if not calls:
        print("    (no tool calls at all)")
        return
    for call in calls:
        name = call["name"]
        payload = call["input"] or {}
        if name == "skills":
            detail = payload.get("skill_name", "?")
        elif name == "file_read":
            detail = payload.get("path", "?")
        elif name == "shell":
            detail = str(payload.get("command", "?"))[:110]
        else:
            detail = json.dumps(payload, ensure_ascii=False)[:110]
        print("    %-10s %s" % (name, detail))


async def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--scenario", default="booking-hotel")
    args = parser.parse_args()

    scenario = load_catalogue().get(args.scenario)
    if scenario is None:
        print("unknown scenario %r" % args.scenario)
        return 2

    print("scenario: %s (%s)\n" % (scenario.id, scenario.title_zh))

    # ---- 1. generation -----------------------------------------------------------------------
    print("== generate agent ==")
    gen_agent = agents_module.build_generate_agent()
    print("  tools: %s" % sorted(gen_agent.tool_names))
    started = time.monotonic()
    gen_result = await gen_agent.invoke_async(
        "Generate one IELTS Listening Part 1 material for this scenario.\n\n"
        "id: %s\ncategory: %s\ntitle: %s\n\n%s"
        % (scenario.id, scenario.category, scenario.title_zh, scenario.prompt_hint)
    )
    gen_seconds = time.monotonic() - started
    gen_calls = _tool_calls(gen_agent)
    print("  %.1fs, %d tool calls:" % (gen_seconds, len(gen_calls)))
    _describe(gen_calls)

    activated = [c["input"].get("skill_name") for c in gen_calls if c["name"] == "skills"]
    ran_validator = [c for c in gen_calls if c["name"] == "shell"
                     and "validate" in str(c["input"].get("command", ""))]
    print("\n  activated a skill:      %s" % (activated or "NO"))
    print("  ran the validator:      %s" % ("yes, %dx" % len(ran_validator) if ran_validator else "NO"))

    try:
        payload = extract_json(str(gen_result))
    except Exception as exc:  # noqa: BLE001
        print("\n  FAILED to parse output: %s" % str(exc)[:200])
        return 1
    material, blueprint = payload.get("material"), payload.get("blueprint")
    print("  returned material:      %s" % ("yes" if isinstance(material, dict) else "NO"))
    print("  returned blueprint:     %s" % ("yes" if isinstance(blueprint, dict) else "NO"))
    if not isinstance(material, dict):
        print("  keys: %s" % sorted(payload)[:8])
        return 1

    # Python re-runs the validator regardless of what the agent reported. Validation is a report,
    # not a gate, so this is not a second opinion on whether to deliver -- it is how the findings
    # reach the material, and incidentally how a false "all clean" would surface.
    from backend.deterministic.validate import validate

    outcome = await validate(material, blueprint if isinstance(blueprint, dict) else {})
    print("  Python's own validate:  ok=%s  errors=%d  warnings=%d"
          % (outcome.ok, len(outcome.errors), len(outcome.warnings)))
    for error in outcome.errors[:3]:
        print("      - %s" % error[:100])

    # ---- 2. deterministic metrics, in the remote sandbox -------------------------------------
    print("\n== metrics (Code Interpreter) ==")
    metrics_runner = SandboxedMetrics(
        "probe-%s" % scenario.id,
        agents_module.pool_dir("audit") / "audit-listening-part1" / "scripts" / "audit_metrics.py",
    )
    try:
        started = time.monotonic()
        metrics = await metrics_runner.run(material)
        print("  %.1fs  assessable=%s  words/turns=%s/%s"
              % (time.monotonic() - started, metrics.get("assessable"),
                 (metrics.get("parts") or [{}])[0].get("dialogue_words"),
                 (metrics.get("parts") or [{}])[0].get("dialogue_turns")))
    finally:
        await metrics_runner.close()

    # ---- 3. audit, blind ---------------------------------------------------------------------
    print("\n== audit agent ==")
    audit_agent = agents_module.build_audit_agent()
    print("  tools: %s" % sorted(audit_agent.tool_names))

    # The blindness boundary, in one place: the message carries the material and the metrics, and
    # the blueprint is simply not part of it.
    message = "\n\n".join([
        "Audit this IELTS Listening Part 1 material.",
        "## material.json\n\n%s" % json.dumps(material, ensure_ascii=False, indent=2),
        "## Deterministic metrics (already calculated; do not recount)\n\n%s"
        % json.dumps(metrics, ensure_ascii=False, indent=2),
    ])
    for forbidden in ("blueprint", "form_group", "question_type_coverage", "item_form"):
        if forbidden in message:
            print("  BLINDNESS VIOLATION: %r is in the audit message" % forbidden)
            return 1
    print("  message is blind:       yes (%d char, no planning fields)" % len(message))

    started = time.monotonic()
    audit_result = await audit_agent.invoke_async(message)
    audit_seconds = time.monotonic() - started
    audit_calls = _tool_calls(audit_agent)
    print("  %.1fs, %d tool calls:" % (audit_seconds, len(audit_calls)))
    _describe(audit_calls)

    reached_out = [c for c in audit_calls
                   if c["name"] == "file_read" and "generate" in str(c["input"].get("path", ""))]
    print("\n  tried to read the generate pool: %s"
          % ("YES — %s" % reached_out if reached_out else "no"))

    try:
        audit = extract_json(str(audit_result))
    except Exception as exc:  # noqa: BLE001
        print("  FAILED to parse audit output: %s" % str(exc)[:200])
        return 1

    print("  verdict:                %s" % audit.get("verdict"))
    score = audit.get("score")
    print("  score:                  %s" % (score.get("total") if isinstance(score, dict) else score))
    print("  findings:               %d" % len(audit.get("findings") or []))
    blind_map = audit.get("blind_information_map") or []
    print("  blind_information_map:  %d points" % len(blind_map))

    review = audit.get("compliance_review")
    if isinstance(review, dict):
        items = review.get("items") or []
        bad = [i for i in items if not i.get("compliant")]
        print("  compliance_review:      %d items, %d non-compliant" % (len(items), len(bad)))
        for item in bad[:4]:
            print("      %s turn=%s %s" % (item.get("code"), item.get("turn_index"),
                                           str(item.get("fix", ""))[:80]))
    else:
        print("  compliance_review:      MISSING")

    # Schema conformance, since compliance_review is new.
    schema_path = (agents_module.pool_dir("audit") / "audit-listening-part1"
                   / "schemas" / "audit.schema.json")
    try:
        import jsonschema

        jsonschema.validate(audit, json.loads(schema_path.read_text(encoding="utf-8")))
        print("  schema:                 valid")
    except ImportError:
        print("  schema:                 (jsonschema not installed, skipped)")
    except Exception as exc:  # noqa: BLE001
        print("  schema:                 INVALID — %s" % str(exc)[:160])

    print("\ntotal model time: %.1fs" % (gen_seconds + audit_seconds))
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
