import asyncio
import json
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from uuid import uuid4

import httpx
from acp import (
    PROTOCOL_VERSION,
    RequestError,
    run_agent,
    start_tool_call,
    text_block,
    tool_content,
    update_agent_message_text,
    update_tool_call,
)
from acp.interfaces import Client
from acp.schema import (
    HttpMcpServer,
    Implementation,
    InitializeResponse,
    McpServerStdio,
    NewSessionResponse,
    PromptResponse,
    SseMcpServer,
    TextContentBlock,
)

from .bridge import Bridge, BridgeError, JsonObject, Settings, ToolAction, Trace
from .tools import TrialTools


@dataclass(frozen=True)
class Session:
    cwd: Path
    fli_server: McpServerStdio | None


class RemoteFlightAgent:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.client: Client
        self.sessions: dict[str, Session] = {}
        self.active: dict[str, asyncio.Task] = {}

    def on_connect(self, conn: Client) -> None:
        self.client = conn

    async def initialize(
        self, protocol_version: int, **kwargs: Any
    ) -> InitializeResponse:
        return InitializeResponse(
            protocol_version=PROTOCOL_VERSION,
            agent_info=Implementation(name="remote-flight-agent", version="0.1.0"),
        )

    async def new_session(
        self,
        cwd: str,
        mcp_servers: list[HttpMcpServer | SseMcpServer | McpServerStdio] | None = None,
        **kwargs: Any,
    ) -> NewSessionResponse:
        session_id = str(uuid4())
        workspace = Path(cwd)
        if not workspace.is_absolute() or not workspace.is_dir():
            raise RequestError.invalid_params(
                {"message": "cwd must be an existing absolute directory"}
            )
        fli_server = next(
            (
                server
                for server in mcp_servers or []
                if isinstance(server, McpServerStdio) and server.name == "fli"
            ),
            None,
        )
        self.sessions[session_id] = Session(workspace, fli_server)
        return NewSessionResponse(session_id=session_id)

    async def prompt(
        self, session_id: str, prompt: list[Any], **kwargs: Any
    ) -> PromptResponse:
        session = self.sessions.pop(session_id, None)
        if session is None:
            raise RequestError.invalid_params(
                {"message": "Expected a fresh trial session"}
            )
        text = "\n".join(
            block.text for block in prompt if isinstance(block, TextContentBlock)
        )
        if not text.strip():
            raise RequestError.invalid_params(
                {"message": "The trial prompt must contain text"}
            )
        trace = Trace(trial_id=session_id, release=self.settings.release)
        self.active[session_id] = asyncio.current_task()

        try:
            async with asyncio.timeout(240):
                response = await self._run(session_id, session, text, trace)
            await self.client.session_update(
                session_id=session_id,
                update=update_agent_message_text(response),
            )
            trace.status = "completed"
            return PromptResponse(stop_reason="end_turn")
        except asyncio.CancelledError:
            trace.status = "cancelled"
            return PromptResponse(stop_reason="cancelled")
        except TimeoutError:
            trace.status = "error"
            trace.error_code = "bridge_timeout"
            raise RequestError.internal_error(
                {"code": trace.error_code, "message": "Remote trial timed out."}
            ) from None
        except BridgeError as exc:
            trace.status = "error"
            trace.error_code = exc.code
            raise RequestError.internal_error(
                {"code": exc.code, "message": str(exc)}
            ) from None
        except Exception:
            trace.status = "error"
            trace.error_code = "adapter_error"
            raise RequestError.internal_error(
                {"code": trace.error_code, "message": "ACP adapter failed."}
            ) from None
        finally:
            self.active.pop(session_id, None)
            trace.write(self.settings.metadata_path)

    async def cancel(self, session_id: str, **kwargs: Any) -> None:
        task = self.active.get(session_id)
        if task is not None:
            task.cancel()

    async def _run(
        self, session_id: str, session: Session, text: str, trace: Trace
    ) -> str:
        tools = TrialTools(self.client, session_id, session.cwd, session.fli_server)

        async def execute(action: ToolAction) -> JsonObject:
            await self.client.session_update(
                session_id=session_id,
                update=start_tool_call(
                    tool_call_id=action.action_id,
                    title=action.name,
                    kind="other",  # Harbor 0.25 uses the title for this kind.
                    status="in_progress",
                    raw_input=action.arguments,
                ),
            )
            try:
                result = await tools.execute(action)
            except BridgeError as exc:
                await self.client.session_update(
                    session_id=session_id,
                    update=update_tool_call(
                        tool_call_id=action.action_id,
                        status="failed",
                        content=[
                            tool_content(text_block(json.dumps({"error": exc.code})))
                        ],
                    ),
                )
                raise
            await self.client.session_update(
                session_id=session_id,
                update=update_tool_call(
                    tool_call_id=action.action_id,
                    status="completed",
                    content=[
                        tool_content(text_block(json.dumps(result, ensure_ascii=False)))
                    ],
                ),
            )
            return result

        try:
            async with httpx.AsyncClient(
                timeout=httpx.Timeout(60, connect=10), follow_redirects=False
            ) as http:
                return await Bridge(http, self.settings, trace).run(text, execute)
        finally:
            with suppress(Exception):
                await tools.close()


def main() -> None:
    try:
        settings = Settings.from_env()
    except BridgeError as exc:
        raise SystemExit(str(exc)) from None
    asyncio.run(run_agent(RemoteFlightAgent(settings)))


if __name__ == "__main__":
    main()
