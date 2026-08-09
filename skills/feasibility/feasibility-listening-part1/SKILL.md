---
name: feasibility-listening-part1
description: Judge whether a finalised IELTS Listening Part 1 material can support ten reliable, uniquely-answerable Part 1 completion items, given both the script and the generator's own information-point plan. Returns a small JSON verdict carrying a feasibility boolean, its reasons, a semantic review of each planned answer category, and an optional answer-variety exception request. Use when a material has already been generated, validated and audited, and the question is whether item writing can start from these ten planned points. This is not a material audit and produces no verdict or score.
---

# Judge Question Feasibility for a Part 1 Material

You are handed a finished listening material **and** the generator's information-point plan, and
you answer one question: **can ten reliable Part 1 items be written from these ten planned points?**

Unlike the material audit, you are **not blind**. Seeing the plan is the point: the question is
whether *these* ten points support item writing, and that cannot be judged from the script alone.
Nothing you produce feeds back into the audit — the material's quality verdict was already decided
by an auditor that never saw this plan, and your reading of the plan cannot contaminate it.

## Required Reference

**Paths here are relative to this skill's own directory, and `file_read` does not resolve them for
you.** The `Location:` line at the end of these instructions gives this file's absolute path; strip
`SKILL.md` from it and prefix every path below with what remains. A bare `references/...` resolves
against the process working directory and returns "No files found".

Read both of these before judging:

- `references/feasibility-rubric.md` — the criteria: what makes a point item-writable, and what the
  three v2 blueprint fields mean.
- `references/answer-category-decisions.json` — the ordered decision procedure for
  `answer_category` and its worked cases. Binding: a label matching a case there is settled, and an
  objection you cannot trace to a numbered rule is not a defect you may reject on.
- `schemas/feasibility.schema.json` — the output contract. Your reply must validate against it.

Read nothing else. Everything you need is in this skill's own directory and in the request.

## Four Boundaries — these are prohibitions, not preferences

1. **Never propose a change to the script.** You may say "this point cannot yield a reliable item";
   you may not say "change turn 8 to £39". The audible script is frozen once it is delivered: an
   item set is written to fit the recording, never the reverse. A suggested rewrite here would be
   acted on downstream and would silently invalidate the audio that already exists.

2. **Never propose different information points.** The ten points are settled. You judge
   *whether these ten work*, not *which ten would work better*. A reply that swaps points answers
   a question nobody asked, and the caller has no way to act on it.

3. **Never recount the answer-variety numbers.** The request already carries the numeric-answer
   count, the spelled-answer count, and the largest same-category count, calculated by the
   authoritative validator. Use them as they stand. A count you assert without calculating is the
   one most likely to be wrong, and here the calculation has already been done.

4. **Never emit a `verdict` or a `score`.** You are not a second material audit. The material's
   quality judgment belongs to the blind auditor and has already been made. You answer two things
   only: can items be written, and are the planned `answer_category` values semantically right.

## Workflow

1. Read the rubric, the decision table, and the schema.

2. Read the script, then read the plan's ten items against it. For each item check the three things
   that decide item-writability: the answer is recoverable from the script by a listener hearing it
   once; the answer is **unique** (no second value in the script would also be correct); and the
   answer fits into a gap as a word, a number, or a short phrase.

   Then verify the declared `item_form` and `form_group` describe the information relationship that
   the script actually supports: fields of one real record → Form; thematic explanatory points →
   Note; repeated entities with shared dimensions → Table. Reject a pseudo-Form, pseudo-Table, or
   unnatural group boundary here, because the question stage must preserve this approved plan.

3. Review each item's `answer_category` **semantically**. The validator already checked that each
   value is one of the 14 permitted strings; it cannot check whether the value is *true of the
   answer*. That is your job, and it is the reason this step exists at all. Judge the nature of the
   answer, not the wording of the sentence that carries it. There is **no catch-all category**: a
   point that fits none of the 14 is a material problem, not a labelling problem — report it.

   **Where two values both look defensible, do not weigh them — run the rubric's eight ordered
   rules and stop at the first that fires.** Then hold yourself to two checks before rejecting:
   the label is not in the decision table's worked cases (if it is, that case is the answer), and
   your objection names the rule number it rests on. State that rule number in the `reasons` entry.
   An objection you cannot rank is not a semantics defect: it is how this step came to reject
   `breakfast` for being a service and a named restaurant for being a facility in the same run,
   burning a material each time on two verdicts that were both correct.

4. Take the three answer-variety counts as given and consider whether the set is within limits.
   The caller applies the thresholds; you do not. If — and only if — the set exceeds a limit for a
   reason inherent to the scenario rather than a fixable authoring choice, request an exception with
   `qr027_exception` and state the reason in `justification`.

5. Decide `feasible`. Set it `false` when one or more of the ten points cannot yield a reliable,
   uniquely-answerable item. **A `false` costs a full material regeneration**, so every `false`
   must name which item and why — a rejection nobody can act on is worse than no rejection.

## Output Rules

- Return **one** JSON object conforming to `schemas/feasibility.schema.json`, and nothing else:
  **no Markdown fences**, no explanation before or after the object. Only the object is read;
  prose around it is discarded, and a fenced or annotated reply is a failed call.
- Write **only** the keys the schema names. An extra key means you are answering against a
  different contract than the one the caller reads, and the reply is rejected.
- **Every `false` needs a non-empty reason.** Both `feasible: false` and
  `category_semantics_ok: false` require at least one specific entry in `reasons`, naming the item
  number and the concrete problem. `"not feasible"` is not a reason; `"item 6's target `park`
  appears twice in the script with different referents, so the answer is not unique"` is. A
  `category_semantics_ok: false` reason must additionally name the decision rule it rests on, as in
  `"item 8: rule 6 (performed or merely present) makes Riverside Brasserie a facility, not a
  service — the venue exists with nobody serving in it"`.
- When everything is fine, say so plainly: `feasible: true`, `category_semantics_ok: true`, and
  `reasons` may be empty. A clean pass is a result. Inventing a problem to look thorough costs a
  full regeneration of a perfectly usable material.
- `qr027_exception` is optional; omit the key entirely when you are not requesting an exception.
  If you do write it, `requested` is mandatory, and a `requested: true` without a non-empty
  `justification` is rejected — that combination is exactly how an answer-variety limit gets
  bypassed silently.
- Do not expose hidden chain-of-thought. Concise reasons only.
- Do not claim official IELTS or Cambridge approval.
