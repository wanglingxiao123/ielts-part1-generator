"""Accept-set rules, one case per rule, on hand-built questions.

Each test names the rule from qti_export/README.md §3 it pins. The four rules added after the first
export review (price targets that carry a currency, word-form durations, the derived distinctive
component R8, British/American spelling R9) each get a positive case and the boundary that keeps
them from over-accepting.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from qti_export import accept_sets  # noqa: E402
from qti_export import questions_input as qin  # noqa: E402

TWO_WORDS = qin.parse_word_limit("NO MORE THAN TWO WORDS AND/OR A NUMBER")
THREE_WORDS = qin.parse_word_limit("NO MORE THAN THREE WORDS AND/OR A NUMBER")


def build(target, category, *, form="phrase", prefix="", suffix="", evidence="",
          distractors=(), visible="", limit=TWO_WORDS):
    spec = accept_sets.build(
        {"number": 1, "answer_category": category, "response_form": form},
        {"canonical": target, "target": target, "alternatives": []},
        limit,
        evidence=evidence, prefix=prefix, suffix=suffix,
        distractors=list(distractors), visible_text=visible,
    )
    return spec


def lower(values):
    return {v.casefold() for v in values}


# ── price: the target itself may carry the currency ───────────────────────────


def test_price_with_symbol_in_target_expands_when_the_paper_prints_none():
    spec = build("£128", "price", form="numeric", prefix="Cost: ", suffix="")
    assert lower(spec.accept) == {"£128", "128", "128 pounds", "£128.00"}
    assert spec.review == []


def test_price_with_currency_word_in_target():
    spec = build("10 pounds", "price", form="numeric", prefix="Deposit: ")
    assert lower(spec.accept) == {"10 pounds", "10", "£10", "£10.00"}


def test_price_with_decimals_does_not_add_a_second_decimal_form():
    spec = build("£12.50", "price", form="numeric", prefix="Fee: ")
    assert lower(spec.accept) == {"£12.50", "12.50", "12.50 pounds"}


def test_price_thousands_separator_gets_both_spellings():
    spec = build("£1,200", "price", form="numeric", prefix="Rent: ")
    assert {"£1,200", "1,200", "1200", "1,200 pounds", "£1,200.00"} <= lower(spec.accept)


def test_price_word_form_is_still_dropped_when_the_paper_prints_the_symbol():
    """R6 keeps applying on top of the new parsing: `£ ___ per night` + `128 pounds` is redundant."""
    spec = build("128", "price", form="numeric", prefix="£ ", suffix=" per night", evidence="£128 per night")
    assert "128 pounds" in spec.reject
    assert "128 pounds" not in spec.accept
    assert "£128" in spec.accept


def test_a_price_distractor_with_a_symbol_is_rejected_in_every_form():
    spec = build("£128", "price", form="numeric", prefix="Cost: ", distractors=["£132"])
    assert {"£132", "132", "132 pounds", "£132.00"} <= lower(spec.reject)
    assert not lower(spec.accept) & lower(spec.reject)


# ── duration / quantity: word-form leading number ─────────────────────────────


def test_word_form_duration_expands_to_digits_and_back():
    spec = build("three nights", "duration", prefix="Stay: ", suffix=" nights")
    # The paper prints the unit, so bare forms are fine (R7 lets them through).
    assert lower(spec.accept) == {"three nights", "3 nights", "3", "three"}
    assert spec.review == []


def test_word_form_duration_bare_forms_are_dropped_without_a_unit_on_the_paper():
    spec = build("three nights", "duration", prefix="The reservation covers ", suffix=" in total")
    assert lower(spec.accept) == {"three nights", "3 nights"}
    assert {"3", "three"} <= lower(spec.reject)


def test_multiword_unit_and_possessive():
    spec = build("five working days", "duration", prefix="Notice: ", limit=THREE_WORDS)
    assert {"five working days", "5 working days"} <= lower(spec.accept)
    spec = build("six weeks' rent", "quantity", prefix="Deposit: ", limit=THREE_WORDS)
    assert {"six weeks' rent", "6 weeks' rent"} <= lower(spec.accept)


def test_bare_number_and_bare_word_are_interchangeable():
    """`for ___ nights` prints the unit, so the answer is `3`; a candidate writing `three` is right too."""
    spec = build("3", "quantity", form="numeric", prefix="for ", suffix=" nights")
    assert lower(spec.accept) == {"3", "three"}
    spec = build("three", "quantity", prefix="for ", suffix=" nights")
    assert lower(spec.accept) == {"three", "3"}


def test_numbers_above_twenty_get_hyphenated_words():
    spec = build("45 minutes", "duration", prefix="Lasts ", suffix=" minutes")
    assert {"45 minutes", "forty-five minutes", "45", "forty-five"} <= lower(spec.accept)
    assert spec.review == []
    assert qin.count_tokens("forty-five minutes") == (2, 0)


def test_non_quantity_shapes_still_fall_back_to_the_target():
    spec = build("fortnightly", "duration", prefix="Paid: ")
    assert spec.accept == ["fortnightly"]
    assert any("不匹配" in r for r in spec.review)


# ── R8: distinctive component derived from a competing candidate ──────────────


def test_shared_head_noun_is_rejected_and_the_modifier_is_not_accepted_off_paper():
    """`A ___ was selected.` — `room` is not printed, so `double` alone does not make a sentence."""
    spec = build("double room", "preference", prefix="A ", suffix=" was selected.",
                 distractors=["twin room"], visible="RESERVATION DETAILS Accommodation A was selected.")
    assert "room" in spec.reject
    assert "twin room" in spec.reject
    assert "double" not in lower(spec.accept)
    assert any("R8" in r and "未接受" in r for r in spec.review)
    assert not any("未在 DISTINCTIVE 登记" in r for r in spec.review)


def test_modifier_is_accepted_when_the_head_noun_is_printed_on_the_paper():
    spec = build("double room", "preference", prefix="Room type: ", suffix="",
                 distractors=["twin room"], visible="Room type: Nightly charge")
    assert {"double room", "double"} <= lower(spec.accept)
    assert "room" in spec.reject
    assert any("R8" in r and "接受 'double'" in r for r in spec.review)


def test_head_noun_check_ignores_signposts_and_uses_only_paper_text():
    """The upstream validator counts signposts as visible text; the paper does not print them."""
    spec = build("double room", "preference", prefix="A ", suffix=" was selected.",
                 distractors=["twin room"], visible="A was selected.")
    assert "double" not in lower(spec.accept)


def test_no_shared_head_means_no_r8_and_the_old_review_note_stays():
    spec = build("quiet floor", "preference", prefix="Prefers a ", distractors=["restaurant"])
    assert lower(spec.accept) == {"quiet floor"}
    assert any("未在 DISTINCTIVE 登记" in r for r in spec.review)
    assert not any("R8" in r for r in spec.review)


def test_hand_registered_distinctive_still_wins_over_derivation():
    spec = build("twin room", "preference", prefix="Room type: ", distractors=["double room"],
                 visible="Room type:")
    assert "twin" in spec.accept and "room" in spec.reject
    assert not any("R8" in r for r in spec.review)


# ── R9: British / American spelling ───────────────────────────────────────────


def test_spelling_variant_is_generated_for_each_word_and_keeps_case():
    spec = build("provisional licence", "document", prefix="Bring your ")
    assert "provisional license" in spec.accept
    spec = build("Sports Centre", "location", prefix="Meet at the ")
    assert "Sports Center" in spec.accept


def test_spelling_variant_handles_plurals_and_both_directions():
    spec = build("two licenses", "quantity", prefix="Hold: ")
    assert "two licences" in spec.accept
    assert "2 licences" in spec.accept


def test_spelling_variants_do_not_change_word_count_so_r5_is_unaffected():
    spec = build("theatre ticket", "requirement", prefix="Bring a ", limit=TWO_WORDS)
    assert "theater ticket" in spec.accept
    assert all(qin.count_tokens(v)[0] <= 2 for v in spec.accept)


def test_words_without_a_pair_get_no_variant():
    spec = build("Rose Garden", "facility", prefix="Guests can use the ")
    assert spec.accept == ["Rose Garden"]


# ── invariants ────────────────────────────────────────────────────────────────


@pytest.mark.parametrize("target,category,kw", [
    ("£128", "price", {"prefix": "Cost: ", "distractors": ["£132", "128"]}),
    ("three nights", "duration", {"prefix": "for ", "suffix": " nights", "distractors": ["3"]}),
])
def test_a_distractor_that_collides_with_the_accept_set_is_escalated_not_dropped(target, category, kw):
    spec = build(target, category, form="numeric", **kw)
    assert not lower(spec.accept) & lower(spec.reject)
    assert any("与接受集重合" in r for r in spec.review)
