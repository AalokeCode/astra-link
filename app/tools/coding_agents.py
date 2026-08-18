"""Natural-language tools for visible Claude Code and Codex Kitty sessions."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

from app.config import PROJECT_ROOT, load_config
from app.integrations.agent_workspace import AgentWorkspaceError, KittyAgentWorkspace
from app.tools.registry import RiskLevel, ToolExecutionError, ToolGroup, registry


def _workspace() -> KittyAgentWorkspace:
    return KittyAgentWorkspace(load_config(), default_project=PROJECT_ROOT)


def _tool_error(exc: AgentWorkspaceError) -> ToolExecutionError:
    message = exc.message
    if exc.action:
        message += f" Suggested action: {exc.action}"
    if exc.detail:
        message += f" Detail: {exc.detail}"
    return ToolExecutionError(message)


def _resolve_instance(workspace: KittyAgentWorkspace, instance_id: str | None) -> str:
    if instance_id:
        return instance_id
    instances = workspace.list_instances(include_terminal=False)
    if not instances:
        raise ToolExecutionError("no ASTRA-managed coding agent instance is currently open")
    if len(instances) > 1:
        choices = ", ".join(f"{item['id']} ({item['title']})" for item in instances)
        raise ToolExecutionError(
            "more than one coding agent is open; ask the user which instance they mean. "
            f"Available instances: {choices}"
        )
    return str(instances[0]["id"])


class LaunchCodingAgentArgs(BaseModel):
    provider: Literal["claude", "codex"] = Field(
        description="Coding agent CLI to launch in a visible Kitty tab."
    )
    project_path: str = Field(
        min_length=1,
        description="Existing project directory under ASTRA's configured ALLOWED_DIRS.",
    )
    prompt: str | None = Field(
        default=None,
        max_length=20_000,
        description="Optional first task to submit as the interactive session opens.",
    )


class ListCodingAgentsArgs(BaseModel):
    pass


class InspectCodingAgentArgs(BaseModel):
    instance_id: str | None = Field(
        default=None,
        description="ASTRA instance ID. May be omitted only when exactly one instance is open.",
    )


class PromptCodingAgentArgs(BaseModel):
    message: str = Field(min_length=1, max_length=20_000)
    instance_id: str | None = Field(
        default=None,
        description="ASTRA instance ID. May be omitted only when exactly one instance is open.",
    )
    mode: Literal["prompt", "steer"] = Field(
        default="prompt",
        description="Use steer to redirect current work; prompt for a normal follow-up task.",
    )


class ControlCodingAgentArgs(BaseModel):
    instance_id: str | None = Field(
        default=None,
        description="ASTRA instance ID. May be omitted only when exactly one instance is open.",
    )


@registry.tool(
    name="launch_coding_agent",
    description=(
        "Launch a visible Claude Code or Codex interactive session in its own Kitty tab, "
        "optionally with an initial task. Use this when the user says to launch/start/open "
        "an agent instance for a project."
    ),
    args_model=LaunchCodingAgentArgs,
    risk=RiskLevel.LOW_RISK_WRITE,
    group=ToolGroup.CLAUDE_CODE,
)
def launch_coding_agent(args: LaunchCodingAgentArgs) -> dict:
    try:
        return _workspace().launch(
            args.provider,
            args.project_path,
            initial_prompt=args.prompt,
        )
    except AgentWorkspaceError as exc:
        raise _tool_error(exc) from exc


@registry.tool(
    name="list_coding_agents",
    description=(
        "List the Claude Code and Codex instances ASTRA currently manages in Kitty. "
        "Returns IDs, projects, providers, status, recent terminal output, and diagnosed errors."
    ),
    args_model=ListCodingAgentsArgs,
    risk=RiskLevel.READ_ONLY,
    group=ToolGroup.CLAUDE_CODE,
)
def list_coding_agents(args: ListCodingAgentsArgs) -> dict:
    del args
    workspace = _workspace()
    return {"capabilities": workspace.capabilities(), "instances": workspace.list_instances()}


@registry.tool(
    name="inspect_coding_agent",
    description=(
        "Explain what a specific ASTRA-managed Claude Code or Codex Kitty instance is doing, "
        "using its real current terminal output. Omit the ID only when one instance is open."
    ),
    args_model=InspectCodingAgentArgs,
    risk=RiskLevel.READ_ONLY,
    group=ToolGroup.CLAUDE_CODE,
)
def inspect_coding_agent(args: InspectCodingAgentArgs) -> dict:
    workspace = _workspace()
    instance_id = _resolve_instance(workspace, args.instance_id)
    try:
        instance = next(
            item for item in workspace.list_instances() if item["id"] == instance_id
        )
    except StopIteration as exc:
        raise ToolExecutionError("that coding agent tab is no longer open") from exc
    return instance


@registry.tool(
    name="prompt_coding_agent",
    description=(
        "Send a normal follow-up prompt or a steering correction to a visible Claude Code or "
        "Codex instance. Use mode=steer for requests like 'stop that approach and do this instead'."
    ),
    args_model=PromptCodingAgentArgs,
    risk=RiskLevel.LOW_RISK_WRITE,
    group=ToolGroup.CLAUDE_CODE,
)
def prompt_coding_agent(args: PromptCodingAgentArgs) -> dict:
    workspace = _workspace()
    instance_id = _resolve_instance(workspace, args.instance_id)
    try:
        workspace.send_prompt(instance_id, args.message, kind=args.mode)
    except AgentWorkspaceError as exc:
        raise _tool_error(exc) from exc
    return {"sent": True, "instance_id": instance_id, "mode": args.mode}


@registry.tool(
    name="interrupt_coding_agent",
    description=(
        "Pause or interrupt the foreground task in a visible Claude Code or Codex instance "
        "by sending SIGINT. This keeps the Kitty tab open for a replacement prompt."
    ),
    args_model=ControlCodingAgentArgs,
    risk=RiskLevel.LOW_RISK_WRITE,
    group=ToolGroup.CLAUDE_CODE,
)
def interrupt_coding_agent(args: ControlCodingAgentArgs) -> dict:
    workspace = _workspace()
    instance_id = _resolve_instance(workspace, args.instance_id)
    try:
        workspace.interrupt(instance_id)
    except AgentWorkspaceError as exc:
        raise _tool_error(exc) from exc
    return {"interrupted": True, "instance_id": instance_id}


@registry.tool(
    name="focus_coding_agent",
    description="Bring a specific ASTRA-managed coding agent's Kitty tab to the foreground.",
    args_model=ControlCodingAgentArgs,
    risk=RiskLevel.LOW_RISK_WRITE,
    group=ToolGroup.CLAUDE_CODE,
)
def focus_coding_agent(args: ControlCodingAgentArgs) -> dict:
    workspace = _workspace()
    instance_id = _resolve_instance(workspace, args.instance_id)
    try:
        workspace.focus(instance_id)
    except AgentWorkspaceError as exc:
        raise _tool_error(exc) from exc
    return {"focused": True, "instance_id": instance_id}


@registry.tool(
    name="close_coding_agent",
    description="Close one ASTRA-managed Claude Code or Codex Kitty tab.",
    args_model=ControlCodingAgentArgs,
    risk=RiskLevel.LOW_RISK_WRITE,
    group=ToolGroup.CLAUDE_CODE,
)
def close_coding_agent(args: ControlCodingAgentArgs) -> dict:
    workspace = _workspace()
    instance_id = _resolve_instance(workspace, args.instance_id)
    try:
        workspace.close(instance_id)
    except AgentWorkspaceError as exc:
        raise _tool_error(exc) from exc
    return {"closed": True, "instance_id": instance_id}


@registry.tool(
    name="shutdown_agent_workspace",
    description=(
        "Shut down ASTRA's dedicated Kitty agent workspace and all Claude Code/Codex tabs "
        "inside it. Use only when the user explicitly asks to shut down the whole workspace."
    ),
    args_model=ListCodingAgentsArgs,
    risk=RiskLevel.LOW_RISK_WRITE,
    group=ToolGroup.CLAUDE_CODE,
)
def shutdown_agent_workspace(args: ListCodingAgentsArgs) -> dict:
    del args
    try:
        return _workspace().shutdown()
    except AgentWorkspaceError as exc:
        raise _tool_error(exc) from exc
