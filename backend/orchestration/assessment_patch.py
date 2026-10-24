"""Deterministically apply a narrow fact edit to an accepted assessment version."""
from __future__ import annotations

import copy
from collections import Counter


def turns(material):
    if isinstance(material.get("turns"), list):
        return material["turns"]
    return material["listening_material_parts"][0]["script"]["turns"]


def numbered(rows):
    if not isinstance(rows, list) or any(
        not isinstance(r, dict) or type(r.get("number")) is not int for r in rows
    ):
        raise ValueError("assessment rows require integer question numbers")
    result = {r["number"]: r for r in rows}
    if len(result) != len(rows):
        raise ValueError("duplicate question numbers")
    return result


def apply_patch(material, blueprint, package, comments, patch):
    """Only declared replacements and anchored rows can be changed.

    Questions not in the teacher's request cannot be rewritten by the model. Broader
    changes require a new teacher decision rather than an implicit full regeneration.
    """
    if set(patch) - {"outcome", "reason", "turn_edits", "question_updates", "blueprint_updates"}:
        raise ValueError("patch contains unsupported fields")
    if patch.get("outcome") != "patch":
        raise ValueError("修改需要扩大范围，请先确认：" + str(patch.get("reason") or ""))
    scope = set()
    for comment in comments:
        anchor = comment.get("anchor") or {}
        if anchor.get("type") != "question" or type(anchor.get("index")) is not int:
            raise ValueError("fact edits require question-anchored comments")
        scope.add(anchor["index"])
    if not scope:
        raise ValueError("no question anchors")
    out_material, out_blueprint, out_package = copy.deepcopy((material, blueprint, package))
    source_turns, target_turns = turns(material), turns(out_material)
    edits = patch.get("turn_edits")
    if not isinstance(edits, list) or not edits:
        raise ValueError("material patch has no turn edits")
    seen = set()
    for edit in edits:
        n = edit.get("turn_index")
        before, after = edit.get("before"), edit.get("after")
        if type(n) is not int or not 0 <= n < len(source_turns):
            raise ValueError("patch turn index is outside the material")
        if source_turns[n].get("speaker") == "speaker1":
            raise ValueError("fact edits cannot rewrite narrator turns")
        if (not isinstance(before, str) or not before or not isinstance(after, str)
                or before == after or (n, before) in seen):
            raise ValueError("invalid/duplicate text replacement")
        seen.add((n, before))
        if source_turns[n]["text"].count(before) != 1 or target_turns[n]["text"].count(before) != 1:
            raise ValueError("patch must match a unique original substring")
        target_turns[n]["text"] = target_turns[n]["text"].replace(before, after, 1)
        if not target_turns[n]["text"].strip():
            raise ValueError("patch cannot delete a whole turn")

    rows = {
        "answer": numbered(out_package["answer_key"]),
        "evidence": numbered(out_package["evidence"]),
        "blueprint_item": numbered(out_blueprint["items"]),
        "question": numbered(out_package["question_face"]["questions"]),
    }
    if any(not scope <= set(by_number) for by_number in rows.values()):
        raise ValueError("comment points to a missing question")
    allowed = {
        "answer": {"canonical", "alternatives"},
        "evidence": {"quote", "turn_index", "paraphrase_relation", "carrier_entity",
                     "evidence_entity", "proposition_relation", "proposition_alignment_result"},
        "blueprint_item": {"target", "evidence", "turn_index", "confirmed", "distractor"},
        "question": {"carrier_before", "carrier_after", "blank_position"},
    }
    updates = patch.get("question_updates")
    if not isinstance(updates, list):
        raise ValueError("question_updates must be a list")
    updated = set()
    for update in updates:
        n = update.get("number")
        if type(n) is not int or n not in scope or n in updated:
            raise ValueError("patch changed an unrequested/duplicate question")
        updated.add(n)
        if set(update) - {"number", *rows} or not {"answer", "evidence", "blueprint_item"} <= set(update):
            raise ValueError("patch must supply matching answer, evidence and blueprint rows")
        for key in rows:
            if key not in update:
                continue
            old, new = rows[key][n], update[key]
            if not isinstance(new, dict) or new.get("number") != n:
                raise ValueError("patch row identity changed")
            immutable_old = {k: v for k, v in old.items() if k not in allowed[key]}
            immutable_new = {k: v for k, v in new.items() if k not in allowed[key]}
            if immutable_old != immutable_new:
                raise ValueError("patch changed question structure; teacher confirmation required")
            old.clear()
            old.update(copy.deepcopy(new))
        for key, quote_key in (("evidence", "quote"), ("blueprint_item", "evidence")):
            row = rows[key][n]
            idx, quote = row.get("turn_index"), row.get(quote_key)
            if (type(idx) is not int or not 0 <= idx < len(target_turns)
                    or not isinstance(quote, str) or not quote.strip()
                    or quote not in target_turns[idx]["text"]):
                raise ValueError("updated answer evidence is not a literal source quote")
        answer, planned = rows["answer"][n], rows["blueprint_item"][n]
        canonical, target = answer.get("canonical"), planned.get("target")
        alternatives = answer.get("alternatives", [])
        if (not isinstance(canonical, str) or not canonical.strip()
                or not isinstance(target, str) or not target.strip()
                or not isinstance(alternatives, list)
                or any(not isinstance(a, str) or not a.strip() for a in alternatives)):
            raise ValueError("updated answer and target must be nonempty text")
        normalize = lambda value: " ".join(value.casefold().split())
        if normalize(target) not in {normalize(a) for a in [canonical, *alternatives]}:
            raise ValueError("updated answer does not match the planned target")
        if normalize(target) not in normalize(planned["evidence"]):
            raise ValueError("updated target is missing from its evidence")
    if not updated:
        raise ValueError("fact edit did not update any question")
    # Even unchanged questions cannot lose previously valid evidence because another turn changed.
    for key, quote_key in (("evidence", "quote"), ("blueprint_item", "evidence")):
        for n, row in rows[key].items():
            if n in updated:
                continue
            idx, quote = row.get("turn_index"), row.get(quote_key)
            if (type(idx) is int and 0 <= idx < len(source_turns) and isinstance(quote, str)
                    and quote in source_turns[idx]["text"] and quote not in target_turns[idx]["text"]):
                raise ValueError("修改会影响未授权的 Q%d 证据，需要老师确认扩大范围" % n)
    metadata = patch.get("blueprint_updates") or {}
    if not isinstance(metadata, dict) or set(metadata) - {"correction", "indirect_confirmation"}:
        raise ValueError("patch changed unrelated blueprint structure")
    out_blueprint.update(copy.deepcopy(metadata))
    return out_material, out_blueprint, out_package, sorted(updated)


def introduced_errors(before, after):
    """Only deterministic defects newly introduced by this edit can block it."""
    return list((Counter(after.errors) - Counter(before.errors)).elements())
