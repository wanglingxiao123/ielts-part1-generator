"""Adversarial tests for the blindness boundary.

The blind information map is the strongest quality signal in the system, and it is worth exactly
nothing if the auditor can see the generator's plan. What makes that dangerous is the failure mode:
no error is raised, the score simply comes out too high, and the artifact looks entirely normal.
Nobody finds it by reading the output.

So these tests try to break the isolation rather than confirm it. Each one asks "if someone made the
obvious mistake here, would anything fail?" -- and the ones that check a sandbox actively attempt
traversal, absolute paths, symlinks and writes rather than trusting the class to behave.

The boundary has four independent parts, and each is tested on its own:

1. **The audit pool holds nothing planning-related.** Physical: the files are not there.
2. **The generator's scratch files are deleted before the audit call.** This is the one that protects
   the *answers* rather than the *shape*: the skill has the generator write its plan to a file so it
   can run its own validator, and that file says which turn carries which target.
3. **The auditor has no way to run a command.** Not by configuration but by absence:
   ``strands_tools.shell`` bypasses ``agent.sandbox`` entirely -- measured -- so the only safe
   arrangement is not to give it to the audit agent at all.
4. **Sandbox-routed access cannot leave the pool.** ``ReadOnlySkillSandbox`` bounds what the SDK
   itself reads (the skills plugin's resource listing).

A correction worth recording, because the tests below were written on the wrong assumption and passed
anyway: ``file_read`` is **not** sandbox-routed. Its signature is ``(tool, **kwargs)`` and its source
names no sandbox, so an absolute path reaches the filesystem regardless of ``agent.sandbox``. The
sandbox tests were real -- they test the class -- but the class was never in that path, and nothing
here noticed for as long as no test called ``file_read`` through an agent. There is one now.
"""

from __future__ import annotations

import asyncio
import json
import os
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


class TestScratchFilesAreGone:
    """Part 2: the plan file the generator wrote, deleted before the auditor exists.

    The highest-value leak in the system and the only one that carries answers. A plan *schema* tells
    an auditor that a field called ``form_group`` exists; a plan *file* tells it that turn 8 replaces
    $45 with $39.
    """

    def test_the_workspace_is_private_per_call(self):
        """Two generations must not share a path. The shared-name era is what caused the damage:
        slot A read back slot B's plan, A's cleanup deleted B's files, and B's write killed A."""
        from backend.steps.agent_steps import GenerationWorkspace

        a, b = GenerationWorkspace(), GenerationWorkspace()
        try:
            assert a.path != b.path
            assert a.path.is_dir() and b.path.is_dir()
            # The agent is told where to work, or it falls back to the skill's own suggestion.
            assert str(a.path) in a.instructions()
        finally:
            a.remove()
            b.remove()

    def test_removing_the_workspace_takes_the_whole_tree(self):
        """Deleting the directory rather than reconstructing filenames.

        The earlier version parsed the agent's shell commands with a regex to find what to delete,
        which missed any path built by interpolation (``D=/tmp; cat > $D/blueprint.json``). A tree
        removal cannot miss a file, whatever the agent named it or however deep it put it.
        """
        from backend.steps.agent_steps import GenerationWorkspace

        workspace = GenerationWorkspace()
        (workspace.path / "nested").mkdir()
        plan = workspace.path / "nested" / "whatever-i-called-it.json"
        plan.write_text(json.dumps({"items": [{"form_group": "x"}]}), encoding="utf-8")

        removed = workspace.remove()

        assert str(plan) in removed
        assert not plan.exists()
        assert not workspace.path.exists()

    @pytest.mark.asyncio
    async def test_the_workspace_is_removed_even_when_the_call_fails(self):
        """A failed generation leaves scratch files too, and the next step is a retry or an audit."""
        import backend.steps.agent_steps as steps
        from backend.steps.call import ModelCallError

        seen = {}

        class Boom:
            messages = []

            async def invoke_async(self, message):
                seen["dir"] = [line for line in message.splitlines() if "ielts-gen-" in line]
                raise RuntimeError("boom")

        original = steps.build_generate_agent
        steps.build_generate_agent = lambda: Boom()
        try:
            class S:
                id = "x"; category = "c"; title_zh = "t"; prompt_hint = "h"
            with pytest.raises(ModelCallError):
                await steps.generate(S())
        finally:
            steps.build_generate_agent = original

        assert seen["dir"], "the request never named a workspace"
        leaked = [Path(part.strip("` ")) for line in seen["dir"] for part in line.split()
                  if "ielts-gen-" in part]
        assert leaked, leaked
        assert not any(path.exists() for path in leaked), leaked

    def test_a_plan_left_outside_the_workspace_is_swept_not_raised(self):
        """The backstop, and it deletes rather than raising. Measured why: the cutoff is the
        *process* start and Runtime instances are long-lived, so one survivor file used to fail every
        later material in that instance -- a real CLI batch spent 9 generation attempts and 6 refill
        rounds to produce nothing. A guard whose false positive costs the whole batch cannot be
        strict; removing the file keeps the property that actually matters."""
        from backend.deterministic.guards import assert_no_plan_on_disk, sweep_plan_files_on_disk

        plan = Path("/tmp/leftover_plan.json")
        plan.write_text(json.dumps({"items": [{"item_form": "form", "form_group": "a"}]}),
                        encoding="utf-8")
        try:
            swept = sweep_plan_files_on_disk()
            assert str(plan) in swept
            assert not plan.exists()
            assert_no_plan_on_disk()
        finally:
            plan.unlink(missing_ok=True)

    def test_a_plan_in_a_subdirectory_is_found(self):
        """`file_read` takes any absolute path, so depth is no protection. A non-recursive glob left
        ``/tmp/x/blueprint.json`` invisible to the guard and readable by the agent."""
        from backend.deterministic.guards import plan_files_on_disk

        nested = Path("/tmp/ielts-guard-probe/deeper")
        nested.mkdir(parents=True, exist_ok=True)
        plan = nested / "plan.json"
        plan.write_text(json.dumps({"items": [{"form_group": "x"}]}), encoding="utf-8")
        try:
            assert str(plan) in plan_files_on_disk(since=0)
        finally:
            import shutil
            shutil.rmtree("/tmp/ielts-guard-probe", ignore_errors=True)

    def test_the_guard_is_content_based_not_name_based(self):
        """The generator picks its own filenames. A plan in `draft2.json` is just as readable."""
        from backend.deterministic.guards import plan_files_on_disk

        plan = Path("/tmp/draft2.json")
        plan.write_text(json.dumps({"items": [{"form_group": "x"}]}), encoding="utf-8")
        innocent = Path("/tmp/innocent_material.json")
        innocent.write_text(json.dumps({"listening_material_parts": []}), encoding="utf-8")
        try:
            found = plan_files_on_disk(since=0)
            assert str(plan) in found
            assert str(innocent) not in found
        finally:
            plan.unlink(missing_ok=True)
            innocent.unlink(missing_ok=True)

    def test_files_predating_this_process_are_ignored(self):
        """Otherwise the guard is unusable, which is worse than absent.

        Measured on a developer machine: /tmp held four blueprint files from runs days earlier. A
        guard that fires on those fires on every single run, gets read as noise, and is switched off
        -- taking the real check with it. A file older than this process is also not this material's
        answer key.
        """
        from backend.deterministic.guards import plan_files_on_disk

        old = Path("/tmp/ancient_plan.json")
        old.write_text(json.dumps({"items": [{"form_group": "x"}]}), encoding="utf-8")
        os.utime(old, (1_600_000_000, 1_600_000_000))
        try:
            assert str(old) not in plan_files_on_disk()
            assert str(old) in plan_files_on_disk(since=0)
        finally:
            old.unlink(missing_ok=True)

    def test_a_clean_disk_passes(self):
        """The control. A guard that always raised would pass every test above."""
        from backend.deterministic.guards import assert_no_plan_on_disk

        assert_no_plan_on_disk()


class TestSandboxCannotLeaveThePool:
    """Part 4: what `ReadOnlySkillSandbox` actually covers -- the SDK's own filesystem access."""

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

    def test_a_permitted_listing_returns_usable_entries(self, sandbox):
        """The success path, which the refusal tests above cannot reach.

        Added after a real failure that no test saw. ``FileInfo`` takes ``name``, not ``path``; the
        first version passed ``path=`` and the constructor raised ``TypeError``. The skills plugin
        catches that and logs a warning, so activation still succeeded -- just without its "Available
        resources" listing, meaning the model was never told which files the skill has. Every refusal
        test passed throughout, because they all raise before a ``FileInfo`` is ever built.

        ``name`` must also be bare rather than pool-relative: the plugin joins it onto the directory
        it asked for, so a path here becomes ``references/references/specification.md``.
        """
        entries = asyncio.run(sandbox.list_files("audit-listening-part1/references"))
        assert entries, "the audit pool's references directory is not empty"
        names = [entry.name for entry in entries]
        assert "audit-rubric.md" in names, names
        assert all("/" not in name for name in names), names

    def test_the_plugin_really_does_list_resources_on_activation(self):
        """End to end through the plugin, which is what the test above only implies.

        This is the assertion that would have caught the bug: it exercises the plugin's own call into
        the sandbox rather than the sandbox in isolation. The lesson from that failure is exactly
        this -- a class tested directly can be correct while nothing reaches it.
        """
        from strands.vended_plugins.skills import AgentSkills, Skill

        plugin = AgentSkills(skills=Skill.from_directory(str(_audit_pool())))
        agent = agents_module.build_audit_agent()
        response = asyncio.run(plugin.skills(
            skill_name="audit-listening-part1",
            tool_context=type("Ctx", (), {"agent": agent})(),
        ))
        assert "Available resources:" in response
        assert "references/audit-rubric.md" in response
        # And the listing stays inside the pool: no generate-side file is named.
        assert "specification.md" not in response
        assert "blueprint" not in response.split("Available resources:")[-1]

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


class TestWhatFileReadActuallyDoes:
    """The measured behaviour of the tool, pinned so it stops being assumed.

    This class exists because the assumption was wrong for a while and nothing failed. Every test in
    ``TestSandboxCannotLeaveThePool`` passed -- they call the sandbox directly -- while the tool the
    agent actually uses never consulted it. If a future SDK routes ``file_read`` through the sandbox,
    these tests fail, and that failure is good news: it means the boundary can be tightened.
    """

    def test_file_read_takes_no_agent_and_names_no_sandbox(self):
        import inspect

        from strands_tools import file_read as module

        fn = getattr(module, "file_read")
        assert "agent" not in inspect.signature(fn).parameters
        assert "sandbox" not in inspect.getsource(fn)

    def test_the_audit_agent_can_in_fact_read_the_generate_pool(self):
        """Uncomfortable, and that is the point of writing it down.

        Asserting the leak rather than the protection, because the protection does not exist at this
        layer and a test claiming otherwise would be worse than no test. What keeps the auditor blind
        is that the *answers* are not on disk (``TestScratchFilesAreGone``) -- not that the filesystem
        is unreachable.
        """
        from strands_tools import file_read as module

        agent = agents_module.build_audit_agent()
        target = _generate_pool() / "generate-listening-part1" / "schemas" / "blueprint.schema.json"
        result = module.file_read(
            {"toolUseId": "t", "name": "file_read",
             "input": {"path": str(target), "mode": "view"}},
            agent=agent,
        )
        assert result.get("status") == "success"
        assert "form_group" in str(result)


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
