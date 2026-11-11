"""Run the flight agent locally against live Fli and a caller-owned workspace."""

import argparse
import asyncio
import os
from pathlib import Path

import httpx

from agent.harness import ToolBudgetExceeded, run_agent
from agent.model import DEFAULT_MODEL, InferenceError, OpenRouter
from agent.tools import DirectToolExecutor


async def run(prompt: str, workspace: Path, api_key: str, model: str) -> str:
    async with httpx.AsyncClient(timeout=40) as client:
        return await run_agent(
            prompt, OpenRouter(client, api_key, model), DirectToolExecutor(workspace)
        )


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Run the flight agent with live Fli and Bash."
    )
    parser.add_argument("prompt")
    parser.add_argument("--workspace", type=Path, required=True)
    args = parser.parse_args()
    api_key = os.getenv("OPENROUTER_API_KEY")
    if not api_key:
        parser.error("Set OPENROUTER_API_KEY before running the live agent.")
    workspace = args.workspace.resolve()
    workspace.mkdir(parents=True, exist_ok=True)
    try:
        print(
            asyncio.run(
                run(
                    args.prompt,
                    workspace,
                    api_key,
                    os.getenv("AGENT_MODEL", DEFAULT_MODEL),
                )
            )
        )
    except (InferenceError, ToolBudgetExceeded) as exc:
        parser.exit(1, f"{exc}\n")


if __name__ == "__main__":
    main()
