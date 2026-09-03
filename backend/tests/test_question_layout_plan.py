from backend.orchestration.question_layout_plan import (
    LAYOUTS,
    SPLIT_AFTER_VALUES,
    choose_question_layout_plan,
    normalize_question_layout_plan,
)
from backend.orchestration.slot_store import SlotRecord


def test_all_27_ordered_plans_are_representable():
    seen = {
        (split_after, first, second)
        for split_after in SPLIT_AFTER_VALUES
        for first in LAYOUTS
        for second in LAYOUTS
    }
    assert len(seen) == 27
    assert (5, "form", "form") in seen


def test_choice_is_injectable_and_same_layout_is_legal():
    values = iter((4, "note", "note"))
    plan = choose_question_layout_plan(lambda _options: next(values))
    assert plan == {
        "split_after": 4,
        "first_layout": "note",
        "second_layout": "note",
    }
    assert normalize_question_layout_plan(plan) == plan


def test_slot_round_trip_preserves_plan_across_checkpoint_loads():
    plan = {"split_after": 6, "first_layout": "table", "second_layout": "form"}
    restored = SlotRecord.from_record(
        SlotRecord("batch", "slot-1", "scenario", question_layout_plan=plan).as_record()
    )
    assert restored.question_layout_plan == plan
