"""Run the same model and tool loop locally or against a Harbor trial."""

import json
from collections.abc import Awaitable, Callable
from typing import Any

from agent.tools import TOOL_NAMES, ToolExecutor, tool_definitions

SYSTEM_PROMPT = """You are a flight research agent. Use the available tools to verify fares.
Follow the user's travel constraints and output format. Treat prices and availability as
unconfirmed until you have checked them. An unsuccessful tool call is an error, while a
successful search with zero eligible options means no match. Use Bash for workspace output.
Never book or purchase travel."""

Completion = Callable[
    [list[dict[str, Any]], list[dict[str, Any]]], Awaitable[dict[str, Any]]
]


class ToolBudgetExceeded(RuntimeError):
    pass


def valid_bash_command(command: object) -> bool:
    return isinstance(command, str) and bool(command.strip()) and len(command) <= 8192


async def run_agent(prompt: str, complete: Completion, executor: ToolExecutor) -> str:
    """Return the final assistant text. The executor owns all tool side effects."""
    tools = await tool_definitions()
    messages = [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": prompt},
    ]
    remaining_actions = 12
    # Leave a model turn to read the final tool result and answer.
    for _ in range(13):
        message = await complete(messages, tools)
        calls = message.get("tool_calls") or []
        if not calls:
            return message.get("content") or ""
        messages.append(message)
        for call in calls:
            if remaining_actions == 0:
                raise ToolBudgetExceeded("The flight agent ran out of tool calls.")
            remaining_actions -= 1
            function = call["function"]
            name = function["name"]
            try:
                arguments = json.loads(function["arguments"])
            except ValueError, TypeError:
                arguments = None

            if not isinstance(arguments, dict) or name not in TOOL_NAMES:
                result = {"success": False, "error": "Invalid tool name or arguments."}
            elif name == "bash" and not valid_bash_command(arguments.get("command")):
                result = {
                    "success": False,
                    "error": "Bash requires a command up to 8192 characters.",
                }
            else:
                result = await executor.execute(name, arguments)

            messages.append(
                {
                    "role": "tool",
                    "tool_call_id": call["id"],
                    "content": json.dumps(result),
                }
            )

    raise ToolBudgetExceeded("The flight agent ran out of model turns.")
