"""Adversarial tests for the blindness boundary.

The blind information map is the strongest quality signal in the system, and it is worth exactly
nothing if the auditor can see the generator's plan. What makes that dangerous is the failure mode:
no error is raised, the score simply comes out too high, and the artifact looks entirely normal.
Nobody finds it by reading the output.

So these tests try to break the isolation rather than confirm it. Each one asks "if someone made the
obvious mistake here, would anything fail?" -- and the ones that check a sandbox actively attempt
traversal, absolute paths, symlinks and writes rather than trusting the class to behave.

The boundary has three independent parts, and each is tested on its own:

1. **The audit pool holds nothing planning-related.** Physical: the files are not there.
2. **`file_read` cannot leave the pool.** Enforced by ``ReadOnlySkillSandbox``, because the default
   sandbox (``NotASandboxLocalEnvironment``) resolves every path against the process cwd and would
   happily read the generator's schema or ``/etc/passwd``.
3. **The auditor has no way to run a command.** Not by configuration but by absence:
   ``strands_tools.shell`` bypasses ``agent.sandbox`` entirely -- measured -- so the only safe
   arrangement is not to give it to the audit agent at all.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from backend import agents as agents_module

REPO = Path(__file__).resolve().parents[2]

# Fields that exist only in blueprint.schema.json. Present in neither material.schema.json nor
# audit.schema.json, so a hit inside the audit pool is a real leak rather than a coincidence.
PLANNING_FIELDS = (
    "form_group",
    "question_type_coverage",
    "item_form",
    "indirect_confirmation",
    "narration_mode",
    "split_after",
)


def _audit_pool() -> Path:
    return agents_module.pool_dir(agents_module.AUDIT_POOL)


def _generate_pool() -> Path:
    return agents_module.pool_dir(agents_module.GENERATE_POOL)


class TestAuditPoolHoldsNoPlan:
    """Part 1: physical absence. The cheapest boundary to keep, and the easiest to erode."""

    def test_no_file_in_the_audit_pool_is_named_after_the_blueprint(self):
        offenders = [str(p.relative_to(REPO)) for p in _audit_pool().rglob("*")
                     if p.is_file() and "blueprint" in p.name.lower()]
        assert offenders == []

    def test_no_file_in_the_audit_pool_contains_a_planning_field(self):
        """Catches the copy that a filename check would miss.

        Someone consolidating schemas could paste the blueprint's properties into the audit schema
        under a different filename; the name check would pass and the auditor would be reading the
        plan's shape.
        """
        hits = []
        for path in sorted(_audit_pool().rglob("*")):
            if not path.is_file():
                continue
            text = path.read_text(encoding="utf-8", errors="ignore")
            for field in PLANNING_FIELDS:
                if '"%s"' % field in text:
                    hits.append((str(path.relative_to(REPO)), field))
        assert hits == []

    def test_the_generate_pool_does_carry_the_plan(self):
        """The control. Without it, the two tests above would also pass on an empty directory."""
        schema = _generate_pool() / "generate-listening-part1" / "schemas" / "blueprint.schema.json"
        assert schema.is_file()
        text = schema.read_text(encoding="utf-8")
        assert all('"%s"' % field in text for field in ("form_group", "item_form"))


class TestSandboxCannotLeaveThePool:
    """Part 2: `file_read` is sandbox-routed, so this boundary is enforceable and enforced."""

    @pytest.fixture
    def sandbox(self):
        return agents_module.ReadOnlySkillSandbox(_audit_pool())

    def test_reads_its_own_rubric(self):
        """The control again: a sandbox that refuses everything would pass every test below."""
        sandbox = agents_module.ReadOnlySkillSandbox(_audit_pool())
        text = asyncio.run(sandbox.read_text(
            "audit-listening-part1/references/audit-rubric.md"))
        assert "Specification Compliance Review" in text

    @pytest.mark.parametrize("path", [
        "../generate/generate-listening-part1/schemas/blueprint.schema.json",
        "../generate/generate-listening-part1/references/specification.md",
        "../generate/generate-listening-part1/scripts/validate_part1.py",
        "audit-listening-part1/../../generate/generate-listening-part1/schemas/blueprint.schema.json",
        "./../generate",
        "../../etc/passwd",
        "/etc/passwd",
        "/etc/hosts",
    ])
    def test_traversal_and_absolute_paths_are_refused(self, sandbox, path):
        with pytest.raises(PermissionError):
            asyncio.run(sandbox.read_text(path))

    def test_a_symlink_out_of_the_pool_is_refused(self, tmp_path):
        """`resolve()` follows symlinks, which is why the check is on the resolved path.

        A check on the *given* path would pass here and read the target anyway -- and a symlink is
        exactly how someone would work around a path restriction without meaning to attack it.
        """
        pool = tmp_path / "pool"
        (pool / "skill-a").mkdir(parents=True)
        (pool / "skill-a" / "SKILL.md").write_text("---\nname: a\ndescription: d\n---\n", encoding="utf-8")
        secret = tmp_path / "outside.txt"
        secret.write_text("the plan", encoding="utf-8")
        link = pool / "skill-a" / "link.txt"
        link.symlink_to(secret)

        sandbox = agents_module.ReadOnlySkillSandbox(pool)
        with pytest.raises(PermissionError):
            asyncio.run(sandbox.read_text("skill-a/link.txt"))

    def test_listing_cannot_escape_either(self, sandbox):
        """A directory listing of the generate pool is a hint the auditor should not have."""
        with pytest.raises(PermissionError):
            asyncio.run(sandbox.list_files("../generate"))

    def test_every_write_path_is_refused(self, sandbox):
        """The pool holds the validator the generator runs.

        An agent able to edit it could make the validator agree with anything -- and unlike a read
        leak, that failure would persist for every later material in the container.
        """
        with pytest.raises(PermissionError):
            asyncio.run(sandbox.write_text("x.txt", "y"))
        with pytest.raises(PermissionError):
            asyncio.run(sandbox.write_file("x.bin", b"y"))
        with pytest.raises(PermissionError):
            asyncio.run(sandbox.remove_file(
                "audit-listening-part1/references/audit-rubric.md"))

    def test_the_sandbox_executes_nothing(self, sandbox):
        async def run_command():
            async for _ in sandbox.execute_streaming("cat ../generate/*/schemas/*.json"):
                pass

        async def run_code():
            async for _ in sandbox.execute_code_streaming("print(1)", "python3"):
                pass

        with pytest.raises(PermissionError):
            asyncio.run(run_command())
        with pytest.raises(PermissionError):
            asyncio.run(run_code())

    def test_it_contributes_no_tools_of_its_own(self, sandbox):
        """`Sandbox.get_tools()` can hand the agent extra capabilities; this one hands it none."""
        assert sandbox.get_tools() == []

    def test_the_default_sandbox_would_not_have_stopped_any_of_this(self):
        """Why this class exists, asserted rather than claimed in a comment.

        If a future SDK ships an isolating default, this test fails and the custom sandbox can be
        reconsidered. Until then it documents that the default is named
        `NotASandboxLocalEnvironment` for a reason.
        """
        from strands import Agent

        default = Agent(system_prompt="t").sandbox
        assert type(default).__name__ == "NotASandboxLocalEnvironment"
        # It reads a path outside any pool without complaint.
        text = asyncio.run(default.read_text(str(
            _generate_pool() / "generate-listening-part1" / "schemas" / "blueprint.schema.json")))
        assert "form_group" in text


class TestAuditAgentCapabilities:
    """Part 3: what the audit agent is given, and what it is deliberately not."""

    def test_the_audit_agent_has_no_shell(self):
        """The one non-negotiable.

        `strands_tools.shell` calls `pty.fork()` and its signature carries no `agent`, so it never
        consults `agent.sandbox` -- measured. A shell-equipped auditor can `cat` the generator's
        blueprint schema no matter what sandbox is configured, so the only safe arrangement is
        absence.
        """
        agent = agents_module.build_audit_agent()
        assert "shell" not in agent.tool_names
        assert sorted(agent.tool_names) == ["file_read", "skills"]

    def test_the_audit_agent_is_sandboxed_to_the_audit_pool(self):
        agent = agents_module.build_audit_agent()
        assert isinstance(agent.sandbox, agents_module.ReadOnlySkillSandbox)
        assert agent.sandbox.root == _audit_pool().resolve()

    def test_the_generate_agent_does_get_shell(self):
        """The asymmetry is deliberate, so it is asserted rather than left to be noticed.

        The generator running its own validator on its own draft learns nothing it should not know,
        and validation is a report rather than a gate -- Python re-runs it on the delivered artifact
        regardless, so a false "all clean" gains nothing.
        """
        agent = agents_module.build_generate_agent()
        assert sorted(agent.tool_names) == ["file_read", "shell", "skills"]
        assert agent.sandbox.root == _generate_pool().resolve()

    def test_shell_is_configured_not_to_wait_for_a_human(self):
        """Not a blindness property, but a hang that looks like a model timeout.

        Building the generate agent must leave the two flags set; without them the tool blocks on
        stdin and, in a Runtime with no tty, the invocation dies at the platform's 15-minute wall
        with nothing naming the cause.
        """
        import os

        agents_module.build_generate_agent()
        assert os.environ.get("STRANDS_NON_INTERACTIVE") == "true"
        assert os.environ.get("BYPASS_TOOL_CONSENT") == "true"

    def test_neither_agent_can_activate_the_other_pool_s_skill(self):
        """Registering the wrong pool is the one real blindness risk on the plugin path."""
        from strands.vended_plugins.skills import AgentSkills, Skill

        audit_skills = Skill.from_directory(str(_audit_pool()))
        plugin = AgentSkills(skills=audit_skills)
        agent = agents_module.build_audit_agent()

        response = asyncio.run(plugin.skills(
            skill_name="generate-listening-part1",
            tool_context=type("Ctx", (), {"agent": agent})(),
        ))
        assert "not found" in response

    def test_the_two_agents_share_no_state(self):
        """What makes the re-audit memoryless: there is no session to inherit from."""
        first = agents_module.build_audit_agent()
        second = agents_module.build_audit_agent()
        assert first is not second
        assert first.messages == []
        assert second.messages == []
        assert first.state is not second.state


class TestAuditMessageCarriesNoPlan:
    """The information-flow half: what Python actually sends.

    The sandbox stops the auditor fetching the plan. This stops Python handing it over, which is the
    likelier mistake -- adding a field to a payload is a one-line change that no sandbox sees.
    """

    def test_the_metrics_uploader_takes_no_blueprint_parameter(self):
        """Structural: there is no argument for planning data to arrive through."""
        import inspect

        from backend.sandboxed_metrics import SandboxedMetrics

        run_params = list(inspect.signature(SandboxedMetrics.run).parameters)
        assert run_params == ["self", "material"]

        init_params = list(inspect.signature(SandboxedMetrics.__init__).parameters)
        assert init_params == ["self", "material_id", "script_path"]

    def test_only_the_script_and_the_material_are_uploaded(self):
        """The blindness boundary for the remote environment, which starts empty.

        Asserted over the AST rather than the file text. A text search matches the module's own
        docstring explaining that the blueprint is never uploaded, which is the opposite of a
        violation -- a check a comment can fail is as useless as one a comment can satisfy.

        What this pins down: every ``FileContent(path=...)`` in the module names one of the two
        permitted files. A third upload would have to add a literal here, and it would fail.
        """
        import ast

        from backend import sandboxed_metrics

        tree = ast.parse(Path(sandboxed_metrics.__file__).read_text(encoding="utf-8"))
        uploaded = []
        for node in ast.walk(tree):
            if not (isinstance(node, ast.Call) and getattr(node.func, "id", "") == "FileContent"):
                continue
            for keyword in node.keywords:
                if keyword.arg == "path":
                    # Either a literal or a module constant; both resolve to a known name.
                    if isinstance(keyword.value, ast.Constant):
                        uploaded.append(keyword.value.value)
                    elif isinstance(keyword.value, ast.Name):
                        uploaded.append(getattr(sandboxed_metrics, keyword.value.id, "?"))

        assert uploaded, "no FileContent uploads found; has the module been rewritten?"
        permitted = {sandboxed_metrics.SCRIPT_NAME, sandboxed_metrics.MATERIAL_NAME}
        assert set(uploaded) <= permitted, set(uploaded) - permitted

    def test_a_material_dict_carries_no_planning_fields(self, tmp_path):
        """The material schema and the blueprint schema are disjoint, so serialising a real
        material cannot leak the plan. Asserted against the shipped fixture rather than a hand-built
        dict, so a schema change that merged them would fail here."""
        fixture = (REPO / "skills" / "generate" / "generate-listening-part1"
                   / "schemas" / "material.schema.json")
        text = fixture.read_text(encoding="utf-8")
        for field in PLANNING_FIELDS:
            assert '"%s"' % field not in text, field
