import asyncio
from pathlib import Path

import httpx
import pytest

from agent.harness import run_agent
from agent.model import DEFAULT_MODEL, InferenceError, OpenRouter
from agent.tools import DirectToolExecutor


@pytest.mark.parametrize(
    "choice",
    [
        {
            "finish_reason": "length",
            "message": {"role": "assistant", "content": "Partial"},
        },
        {
            "finish_reason": "tool_calls",
            "message": {"role": "assistant", "tool_calls": [{}]},
        },
    ],
)
def test_unusable_model_response_is_an_inference_error(choice) -> None:
    async def check() -> None:
        transport = httpx.MockTransport(
            lambda _: httpx.Response(200, json={"choices": [choice]})
        )
        async with httpx.AsyncClient(transport=transport) as client:
            with pytest.raises(InferenceError):
                await OpenRouter(client, "test-key", DEFAULT_MODEL)([], [])

    asyncio.run(check())


def test_direct_agent_writes_in_its_workspace(tmp_path: Path) -> None:
    async def model(messages, tools):
        assert {tool["function"]["name"] for tool in tools} == {
            "bash",
            "search_dates",
            "search_flights",
        }
        if messages[-1]["role"] == "tool":
            assert messages[-1]["tool_call_id"] == "bash-1"
            assert '"exit_code": 0' in messages[-1]["content"]
            return {"role": "assistant", "content": "Wrote the report."}
        return {
            "role": "assistant",
            "tool_calls": [
                {
                    "id": "bash-1",
                    "type": "function",
                    "function": {
                        "name": "bash",
                        "arguments": '{"command":"printf example > report.json"}',
                    },
                },
            ],
        }

    assert (
        asyncio.run(run_agent("Write a report", model, DirectToolExecutor(tmp_path)))
        == "Wrote the report."
    )
    assert (tmp_path / "report.json").read_text() == "example"
