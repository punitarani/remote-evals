"""Invoke one tool on the task's stdio Fli server inside the trial image."""

import asyncio
import json
import sys

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from mcp.types import TextContent


async def call_tool(name, arguments, command, *args):
    async with stdio_client(
        StdioServerParameters(command=command, args=list(args))
    ) as streams:
        async with ClientSession(*streams) as session:
            await session.initialize()
            result = await session.call_tool(name, json.loads(arguments))

    if result.is_error:
        return {"success": False, "error": "Fli rejected the tool call."}
    value = result.structured_content
    if (
        value is None
        and len(result.content) == 1
        and isinstance(result.content[0], TextContent)
    ):
        value = json.loads(result.content[0].text)
    if not isinstance(value, dict):
        raise ValueError("Fli returned a non-object result.")
    return value


if __name__ == "__main__":
    print(json.dumps(asyncio.run(call_tool(*sys.argv[1:]))))
