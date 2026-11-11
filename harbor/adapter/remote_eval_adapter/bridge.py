from __future__ import annotations

import asyncio
import json
import os
import re
from collections.abc import Awaitable, Callable
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import httpx

type JsonObject = dict[str, Any]

TOOL_NAMES = {"bash", "search_dates", "search_flights"}
RETRYABLE_STATUS = {429, 502, 503, 504}
ACTION_ID = re.compile(r"[A-Za-z0-9_-]{1,128}")
ERROR_CODE = re.compile(r"[a-z][a-z0-9_]{0,63}")


class BridgeError(Exception):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class Settings:
    url: str
    token: str = field(repr=False)
    release: str
    metadata_path: Path

    @classmethod
    def from_env(cls) -> Settings:
        url, token, release = (
            os.environ.get(key, "").strip()
            for key in ("STAGING_URL", "BRIDGE_TOKEN", "AGENT_RELEASE")
        )
        if not (url and token and release):
            raise BridgeError(
                "endpoint_config", "Set STAGING_URL, BRIDGE_TOKEN, and AGENT_RELEASE."
            )
        endpoint = urlsplit(url)
        loopback = endpoint.hostname in {"localhost", "127.0.0.1", "::1"}
        if (
            not endpoint.hostname
            or (
                endpoint.scheme != "https"
                and not (loopback and endpoint.scheme == "http")
            )
            or endpoint.username is not None
            or endpoint.query
            or endpoint.fragment
            or endpoint.path not in {"", "/"}
        ):
            raise BridgeError(
                "endpoint_config",
                "STAGING_URL must be an HTTPS origin or local HTTP origin.",
            )
        metadata_path = Path(
            os.getenv("REMOTE_EVAL_METADATA_PATH", "/logs/artifacts/remote-eval.json")
        )
        return cls(url.rstrip("/"), token, release, metadata_path)


@dataclass
class Trace:
    trial_id: str
    release: str
    model: str | None = None
    provider: str | None = None
    mode: str | None = None
    status: str = "running"
    error_code: str | None = None
    actions: list[dict[str, str]] = field(default_factory=list)
    started_at: str = field(default_factory=lambda: datetime.now(UTC).isoformat())
    finished_at: str | None = None

    def write(self, path: Path) -> None:
        self.finished_at = datetime.now(UTC).isoformat()
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_suffix(f".{self.trial_id}.tmp")
        temporary.write_text(
            json.dumps(asdict(self), indent=2) + "\n", encoding="utf-8"
        )
        temporary.replace(path)


@dataclass(frozen=True)
class ToolAction:
    action_id: str
    name: str
    arguments: JsonObject

    @classmethod
    def from_response(cls, response: JsonObject) -> ToolAction:
        action_id = response.get("action_id")
        name = response.get("name")
        arguments = response.get("arguments")
        if (
            not isinstance(action_id, str)
            or not ACTION_ID.fullmatch(action_id)
            or not isinstance(name, str)
            or name not in TOOL_NAMES
            or not isinstance(arguments, dict)
        ):
            raise BridgeError(
                "endpoint_protocol", "Staging returned an invalid tool action."
            )
        return cls(action_id, name, arguments)


class Bridge:
    def __init__(
        self, client: httpx.AsyncClient, settings: Settings, trace: Trace
    ) -> None:
        self.client = client
        self.settings = settings
        self.trace = trace
        self.run_path = f"{settings.url}/v1/evals/{trace.trial_id}"

    async def _post(self, path: str, body: JsonObject) -> JsonObject:
        for attempt in range(3):
            try:
                response = await self.client.post(
                    f"{self.run_path}{path}",
                    headers={"Authorization": f"Bearer {self.settings.token}"},
                    json=body,
                )
            except httpx.TransportError:
                if attempt == 2:
                    raise BridgeError(
                        "endpoint_unreachable", "Could not reach staging over HTTPS."
                    ) from None
            else:
                if response.status_code not in RETRYABLE_STATUS or attempt == 2:
                    break
            await asyncio.sleep(0.25 * (attempt + 1))

        if not response.is_success:
            code = {
                401: "endpoint_auth",
                403: "endpoint_auth",
                409: "endpoint_conflict",
            }.get(response.status_code, "endpoint_http")
            raise BridgeError(code, f"Staging returned HTTP {response.status_code}.")
        try:
            data = response.json()
        except ValueError:
            raise BridgeError(
                "endpoint_protocol", "Staging returned invalid JSON."
            ) from None
        if not isinstance(data, dict) or data.get("trial_id") != self.trace.trial_id:
            raise BridgeError(
                "endpoint_protocol", "Staging response did not identify this trial."
            )
        if "release" in data and data["release"] != self.settings.release:
            raise BridgeError("endpoint_release", "Staging served a different release.")
        return data

    async def run(
        self, prompt: str, execute: Callable[[ToolAction], Awaitable[JsonObject]]
    ) -> str:
        response = await self._post(
            "/start", {"prompt": prompt, "release": self.settings.release}
        )
        for field_name in ("model", "provider", "mode"):
            value = response.get(field_name)
            if isinstance(value, str):
                setattr(self.trace, field_name, value)

        completed: dict[str, tuple[ToolAction, JsonObject]] = {}
        for _ in range(33):
            kind = response.get("kind")
            if kind == "done" and isinstance(response.get("text"), str):
                return response["text"]
            if kind == "error":
                code = response.get("code")
                message = response.get("message")
                if (
                    isinstance(code, str)
                    and ERROR_CODE.fullmatch(code)
                    and isinstance(message, str)
                ):
                    raise BridgeError(code, message[:300])
            if kind != "tool":
                raise BridgeError(
                    "endpoint_protocol",
                    "Staging returned an invalid action or stop response.",
                )

            action = ToolAction.from_response(response)
            previous = completed.get(action.action_id)
            if previous is not None:
                if previous[0] != action:
                    raise BridgeError(
                        "endpoint_protocol",
                        "Staging reused an action ID for different input.",
                    )
                result = previous[1]
            else:
                record = {
                    "action_id": action.action_id,
                    "name": action.name,
                    "status": "failed",
                }
                self.trace.actions.append(record)
                result = await execute(action)
                record["status"] = "completed"
                completed[action.action_id] = (action, result)
            response = await self._post(
                f"/actions/{action.action_id}/result", {"result": result}
            )

        raise BridgeError("endpoint_protocol", "Staging exceeded the action limit.")
