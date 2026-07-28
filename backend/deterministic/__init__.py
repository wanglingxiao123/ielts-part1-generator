"""Deterministic layer: runs scripts and pure-Python checks, never calls a model.

Enforced by a CI grep (implement.md phase 1): no module in this package may import strands or
openai. That keeps the layer unit-testable offline, which is the only way to build confidence
in the Loop without spending tokens.
"""
