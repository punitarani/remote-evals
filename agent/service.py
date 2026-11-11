"""Keep inference on staging and delegate tool actions to the Harbor trial."""

import asyncio
import hashlib
import json
import logging
import os
import re
import secrets
import time
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from typing import Annotated, Any, Literal
from uuid import UUID

import httpx
import uvicorn
from fastapi import Depends, FastAPI, Header, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field, ValidationError
from starlette.requests import ClientDisconnect

from agent.harness import ToolBudgetExceeded, run_agent
from agent.model import DEFAULT_MODEL, InferenceError, OpenRouter
from agent.tools import ToolExecutor

logger = logging.getLogger(__name__)
RUN_TIMEOUT = 180
REPLAY_TTL = 300
MAX_SESSIONS = 64
MAX_RESULT_BYTES = 512_000
MAX_REQUEST_BYTES = MAX_RESULT_BYTES + 4096

Payload = dict[str, Any]


@dataclass(frozen=True)
class Settings:
    release: str
    bridge_token: str = field(repr=False)
    mode: Literal["model", "scripted"] = "model"
    model: str = DEFAULT_MODEL
    api_key: str = field(default="", repr=False)

    def __post_init__(self) -> None:
        if not re.fullmatch(r"[0-9a-f]{40}", self.release):
            raise ValueError("AGENT_RELEASE must be a full Git commit SHA.")
        if not self.bridge_token or not self.bridge_token.isascii():
            raise ValueError("Set a dedicated ASCII BRIDGE_TOKEN.")
        if self.mode not in ("model", "scripted"):
            raise ValueError("AGENT_MODE must be model or scripted.")
        if self.mode == "model" and not self.api_key:
            raise ValueError(
                "Set OPENROUTER_API_KEY to start the model-backed service."
            )

    @classmethod
    def from_env(cls) -> "Settings":
        return cls(
            release=os.getenv("AGENT_RELEASE", ""),
            bridge_token=os.getenv("BRIDGE_TOKEN", ""),
            mode=os.getenv("AGENT_MODE", "model"),
            model=os.getenv("AGENT_MODEL", DEFAULT_MODEL),
            api_key=os.getenv("OPENROUTER_API_KEY", ""),
        )

    def public(self) -> dict[str, str]:
        model = self.model if self.mode == "model" else "scripted"
        provider = "openrouter" if self.mode == "model" else "scripted"
        return {
            "release": self.release,
            "model": model,
            "provider": provider,
            "mode": self.mode,
        }


class StartRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    prompt: str = Field(min_length=1, max_length=16_000)
    release: str = Field(pattern=r"^[0-9a-f]{40}$")
    model_name: str | None = Field(default=None, max_length=256)


class ResultRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    result: Payload


async def read_request[Body: BaseModel](request: Request, schema: type[Body]) -> Body:
    body = bytearray()
    try:
        async for chunk in request.stream():
            if len(body) + len(chunk) > MAX_REQUEST_BYTES:
                raise HTTPException(413, "Request body is too large.")
            body.extend(chunk)
    except ClientDisconnect:
        raise HTTPException(400, "Request body was interrupted.") from None
    try:
        return schema.model_validate_json(body)
    except ValidationError:
        raise HTTPException(422, "Invalid request body.") from None


@dataclass
class Action:
    id: str
    result: asyncio.Future[Payload]


@dataclass
class Receipt:
    """Remember the response to an accepted result, even while inference is pending."""

    fingerprint: str
    response: asyncio.Future[Payload]


class TrialExecutor:
    """Suspend one agent run at each tool call until Harbor posts its result."""

    def __init__(self, trial_id: str, settings: Settings):
        self.trial_id = trial_id
        self.settings = settings
        self.initial_response = self.response = (
            asyncio.get_running_loop().create_future()
        )
        self.pending: Action | None = None
        self.receipts: dict[str, Receipt] = {}
        self.terminal: Payload | None = None
        self.finished_at: float | None = None

    def event(self, kind: str, **details: Any) -> Payload:
        return {
            "kind": kind,
            "trial_id": self.trial_id,
            **self.settings.public(),
            **details,
        }

    async def execute(self, name: str, arguments: Payload) -> Payload:
        action_id = str(len(self.receipts) + 1)
        self.pending = Action(action_id, asyncio.get_running_loop().create_future())
        self.response.set_result(
            self.event("tool", action_id=action_id, name=name, arguments=arguments)
        )
        return await self.pending.result

    async def submit(self, action_id: str, result: Payload) -> Payload:
        """Accept a result once. Identical retries share the next event."""
        encoded_result = json.dumps(
            result, sort_keys=True, separators=(",", ":")
        ).encode()
        if len(encoded_result) > MAX_RESULT_BYTES:
            raise HTTPException(413, "Tool result is too large.")
        fingerprint = hashlib.sha256(encoded_result).hexdigest()
        if receipt := self.receipts.get(action_id):
            if receipt.fingerprint != fingerprint:
                raise HTTPException(409, "Conflicting result for this action.")
            return await asyncio.shield(receipt.response)
        if self.pending is None or self.pending.id != action_id:
            raise HTTPException(409, "This action is not pending.")
        if self.terminal is not None:
            return self.terminal

        # Register the next response before waking the agent. Shield it from
        # request cancellation so an HTTP retry can still receive the event.
        self.response = asyncio.get_running_loop().create_future()
        self.receipts[action_id] = Receipt(fingerprint, self.response)
        pending, self.pending = self.pending, None
        pending.result.set_result(result)
        return await asyncio.shield(self.response)

    def finish(self, event: Payload) -> None:
        self.terminal = event
        self.finished_at = time.monotonic()
        if not self.response.done():
            self.response.set_result(event)


@dataclass
class Session:
    prompt: str
    executor: TrialExecutor
    task: asyncio.Task[None]


async def scripted_probe(executor: ToolExecutor) -> str:
    result = await executor.execute(
        "bash",
        {"command": "printf 'bridge-ok\\n' > bridge-probe.txt; cat bridge-probe.txt"},
    )
    if result.get("exit_code") != 0 or result.get("output") != "bridge-ok\n":
        raise RuntimeError("Scripted probe received an unexpected tool result.")
    return "Scripted Bash round trip completed."


async def run_session(executor: TrialExecutor, prompt: str, model: OpenRouter) -> None:
    try:
        async with asyncio.timeout(RUN_TIMEOUT):
            if executor.settings.mode == "scripted":
                text = await scripted_probe(executor)
            else:
                text = await run_agent(prompt, model, executor)
        event = executor.event("done", text=text)
    except InferenceError as exc:
        event = executor.event("error", code="inference", message=str(exc))
    except TimeoutError, ToolBudgetExceeded:
        event = executor.event(
            "error", code="budget", message="The trial exceeded its run budget."
        )
    except Exception as exc:
        logger.error("Trial %s failed with %s", executor.trial_id, type(exc).__name__)
        event = executor.event(
            "error", code="service", message="The staging harness failed."
        )

    executor.finish(event)
    logger.info("Trial %s ended with %s", executor.trial_id, event["kind"])


def create_app(
    settings: Settings, *, model_transport: httpx.AsyncBaseTransport | None = None
) -> FastAPI:
    # Sessions are process-local. Keep staging on one worker so retries find
    # the same run and its cached responses.
    sessions: dict[str, Session] = {}

    def get_session(trial_id: str) -> Session | None:
        now = time.monotonic()
        for key, session in list(sessions.items()):
            finished_at = session.executor.finished_at
            if finished_at is not None and now - finished_at > REPLAY_TTL:
                del sessions[key]
        return sessions.get(trial_id)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        async with httpx.AsyncClient(timeout=40, transport=model_transport) as client:
            app.state.model = OpenRouter(client, settings.api_key, settings.model)
            try:
                yield
            finally:
                tasks = [session.task for session in sessions.values()]
                for task in tasks:
                    task.cancel()
                await asyncio.gather(*tasks, return_exceptions=True)

    app = FastAPI(lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)

    def authorize(authorization: Annotated[str | None, Header()] = None) -> None:
        expected = f"Bearer {settings.bridge_token}".encode()
        if not authorization or not secrets.compare_digest(
            authorization.encode(), expected
        ):
            raise HTTPException(401, "Invalid bridge credential.")

    @app.get("/healthz")
    async def health() -> dict[str, str]:
        return settings.public()

    @app.post("/v1/evals/{trial_id}/start", dependencies=[Depends(authorize)])
    async def start(trial_id: UUID, request: Request) -> Payload:
        body = await read_request(request, StartRequest)
        if body.release != settings.release:
            raise HTTPException(409, "Staging release does not match this trial.")
        if body.model_name is not None and (
            settings.mode != "model"
            or body.model_name != f"openrouter/{settings.model}"
        ):
            raise HTTPException(409, "Staging model does not match this trial.")
        key = str(trial_id)
        if session := get_session(key):
            if body.prompt != session.prompt:
                raise HTTPException(409, "Conflicting start request for this trial.")
        else:
            if len(sessions) >= MAX_SESSIONS:
                raise HTTPException(503, "Staging is at capacity.")
            executor = TrialExecutor(key, settings)
            task = asyncio.create_task(
                run_session(executor, body.prompt, app.state.model)
            )
            session = sessions[key] = Session(body.prompt, executor, task)
        return await asyncio.shield(session.executor.initial_response)

    @app.post(
        "/v1/evals/{trial_id}/actions/{action_id}/result",
        dependencies=[Depends(authorize)],
    )
    async def result(trial_id: UUID, action_id: str, request: Request) -> Payload:
        body = await read_request(request, ResultRequest)
        session = get_session(str(trial_id))
        if session is None:
            raise HTTPException(404, "Staging has no session for this trial.")
        return await session.executor.submit(action_id, body.result)

    return app


def main() -> None:
    try:
        settings = Settings.from_env()
    except ValueError as exc:
        raise SystemExit(str(exc)) from None
    logging.basicConfig(level=logging.INFO)
    uvicorn.run(
        create_app(settings),
        host="127.0.0.1",
        port=int(os.getenv("PORT", "8000")),
        access_log=False,
    )


if __name__ == "__main__":
    main()
