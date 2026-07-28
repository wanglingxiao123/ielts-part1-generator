"""Turn text -> SSML. A render-time transform; material.json is never modified.

Why render-time: the contract is frozen, blueprint `evidence` values are substrings cut out
of the material text, and the 答案原词铁律 says a recordable answer word must survive
untouched. Keeping SSML out of the data satisfies all three structurally -- the stored text
is not touched, so it cannot be damaged (design.md §3, §3.3).

The operation order is fixed and load-bearing:

    XML-escape -> exception table -> spelling rule -> digits rule -> wrap

Escaping must come first or `Fish & Chips` breaks the document. Tag insertion must come
after escaping or the tags this module inserts get escaped into literal text and Polly reads
them aloud. To make that order safe rather than merely intended, the text is carried as a
list of segments: rules only ever see PLAIN segments, and anything a rule emits becomes a
MARKUP segment that no later rule can look inside. So rule 2 cannot corrupt rule 1's output
and no rule can re-escape an inserted tag.

Every rule is individually toggleable through RenderConfig, because design.md §0 lists the
Polly behaviours these rules compensate for as 待实测: a human may find that the spelling
rule needs its hyphens kept, or that the digits rule should read 0 as "oh". Toggles keep
that a config change rather than a rewrite.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Dict, Iterable, List, Optional, Sequence, Tuple
from xml.sax.saxutils import escape as xml_escape
from xml.sax.saxutils import unescape as xml_unescape

# Bumped whenever a rule changes what Polly is sent. It feeds the synthesis cache key, so a
# ruleset change forces a full resynthesis rather than leaving one material half-rendered
# under the old rules and half under the new (design.md §4.3).
SSML_RULESET_VERSION = 1

# Polly's per-request billable character ceiling, order of magnitude. A single turn is far
# below it, but §Constraints forbids relying on "the text is surely short enough".
MAX_BILLABLE_CHARS = 3000

# Rule 1: at least three single letters joined by hyphens. Requiring three (two hyphens)
# rather than two is what keeps `T-shirt` and `e-mail` out: those have a single letter on
# one side only, so they never reach the second repetition.
SPELLING_RE = re.compile(r"\b(?:[A-Za-z]-){2,}[A-Za-z]\b")

# Rule 2: five or more consecutive digits. Deliberately not four: a bare year like 2010 must
# keep Polly's own normalisation ("twenty ten"), and reading it digit by digit would be
# wrong. Phone numbers, reference numbers and the numeric half of a postcode all clear five.
LONG_DIGITS_RE = re.compile(r"\b\d{5,}\b")

_TAG_RE = re.compile(r"<[^>]*>")

PLAIN = "plain"
MARKUP = "markup"


class SsmlError(ValueError):
    """The turn cannot be rendered into SSML safely."""


class TargetDamaged(AssertionError):
    """A render rule changed a word that a candidate is expected to write down."""


# Measured 2026-07-28 (Amy neural, us-east-1): the default rate reads 125 words of real
# dialogue in 48.72s = 153.9 WPM. The specification asks for ~140 WPM, hence 91%.
# The original spoken requirement of "150 WPM" turns out to describe Polly's DEFAULT rather
# than the spec target, which is why this constant is below 100%.
MEASURED_DEFAULT_WPM = 153.9
SPEC_TARGET_WPM = 140
MEASURED_DIALOGUE_RATE = "91%"

# Measured 2026-07-28 by listening, plus a survey of the 31 archived real scripts. Both
# compensating say-as rules turned out to be unnecessary or harmful, so they now default OFF:
#
# * Bare `04196570156` is NOT read as a cardinal number, so the digits rule was solving a
#   problem that does not exist. Worse, <say-as interpret-as="digits"> reads the leading 0 as
#   "zero", while bare text reads it "oh" -- and "oh" is the British convention this exam
#   uses. The archived scripts never spell a digit out: `zero` and `nought` appear 0 times
#   across all 31, all 77 occurrences of "oh" are the interjection, and numbers are always
#   left as digits (`02079460385`) for the reader to voice.
# * Bare `S-U-T-C-L-I-F-F` already spells out correctly. Feeding hyphens into
#   <say-as interpret-as="characters"> makes Polly say "dash" aloud, and stripping them first
#   makes it 37% faster than bare text -- too fast to write down.
#
# What the audio actually needed was pacing, not markup. The archived scripts get clarity the
# same way: comma grouping (`07958, 8472 double 2`) and the `double` idiom
# (`S-U-T-C-L-I, double F`), both of which are plain text the generator can emit directly.
COMMA_GROUPING_ADDS_MS = 1420


@dataclass(frozen=True)
class RenderConfig:
    """Which compensating rules are active, and with what parameters.

    Defaults are the conservative ones: tag the two high-risk patterns, add no prosody at
    all. Callers opt into MEASURED_DIALOGUE_RATE explicitly rather than getting it by default,
    so a clip's rate is always a stated choice and the manifest's WPM figure stays honest.
    """

    # Off by default: bare hyphenated letters already spell out, and the say-as path either
    # says "dash" or races. See the note above MEASURED_DIALOGUE_RATE.
    spelling_say_as: bool = False
    # Whether hyphens are removed before the letters reach <say-as>. 待实测: a hyphen may or
    # may not be read as "dash" inside interpret-as="characters".
    strip_spelling_hyphens: bool = True
    # None until a human calibrates. "90%" is the design's untested starting point.
    spelling_rate: Optional[str] = None
    # Off by default: bare digits are not read as cardinals, and say-as would replace the
    # British "oh" with "zero". See the note above MEASURED_DIALOGUE_RATE.
    digits_say_as: bool = False
    # Escape hatch, exact substring -> SSML fragment. Filled in only from things a human
    # actually heard go wrong; never pre-populated with speculation.
    pronunciation_overrides: Dict[str, str] = field(default_factory=dict)
    # Turn-level speaking rate. None means "send no <prosody>", i.e. Polly's default.
    rate: Optional[str] = None
    # Silence baked onto the end of the clip (design.md §7). Measured 2026-07-28: Polly keeps
    # a trailing <break> to within ~10ms (792ms returned for 800ms requested), so baking is safe.
    trailing_silence_ms: int = 0

    def rule_ids(self) -> List[str]:
        active = []
        if self.pronunciation_overrides:
            active.append("pronunciation_overrides")
        if self.spelling_say_as:
            active.append("spelling_say_as")
        if self.digits_say_as:
            active.append("digits_say_as")
        return active


DEFAULT_CONFIG = RenderConfig()

Segment = Tuple[str, str]


def _escape(text: str) -> str:
    """XML-escape &, < and >. Step 1, and it must stay step 1."""
    return xml_escape(text)


def _apply_overrides(segments: List[Segment], overrides: Dict[str, str]) -> List[Segment]:
    """Step 2. Longest key first so a specific override beats a shorter prefix of it."""
    for key in sorted(overrides, key=len, reverse=True):
        needle = _escape(key)
        if not needle:
            continue
        replacement = overrides[key]
        segments = _split_on_literal(segments, needle, replacement)
    return segments


def _split_on_literal(
    segments: List[Segment], needle: str, replacement: str
) -> List[Segment]:
    out: List[Segment] = []
    for kind, value in segments:
        if kind != PLAIN or needle not in value:
            out.append((kind, value))
            continue
        parts = value.split(needle)
        for index, part in enumerate(parts):
            if index:
                out.append((MARKUP, replacement))
            if part:
                out.append((PLAIN, part))
    return [seg for seg in out if seg[1]]


def _apply_pattern(
    segments: List[Segment], pattern: "re.Pattern[str]", build
) -> List[Segment]:
    """Rewrite regex hits inside PLAIN segments only; emissions become MARKUP."""
    out: List[Segment] = []
    for kind, value in segments:
        if kind != PLAIN:
            out.append((kind, value))
            continue
        cursor = 0
        for match in pattern.finditer(value):
            if match.start() > cursor:
                out.append((PLAIN, value[cursor : match.start()]))
            out.append((MARKUP, build(match.group(0))))
            cursor = match.end()
        if cursor < len(value):
            out.append((PLAIN, value[cursor:]))
    return [seg for seg in out if seg[1]]


def _spelling_fragment(run: str, config: RenderConfig) -> str:
    letters = run.replace("-", "") if config.strip_spelling_hyphens else run
    fragment = '<say-as interpret-as="characters">{0}</say-as>'.format(letters)
    if config.spelling_rate:
        fragment = '<prosody rate="{0}">{1}</prosody>'.format(config.spelling_rate, fragment)
    return fragment


def _digits_fragment(run: str) -> str:
    return '<say-as interpret-as="digits">{0}</say-as>'.format(run)


def billable_chars(text: str) -> int:
    """Characters Polly is expected to bill: the plain text, not the tags.

    Counting plain text follows design.md §3.1, which records "tags are not billed" as known
    but flagged for re-check (assumptions.ssml-tags-not-billed). If that turns out to be
    wrong the guard must count the rendered SSML instead; the call sites are here and in
    render_ssml only.
    """
    return len(text)


def render_ssml(text: str, config: RenderConfig = DEFAULT_CONFIG) -> str:
    """Render one turn. Pure: same text plus same config always gives the same SSML."""
    if not isinstance(text, str):
        raise SsmlError("turn text must be a string, got {0}".format(type(text).__name__))
    if not text.strip():
        raise SsmlError("turn text is empty; refusing to synthesise silence")
    count = billable_chars(text)
    if count > MAX_BILLABLE_CHARS:
        # Explicit failure rather than a silent truncation: a truncated turn would produce a
        # clip that sounds complete while missing its final words.
        raise SsmlError(
            "turn has {0} billable characters, over the {1} per-request limit; "
            "split the turn upstream".format(count, MAX_BILLABLE_CHARS)
        )

    segments: List[Segment] = [(PLAIN, _escape(text))]  # 1. escape
    if config.pronunciation_overrides:  # 2. exception table
        segments = _apply_overrides(segments, config.pronunciation_overrides)
    if config.spelling_say_as:  # 3. spelling
        segments = _apply_pattern(
            segments, SPELLING_RE, lambda run: _spelling_fragment(run, config)
        )
    if config.digits_say_as:  # 4. digits
        segments = _apply_pattern(segments, LONG_DIGITS_RE, _digits_fragment)

    body = "".join(value for _, value in segments)  # 5. wrap
    if config.rate:
        body = '<prosody rate="{0}">{1}</prosody>'.format(config.rate, body)
    if config.trailing_silence_ms > 0:
        body += '<break time="{0}ms"/>'.format(config.trailing_silence_ms)
    return "<speak>{0}</speak>".format(body)


def strip_tags(ssml: str) -> str:
    """Inverse of rendering, as far as it can be: drop tags, undo XML escaping."""
    return xml_unescape(_TAG_RE.sub("", ssml))


def _whitelisted(text: str, config: RenderConfig) -> str:
    """The original text with exactly the differences the rules are allowed to introduce.

    Used by the invariant check. Anything beyond this -- a dropped word, a mangled number --
    is a rendering bug, and the assertion below is what catches it.
    """
    result = text
    for key in sorted(config.pronunciation_overrides, key=len, reverse=True):
        replacement = strip_tags(config.pronunciation_overrides[key])
        result = result.replace(key, replacement)
    if config.spelling_say_as and config.strip_spelling_hyphens:
        result = SPELLING_RE.sub(lambda m: m.group(0).replace("-", ""), result)
    return result


def assert_invariant(text: str, ssml: str, config: RenderConfig = DEFAULT_CONFIG) -> None:
    """strip_tags(render(text)) must equal the text, modulo the rule whitelist (§3.3)."""
    actual = strip_tags(ssml)
    expected = _whitelisted(text, config)
    if actual != expected:
        raise TargetDamaged(
            "SSML render changed the spoken text.\n  expected: {0!r}\n  actual:   {1!r}".format(
                expected, actual
            )
        )


def render_turn(text: str, config: RenderConfig = DEFAULT_CONFIG) -> str:
    """render_ssml plus the invariant check. Use this on the synthesis path."""
    ssml = render_ssml(text, config)
    assert_invariant(text, ssml, config)
    return ssml


def _speakable(value: str) -> str:
    """Letters and digits only, casefolded.

    Comparing this way is what lets a spelling point pass: the blueprint target is `Patel`
    while the script says `P-A-T-E-L`, and rule 1 renders that as `PATEL`. All three reduce
    to `patel`. It also means punctuation or whitespace changes cannot be mistaken for
    damage to an answer word.
    """
    return re.sub(r"[^0-9a-z]+", "", value.casefold())


def check_targets_intact(
    turns: Sequence[dict],
    blueprint: dict,
    config: RenderConfig = DEFAULT_CONFIG,
    rendered: Optional[Dict[int, str]] = None,
) -> List[dict]:
    """Report, per blueprint item, whether its `target` survives rendering.

    Three outcomes per item:
      * intact           -- the target is still speakable in the rendered turn
      * damaged          -- it was in the source turn and is not in the render: a rule bug
      * absent_from_source -- it was never literally in the turn text, so rendering cannot
                            be blamed; surfaced anyway because it usually means the
                            blueprint anchor is wrong

    Only `damaged` is a violation here; the other two are informational. Splitting them
    matters, because folding them together would let a genuine rule bug hide behind the much
    more common paraphrased-target case.
    """
    results: List[dict] = []
    items = blueprint.get("items") if isinstance(blueprint, dict) else None
    for item in items or []:
        if not isinstance(item, dict):
            continue
        target = item.get("target")
        turn_index = item.get("turn_index")
        if not isinstance(target, str) or not target.strip():
            results.append(
                {"number": item.get("number"), "status": "no_target", "turn_index": turn_index}
            )
            continue
        if (
            not isinstance(turn_index, int)
            or isinstance(turn_index, bool)
            or not 0 <= turn_index < len(turns)
        ):
            results.append(
                {
                    "number": item.get("number"),
                    "status": "bad_anchor",
                    "turn_index": turn_index,
                    "target": target,
                }
            )
            continue
        turn = turns[turn_index]
        source = turn.get("text") if isinstance(turn, dict) else None
        if not isinstance(source, str):
            results.append(
                {
                    "number": item.get("number"),
                    "status": "bad_anchor",
                    "turn_index": turn_index,
                    "target": target,
                }
            )
            continue
        ssml = (rendered or {}).get(turn_index) or render_ssml(source, config)
        spoken = _speakable(strip_tags(ssml))
        needle = _speakable(target)
        if needle and needle in spoken:
            status = "intact"
        elif needle and needle in _speakable(source):
            status = "damaged"
        else:
            status = "absent_from_source"
        results.append(
            {
                "number": item.get("number"),
                "status": status,
                "turn_index": turn_index,
                "target": target,
            }
        )
    return results


def assert_targets_intact(
    turns: Sequence[dict],
    blueprint: dict,
    config: RenderConfig = DEFAULT_CONFIG,
    rendered: Optional[Dict[int, str]] = None,
) -> List[dict]:
    """Raise if any answer word was damaged by rendering. Returns the full report."""
    report = check_targets_intact(turns, blueprint, config, rendered)
    damaged = [row for row in report if row["status"] == "damaged"]
    if damaged:
        raise TargetDamaged(
            "SSML rendering removed answer words: "
            + "; ".join(
                "item {0} target {1!r} at turn {2}".format(
                    row["number"], row["target"], row["turn_index"]
                )
                for row in damaged
            )
        )
    return report


def rendered_differs(text: str, ssml: str) -> bool:
    """True when a rule fired, i.e. the manifest should record the SSML for audit (§5)."""
    return ssml != "<speak>{0}</speak>".format(_escape(text))


def render_all(
    turns: Iterable[dict], config_for: "callable"
) -> Dict[int, str]:
    """Render every turn. `config_for(index, turn) -> RenderConfig` supplies per-turn config
    (speaker rate and trailing silence differ by position, design.md §7)."""
    out: Dict[int, str] = {}
    for index, turn in enumerate(turns):
        if not isinstance(turn, dict) or not isinstance(turn.get("text"), str):
            raise SsmlError("turn {0} is malformed; cannot render".format(index))
        out[index] = render_turn(turn["text"], config_for(index, turn))
    return out
