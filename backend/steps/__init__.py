"""Model-calling steps. Each is "one call + parse output" with no branching.

Every branch in this system lives in orchestration/loop.py. A step that decided anything for
itself would move control flow into the model's reach, which design.md §3 rules out.
"""
