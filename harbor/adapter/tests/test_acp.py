import asyncio
import json
import os
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from acp import PROTOCOL_VERSION, spawn_agent_process, text_block
from acp.schema import (
    ClientCapabilities,
    CreateTerminalResponse,
    McpServerStdio,
    ReleaseTerminalResponse,
    TerminalOutputResponse,
    WaitForTerminalExitResponse,
)

RELEASE = "2" * 40
MCP_SERVER = """
from mcp.server.fastmcp import FastMCP

server = FastMCP('fli')

@server.tool()
def search_dates(origin: str) -> dict:
    return {'success': True, 'dates': [], 'origin': origin}

server.run()
"""


class LocalTerminal:
    def __init__(self):
        self.updates = []

    async def session_update(self, session_id, update, **kwargs):
        self.updates.append(update)

    async def create_terminal(self, session_id, command, args, cwd, **kwargs):
        self.process = await asyncio.create_subprocess_exec(
            command,
            *args,
            cwd=cwd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
        )
        return CreateTerminalResponse(terminal_id="terminal-1")

    async def wait_for_terminal_exit(self, session_id, terminal_id, **kwargs):
        self.output, _ = await self.process.communicate()
        return WaitForTerminalExitResponse(exit_code=self.process.returncode)

    async def terminal_output(self, session_id, terminal_id, **kwargs):
        return TerminalOutputResponse(output=self.output.decode(), truncated=False)

    async def release_terminal(self, session_id, terminal_id, **kwargs):
        return ReleaseTerminalResponse()


def test_acp_session_runs_fli_and_bash_inside_client_workspace(monkeypatch, tmp_path):
    received = []

    class Staging(BaseHTTPRequestHandler):
        def do_POST(self):
            received.append(
                json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            )
            _, _, _, trial_id, *parts = self.path.split("/")
            if parts == ["start"]:
                response = {
                    "kind": "tool",
                    "action_id": "1",
                    "name": "search_dates",
                    "arguments": {"origin": "SFO"},
                    "release": RELEASE,
                    "mode": "scripted",
                    "model": None,
                }
            elif parts == ["actions", "1", "result"]:
                response = {
                    "kind": "tool",
                    "action_id": "2",
                    "name": "bash",
                    "arguments": {
                        "command": 'printf "%s" "${BRIDGE_TOKEN:-unset}" > report.txt; cat report.txt'
                    },
                }
            else:
                response = {"kind": "done", "text": "complete"}
            body = json.dumps({"trial_id": trial_id, **response}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    with ThreadingHTTPServer(("127.0.0.1", 0), Staging) as server:
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        metadata = tmp_path / "remote-eval.json"
        for key, value in {
            "STAGING_URL": f"http://127.0.0.1:{server.server_port}",
            "BRIDGE_TOKEN": "trial-bridge-token",
            "AGENT_RELEASE": RELEASE,
            "REMOTE_EVAL_METADATA_PATH": str(metadata),
        }.items():
            monkeypatch.setenv(key, value)

        async def run():
            client = LocalTerminal()
            async with spawn_agent_process(
                client,
                sys.executable,
                "-m",
                "remote_eval_adapter",
                env=os.environ.copy(),
                transport_kwargs={"stderr": None},
            ) as (conn, _):
                await conn.initialize(
                    protocol_version=PROTOCOL_VERSION,
                    client_capabilities=ClientCapabilities(terminal=True),
                )
                session = await conn.new_session(
                    cwd=str(tmp_path),
                    mcp_servers=[
                        McpServerStdio(
                            name="fli",
                            command=sys.executable,
                            args=["-c", MCP_SERVER],
                            env=[],
                        )
                    ],
                )
                response = await conn.prompt(
                    session_id=session.session_id, prompt=[text_block("do the task")]
                )
                assert response.stop_reason == "end_turn"
                assert [
                    update.title
                    for update in client.updates
                    if update.session_update == "tool_call"
                ] == ["search_dates", "bash"]
                search_update = next(
                    update
                    for update in client.updates
                    if update.session_update == "tool_call_update"
                    and update.tool_call_id == "1"
                )
                assert json.loads(search_update.content[0].content.text)["dates"] == []
                return session.session_id

        try:
            trial_id = asyncio.run(run())
        finally:
            server.shutdown()
            thread.join()

    assert received == [
        {"prompt": "do the task", "release": RELEASE},
        {"result": {"success": True, "dates": [], "origin": "SFO"}},
        {"result": {"exit_code": 0, "output": "unset", "truncated": False}},
    ]
    assert (tmp_path / "report.txt").read_text() == "unset"
    trace = json.loads(metadata.read_text())
    assert (trace["status"], trace["trial_id"], trace["release"]) == (
        "completed",
        trial_id,
        RELEASE,
    )
    assert trace["actions"] == [
        {"action_id": "1", "name": "search_dates", "status": "completed"},
        {"action_id": "2", "name": "bash", "status": "completed"},
    ]
    assert "trial-bridge-token" not in metadata.read_text()
