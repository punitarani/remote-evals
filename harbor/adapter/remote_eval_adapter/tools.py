import asyncio
import json
from contextlib import AsyncExitStack, suppress
from pathlib import Path

from acp import RequestError
from acp.interfaces import Client
from acp.schema import McpServerStdio
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from mcp.types import TextContent

from .bridge import BridgeError, JsonObject, ToolAction

TOOL_TIMEOUT = 20
BASH_OUTPUT_LIMIT = 16_384


class TrialTools:
    """Run Bash in Harbor's terminal and connect to the task's stdio Fli server."""

    def __init__(
        self,
        client: Client,
        session_id: str,
        cwd: Path,
        fli_server: McpServerStdio | None,
    ) -> None:
        self.client = client
        self.session_id = session_id
        self.cwd = cwd
        self.fli_server = fli_server
        self.mcp: ClientSession | None = None
        self.stack = AsyncExitStack()

    async def close(self) -> None:
        await self.stack.aclose()

    async def execute(self, action: ToolAction) -> JsonObject:
        if action.name == "bash":
            return await self._bash(action.arguments)
        return await self._fli(action)

    async def _bash(self, arguments: JsonObject) -> JsonObject:
        command = arguments.get("command")
        if not isinstance(command, str) or not command.strip():
            return {
                "exit_code": 2,
                "output": "bash requires a nonempty command",
                "truncated": False,
            }

        terminal_id = None
        exited = False
        try:
            async with asyncio.timeout(TOOL_TIMEOUT):
                # env -i keeps bridge credentials out of the shell's environment.
                terminal = await self.client.create_terminal(
                    session_id=self.session_id,
                    command="/usr/bin/env",
                    args=[
                        "-i",
                        "PATH=/usr/local/bin:/usr/bin:/bin",
                        f"HOME={self.cwd}",
                        "/bin/bash",
                        "-c",
                        command,
                    ],
                    cwd=str(self.cwd),
                    output_byte_limit=BASH_OUTPUT_LIMIT,
                )
                terminal_id = terminal.terminal_id
                status = await self.client.wait_for_terminal_exit(
                    session_id=self.session_id, terminal_id=terminal_id
                )
                exited = True
                output = await self.client.terminal_output(
                    session_id=self.session_id, terminal_id=terminal_id
                )
        except TimeoutError:
            raise BridgeError("tool_timeout", "The trial terminal timed out.") from None
        except (ConnectionError, RequestError):
            raise BridgeError(
                "tool_terminal", "The trial terminal could not complete the action."
            ) from None
        finally:
            if terminal_id is not None:
                if not exited:
                    with suppress(ConnectionError, RequestError, TimeoutError):
                        async with asyncio.timeout(3):
                            await self.client.kill_terminal(
                                session_id=self.session_id, terminal_id=terminal_id
                            )
                with suppress(ConnectionError, RequestError, TimeoutError):
                    async with asyncio.timeout(3):
                        await self.client.release_terminal(
                            session_id=self.session_id, terminal_id=terminal_id
                        )
        return {
            "exit_code": status.exit_code,
            "output": output.output,
            "truncated": output.truncated,
        }

    async def _fli(self, action: ToolAction) -> JsonObject:
        server = self.fli_server
        if server is None:
            raise BridgeError(
                "tool_mcp", "The trial did not register a stdio Fli MCP server."
            )
        try:
            async with asyncio.timeout(TOOL_TIMEOUT):
                if self.mcp is None:
                    # The task selects live or emulated Fli. Reuse its process
                    # for the rest of this prompt.
                    params = StdioServerParameters(
                        command=server.command,
                        args=server.args,
                        env={entry.name: entry.value for entry in server.env},
                        cwd=self.cwd,
                    )
                    streams = await self.stack.enter_async_context(stdio_client(params))
                    self.mcp = await self.stack.enter_async_context(
                        ClientSession(*streams)
                    )
                    await self.mcp.initialize()
                result = await self.mcp.call_tool(action.name, action.arguments)
            if result.isError:
                raise ValueError("Fli returned a tool error")
            if result.structuredContent is not None:
                return result.structuredContent
            if len(result.content) == 1 and isinstance(result.content[0], TextContent):
                value = json.loads(result.content[0].text)
                if isinstance(value, dict):
                    return value
            raise ValueError("Fli returned a non-object result")
        except TimeoutError:
            raise BridgeError(
                "tool_timeout", "The trial Fli MCP call timed out."
            ) from None
        except Exception:
            raise BridgeError("tool_mcp", "The trial Fli MCP call failed.") from None
