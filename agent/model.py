from typing import Any

import httpx

DEFAULT_MODEL = "deepseek/deepseek-v4.1-flash-1017"


class InferenceError(RuntimeError):
    pass


class OpenRouter:
    def __init__(self, client: httpx.AsyncClient, api_key: str, model: str):
        self.client = client
        self.api_key = api_key
        self.model = model

    async def __call__(
        self, messages: list[dict[str, Any]], tools: list[dict[str, Any]]
    ) -> dict[str, Any]:
        try:
            response = await self.client.post(
                "https://openrouter.ai/api/v1/chat/completions",
                headers={"Authorization": f"Bearer {self.api_key}"},
                json={
                    "model": self.model,
                    "messages": messages,
                    "tools": tools,
                    "max_tokens": 2048,
                    "reasoning": {"effort": "low"},
                },
            )
            response.raise_for_status()
            choice = response.json()["choices"][0]
            finish_reason = choice.get("finish_reason")
            if finish_reason in ("length", "content_filter", "error"):
                raise InferenceError(
                    f"OpenRouter stopped the completion ({finish_reason})."
                )
            message = choice["message"]
            if not isinstance(message, dict) or message.get("role") != "assistant":
                raise ValueError("Expected an assistant message")
            content = message.get("content")
            calls = message.get("tool_calls")
            if content is not None and not isinstance(content, str):
                raise ValueError("Expected text content")
            if calls is not None and not isinstance(calls, list):
                raise ValueError("Expected a list of tool calls")
            if not calls and (content is None or finish_reason == "tool_calls"):
                raise ValueError("Expected text or tool calls")
            for call in calls or []:
                function = call["function"]
                if not all(
                    isinstance(value, str)
                    for value in (
                        call["id"],
                        function["name"],
                        function["arguments"],
                    )
                ):
                    raise ValueError("Expected tool call ID, name, and arguments")
        except httpx.HTTPStatusError as exc:
            raise InferenceError(
                f"OpenRouter returned HTTP {exc.response.status_code}."
            ) from None
        except httpx.RequestError:
            raise InferenceError("Could not reach OpenRouter.") from None
        except AttributeError, ValueError, KeyError, IndexError, TypeError:
            raise InferenceError("OpenRouter returned an invalid completion.") from None

        return {
            key: message[key]
            for key in (
                "role",
                "content",
                "tool_calls",
                "reasoning",
                "reasoning_details",
            )
            if key in message
        }
