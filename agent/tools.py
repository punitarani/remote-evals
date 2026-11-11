import asyncio
import json
import os
import signal
from pathlib import Path
from typing import Any, Protocol

from fli.mcp.server import mcp

FLI_TOOLS = {"search_dates", "search_flights"}
TOOL_NAMES = FLI_TOOLS | {"bash"}
BASH_TIMEOUT = 20
BASH_OUTPUT_LIMIT = 16 * 1024


async def tool_definitions() -> list[dict[str, Any]]:
    tools = [
        {
            "name": "bash",
            "description": "Run Bash in the current workspace and return its exit code and output.",
            "parameters": {
                "type": "object",
                "properties": {"command": {"type": "string"}},
                "required": ["command"],
                "additionalProperties": False,
            },
        }
    ]
    tools.extend(
        {
            "name": tool.name,
            "description": tool.description,
            "parameters": tool.parameters,
        }
        for tool in await mcp.list_tools()
        if tool.name in FLI_TOOLS
    )
    return [{"type": "function", "function": tool} for tool in tools]


async def run_bash(command: str, workspace: Path) -> dict[str, Any]:
    process = await asyncio.create_subprocess_exec(
        "/bin/bash",
        "-c",
        command,
        cwd=workspace,
        env={"PATH": "/usr/local/bin:/usr/bin:/bin", "HOME": str(workspace)},
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
        start_new_session=True,
    )
    assert process.stdout is not None
    output = bytearray()
    truncated = False
    timed_out = False
    try:
        async with asyncio.timeout(BASH_TIMEOUT):
            while chunk := await process.stdout.read(8192):
                output.extend(chunk)
                if len(output) > BASH_OUTPUT_LIMIT:
                    del output[:-BASH_OUTPUT_LIMIT]
                    truncated = True
            await process.wait()
    except TimeoutError:
        timed_out = True
    finally:
        if process.returncode is None or timed_out:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            await process.wait()

    text = output.decode(errors="replace")
    if timed_out:
        text += "\nBash timed out."
    return {"exit_code": process.returncode, "output": text, "truncated": truncated}


async def call_fli(name: str, arguments: dict[str, Any]) -> dict[str, Any]:
    try:
        result = await mcp.call_tool(name, arguments)
    except Exception:
        return {"success": False, "error": "Fli could not run the requested search."}
    if result.structured_content is not None:
        return result.structured_content
    return json.loads(result.content[0].text)


class ToolExecutor(Protocol):
    async def execute(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]: ...


class DirectToolExecutor:
    def __init__(self, workspace: Path):
        self.workspace = workspace

    async def execute(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        if name == "bash":
            return await run_bash(arguments["command"], self.workspace)
        return await call_fli(name, arguments)
