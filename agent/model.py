from typing import Any

import httpx

DEFAULT_MODEL = "openai/gpt-5.6-luna"


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
            message = response.json()["choices"][0]["message"]
            if not isinstance(message, dict) or message.get("role") != "assistant":
                raise ValueError("Expected an assistant message")
        except httpx.HTTPStatusError as exc:
            raise InferenceError(
                f"OpenRouter returned HTTP {exc.response.status_code}."
            ) from None
        except httpx.RequestError:
            raise InferenceError("Could not reach OpenRouter.") from None
        except ValueError, KeyError, IndexError, TypeError:
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
