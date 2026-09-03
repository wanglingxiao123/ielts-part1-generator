"""Question-layout plans shared by generation and durable slot orchestration."""

from __future__ import annotations

import random
from typing import Any, Callable, Dict, Mapping, Optional

LAYOUTS = ("form", "note", "table")
SPLIT_AFTER_VALUES = (4, 5, 6)


def choose_question_layout_plan(
    choice: Optional[Callable[[tuple], Any]] = None,
) -> Dict[str, Any]:
    """Choose uniformly from the 27 ordered split/layout plans."""
    pick = choice or random.SystemRandom().choice
    return {
        "split_after": int(pick(SPLIT_AFTER_VALUES)),
        "first_layout": str(pick(LAYOUTS)),
        "second_layout": str(pick(LAYOUTS)),
    }


def normalize_question_layout_plan(plan: Mapping[str, Any]) -> Dict[str, Any]:
    """Return the canonical persisted shape or raise ``ValueError``."""
    split_after = plan.get("split_after")
    first_layout = plan.get("first_layout")
    second_layout = plan.get("second_layout")
    if split_after not in SPLIT_AFTER_VALUES:
        raise ValueError("question layout split_after must be one of 4, 5, 6")
    if first_layout not in LAYOUTS or second_layout not in LAYOUTS:
        raise ValueError("question layouts must be form, note, or table")
    return {
        "split_after": int(split_after),
        "first_layout": str(first_layout),
        "second_layout": str(second_layout),
    }
