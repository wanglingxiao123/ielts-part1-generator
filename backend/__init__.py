"""IELTS Listening Part 1 generation backend.

Layering (design.md §1), enforced by CI greps:

* ``deterministic/`` runs scripts and pure Python, never imports strands or openai
* ``steps/`` makes one model call each, holds no branch decisions
* ``orchestration/`` holds every branch decision
* ``app.py`` is protocol only
"""
