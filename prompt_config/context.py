"""Per-async-task immutable prompt snapshot, never a mutable module-global override."""
from contextlib import contextmanager
from contextvars import ContextVar
from copy import deepcopy

_current = ContextVar("prompt_release", default=None)


def current_values():
    bundle = _current.get()
    if bundle is None:
        from .catalogue import defaults
        return defaults()
    return deepcopy(bundle["values"])


def metadata():
    bundle = _current.get()
    return {k: bundle[k] for k in ("release_id", "digest")} if bundle else {}


@contextmanager
def bind(bundle):
    token = _current.set(deepcopy(bundle))
    try:
        yield
    finally:
        _current.reset(token)
