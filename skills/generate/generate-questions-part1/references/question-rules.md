# Part 1 Question Authoring Rules

The rules for turning one finalised material plus its ten planned information points into ten Part 1
completion items. Read this before writing; `SKILL.md` carries the boundaries, the workflow and the
output rules, and `schemas/question_package.schema.json` carries the field-by-field contract.

## Contents

1. Two layers: `question_type` and `layout`
2. The ten points are given input
3. Groups: the five constraints
4. Carriers, blanks and blank position
5. Titles and signposts
6. The answer key: AR-003 in tiers
7. Word limits: no default
8. Answer variety
9. Leakage: the group is the scope
10. Evidence and proposition alignment
11. What the validator decides and what the auditor decides

## 1. Two Layers: `question_type` and `layout`

```
question_type = completion            <- the top level; Part 1 has only this one
    └─ layout ∈ { form, note, table } <- the printed shape; one per group, mixable across groups
```

Form, note and table are **not** three question types. Keeping the two levels apart is not
terminological tidiness — it decides which field goes where:

| Layer | Fields | Why here |
|---|---|---|
| completion | `instruction_text`, `word_limit`, `numeral_allowance` | Identical in kind for all three layouts: the candidate is told what to write and how much of it, regardless of shape. |
| layout | `title`, `structure.row_labels`, `structure.column_labels`, `structure.hierarchy` | Shape-specific. A table's axes and a note's heading hierarchy are not the same object. |

Put one on the wrong layer and a table group acquires a note's title structure.

`layout` is declared **once**, on the group. Nothing restates it: two authorities for one fact
disagree eventually. Homogeneity inside a group is therefore true by construction, which is why the
validator spends its group checks on the constraints that can still be false.

## 2. The Ten Points Are Given Input

They are the blueprint's ten points, in the blueprint's numbering and evidence order. Not a candidate
pool.

- Do not delete a point, replace one, or reorder them.
- Do not change a point's `answer_category`. The validator compares it against the blueprint.
- Do not adjust the script to make a point easier to use. The audible script is frozen (SR-021).

The answer-variety balance of exactly these ten was checked and approved upstream by the
question-feasibility preflight. Satisfying QR-027 at this stage is therefore **not** done by picking
different points — it is done by choosing the right response form and carrier wording for the points
you have. If a point genuinely cannot carry a reliable item, the preflight missed it, and the only
compliant remedy is a new material. Report it and stop; do not improvise around it.

## 3. Groups: The Five Constraints

Cut the ten items, in number order, into consecutive groups. All five are checked deterministically:

| # | Constraint |
|---|---|
| 1 | Every item belongs to exactly one declared group. No floating item, no empty group. |
| 2 | Each group is homogeneous — one `layout`, declared on the group. |
| 3 | A group's question numbers are contiguous. |
| 4 | A group's items are contiguous **in the ordered evidence sequence** — no other group's point falls between two of yours. |
| 5 | A group sits entirely inside one narrator question-number window. It may not straddle or merge windows (SC-019 / QR-022). |

**Group count is not pre-set.** One window may hold several consecutive groups, and one package may
mix form, note and table freely as long as each group is itself homogeneous. Let the script decide:
personal details taken in sequence are a form; preferences discussed in prose are notes;
requirements compared along two axes are a table.

Constraint 4 needs no turn-distance threshold. Once numbers are contiguous, evidence strictly
increases and no group crosses a window, "no other group's point in between" is decidable as it
stands. Whether a group's span *feels* too wide is a judgment for the question auditor, not an error
here.

## 4. Carriers, Blanks and Blank Position

Each item is `carrier_before` + `blank` + `carrier_after`. The blank must carry its own question
number — that number is how an answer sheet is matched to an item.

**Position is classified by content words, not by character offset** (QR-025's criterion, which
QR-026 borrows):

- **initial** — at most one content word before the blank, and content after it;
- **final** — no content word after the blank, and content before it;
- **medial** — everything else.

A bare `Name: ....` is therefore **final**, which is the honest reading: it is exactly the
systematic end-of-line blanking QR-026 limits.

Requirements:

- All three positions should appear across the ten items.
- End-of-line blanks: **at most 7 of 10**. If the form or table structure genuinely does not admit
  variation, the reason has to be recorded.
- Never leave a blank with no context on either side.

Carriers may re-word the script minimally and naturally, and may retain a locating signpost that
does not give the answer away (QR-024, QR-034). What they may not do is mirror the evidence sentence
so closely that the item is answerable from the page.

## 5. Titles and Signposts

- A **note** group must have a short, specific, non-leaking scenario or topic title (QR-031). It must
  not contain the canonical answer, a unique answer word, or a category hint that narrows the blank
  to one candidate. Form and table groups carry their identity in their labels; a title there is
  optional.
- Every **narrator window** needs at least one blank-free, specific, script-grounded navigation line
  (QR-026). Counted per window, not per group: a one-item group inside a well-signposted window
  needs no line of its own.
- "Requirements for the new home are discussed next" locates the candidate. "Information" does not —
  a line that could sit on any material of this kind is not a signpost.

Content structure is a content-review gate; border style, font, spacing and non-destructive
pagination are not (QR-015). Missing row or column labels, a missing note hierarchy, an unreadable
question number — those block. Visual polish does not.

## 6. The Answer Key: AR-003 in Tiers

The canonical is the blueprint item's target, in the script's own wording. **Which tier applies is
decided by tokenising the canonical you actually wrote — never by the `word_limit` you printed.** An
item in a `NO MORE THAN TWO WORDS` group may perfectly well have a one-word answer, and that answer
is held to the strict single-token rule.

| Answer shape | Requirement |
|---|---|
| **One token** | Word-for-word identity with one **complete orthographic token** of the decisive evidence. Tokenise and match on boundaries: **no substring credit in either direction** — the script's `Educational` cannot key `education`, and the reverse is equally refused. |
| **Multiple tokens** | Every component word must appear as a complete token of the decisive evidence, and the whole must satisfy the word limit. It is **not** required to equal one token: `guest room` is two, legitimately. |
| **Hyphenated compound** | Counts as **one word** (AR-014) but the whole token must be kept. Where the evidence has only `eco-tourism`, `tourism` is not an answer, and the carrier may not pre-fill `eco-` leaving the candidate a fragment. Only a separate standalone occurrence of the bare token in the same decisive evidence licenses treating it on its own. |
| **All shapes** | No derivation, no synonym substitution, no splitting a token. |

Other answer rules that apply here:

- **AR-005**: never key a value the script rejects, corrects away, or offers only as a distractor.
  Where a point is corrected or confirmed, the **final** value is the answer.
- **AR-004**: an `alternatives` entry must be genuinely equivalent in this context and satisfy the
  same limit on its own. An empty list is a normal answer, not an omission; inventing a variant that
  is not truly equivalent marks wrong answers right.
- **AR-006**: handle foreseeable British/American variants consistently, and have a reason if only
  one is accepted.
- **QR-017**: state the counting rule you used. Whitespace splits tokens; a hyphenated compound is
  one word; a pure number consumes the numeral allowance rather than a word; a slash does not create
  a second answer.
- **QR-043** (a review signal, not an automatic failure): for a one-word answer, consider whether
  another Script-grounded candidate in the same window would pass every gate equally while carrying
  a lower spelling burden. Prefer it if so. Do not resolve a spelling burden by switching to a
  number, a single letter, or an abbreviation. Low word frequency is a triage signal for a human, not
  a threshold that decides.

## 7. Word Limits: No Default

There is **no global default limit**. Per group, take the **strictest** standard rubric that every
one of that group's canonicals satisfies:

```
ONE WORD ONLY
ONE WORD AND/OR A NUMBER
NO MORE THAN TWO WORDS
NO MORE THAN TWO WORDS AND/OR A NUMBER
NO MORE THAN THREE WORDS
NO MORE THAN THREE WORDS AND/OR A NUMBER
```

That closed set is the whole vocabulary — an invented phrasing is neither countable nor comparable
against the answers. Print the chosen string **verbatim** inside `instruction_text` (LG-006): the
printed rubric is the one the candidate obeys, and a rubric that disagrees with the machine-readable
field is unanswerable. Restate the same limit on each of that group's answer-key entries, because
marking reads that block alone.

The validator re-derives the strictest fitting rubric and reports a group that printed a looser one.
A loose limit is not generosity: it accepts responses your own key marks wrong.

No example question is provided for Part 1 completion, and none should be invented.

## 8. Answer Variety

Across the ten items:

- at most **4** purely numeric answers;
- at least **4** requiring spelling a word or phrase (two letters or more; single letters and common
  abbreviations do not count);
- **fewer than 3** items in the same `answer_category`.

These were already confirmed satisfiable for these ten points at the preflight. If a limit is
exceeded here, the fix is the response form and carrier wording, not a different point.

`response_form` (`numeric` / `word` / `phrase`) counts **tokens** and is persisted on the question
face, because it is what the candidate is being asked to produce. A hyphenated compound is one
token: `two-bedroom` is a `word`. The internal QR-027 class splits on **character composition**
instead and is never written down — `Room 4B` is a `phrase` whose class is mixed.

## 9. Leakage: The Group Is the Scope

Before the recording starts, a candidate can read the group's **entire** visible surface: its title,
its headings, every signpost, every row and column label, every carrier, and every neighbouring
item. None of it may give an answer away (QR-040 / SC-012 / QR-008).

- Scan for the exact phrase, for case variants, and for **ordinary inflections** — a plural, an
  `-ing`, an `-ed`. `parks` in a heading leaks `park`.
- Scanning only the sentence containing the blank does **not** discharge this. The audit is
  group-scope by definition.
- A near-restatement of the same fact in different words leaks just as effectively as the word
  itself. That one is semantic, so the validator cannot catch it — you must, and the question
  auditor will look for it from a stronger position, having rebuilt the answers from the script
  without your key.

## 10. Evidence and Proposition Alignment

Per item, record:

- `turn_index` — the zero-based position in the material's `turns` array, counting narration turns.
  Copy it out of the array rather than counting by eye.
- `quote` — the shortest sufficient **verbatim** span from that exact turn. Verbatim is checkable and
  a paraphrase is not, which is the entire reason this field is a quote. "Shortest sufficient" has a
  floor: the span must occur in that turn and **not** in either neighbouring dialogue turn. The
  validator rejects one that occurs in both. A span appearing twice inside that window identifies no
  sentence, and the blind cross-check reconciles your anchor against the auditor's across exactly ±1 —
  so an ambiguous quote is what turns a sound item into one no deterministic check can settle.
- `narrator_window_id` — the item's window. Evidence may not leave it (AL-017 / SC-019).
- `paraphrase_relation` — `exact`, `signpost` (a retained locating label rather than a rewrite,
  which QR-034 often requires) or `paraphrase`.
- `carrier_entity`, `evidence_entity`, `proposition_relation`, `proposition_alignment_result` —
  AL-018. The carrier's assertion and the evidence must be **one factual proposition**: same subject,
  same object, same place, same time, same relation. A question label printed on the same line as the
  answer word is not alignment.

The ten indices must **strictly increase** with the question numbers (QR-004 / AL-003). The decisive
evidence may be a confirmation turn rather than the first mention — that is ordinary Part 1 writing —
but it must still be inside the item's own window and still support the same proposition.

## 11. What the Validator Decides and What the Auditor Decides

**`validate_questions_part1.py` decides**, and it is authoritative on all of it: ten items numbered
1–10; the three blocks describing the same ten with no orphans; all ten blueprint points used
unchanged; the five group constraints; blank-position classification and the QR-026 distribution; the
per-group word limit and each canonical against it; AR-003 tiering; the quote's presence in the
declared turn; evidence monotonicity and window containment; the QR-027 counts; word-level group
leakage; note titles and per-window signposts; and every recomputed declaration
(`blank_position`, `response_form`, `narrator_window_id`, `numeral_allowance`).

**The question auditor decides**, blind, from the script and the question face alone: answer
uniqueness by substituting every same-level candidate (AR-012, QR-010); paraphrase fidelity (AL-004,
QR-024); proposition-level alignment (AL-018); semantic leakage (QR-040's second half, SC-012);
grammatical and semantic fit once the answer is inserted (QR-009, AL-015); naturalness, register and
unnecessary lexical difficulty (LG-001/002/003/005/015); spelling burden (QR-043); whether the ten
items really form a form, note or table rather than ten disconnected sentences (SC-015, QR-026); and
whether the script itself signposts its answers (SR-006, SR-007).

Run the validator to zero errors. Then reread the question face **without** the key beside it and ask
what a candidate would: can each blank be filled from the recording, and can any of them be filled
without it?
