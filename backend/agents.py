"""The two agents, each holding a pool of skills it chooses from.

Replaces the previous shape, where every step built a fresh ``Agent`` and handed it exactly one
skill whose instructions Python had already assembled. The model had no choice to make, and adding
a subject meant editing the backend. Here each agent sees every skill in its pool as
name + description and activates the one that fits, so a reading or writing capability is a new
directory rather than a code change.

**Two pools, not one agent with all skills.** The generator and the auditor are separate agents
because the auditor's value comes from what it has *not* seen. One agent holding both pools could
activate the generate skill while auditing and read the authoring specification's private planning
rules; worse, a single conversation would carry the plan it had just written into the audit of it.
Two agents share no state at all.

**Where the tools differ, and why.**

``generate_agent`` gets ``file_read`` and ``shell``. It reads its own specification and runs its own
validator, which is what "the agent executes the workflow" means. Neither is a leak: the generator
reading the authoring rules is the point, and a validator report on its own draft tells it nothing
it should not know. Note what this does *not* grant -- validation is a report, not a gate
(``orchestration/loop.py``), so an agent that lied about a clean run would gain nothing: Python
re-runs the validator on the delivered artifact regardless, and the findings travel with the
material either way.

``audit_agent`` gets ``file_read`` only, and its metrics script runs in AgentCore's Code Interpreter
(``sandboxed_metrics.py``). The reason is measured rather than assumed: ``strands_tools.shell`` calls
``pty.fork()`` directly and its signature carries no ``agent``, so it never touches
``agent.sandbox`` and no sandbox can constrain it. A shell-equipped auditor can read the
generator's files off the local filesystem, which is the one thing the audit side cannot survive.
Code Interpreter inverts the problem: the remote environment starts empty and holds only the two
files uploaded to it.

**``file_read`` is not sandbox-routed either, and this was measured after being assumed otherwise.**
Its signature is ``(tool, **kwargs)``, its source names no sandbox, and calling it on the audit agent
with an absolute path to the generate pool returns the file. ``ReadOnlySkillSandbox`` below therefore
bounds what the *skills plugin* reads through it -- resource listing on activation -- and nothing
about what the agent reads directly. What actually keeps the audit side blind is three things that do
not depend on the SDK routing anything: the audit pool physically contains no plan schema, the
auditor has no way to run a command, and ``steps/agent_steps.purge_plan_scratch`` deletes the
generator's scratch files before the audit call, with ``guards.assert_no_plan_on_disk`` raising if any
survived. The last of those is the one that matters most: a plan *schema* only reveals field names,
while a plan *file* holds this material's answers.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Dict, List, Optional

from . import paths
from .model import provider

__all__ = [
    "AUDIT_POOL",
    "AUDIT_SYSTEM_PROMPT",
    "GENERATE_POOL",
    "GENERATE_SYSTEM_PROMPT",
    "build_audit_agent",
    "build_generate_agent",
    "pool_dir",
]

# Pool directory names under the skills root. Each holds one directory per subject.
GENERATE_POOL = "generate"
AUDIT_POOL = "audit"

# Reasoning effort per role. The auditor runs higher: a verdict that drifts between runs is worse
# than a slow one, because the revision instructions and the pick-better decision are both built
# from it.
GENERATE_EFFORT = "medium"
AUDIT_EFFORT = "high"
GENERATE_MAX_TOKENS = 32000
AUDIT_MAX_TOKENS = 32000

# Procedural only. Every authoring and grading rule lives in the skill files, so neither prompt
# names a subject -- that is what lets a second pool member work without touching this file.
GENERATE_SYSTEM_PROMPT = (
    "You are a listening-material generation specialist.\n\n"
    "Your available skills are listed in your system context. Read the request, decide which skill "
    "covers it, and activate that skill with the `skills` tool before doing anything else. Do not "
    "draft from memory of what such material looks like: the skill carries the authoritative "
    "specification, and a draft written without it is rejected.\n\n"
    "Once the skill is active, execute its workflow completely — including reading the reference "
    "files it points to with `file_read`, and running the validation script it names with `shell`. "
    "Fix what the validator reports and run it again. The workflow is yours to carry out; nobody "
    "will run those steps for you.\n\n"
    "Reply with the JSON artifacts the skill specifies, in the single envelope object it names, and "
    "nothing else: no Markdown fences, no commentary before or after.\n\n"
    "Files you write while working are scratch space on a container that is thrown away. Only your "
    "reply is read, so every artifact has to be in it — a result left in a file was not delivered."
)

AUDIT_SYSTEM_PROMPT = (
    "You are a listening-material audit specialist.\n\n"
    "Your available skills are listed in your system context. Decide which one covers the material "
    "you were sent, and activate it with the `skills` tool before judging anything. Then execute "
    "its workflow completely, reading the rubric it points to with `file_read`.\n\n"
    "You receive the material text only. You are never given the generator's information-point "
    "annotation, and you must not ask for it: your reconstruction is worth something precisely "
    "because it was made without it. A detail you cannot recover from the script is a real defect, "
    "because a candidate hearing the recording once will not recover it either.\n\n"
    "Reply with the JSON the skill's schema specifies and nothing else."
)


def _sandbox_base() -> Any:
    """The SDK's ``Sandbox`` ABC.

    Imported through a function because ``Agent`` type-checks the sandbox argument
    (``isinstance(sandbox, Sandbox)``), so duck typing is not enough -- and importing the ABC at
    module scope would drag the SDK into every import of this module, including the deterministic
    layer's tests.
    """
    from strands.sandbox.base import Sandbox

    return Sandbox


class ReadOnlySkillSandbox(_sandbox_base()):
    """Confines sandbox-routed filesystem access to one pool directory, and refuses every write.

    **What this does and does not cover.** Everything the SDK routes through ``agent.sandbox`` is
    bounded here -- notably the skills plugin's resource listing on activation. Neither
    ``strands_tools.shell`` nor ``strands_tools.file_read`` is routed through it: both were measured,
    and neither signature carries an ``agent``. So this is a real boundary for the SDK's own
    filesystem access and not a boundary against a determined agent, which is why the audit side also
    relies on physical absence, on having no shell, and on the scratch purge in ``steps/agent_steps``.

    Two properties, both asserted by tests:

    * **reads are confined to the pool.** The default sandbox
      (``NotASandboxLocalEnvironment``) resolves any path against the process cwd. Paths are resolved
      here and rejected unless they stay inside the root, which also covers ``..`` traversal and
      symlinks, since ``resolve()`` follows both.
    * **nothing is written.** A skill directory contains the validator the generator runs; an agent
      able to edit it could make the validator agree with anything.

    Delegates the rest to the platform default rather than reimplementing process handling: the
    execute paths are refused outright, so nothing is inherited that could run a command.
    """

    def __init__(self, root: Path) -> None:
        self._root = Path(root).resolve()

    @property
    def root(self) -> Path:
        return self._root

    def _checked(self, path: str) -> Path:
        candidate = Path(path)
        if not candidate.is_absolute():
            candidate = self._root / candidate
        resolved = candidate.resolve()
        try:
            resolved.relative_to(self._root)
        except ValueError:
            raise PermissionError(
                "%s is outside this agent's skill pool (%s)" % (resolved, self._root)
            )
        return resolved

    async def read_file(self, path: str, **_: Any) -> bytes:
        return self._checked(path).read_bytes()

    async def read_text(self, path: str, encoding: str = "utf-8", **_: Any) -> str:
        return self._checked(path).read_text(encoding=encoding)

    async def list_files(self, path: str, **_: Any) -> List[Any]:
        from strands.sandbox.types import FileInfo

        target = self._checked(path)
        entries: List[Any] = []
        for child in sorted(target.iterdir()):
            entries.append(FileInfo(
                path=str(child.relative_to(self._root)),
                is_dir=child.is_dir(),
                size=child.stat().st_size if child.is_file() else 0,
            ))
        return entries

    async def write_file(self, path: str, content: bytes, **_: Any) -> None:
        raise PermissionError("this agent's skill pool is read-only")

    async def write_text(self, path: str, content: str, encoding: str = "utf-8", **_: Any) -> None:
        raise PermissionError("this agent's skill pool is read-only")

    async def remove_file(self, path: str, **_: Any) -> None:
        raise PermissionError("this agent's skill pool is read-only")

    async def execute_streaming(self, command: str, **_: Any):
        raise PermissionError("this sandbox does not execute commands")
        yield  # pragma: no cover - unreachable, keeps this an async generator

    async def execute_code_streaming(self, code: str, language: str, **_: Any):
        raise PermissionError("this sandbox does not execute code")
        yield  # pragma: no cover - unreachable, keeps this an async generator

    def get_tools(self) -> List[Any]:
        return []


def pool_dir(pool: str) -> Path:
    """Directory holding a pool's skills, resolved through ``paths`` for the container layout."""
    root = paths.skills_root()
    candidate = root / pool
    if candidate.is_dir():
        return candidate
    raise FileNotFoundError("skill pool %r not found under %s" % (pool, root))


def _load_pool(pool: str) -> List[Any]:
    """Every skill in a pool, as the SDK loads them from disk.

    ``Skill.from_directory`` treats each subdirectory carrying a SKILL.md as a skill and skips the
    rest, so the pool is defined by the filesystem rather than by a list in this file.
    """
    from strands.vended_plugins.skills import Skill

    skills = Skill.from_directory(str(pool_dir(pool)))
    if not skills:
        raise FileNotFoundError("skill pool %r contains no skills" % pool)
    return skills


def _require_non_interactive_shell() -> None:
    """Set the two flags that stop ``shell`` from waiting for a human.

    Measured, and it is a hard blocker rather than a nuisance: the tool prints
    ``Do you want to proceed with execution? [y/*]`` and blocks on stdin. In AgentCore Runtime there
    is no tty, so the call hangs until the platform's 15-minute wall kills the invocation -- and the
    symptom is a material that took 15 minutes and produced nothing, with no error naming the cause.

    Set here rather than in the task definition on purpose. A deployment-time variable is one a new
    environment forgets, and the failure it produces looks like a model timeout rather than a missing
    flag. ``setdefault`` so an operator can still override either.
    """
    os.environ.setdefault("STRANDS_NON_INTERACTIVE", "true")
    os.environ.setdefault("BYPASS_TOOL_CONSENT", "true")


def _build(
    pool: str,
    system_prompt: str,
    *,
    effort: str,
    max_tokens: int,
    with_shell: bool,
) -> Any:
    from strands import Agent
    from strands.vended_plugins.skills import AgentSkills
    from strands_tools import file_read

    tools: List[Any] = [file_read]
    if with_shell:
        from strands_tools import shell

        _require_non_interactive_shell()
        tools.append(shell)

    return Agent(
        model=provider.build_model(max_output_tokens=max_tokens, reasoning_effort=effort),
        system_prompt=system_prompt,
        plugins=[AgentSkills(skills=_load_pool(pool))],
        tools=tools,
        sandbox=ReadOnlySkillSandbox(pool_dir(pool)),
        callback_handler=None,
    )


def build_generate_agent() -> Any:
    """A generator over the whole generate pool.

    Built per material rather than held as a module global. The pool and the prompts are shared, but
    a conversation is not: a second material generated in the same session would inherit the first
    one's script and tend to echo it, and a revision would inherit the audit it was asked to fix,
    which is exactly the independence the loop depends on.
    """
    return _build(
        GENERATE_POOL, GENERATE_SYSTEM_PROMPT,
        effort=GENERATE_EFFORT, max_tokens=GENERATE_MAX_TOKENS, with_shell=True,
    )


def build_audit_agent() -> Any:
    """An auditor over the whole audit pool. No ``shell`` -- see the module docstring.

    Also built per call, which is what makes the re-audit memoryless: it cannot inherit the first
    audit's conclusions because there is no shared session to inherit them from.
    """
    return _build(
        AUDIT_POOL, AUDIT_SYSTEM_PROMPT,
        effort=AUDIT_EFFORT, max_tokens=AUDIT_MAX_TOKENS, with_shell=False,
    )
