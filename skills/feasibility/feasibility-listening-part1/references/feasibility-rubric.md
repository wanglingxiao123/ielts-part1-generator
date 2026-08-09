# Question Feasibility Rubric — Listening Part 1

The criteria for judging whether ten planned information points can support ten reliable Part 1
items. Read this before judging; `SKILL.md` carries the boundaries and the output rules.

## Contents

1. What this rubric decides, and what it does not
2. Item-writability: the three tests
3. The three v2 blueprint fields
4. `answer_category`: the 14 values and their boundaries
5. Answer variety and the exception
6. Reporting

## 1. What This Rubric Decides, and What It Does Not

**Decides:** can ten reliable, uniquely-answerable, gap-fillable Part 1 items be written from the
ten points in the plan you were given.

**Does not decide:** whether the material is good (the blind audit settled that), whether the script
should change (it must not — see boundary 1), whether different points would be better (boundary 2),
or whether the answer-variety counts are within their thresholds (the caller applies those).

Part 1 delivers **Form / Note / Table completion only**. Every judgment below is against that.

## 2. Item-Writability: The Three Tests

Apply all three to each of the ten points. A point failing any one of them makes the set
`feasible: false`, and the failing point must be named in `reasons`.

### Recoverable

A candidate hearing the recording **once** can recover the answer. The evidence sits in the script,
in one place, stated clearly enough to be written down at speaking pace. A detail that requires
holding three earlier turns in memory and inferring across them is not recoverable in Part 1.

Where the point is confirmed, corrected, or indirectly referenced, the **final** value must be the
recoverable one. A self-correction whose replacement value is never stated plainly leaves two
candidate answers and no way to choose.

### Uniquely answerable

No second value in the script would also be a correct answer to the item that this point implies.
This is the test that most often fails, and it fails quietly:

- the same word appears twice with different referents (a `park` the caller asks about and a `park`
  mentioned in passing), so the gap has two defensible fillers;
- a distractor was never resolved, so the earlier value remains arguable;
- two planned points are close enough that one item's answer could be the other's.

Uniqueness is a property of the **pair** (script, planned point), which is exactly why this
judgment needs both and cannot be made blind.

### Gap-fillable

The answer can be written into a gap as a word, a number, or a short phrase. Not a sentence, not a
choice between options, not a yes/no. If the natural item for a point would be a true/false or a
multiple choice, that point does not belong in a Part 1 completion set.

Also consider the set as a whole. Fields of one real record support Form. Thematic explanatory
points such as requirements, preferences, procedures, facilities, advice, reasons, or arrangements
support Note. Repeated entities with shared comparison dimensions support Table. A list is not Form
merely because labels can be invented for every line. Form, Note and Table are equally legal
completion layouts; do not reject a coherent Note-only plan merely because it is not a Form or
Table. Ten unrelated gap-fills are a real finding — the material exists to support item writing,
and a set with no organisable structure produces a worse test than the same ten facts arranged into
a natural completion layout.

## 3. The Three v2 Blueprint Fields

Source: `skills/generate/generate-listening-part1/references/specification.md` §"Blueprint version
2" (including its "Deciding `answer_category`" subsection). Summarised here because your pool does
not contain that file. **That file is
authoritative**; where this summary disagrees with it, it is this summary that is wrong. Two copies
of the same rule drift, so the points below are deliberately kept to what you need for judging.

- **`response_form`** — `numeric | word | phrase`. The shape of the recordable answer **by token
  count**, derived from the actual `target` text and nothing else. `numeric` when every token is a
  pure number/time/date form; `word` for a single non-numeric token; `phrase` for several. A
  hyphenated compound counts as **one** word (`two-bedroom` is `word`). Note the two traps the
  specification calls out: `118 Fordyce` is a `phrase` even though its type is an address, and
  `BT14 9BJ` is a `phrase` because "contains a digit" is not the same as "is a number".

- **`answer_category`** — which micro-category of information the answer is. See §4.

- **`narrator_window_id`** — `1` or `2`, which narrator question window the item falls in.
  Redundant with the item's group by construction, and deliberately so.

You are judging **semantics**, not the enum. The validator already rejected any value outside the
permitted strings and already recomputed the window. What it cannot check is whether a permitted
value is *true of this answer* — that is what you add.

## 4. `answer_category`: The 14 Values and Their Boundaries

`person_name`, `contact`, `location`, `date`, `time`, `duration`, `price`, `quantity`, `job_title`,
`service`, `facility`, `requirement`, `preference`, `document`.

**There is no catch-all.** A point that fits none of the 14 is not a categorisation problem, it is a
material problem: report it in `reasons` rather than forcing a label. Do not invent another value,
and do not pick the closest fit as a way of avoiding the report.

**Judge the nature of the answer, not the wording of the sentence.** An answer reached through "we
definitely need a two-bedroom property" is still a `quantity`, because `two-bedroom` is a
specification. This is the single most common way a semantically wrong label passes the validator: a
label chosen from the surrounding sentence rather than from the answer itself.

### The decision procedure is binding

Where two values both look defensible, **apply these rules in order and stop at the first that
fires.** You are not weighing them. This ordering exists because you contradicted yourself in
production: within one run you rejected `breakfast` as "an included service, not a physical facility"
and then rejected a named restaurant as "a physical venue, so a facility rather than a purchasable
service". Both conclusions were right, but the axis you reasoned from ("purchasable" versus
"described") could not produce both, so each call picked its own axis and the two calls disagreed.

1. **Form first** — the answer *is* a person's name, date, clock time, span, currency amount, or
   count/measure → `person_name`, `date`, `time`, `duration`, `price`, `quantity`. Nothing about the
   setting overrides this. `date`/`time`/`duration` never merge; `price` is currency only, so
   `10 lessons` is `quantity`.
2. **A named occupation or role is a `job_title`** — use it for the name of employment, a vacancy,
   or a role a person performs, such as `warehouse assistant`. It is not the `facility` where the
   person works or the `service` they perform.
3. **An artefact beats the thing it governs** — an artefact or record identifier that is issued,
   carried, shown, signed, quoted or presented → `document`. A `parking permit` is a `document`, not the `facility` it admits you to.
4. **`contact` is a route to a person** — a phone number, extension, email address. A reference,
   booking or property code is **not** `contact`; `KJ47` identifies a record and reaches nobody, so
   rule 3 makes it a `document`.
5. **`location` is a position** — an address, postcode, or place name given as *where* something is.
   Outranks `facility`: one name is a `location` when the item asks where, a `facility` when the item
   asks what is there.
6. **Performed, or merely present?** — the `service`/`facility` axis. Would it still exist with nobody
   performing it? Yes → `facility`; no → `service`. `breakfast` is a `service` charged or included; a
   named restaurant is a `facility` even with a service running inside it. Charged-versus-included is
   **not** the axis.
7. **`preference` requires named alternatives** — the script must name two or more and settle on one.
   Otherwise a condition asked for or satisfied is a `requirement`: `furnished` states an existing
   attribute against no stated alternative, so `requirement`. `requirement` is also what is *asked
   for* (a `guest room`) where `facility` is what already exists (a `park` nearby).
8. **No catch-all** — report it, per the paragraph above.

**You may not reach opposite conclusions on inputs that the same rule decides.** Before you set
`category_semantics_ok: false`, name the rule number your objection rests on and check the worked
cases in `references/answer-category-decisions.json`. If the label under review matches a case there,
that case is the answer and you do not re-litigate it. If your objection cannot be traced to a
numbered rule, it is not a semantics defect and you must not reject on it — an unrankable objection is
exactly what produced the contradictory pair above. Where the JSON and
`generate-listening-part1/references/specification.md` disagree, the specification wins.

## 5. Answer Variety and the Exception

Three counts arrive in the request, already calculated:

- purely numeric answers among the ten;
- answers requiring a word or phrase to be spelled out;
- the largest number of items sharing one `answer_category`.

**You do not apply the thresholds and you do not recount** (boundary 3). The caller compares these
against the authoritative constants and decides.

Your one contribution here is the exception. Request it only when the set exceeds a limit for a
reason **inherent to the scenario** rather than a fixable authoring choice — a booking enquiry that
is genuinely about four dates, say, versus a script that simply happened to ask for four numbers.
An exception granted on a fixable cause is a permanently lowered standard, so the `justification`
must name what about the scenario makes the concentration unavoidable.

Omit `qr027_exception` entirely when you are not requesting one. A `requested: true` with no
non-empty `justification` is rejected outright: that combination is how a limit gets bypassed
silently.

## 6. Reporting

- `feasible` — `false` when any of the ten points fails any test in §2.
- `category_semantics_ok` — `false` when any `answer_category` is not true of its answer, or when a
  point fits none of the 14.
- `reasons` — **required non-empty whenever either boolean is `false`.** One entry per problem,
  each naming the **item number**, the **test or category** it fails, and the **concrete reason**.
  A rejection nobody can act on is worse than no rejection: a `false` costs a full material
  regeneration, and the next attempt needs to know what to avoid.
- When both booleans are `true`, `reasons` may be empty. Report a clean set as clean.
