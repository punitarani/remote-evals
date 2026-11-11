import asyncio
import json
from pathlib import Path

import httpx
import pytest

from remote_eval_adapter.bridge import Bridge, BridgeError, Settings, Trace

RELEASE = "1" * 40
TRIAL_ID = "trial-id"


def tool(trial_id=TRIAL_ID, command="echo hi"):
    return {
        "kind": "tool",
        "trial_id": trial_id,
        "action_id": "action-1",
        "name": "bash",
        "arguments": {"command": command},
        "release": RELEASE,
        "mode": "scripted",
        "model": None,
    }


def test_retry_and_duplicate_tool_delivery_execute_once(tmp_path):
    requests = []
    calls = []

    def respond(request):
        assert request.headers["Authorization"] == "Bearer test-token"
        requests.append((request.url.path, json.loads(request.content)))
        if len(requests) == 2:
            raise httpx.ReadError("result response lost", request=request)
        if len(requests) < 4:
            return httpx.Response(200, json=tool())
        return httpx.Response(
            200, json={"kind": "done", "trial_id": TRIAL_ID, "text": "ok"}
        )

    async def execute(action):
        calls.append(action.arguments)
        return {"output": "hi", "exit_code": 0, "truncated": False}

    async def run():
        settings = Settings(
            "https://example.test", "test-token", RELEASE, tmp_path / "meta.json"
        )
        trace = Trace(trial_id=TRIAL_ID, release=RELEASE)
        async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
            assert await Bridge(client, settings, trace).run("check", execute) == "ok"
        assert trace.actions == [
            {"action_id": "action-1", "name": "bash", "status": "completed"}
        ]

    asyncio.run(run())
    assert calls == [{"command": "echo hi"}]
    assert requests[0] == (
        f"/v1/evals/{TRIAL_ID}/start",
        {"prompt": "check", "release": RELEASE},
    )
    assert (
        requests[1:]
        == [
            (
                f"/v1/evals/{TRIAL_ID}/actions/action-1/result",
                {"result": {"output": "hi", "exit_code": 0, "truncated": False}},
            )
        ]
        * 3
    )


@pytest.mark.parametrize(
    "bad_tool", [tool(trial_id="other-trial"), tool(command="echo changed")]
)
def test_rejects_mismatched_trial_or_conflicting_duplicate(bad_tool):
    responses = iter([tool(), bad_tool])
    calls = []

    async def execute(action):
        calls.append(action.arguments)
        return {"output": "hi", "exit_code": 0, "truncated": False}

    async def run():
        settings = Settings(
            "https://example.test", "test-token", RELEASE, Path("metadata.json")
        )
        trace = Trace(trial_id=TRIAL_ID, release=RELEASE)
        transport = httpx.MockTransport(
            lambda _: httpx.Response(200, json=next(responses))
        )
        async with httpx.AsyncClient(transport=transport) as client:
            with pytest.raises(BridgeError, match="trial|action"):
                await Bridge(client, settings, trace).run("check", execute)

    asyncio.run(run())
    assert calls == [{"command": "echo hi"}]
