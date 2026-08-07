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

echo "== gate 1: the audit pool carries no planning assets =="
# Was a grep over backend/steps/audit.py. That module is gone: generation, revision and audit share
# one file now, and generation legitimately handles the blueprint. So the check moved to where the
# boundary actually lives -- the audit skill pool, which must not contain the generator's plan schema
# in any form. The function-level version of this check is a unit test
# (test_guards.py::test_planning_identifiers_absent_from_the_audit_functions).
if grep -rlnE '"(blueprint|form_group|question_type_coverage|item_form)"' skills/audit/; then
    fail "BLINDNESS_VIOLATION: the audit pool contains planning fields"
elif ls skills/audit/*/schemas/blueprint*.json >/dev/null 2>&1; then
    fail "BLINDNESS_VIOLATION: the audit pool ships a blueprint schema"
else
    echo "  ok"
fi

echo "== gate 1b: the feasibility skill lives in its own pool =="
# Gate 1 above cannot cover this. It asks "does the audit pool contain planning fields", and the
# feasibility skill is *supposed* to read a blueprint -- so the only thing worth checking is where it
# lives. That check falls entirely outside gate 1's grep (measured): moving the skill directory into
# skills/generate/ breaks nothing there, and moving it into skills/audit/ would trip gate 1 only
# because of the schema's field names rather than because of the pool boundary itself.
#
# Why the boundary matters in both directions: a pool member is offered to the model by name and
# description, so a feasibility skill in the generate pool lets the generator activate it mid-run and
# approve its own work, and one in the audit pool ends "the audit pool physically contains no plan
# schema" -- one of the three legs holding the blind audit up. An isolation that only holds in a
# design document is not an isolation.
if [ ! -d skills/feasibility ]; then
    fail "the feasibility pool (skills/feasibility/) does not exist"
elif [ "$(find skills/feasibility -mindepth 2 -maxdepth 2 -name SKILL.md | wc -l | tr -d ' ')" != "1" ]; then
    fail "skills/feasibility/ must hold exactly one skill directory"
elif find skills/audit skills/generate -mindepth 1 -maxdepth 1 -type d -name '*feasibility*' \
        | grep . ; then
    fail "POOL_VIOLATION: a feasibility skill directory is in the audit or generate pool"
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
for f in skills/*/*/scripts/*.py skills/shared/*.py; do
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

echo "== gate 6: skill contract regression suite =="
python3 skills/shared/tests/run_tests.py >/tmp/skill_tests.log 2>&1 \
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

echo "== gate 9: every path a Dockerfile or codegen script names still exists =="
# Gate 8 compares at package granularity, so `COPY skills/ielts-listening-skills/` satisfied it by
# starting with `skills/` while naming a directory that had been deleted. Docker fails such a build,
# but nobody builds an image to run the tests -- the reorganisation shipped with a Dockerfile that
# would have produced a container holding no skill pool at all, and an agent with no pool activates
# no skill and returns an unusable reply without anything naming the cause.
#
# The frontend codegen scripts are here for the same reason, from the other direction: those DID
# fail loudly (ENOENT on `npm run verify`), which is how the Dockerfile was found. Pinning them keeps
# that alarm working without needing node installed.
"$BACKEND_PYTHON" - <<'PY' || fail "a referenced path no longer exists"
import pathlib, re, sys

ok = True
# `probe.Dockerfile` is here but NOT in gate 8: that gate compares a whole package's imports against
# the COPY list, and the probe deliberately copies two files out of `backend/` rather than the
# package, so gate 8 would report every module the rest of the backend imports as missing. This gate
# asks a narrower question -- does each named path exist -- which applies to any Dockerfile.
for dockerfile in ("backend/Dockerfile", "backend/probe.Dockerfile", "web/Dockerfile"):
    # `COPY --from=` reads an earlier build stage, not the build context, so its source is a path
    # inside another image and does not exist here.
    for source in re.findall(r"^COPY\s+(\S+)\s+\S+\s*$",
                             pathlib.Path(dockerfile).read_text(encoding="utf-8"), re.M):
        if source.startswith("--"):
            continue
        if not pathlib.Path(source).exists():
            ok = False
            print("  %s COPYs %s, which is not in the build context" % (dockerfile, source))

# Repo-relative literals in the generators, which resolve against the repository root.
for script in sorted(pathlib.Path("frontend/scripts").glob("*.mjs")):
    body = script.read_text(encoding="utf-8")
    for literal in re.findall(r"""["'`](skills/[^"'`\n]+)["'`]""", body):
        if not pathlib.Path(literal).exists():
            ok = False
            print("  %s names %s, which does not exist" % (script, literal))

if ok:
    print("  ok")
sys.exit(0 if ok else 1)
PY

echo "== gate 10: every third-party package the backend imports is installed in the image =="
# Gate 8 compares first-party packages, so it cannot see this: `strands_tools` was imported by
# agents.py and sandboxed_metrics.py while appearing in neither the Dockerfile nor pyproject.toml. It
# is not a dependency of `strands-agents`, so the image would have built, started, and answered /ping
# -- then failed every material with `unhandled_error`, because both imports are inside functions and
# `batch.py` catches everything. Nothing would have named the missing package.
"$BACKEND_PYTHON" - <<'PY' || fail "the image does not install a package the backend imports"
import ast, pathlib, re, sys

# import name -> the distribution that provides it, when the two differ.
DISTRIBUTION = {"strands": "strands-agents", "strands_tools": "strands-agents-tools",
                "yaml": "pyyaml", "bedrock_agentcore": "bedrock-agentcore"}
# First-party, including modules loaded off a runtime sys.path rather than as a package:
# `cross_check` lives in skills/shared/ and is imported after that directory is appended, so it looks
# like a third-party top-level import here. Gate 8 covers whether `skills/` reaches the image.
# `cross_check_questions` is its question-stage sibling in the same directory, imported the same way by
# `deterministic/question_crosscheck.py` -- which is the module that exists specifically so that
# comparison is imported rather than reimplemented, so this entry is the price of that discipline.
#
# `question_feasibility_preflight` and `validate_part1` are the same case one pool over: both live in
# skills/generate/generate-listening-part1/scripts/, and `deterministic/feasibility.py` imports the
# first after inserting that directory (the aggregator then imports the second at its own module
# scope). Without them listed here this gate demands that pip install two of our own skill scripts.
#
# `validate_questions_part1` is that case a third time: it lives in
# skills/generate/generate-questions-part1/scripts/ and `deterministic/question_metrics.py` imports it
# after inserting that directory, so the blank-position classifier is shared with the validator
# instead of reimplemented. Listing it here is the whole fix -- gate 8 is what checks that the pool
# reaches the image, and it does.
FIRST_PARTY = {"audio_storage", "backend", "web", "skills", "config", "cross_check",
               "cross_check_questions", "question_feasibility_preflight", "validate_part1",
               "validate_questions_part1"}

dockerfile = pathlib.Path("backend/Dockerfile").read_text(encoding="utf-8")
pyproject = pathlib.Path("backend/pyproject.toml").read_text(encoding="utf-8")
# `"name[extra]>=x"` in the pip line, and the same in pyproject's dependency list.
installed = {m.lower() for m in re.findall(r'"([A-Za-z0-9_.-]+)(?:\[[^\]]*\])?[><=]', dockerfile)}
declared = {m.lower() for m in re.findall(r'"([A-Za-z0-9_.-]+)(?:\[[^\]]*\])?[><=]', pyproject)}

imported = set()
for path in pathlib.Path("backend").rglob("*.py"):
    if "tests" in path.parts or "scripts" in path.parts or "__pycache__" in path.parts:
        continue
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
        if isinstance(node, ast.Import):
            imported |= {a.name.split(".")[0] for a in node.names}
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            imported.add(node.module.split(".")[0])

ok = True
for name in sorted(imported):
    if name in FIRST_PARTY or name in sys.stdlib_module_names:
        continue
    dist = DISTRIBUTION.get(name, name).lower()
    for label, present in (("backend/Dockerfile", installed), ("backend/pyproject.toml", declared)):
        if dist not in present:
            ok = False
            print("  %s never installs %s (imported as %s)" % (label, dist, name))
sys.exit(0 if ok else 1)
PY
[ "$status" = 0 ] && echo "  ok"

echo "== gate 11: no IAM policy is written only when its role is created =="
# This trap has now been found twice in provision.sh: a `put-role-policy` inside the `else` of
# `if aws iam get-role ...` means an account whose role already exists never receives a permission a
# later version added. Provision reports success and the failure arrives later as AccessDenied, which
# reads as a deployment problem rather than a provisioning one. `put-role-policy` overwrites by name,
# so there is never a reason to guard it.
"$BACKEND_PYTHON" - <<'PY' || fail "an IAM policy is written only on role creation"
import pathlib, re, sys

ok = True
for script in sorted(pathlib.Path("deploy").glob("*.sh")):
    depth, guarded = 0, []
    for number, line in enumerate(script.read_text(encoding="utf-8").splitlines(), 1):
        stripped = line.strip()
        if re.match(r"^(if|for|while)\b", stripped):
            depth += 1
        elif stripped in ("fi", "done"):
            depth = max(0, depth - 1)
        # A policy call at any nesting level is suspect; at top level it always runs.
        elif depth > 0 and re.search(r"aws iam (put-role-policy|attach-role-policy)", stripped):
            guarded.append(number)
    if guarded:
        ok = False
        print("  %s writes an IAM policy inside a conditional at line(s) %s"
              % (script, ", ".join(str(n) for n in guarded)))
sys.exit(0 if ok else 1)
PY
[ "$status" = 0 ] && echo "  ok"

echo "== gate 12: no deploy script defaults its image tag =="
# A defaulted tag is how rollback was lost. Both ECR repositories are IMMUTABLE now, so a reused tag
# is rejected at push -- but `deploy/runtime.sh` does not push, it switches live traffic, and a
# default there meant an argument-less run silently repointed production at whatever `dev` was.
# Requiring the tag also makes the tag a decision rather than an accident.
"$BACKEND_PYTHON" - <<'PY' || fail "a deploy script defaults its image tag"
import pathlib, re, sys

SCRIPTS = ["deploy/runtime.sh", "deploy/service.sh", "backend/scripts/deploy.sh"]
ok = True
for name in SCRIPTS:
    path = pathlib.Path(name)
    if not path.is_file():
        continue
    for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        # `TAG="${1:-dev}"` defaults; `TAG="${1:?...}"` requires. Only the first is a problem.
        match = re.search(r'TAG="\$\{(\d+):-', line)
        if match:
            ok = False
            print("  %s:%d defaults the image tag: %s" % (name, number, line.strip()))
sys.exit(0 if ok else 1)
PY
[ "$status" = 0 ] && echo "  ok"

exit "$status"
