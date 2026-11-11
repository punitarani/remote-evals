import asyncio
import json

import httpx
import pytest
from acp.schema import CreateTerminalResponse

from remote_eval_adapter.bridge import Bridge, BridgeError, Settings, ToolAction, Trace
from remote_eval_adapter.tools import TrialTools

RELEASE = "1" * 40
TRIAL_ID = "trial-id"


def test_timed_out_terminal_is_stopped_before_release(monkeypatch, tmp_path):
    monkeypatch.setattr("remote_eval_adapter.tools.TOOL_TIMEOUT", 0.01)
    cleanup = []

    class TerminalClient:
        async def create_terminal(self, **kwargs):
            return CreateTerminalResponse(terminal_id="terminal-1")

        async def wait_for_terminal_exit(self, **kwargs):
            await asyncio.Event().wait()

        async def kill_terminal(self, **kwargs):
            cleanup.append(("kill", kwargs["terminal_id"]))

        async def release_terminal(self, **kwargs):
            cleanup.append(("release", kwargs["terminal_id"]))

    async def check():
        tools = TrialTools(TerminalClient(), TRIAL_ID, tmp_path, None)
        with pytest.raises(BridgeError, match="timed out"):
            await tools.execute(ToolAction("action-1", "bash", {"command": "sleep 1"}))

    asyncio.run(check())
    assert cleanup == [("kill", "terminal-1"), ("release", "terminal-1")]


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


@pytest.mark.parametrize("release", [None, "f" * 40])
def test_start_must_confirm_release_before_running_tools(tmp_path, release):
    settings = Settings(
        "https://example.test", "test-token", RELEASE, tmp_path / "meta.json"
    )
    trace = Trace(trial_id=TRIAL_ID, release=RELEASE)
    response = tool()
    if release is None:
        del response["release"]
    else:
        response["release"] = release

    async def execute(action):
        pytest.fail(f"Unexpected tool call: {action.name}")

    async def run():
        transport = httpx.MockTransport(lambda _: httpx.Response(200, json=response))
        async with httpx.AsyncClient(transport=transport) as client:
            with pytest.raises(BridgeError) as error:
                await Bridge(client, settings, trace).run("check", execute)
            assert error.value.code == "endpoint_release"

    asyncio.run(run())


def test_retry_and_duplicate_tool_delivery_execute_once(tmp_path):
    requests = []
    calls = []
    metadata = tmp_path / "meta.json"

    def respond(request):
        assert request.headers["Authorization"] == "Bearer test-token"
        trace = json.loads(metadata.read_text())
        assert trace["status"] == "running"
        assert trace["finished_at"] is None
        if request.url.path.endswith("/start"):
            assert trace["actions"] == []
        else:
            assert trace["actions"] == [
                {"action_id": "action-1", "name": "bash", "status": "completed"}
            ]
        requests.append((request.url.path, json.loads(request.content)))
        if len(requests) == 2:
            raise httpx.ReadError("result response lost", request=request)
        if len(requests) < 4:
            return httpx.Response(200, json=tool())
        return httpx.Response(
            200, json={"kind": "done", "trial_id": TRIAL_ID, "text": "ok"}
        )

    async def execute(action):
        trace = json.loads(metadata.read_text())
        assert trace["mode"] == "scripted"
        assert trace["actions"] == [
            {"action_id": "action-1", "name": "bash", "status": "in_progress"}
        ]
        calls.append(action.arguments)
        return {"output": "hi", "exit_code": 0, "truncated": False}

    async def run():
        settings = Settings("https://example.test", "test-token", RELEASE, metadata)
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
def test_rejects_mismatched_trial_or_conflicting_duplicate(bad_tool, tmp_path):
    responses = iter([tool(), bad_tool])
    calls = []

    async def execute(action):
        calls.append(action.arguments)
        return {"output": "hi", "exit_code": 0, "truncated": False}

    async def run():
        settings = Settings(
            "https://example.test", "test-token", RELEASE, tmp_path / "metadata.json"
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


@pytest.mark.parametrize(
    ("error", "status"),
    [
        (BridgeError("tool_terminal", "failed"), "failed"),
        (asyncio.CancelledError(), "cancelled"),
    ],
)
def test_interrupted_tool_leaves_a_checkpoint(tmp_path, error, status):
    metadata = tmp_path / "metadata.json"
    settings = Settings("https://example.test", "test-token", RELEASE, metadata)
    trace = Trace(trial_id=TRIAL_ID, release=RELEASE)

    async def execute(action):
        raise error

    async def run():
        transport = httpx.MockTransport(lambda _: httpx.Response(200, json=tool()))
        async with httpx.AsyncClient(transport=transport) as client:
            with pytest.raises(type(error)):
                await Bridge(client, settings, trace).run("check", execute)

    asyncio.run(run())
    checkpoint = json.loads(metadata.read_text())
    assert checkpoint["actions"] == [
        {"action_id": "action-1", "name": "bash", "status": status}
    ]
