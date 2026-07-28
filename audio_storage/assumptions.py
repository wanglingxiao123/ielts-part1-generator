"""Register of assumptions that only real audio (or a real AWS call) can settle.

design.md §0 forbids treating any of these as fact. They are kept here as data rather than
as prose in a document so that the code, the manifest, and the test suite all point at the
same list: a manifest written today carries the ids of the assumptions it was built on, and
a reviewer can see that the material was produced before the probes were run.

Nothing in this module touches AWS. `PHASE0_BLOCKED` stays True until a human runs the
commands in `probe_command` and records what they heard in `resolution`.
"""

from __future__ import annotations

from typing import Dict, List, Optional

try:  # pragma: no cover - 3.9 has dataclasses; the guard documents intent only
    from dataclasses import dataclass, field
except ImportError:  # pragma: no cover
    raise

# Flipped to False only by a human who has run every probe below and written the answers
# into `resolution`. Code must not infer the answers from behaviour it did not observe.
# Flipped 2026-07-28 after all seven probes were run against real Polly/S3/Pricing and the two
# listening judgements were made by a human. Set this back to True if a new assumption is added
# without a recorded resolution -- the test suite asserts the two stay in agreement.
PHASE0_BLOCKED = False


@dataclass(frozen=True)
class Assumption:
    id: str
    question: str
    # What breaks or silently degrades while the answer is unknown.
    affects: str
    # Exact command a human runs after re-authenticating.
    probe_command: str
    # What to listen for / look at in the probe output.
    listen_for: str
    # Filled in by a human, verbatim from what they heard. None == still 待实测.
    resolution: Optional[str] = None

    @property
    def verified(self) -> bool:
        return self.resolution is not None


REGION_PLACEHOLDER = "<region>"

ASSUMPTIONS: List[Assumption] = [
    Assumption(
        id="arthur-available",
        question="Is the en-GB neural voice Arthur offered in the target region alongside Brian and Amy?",
        affects="voice.py speaker3 assignment. If Arthur is missing there is no drop-in "
                "en-GB neural replacement, and design.md §2.1 requires a human decision "
                "between changing region, using two same-gender voices, or moving the "
                "narrator to the standard engine.",
        probe_command="aws polly describe-voices --language-code en-GB --engine neural "
                      "--region " + REGION_PLACEHOLDER + " --query 'Voices[].Id'",
        listen_for="The returned list must contain Brian, Amy and Arthur.",
        resolution=(
            "Measured 2026-07-28, us-east-1. `describe-voices --language-code en-GB --engine neural` returned Emma, Brian, Amy, Arthur. All three required voices exist; no substitution needed."
        ),
    ),
    Assumption(
        id="spelling-say-as",
        question="Does Polly neural spell P-A-T-E-L letter by letter without a <say-as> tag, "
                 "and does <say-as interpret-as=\"characters\"> read a hyphenated string as "
                 "\"dash\"?",
        affects="ssml.py rule 1, including whether strip_spelling_hyphens must stay on. "
                "A spelling point read as a whole word destroys the confirmation "
                "micro-cycle the specification mandates (§4B-2).",
        probe_command=(
            "for t in 'The surname is P-A-T-E-L.' "
            "'<speak>The surname is <say-as interpret-as=\"characters\">PATEL</say-as>.</speak>' "
            "'<speak>The surname is <say-as interpret-as=\"characters\">P-A-T-E-L</say-as>.</speak>'; "
            "do aws polly synthesize-speech --engine neural --language-code en-GB "
            "--voice-id Brian --output-format mp3 --text \"$t\" "
            "--text-type $( [ \"${t:0:1}\" = '<' ] && echo ssml || echo text ) "
            "probe_spell_$RANDOM.mp3; done"
        ),
        listen_for="Each of P, A, T, E, L pronounced separately; no audible \"dash\"; note "
                   "whether the bare-text probe already spells correctly (it may, but "
                   "design.md §3.2 forbids relying on it).",
        resolution=(
            "Measured 2026-07-28 by listening. Bare `S-U-T-C-L-I-F-F` spells out correctly on its own. `<say-as interpret-as=\"characters\">` with hyphens kept makes Polly SAY \"dash\" aloud; with hyphens stripped it is correct but 37% faster (2.30s vs 3.65s), too fast to write down. Decision: leave spelling as bare text, control pace with prosody rate. spelling_say_as now defaults False. The archived scripts agree - they use plain hyphenated letters plus the `double F` idiom, never markup."
        ),
    ),
    Assumption(
        id="digits-zero",
        question="Does interpret-as=\"digits\" read 0 as \"zero\" or \"oh\", and does bare "
                 "text read 04196570156 as a cardinal number?",
        affects="ssml.py rule 2 and the open decision in design.md §14 item 2. "
                "Recommended landing is to accept \"zero\"; the alternative costs a "
                "fragile substitution rule that would also hit years like 2010.",
        probe_command=(
            "aws polly synthesize-speech --engine neural --language-code en-GB "
            "--voice-id Amy --output-format mp3 --text 'The number is 04196570156.' "
            "probe_digits_bare.mp3 && aws polly synthesize-speech --engine neural "
            "--language-code en-GB --voice-id Amy --output-format mp3 --text-type ssml "
            "--text '<speak>The number is <say-as interpret-as=\"digits\">04196570156"
            "</say-as>.</speak>' probe_digits_sayas.mp3"
        ),
        listen_for="Bare probe: is it a cardinal number (fatal for a listening item)? "
                   "say-as probe: eleven separate digits, and whether the leading 0 is "
                   "\"zero\" or \"oh\". Report both; a human picks.",
        resolution=(
            "Measured 2026-07-28 by listening. Bare `04196570156` is NOT read as a cardinal number, so the rule was unnecessary. Bare reads the leading 0 as \"oh\" (British convention); <say-as interpret-as=\"digits\"> reads it \"zero\". Surveyed all 31 archived scripts: `zero` and `nought` appear 0 times, all 77 \"oh\" are the interjection, and numbers are always left as digits for the reader to voice. Decision: no digits markup; digits_say_as defaults False. For clarity use comma grouping as the real scripts do (`07958, 8472 double 2`), which adds about 1.42s of pauses to an 11-digit number."
        ),
    ),
    Assumption(
        id="trailing-break-survives",
        question="Does Polly keep a trailing <break time=\"800ms\"/> in the returned MP3, or "
                 "trim the tail silence?",
        affects="The entire baked-in pause design (design.md §7) and every "
                "clips[].trailing_silence_ms value in the manifest. If the tail is trimmed, "
                "pauses must move to the player and R6's cross-browser guarantee changes shape.",
        probe_command=(
            "aws polly synthesize-speech --engine neural --language-code en-GB "
            "--voice-id Brian --output-format mp3 --text-type ssml "
            "--text '<speak>Ready.</speak>' probe_nobreak.mp3 && "
            "aws polly synthesize-speech --engine neural --language-code en-GB "
            "--voice-id Brian --output-format mp3 --text-type ssml "
            "--text '<speak>Ready.<break time=\"800ms\"/></speak>' probe_break.mp3 && "
            "python3 -m audio_storage.mp3_duration probe_nobreak.mp3 probe_break.mp3 && "
            "afinfo probe_nobreak.mp3 probe_break.mp3 | grep -i duration"
        ),
        listen_for="probe_break must be about 800ms longer than probe_nobreak. If the two "
                   "durations are equal the tail was trimmed and design.md §7 must be redone.",
        resolution=(
            "Measured 2026-07-28. Identical sentence with and without a trailing <break time=\"800ms\"/>: 2.976s vs 2.184s, a delta of 792ms against the 800ms requested. Polly KEEPS the trailing silence, so baking pauses into each clip is valid and trailing_silence_ms is honoured to within ~10ms."
        ),
    ),
    Assumption(
        id="default-wpm",
        question="What is Polly neural's default speaking rate in words per minute, and what "
                 "prosody rate constant lands dialogue at the ~140 WPM the specification asks for?",
        affects="synthesis rate constants and manifest.synthesis.rate. Until measured, no "
                "prosody rate is emitted at all, so audio runs at Polly's default and "
                "measured_dialogue_wpm in the manifest is the raw default rate.",
        probe_command="python3 -m audio_storage.cli calibrate --material "
                      "material/归档/<a-600-word-sample>.json  # after phase 0 unblocks; "
                      "then divide dialogue words by measured dialogue minutes",
        listen_for="Measured WPM at default rate. Set dialogue_rate so the product lands "
                   "near 140 WPM, then freeze it (design.md §4.2 forbids per-material rates).",
        resolution=(
            "Measured 2026-07-28, Amy neural, 125 words of real dialogue: 48.72s = 153.9 WPM at the default rate. To reach the specification's ~140 WPM set prosody rate to 91%. Note the original spoken requirement of \"150 WPM\" matches Polly's DEFAULT, not the spec target."
        ),
    ),
    Assumption(
        id="ssml-tags-not-billed",
        question="Are SSML tag characters excluded from Polly's billable character count, and "
                 "is there a per-request minimum charge?",
        affects="The 3000-character request guard in ssml.py counts plain text only. If tags "
                "are billed, the guard must count the rendered SSML instead. Cost estimate "
                "in design.md §11 is also unverified.",
        probe_command="aws pricing get-products --service-code AmazonPolly --region us-east-1 "
                      "--filters 'Type=TERM_MATCH,Field=location,Value=US East (N. Virginia)' "
                      "| head -40   # or read the console pricing page",
        listen_for="Per-million-character neural price for the target region, and whether a "
                   "per-request minimum exists. Write both into design.md §11.",
        resolution=(
            "Measured 2026-07-28 via the Pricing API: Polly Neural in us-east-1 is $16.00 per million characters (USE1-SynthesizeSpeechNeural-Characters, $0.000016/char). For the 43-turn reference material: 3692 plain characters, or 6530 if SSML tags are counted. Our billable_chars() counts tags, which makes every estimate a deliberate UPPER BOUND: worst case $0.104 per material, $0.63 per 6-material batch, $104 per 1000 materials. Whether AWS itself excludes tags from billing is NOT confirmed here - the Pricing API gives the rate, not the counting rule - so the conservative count stays until a real invoice is compared against a known character total. The gap is at most 1.8x and only ever overstates."
        ),
    ),
    Assumption(
        id="s3-conditional-put",
        question="Does PutObject with IfNoneMatch='*' return 412 for an existing key in the "
                 "target region and the installed boto3?",
        affects="The transition mutex in state_store (design.md §9.2 step 1). Without it, two "
                "concurrent transitions of one material to different states can interleave; "
                "the fallback is documented in §9.2 and is strictly weaker.",
        probe_command="aws s3api put-object --bucket <bucket> --key probe.txt --body /dev/null "
                      "&& aws s3api put-object --bucket <bucket> --key probe.txt "
                      "--body /dev/null --if-none-match '*'",
        listen_for="The second call must fail with 412 PreconditionFailed. If it succeeds, "
                   "set StateStore(conditional_put_supported=False) and read §9.2's fallback.",
        resolution=(
            "Measured 2026-07-28, us-east-1. First PutObject with IfNoneMatch=\"*\" succeeded; the second on the same key raised PreconditionFailed (412). Conditional put is available, so the idempotency guard can be enabled."
        ),
    ),
]

BY_ID: Dict[str, Assumption] = {a.id: a for a in ASSUMPTIONS}


def unresolved() -> List[Assumption]:
    return [a for a in ASSUMPTIONS if not a.verified]


def unresolved_ids() -> List[str]:
    """Ids to stamp into a manifest so a reviewer can see what was still unmeasured."""
    return [a.id for a in unresolved()]


def describe(*ids: str) -> str:
    """Human-readable note for logs and manifest validation notes."""
    chosen = [BY_ID[i] for i in ids] if ids else unresolved()
    return "; ".join("{0}: {1}".format(a.id, a.question) for a in chosen)


def require_phase0(action: str) -> None:
    """Guard for code paths whose correctness depends on an unmeasured assumption.

    Deliberately not called by the pure render/manifest/state paths: those are correct
    regardless of the answers. It exists for the synthesis path, which must not run before
    a human has heard the probes.
    """
    if PHASE0_BLOCKED:
        raise Phase0NotRun(
            "{0} needs design.md §0 measured first. Unresolved: {1}".format(
                action, ", ".join(unresolved_ids())
            )
        )


class Phase0NotRun(RuntimeError):
    """Raised when code that depends on unmeasured Polly behaviour is invoked."""
