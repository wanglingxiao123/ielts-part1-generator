"""Input isolation for the blind question audit.

The question auditor's entire product is the answer it rebuilt from the script without a key beside
it. That makes the leak here worse than the material side's, not better: a material audit that saw the
plan produces an inflated score, while a question audit that saw the answers produces a document that
*agrees with the writer about everything* -- every reconstruction matches, every uniqueness check
passes, the status comes out clean, and the shape is byte-for-byte a genuine review. There is no error
and nothing in the artifact to notice.

So these tests try to break the isolation rather than confirm it, and each asks the same question: if
somebody made the obvious mistake here, would anything fail? The obvious mistakes are specific and all
of them have been made once already on the material side -- reuse the existing guard, reuse the
existing payload builder, add a parameter to the frozen type, pass the whole package where block A was
wanted.

Four independent parts, tested separately:

1. **The two guards reject each other's payloads.** Neither can become an option on a shared function.
2. **The frozen type cannot gain the answers after construction.**
3. **Every key in ``ANSWER_ONLY_KEYS`` is actually enforced**, and the material payload is unchanged.
4. **The agent has no way to run a command**, which is what makes 1-3 hold at run time: the answers for
   these ten items exist on disk while they are being generated.
"""

from __future__ import annotations

import asyncio
import json

import pytest

from backend.deterministic.guards import (
    ANSWER_ONLY_KEYS,
    BLUEPRINT_ONLY_KEYS,
    BlindnessViolation,
    assert_answer_blind,
    assert_blind,
)
from backend.deterministic.question_metrics import question_metrics
from backend.steps.agent_steps import (
    BlindAuditInput,
    BlindQuestionAuditInput,
    build_audit_payload,
    build_question_audit_message,
    build_question_audit_payload,
)


@pytest.fixture
def metrics(material, question_face) -> dict:
    return question_metrics(material, question_face)


class TestTheTwoBlindGuardsAreNotInterchangeable:
    """Part 1, and the property the whole design turns on.

    ``ANSWER_ONLY_KEYS`` exists because ``BLUEPRINT_ONLY_KEYS`` contains ``response_form``,
    ``answer_category`` and ``narrator_window_id`` -- all three of which a question face legitimately
    prints. The tempting fix was to drop them from that tuple so one guard could serve both callers,
    which would have silently reopened the material-audit leak the tuple was built for. These two tests
    fail if anyone tries it.
    """

    def test_the_question_payload_fails_the_material_guard(self, material, question_face, metrics):
        """Proof by contradiction: a clean question payload is not a clean material payload."""
        payload = build_question_audit_message(material, question_face, metrics)
        assert_answer_blind(payload)
        with pytest.raises(BlindnessViolation) as exc:
            assert_blind(payload, "question payload sent down the material audit path")
        # The three face fields, named, so a future edit that "fixes" this test has to confront them.
        assert "response_form" in str(exc.value)
        assert "answer_category" in str(exc.value)

    def test_the_material_payload_passes_both(self, material):
        """The other direction, and it must NOT raise.

        A material payload carries neither a plan nor an answer, so both guards accept it. Asserted
        because the cheap way to make the test above pass is to widen ``ANSWER_ONLY_KEYS`` until it
        rejects everything, and an assertion that cannot fail reads as coverage.
        """
        payload = build_audit_payload(BlindAuditInput(material, {"dialogue_words": 618}))
        assert_blind(payload)
        assert_answer_blind(payload)

    def test_neither_builder_can_be_asked_to_be_less_blind(self, material, question_face, metrics):
        """No ``blind=`` parameter on either builder, checked at the signature.

        The failure mode this pins is a shared builder with a flag: it would pass every other test in
        this file, because every other test constructs its input correctly.
        """
        import inspect

        for builder in (build_audit_payload, build_question_audit_payload):
            parameters = list(inspect.signature(builder).parameters)
            assert parameters == ["data"], (builder.__name__, parameters)

    def test_the_material_tuple_still_holds_the_three_face_fields(self):
        """The reason two tuples exist, stated as an assertion instead of only as a comment.

        If a later edit removes these three from ``BLUEPRINT_ONLY_KEYS`` -- the one-line "fix" that
        makes one guard serve both callers -- the material audit stops catching a serialised plan and
        the only symptom is a score that comes out too high. This test fails first.
        """
        for field in ("response_form", "answer_category", "narrator_window_id"):
            assert field in BLUEPRINT_ONLY_KEYS, field
            assert field not in ANSWER_ONLY_KEYS, field

    def test_the_material_payload_is_still_byte_identical(self, material):
        """The material audit must not have changed by a character.

        Duplicated from test_guards.py deliberately: that copy guards against the feasibility path
        disturbing it, this one against the question path. A single copy would leave whichever caller
        was added later unguarded.
        """
        payload = build_audit_payload(BlindAuditInput(material, {"dialogue_words": 618}))
        expected = "\n\n".join([
            "Audit the listening material below.",
            "## material.json\n\n%s" % json.dumps(material, ensure_ascii=False, indent=2),
            "## Deterministic metrics (already calculated; do not recount)\n\n%s"
            % json.dumps({"dialogue_words": 618}, ensure_ascii=False, indent=2),
        ])
        assert payload == expected


class TestTheAnswersCannotReachThePayload:
    """Part 2 and 3: the type, and then the wire.

    Ordered that way because they catch different mistakes. The type catches an extra argument at a
    call site; the guard catches an answer arriving inside a field that was supposed to hold something
    else -- a face assembled from the wrong slice of the package, for instance.
    """

    def test_the_input_type_is_frozen(self, material, question_face, metrics):
        data = BlindQuestionAuditInput(material, question_face, metrics)
        with pytest.raises(AttributeError):
            data.answer_key = [{"number": 1, "canonical": "Anna Woods"}]
        with pytest.raises(AttributeError):
            data.question_face = {}
        with pytest.raises(AttributeError):
            del data.material

    def test_the_input_type_has_exactly_three_slots(self):
        """Adding a fourth is the one change that breaks the isolation, so it must be a visible act."""
        assert BlindQuestionAuditInput.__slots__ == (
            "material", "question_face", "question_metrics")

    @pytest.mark.parametrize("key", [k for k in ANSWER_ONLY_KEYS])
    def test_every_declared_key_is_actually_enforced(self, key):
        """A key listed but not enforced is worse than no list: it reads as protection.

        Four entries are quoted JSON-field forms (``"canonical"``, ``"alternatives"``, ``"evidence"``,
        ``"quote"``) because those words occur in ordinary dialogue -- a removals firm quoting a price
        is a textbook Part 1 scenario. The parametrisation uses the tuple's own entries, so it tests
        each one in the form the guard actually matches.
        """
        with pytest.raises(BlindnessViolation):
            assert_answer_blind("payload fragment %s trailing text" % key)

    @pytest.mark.parametrize("word", ["quote", "quoted", "canonical", "alternatives", "evidence"])
    def test_the_prose_words_do_not_fire(self, word):
        """The anti-false-positive direction, and it is not a formality.

        ``confirmed`` had to be removed from ``BLUEPRINT_ONLY_KEYS`` for exactly this reason: a guard
        that fires on every legitimate call gets diagnosed as noise and switched off, taking the real
        check with it. A script where the removals firm "quoted us £450" must audit normally.
        """
        assert_answer_blind("the firm %s us a price on the phone" % word)

    def test_a_leaked_answer_block_is_caught(self, material, question_face, metrics,
                                             question_package):
        """The realistic accident: the whole package passed where block A was wanted."""
        payload = build_question_audit_payload(
            BlindQuestionAuditInput(material, question_package, metrics))
        with pytest.raises(BlindnessViolation) as exc:
            assert_answer_blind(payload)
        assert "answer_key" in str(exc.value)

    def test_a_leaked_evidence_block_is_caught(self, material, question_face, metrics,
                                               question_package):
        """And the half that carries the anchors rather than the answers.

        Tested separately from the case above because a slice that drops ``answer_key`` and keeps
        ``evidence`` looks sanitised -- and it hands over the quote and the turn index, which is most of
        the reconstruction the auditor is supposed to produce.
        """
        face_plus_evidence = dict(question_face, evidence=question_package["evidence"])
        payload = build_question_audit_payload(
            BlindQuestionAuditInput(material, face_plus_evidence, metrics))
        with pytest.raises(BlindnessViolation) as exc:
            assert_answer_blind(payload)
        assert any(hit in str(exc.value) for hit in ('"quote"', "turn_index"))

    def test_a_leaked_plan_is_caught(self, material, question_face, metrics, blueprint):
        """``items[].target`` IS the answer, so the plan is an answer key under another name.

        Caught on the bare word ``blueprint`` rather than on ``target``, and that is the intended
        mechanism: ``target`` is deliberately absent from the tuple because a carrier can say "our
        target date" in either quoted or unquoted form, and a serialised plan always carries
        ``blueprint_schema_version``. Asserted by name so a future edit that drops ``blueprint`` from
        the tuple fails here rather than passing quietly.
        """
        payload = build_question_audit_payload(
            BlindQuestionAuditInput(material, question_face, dict(metrics, plan=blueprint)))
        with pytest.raises(BlindnessViolation) as exc:
            assert_answer_blind(payload)
        assert "blueprint" in str(exc.value)

    def test_the_clean_payload_carries_the_narration(self, material, question_face, metrics):
        """The one thing that must NOT be withheld, and withholding it would look like caution.

        Without narrator turns the auditor cannot decide window membership at all (SC-019 / AL-017),
        which is one of the judgements this step exists to obtain. So the payload is checked for
        containing them rather than only for not containing answers.
        """
        payload = build_question_audit_message(material, question_face, metrics)
        assert_answer_blind(payload)
        narration = [turn["text"] for turn
                     in material["listening_material_parts"][0]["script"]["turns"]
                     if turn.get("speaker") == "speaker1"]
        assert narration, "the fixture must have narration for this test to mean anything"
        for text in narration:
            assert text in payload


class TestTheQuestionMetricsAreFaceOnly:
    """Part 3b: the counts are computed from the page, not from the answers.

    ``validate_questions_part1.py`` computes a richer set for its own use, and its QR-027 tallies split
    on the canonicals. Reusing them here would have been the obvious economy, and an aggregate over the
    answers is still information about the answers -- with the difference that nothing downstream would
    look wrong.
    """

    def test_the_metrics_pass_the_guard(self, material, question_face, metrics):
        assert_answer_blind(json.dumps(metrics, ensure_ascii=False))

    def test_the_metrics_do_not_contain_any_canonical(self, question_package, metrics):
        serialised = json.dumps(metrics, ensure_ascii=False).casefold()
        for entry in question_package["answer_key"]:
            canonical = str(entry["canonical"]).casefold()
            assert canonical not in serialised, canonical

    def test_the_metrics_are_computed_without_the_answers(self, material, question_face):
        """The signature is the assertion: there is no parameter an answer could arrive through."""
        import inspect

        assert list(inspect.signature(question_metrics).parameters) == [
            "material", "question_face"]

    def test_the_blank_distribution_is_recomputed_not_read_back(self, material, question_face,
                                                               clone):
        """Reading the declared ``blank_position`` would hand QR-026 to whoever wrote the face.

        Mutating every declaration to ``initial`` must not move the computed distribution by one.
        """
        before = question_metrics(material, question_face)["blank_positions"]
        tampered = clone(question_face)
        for question in tampered["questions"]:
            question["blank_position"] = "initial"
        assert question_metrics(material, tampered)["blank_positions"] == before

    def test_an_unparseable_narration_omits_the_windows_rather_than_guessing(self, material,
                                                                            question_face, clone):
        """A missing key beats a plausible wrong one.

        The auditor is told to treat these numbers as settled, so a window attribution invented from
        half-read narration is a wrong answer it has been instructed not to re-derive. ``{}`` would
        have been the lazy return and reads as "window 0 for everything".
        """
        broken = clone(material)
        broken["listening_material_parts"][0]["script"]["turns"] = [
            turn for turn in broken["listening_material_parts"][0]["script"]["turns"]
            if turn.get("speaker") != "speaker1"]
        computed = question_metrics(broken, question_face)
        assert "narrator_windows" not in computed
        # And the face-side counts still arrive: one unparseable input must not blank the rest.
        assert computed["item_count"] == 10


class TestTheQuestionAuditorCannotRunAnything:
    """Part 4: no shell, by absence rather than by configuration.

    ``strands_tools.shell`` calls ``pty.fork()`` directly and its signature takes no ``agent``, so
    ``agent.sandbox`` cannot bound it -- measured on the material side. The only safe arrangement is not
    to hand it over. It matters more here: the material auditor could have read a stale plan, while this
    agent runs immediately after the questions were written, so the file on disk holds *this set's*
    actual answers.
    """

    def test_the_question_audit_agent_has_no_shell(self):
        from backend import agents as agents_module

        agent = agents_module.build_question_audit_agent()
        assert sorted(agent.tool_names) == ["file_read", "skills"]

    def test_it_is_sandboxed_to_the_audit_pool(self):
        from backend import agents as agents_module

        agent = agents_module.build_question_audit_agent()
        assert agent.sandbox.root == agents_module.pool_dir(agents_module.AUDIT_POOL).resolve()

    def test_it_shares_no_state_between_calls(self):
        """A re-review must not inherit the first review's conclusions."""
        from backend import agents as agents_module

        first = agents_module.build_question_audit_agent()
        second = agents_module.build_question_audit_agent()
        assert first is not second
        assert first.messages == [] and second.messages == []
        assert first.state is not second.state

    def test_its_prompt_names_what_is_withheld(self):
        """Procedural prompts elsewhere; this one has to state the omission.

        Measured reasoning rather than style: an auditor looking at ten gaps that each have a right
        answer somewhere tends to treat a missing key as a packaging accident and hedge around it. The
        prompt says the omission is the design.
        """
        from backend import agents as agents_module

        prompt = agents_module.QUESTION_AUDIT_SYSTEM_PROMPT
        assert "must not ask" in prompt
        assert "report the leak" in prompt

    def test_it_activates_the_question_audit_skill_and_not_the_generator_s(self):
        """The pool boundary still holds for the second audit-pool member.

        Both audit skills are reachable from both audit agents, which is acceptable -- neither carries
        answers, and activating the wrong one produces a reply the envelope rejects, loudly. What must
        stay unreachable is the generate pool, where the answers and the plan live.
        """
        from strands.vended_plugins.skills import AgentSkills, Skill
        from backend import agents as agents_module

        agent = agents_module.build_question_audit_agent()
        pool = agents_module.pool_dir(agents_module.AUDIT_POOL)
        plugin = AgentSkills(skills=Skill.from_directory(str(pool)))

        def activate(name: str) -> str:
            return asyncio.run(plugin.skills(
                skill_name=name, tool_context=type("Ctx", (), {"agent": agent})()))

        assert "not found" not in activate("audit-questions-part1")
        for forbidden in ("generate-questions-part1", "generate-listening-part1",
                         "feasibility-listening-part1"):
            assert "not found" in activate(forbidden), forbidden


class TestTheReviewEnvelopeRejectsAnEmptyReconstruction:
    """The one shape check the other envelopes have no equivalent of.

    ``reconstructed_answers: []`` is well-formed, satisfies the schema's array type, passes any
    key-presence check, and leaves the deterministic cross-check with nothing to compare -- which it
    reports as agreement. It is the only field in the reply that is worthless while being valid.
    """

    def test_an_empty_reconstruction_is_rejected(self):
        from backend.steps.agent_steps import _question_audit_envelope
        from backend.steps.call import ModelCallError

        reply = json.dumps({"reconstructed_answers": [], "per_question_findings": [],
                            "coverage": {"reviewed_question_ids": [], "unreviewed": []},
                            "question_qc_status": "PASS"})
        with pytest.raises(ModelCallError) as exc:
            _question_audit_envelope(reply, "question audit")
        assert "reconstructed" in str(exc.value)

    def test_a_decoy_first_object_does_not_qualify(self):
        """Measured on the material side: a ``{"verdict": "PASS"}`` decoy was accepted as a pass.

        ``extract_json`` returns the first balanced object, so requiring the four load-bearing keys is
        what makes a decoy fail rather than win.
        """
        from backend.steps.agent_steps import _question_audit_envelope
        from backend.steps.call import ModelCallError

        reply = '{"question_qc_status": "PASS", "note": "let me reconsider"}\n' \
                '{"reconstructed_answers": [{"number": 1}], "per_question_findings": [], ' \
                '"coverage": {"reviewed_question_ids": [1], "unreviewed": []}, ' \
                '"question_qc_status": "FAIL"}'
        with pytest.raises(ModelCallError):
            _question_audit_envelope(reply, "question audit")

    def test_a_real_review_passes(self):
        from backend.steps.agent_steps import _question_audit_envelope

        reply = json.dumps({
            "reconstructed_answers": [{"number": 1, "answer": "Anna Woods", "turn_index": 3,
                                       "quote": "It's Anna Woods.", "confidence": "high",
                                       "competing_candidates": [],
                                       "derivable_without_recording": False}],
            "per_question_findings": [],
            "coverage": {"reviewed_question_ids": [1], "unreviewed": list(range(2, 11)),
                         "reason": "single-item probe"},
            "question_qc_status": "PASS"})
        assert _question_audit_envelope(reply, "question audit")["question_qc_status"] == "PASS"
