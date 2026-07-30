#!/usr/bin/env bash
# Structural gates from implement.md. These are greps rather than comments on purpose: a comment
# asking people not to break the blindness guarantee has never stopped anyone, and a violation
# produces no error at runtime -- only a quietly inflated score.
#
# Run from the repository root: bash backend/scripts/ci_gates.sh
set -uo pipefail

cd "$(dirname "$0")/../.." || exit 1
status=0

fail() { echo "GATE FAILED: $1"; status=1; }

echo "== gate 1: audit step carries no planning identifiers =="
if grep -nE 'blueprint|form_group|question_type_coverage|item_form' backend/steps/audit.py; then
    fail "BLINDNESS_VIOLATION in backend/steps/audit.py"
else
    echo "  ok"
fi

echo "== gate 2: deterministic layer has no model dependency =="
if grep -rnE '^[[:space:]]*(from|import)[[:space:]]+(strands|openai)' backend/deterministic/; then
    fail "deterministic/ imports a model SDK"
else
    echo "  ok"
fi

echo "== gate 3: no hand-written token refresh logic =="
if grep -rniE 'refresh_token|token_cache|is_expired|renew_token' backend/model/; then
    fail "model/ contains token lifecycle logic; Strands mints per call"
else
    echo "  ok"
fi

echo "== gate 4: skill assets stay Python 3.9 parseable =="
for f in skills/ielts-listening-skills/*/scripts/*.py skills/ielts-listening-skills/shared/*.py; do
    python3 -c "import ast,sys; ast.parse(open(sys.argv[1], encoding='utf-8').read())" "$f" \
        || fail "$f is not parseable by the system python3 ($(python3 -V 2>&1))"
done
echo "  ok"

echo "== gate 5: backend sources parse =="
# Prune virtualenvs and caches: a venv created inside backend/ would otherwise drag
# thousands of third-party files into this gate and fail on their syntax, not ours.
find backend \( -name '.venv' -o -name 'venv' -o -name '__pycache__' -o -name 'site-packages' \) -prune \
    -o -name '*.py' -print0 | xargs -0 "${PYTHON:-python3}" -c \
    "import ast,sys; [ast.parse(open(f, encoding='utf-8').read(), f) for f in sys.argv[1:]]" \
    || fail "backend has a syntax error"
echo "  ok"

echo "== gate 6: skill contract regression suite (51 checks) =="
python3 skills/ielts-listening-skills/shared/tests/run_tests.py >/tmp/skill_tests.log 2>&1 \
    && tail -1 /tmp/skill_tests.log || { tail -20 /tmp/skill_tests.log; fail "skill suite"; }

echo "== gate 7: backend unit tests =="
# The backend needs 3.12 and its own dependencies, so `python3` is the wrong interpreter on a dev
# machine where it is the system 3.9 without pytest. Preferring the project venv makes this gate
# actually run locally instead of failing for a reason unrelated to the code under test.
BACKEND_PYTHON="${PYTHON:-}"
if [ -z "$BACKEND_PYTHON" ]; then
    if [ -x .venv-backend/bin/python ]; then
        BACKEND_PYTHON=.venv-backend/bin/python
    else
        BACKEND_PYTHON=python3
    fi
fi
"$BACKEND_PYTHON" -m pytest backend/tests -q || fail "backend unit tests"

echo "== gate 8: every first-party package an image imports is COPYed into it =="
# Twice now a Dockerfile has shipped without a module its code imports at run time, and neither
# time did anything fail until production: the imports sit inside functions, so the build, the
# health check and every local test pass while the feature is dead in the container. First
# backend/Dockerfile without audio_storage (symptom: material_id null), then web/Dockerfile
# without it (symptom: /api/batch-history 502). This gate compares imports against COPYs.
"$BACKEND_PYTHON" - <<'PY' || fail "image is missing a package it imports"
import pathlib, re, sys

FIRST_PARTY = {"audio_storage", "backend", "web", "skills"}
ok = True
for dockerfile, sources in (("backend/Dockerfile", "backend"), ("web/Dockerfile", "web")):
    text = pathlib.Path(dockerfile).read_text(encoding="utf-8")
    copied = set(re.findall(r"^COPY\s+(?:--from=\S+\s+)?(\w+)/", text, re.M))
    imported = set()
    for path in pathlib.Path(sources).rglob("*.py"):
        if "tests" in path.parts or "scripts" in path.parts:
            continue
        body = path.read_text(encoding="utf-8")
        imported |= set(re.findall(r"^\s*(?:from|import)\s+(\w+)", body, re.M))
    missing = sorted((imported & FIRST_PARTY) - copied - {sources})
    if missing:
        ok = False
        print("  %s never COPYs: %s" % (dockerfile, ", ".join(missing)))
    else:
        print("  %s: ok" % dockerfile)
sys.exit(0 if ok else 1)
PY

exit "$status"
