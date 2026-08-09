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
| layout | `title`, `structure.row_header_label`, `structure.row_labels`, `structure.column_labels`, `structure.note_sections` | Shape-specific. A table's axes and a note's heading-to-question mapping are not the same object. |

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

## 3. Groups: Printed Structure and Evidence Windows

Cut the ten items, in number order, into consecutive candidate-visible groups:

| # | Constraint |
|---|---|
| 1 | Every item belongs to exactly one declared group. No floating item, no empty group. |
| 2 | Each group is homogeneous — one `layout`, declared on the group. |
| 3 | A group's question numbers are contiguous. |
| 4 | A group's items are contiguous **in the ordered evidence sequence** — no other group's point falls between two of yours. |

**Group count is not pre-set or preferred.** Before creating any boundary, first test whether
Q1-Q10 together form one natural candidate-visible Form, Note, or Table. If they do, use one group
across both narrator windows. If they do not, split only where the visible record structure genuinely
changes. One, two, or three groups are all valid outcomes; there is no quota or target distribution.
Narrator windows are listening/read-ahead boundaries, not printed-layout boundaries, so the midpoint
cue alone is never a reason to split. Each item's decisive evidence must still stay inside its own
announced window, and the ten evidence points must still advance with the question numbers.

**The script's own structure picks the layout — not a wish for variety.**

| Layout | Use it when | Do not use it when |
|---|---|---|
| **form** | The dialogue fills in a record field by field: name, date, number, selection. Each item is one labelled slot. | The items are a discussion rather than a record being filled in. |
| **note** | The information is hierarchical or narrative: a topic with points under it, explanations, preferences talked through. This is the default when the material is not a record and has no comparison axis. | Nothing — note is the honest fallback. |
| **table** | Both axes carry real meaning, and reading down the content column compares like with like. The current schema supports one row-header column plus exactly one content column. | The headings are filler words (`Detail`, `Details`, `Notes`, `Information`, `Answer`), row and cell wording repeat one another, unrelated facts have merely been placed behind borders, or multiple content columns are declared without question-to-cell coordinates. |

A table is a **pseudo-table** when its printed axes do not organise or compare the information,
regardless of the raw column count. Column count and generic headings are triage signals, not an
automatic verdict. Ask whether the grid communicates a relationship that a form or note would not
express more naturally. Mixing layouts is legitimate only where the script really changes mode — a
record being taken, then requirements being discussed. Alternating layout group by group to look
varied is forced mixing, and the auditor reports it.

`structure.row_header_label` names the left column above `row_labels`. `column_labels` names only
the content columns to its right. The current package has no question-to-cell mapping, so it can
represent exactly one content column. A two-column table therefore uses one of each:

```json
{
  "row_header_label": "Volunteer topic",
  "row_labels": ["Young volunteers", "Time commitment"],
  "column_labels": ["Project arrangement"]
}
```

Do not put `Volunteer topic` into `column_labels`; that declares an empty corner plus two content
columns even though each row supplies only one content value.

Constraint 4 needs no turn-distance threshold. Once numbers are contiguous and evidence strictly
increases, "no other group's point in between" is decidable as it stands. Whether a group's span
*feels* too wide is a judgment for the question auditor, not an error here.

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

- All three positions are desirable across the ten items, and they should appear **because the lines
  are written differently**, not because one line was bent out of shape to fill a quota. A form row
  whose natural print is `Arrival date: ....` stays final; the variation comes from the items that
  really are sentences. Absence of a position is a review warning, not a generation error.
- End-of-line blanks: **7 of 10 is a review guideline, not a hard cap**. A genuine labelled form or
  table may naturally exceed it; do not add prose to move those gaps.
- Never leave a blank with neither carrier text nor candidate-visible structural context. A form row
  label, or a table's real row and column labels, already supplies that context.

Carriers may re-word the script minimally and naturally, and may retain a locating signpost that
does not give the answer away (QR-024, QR-034). What they may not do is mirror the evidence sentence
so closely that the item is answerable from the page.

### Row label and carrier: one job each

In a form group the row label and the carrier are printed **side by side on the same line**. They
therefore may not say the same thing:

```
BAD    row_label: Arrival date   carrier_before: "Arrival date:"     -> prints the words twice
BAD    row_label: Nightly charge carrier_before: "Nightly charge:"
GOOD   row_label: Arrival date   carrier_before: "" carrier_after: " (day and month)"
GOOD   row_label: Room chosen    carrier_before: "" carrier_after: " room"
```

The division of labour:

- the **row label** names the field — it is the left column, and it is the only place the field is
  named;
- the **carrier** carries whatever the *line* needs beyond its name: a unit, a short qualifier, or
  nothing. A form row is a record field, not an instruction to the candidate.

An empty carrier in a labelled form row is normal and correct: the label already did that work.
`Surname | ....` is a complete record field. A blank is rejected only when it has neither carrier
text nor structural labels. Whether a label such as `Preferences` is specific enough is a semantic
QR-010 judgement for the blind auditor, not a deterministic reason to force carrier prose.

What is never correct is repeating the label, or restating it as a near-synonym (`Family name` /
`Surname:`) — the duplication is the finding, not the exact wording. The same applies to a table's
row and column headings against the cell text, and to a note's heading against the line beneath it.

Do not repair duplication by replacing it with a full instruction sentence:

```
BAD    Surname       | Please record .... for correspondence
BAD    Flat size     | Accommodation consists of ....
GOOD   Surname       | ....
GOOD   Monthly rent  | £ .... per month
```

Parentheses are permitted only when all four checks pass:

1. removing the text would create a real ambiguity or lose the required answer scope;
2. the limit agrees with the recording or the simulated record's format;
3. it is natural, non-redundant record text, not commentary on speaking, spelling or answering;
4. it does not reveal the answer or eliminate all same-level rivals without listening.

`(day and month)` can pass when the field genuinely asks for only those parts. `(as spelt)`,
`(as mentioned)` and normally `(in block capitals)` fail because they describe the recording or the
candidate's behaviour rather than the information being recorded. Do not add parentheses merely to
change `blank_position`.

A labelled row with no carrier ends at the gap and classifies as **final**. A genuine qualifier after
the gap can change that classification; an invented qualifier may not be used to manufacture
position variety.

## 5. Titles and Signposts

**Titles.** QR-031 requires a short, specific, non-leaking scenario title for note groups. This
project applies the same title convention to form and table groups for a consistent paper: that
extension and the use of capitals are project authoring conventions, not additional claims about
QR-031. The title tells the candidate which part of the conversation this block belongs to:

```
GOOD   HOTEL BOOKING          ARRIVAL AND FACILITIES        CHILD'S EDUCATION
BAD    Hotel booking form     TABLE: Hotel information      Questions 6-10 (window 2)
BAD    Information            Details                        Section B
```

A title must not contain:

- the question type or layout name (`form`, `table`, `notes`, `completion`);
- a question range or a narrator window number — those are printed by the instruction, or belong to
  the internal audit region, never to the heading;
- the canonical answer, a unique answer word, or a category hint that narrows a blank to one
  candidate.

**Note hierarchy: two levels at most.** As a project authoring convention, a note group's
`structure.hierarchy` may go one level deep under the title — a main item and its sub-items. Deeper
nesting on a Part 1 page is a structure the candidate has to decode rather than read. Level names are
concrete and drawn from the conversation (`Room`, `Meals`, `Getting in`), never generic (`Point 1`,
`Other`, `Details`).

Use `structure.note_sections` to record the relationship explicitly:

```json
[
  {"heading": "Deposit", "question_numbers": [6]},
  {"heading": "Household provision", "question_numbers": [7]},
  {"heading": "Transport", "question_numbers": [8, 9]}
]
```

The sections must cover every question in the group exactly once. Do not emit several headings and
then a detached list of questions; each heading is rendered immediately above the items it governs.
`hierarchy` remains readable only for archived packages and is not sufficient for new generation.

**Signposts.** `signposts` is internal navigation and audit metadata. It helps reviewers connect a
group to the recording, but it is not prose to print on the candidate paper. Every **narrator
window** needs at least one blank-free, specific, script-grounded navigation line (QR-026). The
validator derives the windows from the group's member questions rather than a group-level scalar;
a continuous group spanning the midpoint lists at least one line for each covered window, in window
order. One line is never counted for both windows.

A signpost must name **what is being talked about** at that point in the recording. Generalised
meta-discourse about the questions themselves is not a signpost and is a finding:

```
GOOD   "Requirements for the new home are discussed next"
GOOD   "The receptionist goes through what the rate includes"
BAD    "Details are confirmed"        <- true of any material; names nothing
BAD    "Information is given"         <- ditto
BAD    "The following details apply"  <- talks about the page, not the recording
```

The test: could this line be copied unchanged onto a completely different Part 1 material? If yes it
carries no navigation value. Ground it in the script's own subject matter instead.

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

**`instruction_text` uses the standard IELTS wording and nothing else**, one sentence naming the
layout plus the rubric sentence:

```
Complete the form below.
Complete the notes below.
Complete the table below.

Write <WORD LIMIT> for each answer.
```

so, joined: `Complete the notes below. Write NO MORE THAN TWO WORDS AND/OR A NUMBER for each answer.`
The layout word must be the group's actual `layout` (`form` / `notes` / `table` — note takes the
plural). Do not describe the content in it: `Complete the booking record.` and `Complete the hotel
information table below.` are both non-standard, and the second also says in the rubric what the
title already says.

**Exactly one instruction per group, printed once at the head of the group.** Do not repeat it above
individual items, and do not restate it in a signpost or a title. Two adjacent groups that happen to
share the same rubric still get one instruction each — the instruction is what carries the group's
`Questions n–m` range — but neither repeats itself.

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
items really form a form, note or table rather than ten disconnected sentences (SC-015, QR-026);
whether a row label and its carrier print the same thing twice (SC-015 / QR-026 natural record
structure); whether a `table` has a real comparison axis or is a pseudo-table (SC-015 / QR-026);
whether a signpost names the recording or is generalised meta-discourse (QR-034); whether the layout
mix follows the script or is forced (SC-015); and whether the script itself signposts its answers
(SR-006, SR-007).

Run the validator to zero errors. Then reread the question face **without** the key beside it and ask
what a candidate would: can each blank be filled from the recording, and can any of them be filled
without it?
