import asyncio
from uuid import uuid4

import httpx

from agent.service import Settings, create_app

RELEASE = "a" * 40
HEADERS = {"Authorization": "Bearer bridge-token"}


def test_replayed_start_and_result_return_the_original_reply() -> None:
    async def check() -> None:
        app = create_app(Settings(RELEASE, "bridge-token", mode="scripted"))
        async with (
            app.router.lifespan_context(app),
            httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url="http://test"
            ) as http,
        ):
            base = f"/v1/evals/{uuid4()}"
            start = {"prompt": "Bridge probe", "release": RELEASE}
            denied = await http.post(f"{base}/start", json=start)
            assert denied.status_code == 401
            wrong_release = await http.post(
                f"{base}/start", json={**start, "release": "b" * 40}, headers=HEADERS
            )
            assert wrong_release.status_code == 409
            first, retry = await asyncio.gather(
                http.post(f"{base}/start", json=start, headers=HEADERS),
                http.post(f"{base}/start", json=start, headers=HEADERS),
            )
            assert first.status_code == 200
            assert first.json() == retry.json()
            action = first.json()
            assert action["kind"] == "tool" and action["name"] == "bash"

            url = f"{base}/actions/{action['action_id']}/result"
            result = {
                "result": {"exit_code": 0, "output": "bridge-ok\n", "truncated": False}
            }
            done, replay = await asyncio.gather(
                http.post(url, json=result, headers=HEADERS),
                http.post(url, json=result, headers=HEADERS),
            )
            assert done.json()["kind"] == "done"
            assert done.json() == replay.json()
            conflict = await http.post(url, json={"result": {}}, headers=HEADERS)
            assert conflict.status_code == 409

    asyncio.run(check())


def test_completed_sessions_expire_on_result_replay(monkeypatch) -> None:
    monkeypatch.setattr("agent.service.REPLAY_TTL", -1)

    async def check() -> None:
        app = create_app(Settings(RELEASE, "bridge-token", mode="scripted"))
        async with (
            app.router.lifespan_context(app),
            httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app),
                base_url="http://test",
                headers=HEADERS,
            ) as http,
        ):
            base = f"/v1/evals/{uuid4()}"
            action = (
                await http.post(
                    f"{base}/start", json={"prompt": "Probe", "release": RELEASE}
                )
            ).json()
            url = f"{base}/actions/{action['action_id']}/result"
            payload = {"result": {"exit_code": 0, "output": "bridge-ok\n"}}
            assert (await http.post(url, json=payload)).json()["kind"] == "done"
            assert (await http.post(url, json=payload)).status_code == 404

    asyncio.run(check())


def test_model_sessions_use_independent_workspaces(tmp_path) -> None:
    import json
    import shlex

    from agent.tools import DirectToolExecutor

    def complete(request: httpx.Request) -> httpx.Response:
        data = json.loads(request.content)
        messages = data["messages"]
        marker = messages[1]["content"]
        if messages[-1]["role"] == "tool":
            result = json.loads(messages[-1]["content"])
            assert result["output"] == marker
            message = {"role": "assistant", "content": marker}
        else:
            arguments = {
                "command": f"printf %s {shlex.quote(marker)} > report.json; cat report.json"
            }
            message = {
                "role": "assistant",
                "tool_calls": [
                    {
                        "id": "write-report",
                        "type": "function",
                        "function": {
                            "name": "bash",
                            "arguments": json.dumps(arguments),
                        },
                    }
                ],
            }
        return httpx.Response(200, json={"choices": [{"message": message}]})

    async def check() -> None:
        settings = Settings(RELEASE, "bridge-token", api_key="test")
        app = create_app(settings, model_transport=httpx.MockTransport(complete))
        async with (
            app.router.lifespan_context(app),
            httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app),
                base_url="http://test",
                headers=HEADERS,
            ) as http,
        ):

            async def trial(marker):
                base = f"/v1/evals/{uuid4()}"
                start = await http.post(
                    f"{base}/start", json={"prompt": marker, "release": RELEASE}
                )
                action = start.json()
                workspace = tmp_path / marker
                workspace.mkdir()
                result = await DirectToolExecutor(workspace).execute(
                    action["name"], action["arguments"]
                )
                end = await http.post(
                    f"{base}/actions/{action['action_id']}/result",
                    json={"result": result},
                )
                assert end.json()["text"] == marker
                assert (workspace / "report.json").read_text() == marker
                return action["trial_id"]

            first, second = await asyncio.gather(trial("first"), trial("second"))
            assert first != second

    asyncio.run(check())
