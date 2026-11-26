"""Concurrency control, prompt extraction, and SSE streaming orchestration."""

import asyncio
from collections.abc import AsyncIterator
import json
import logging
import time
import uuid

from adapters.base_adapter import BaseAdapter
from core.models import ChatCompletionRequest, ChatCompletionResponse, Choice, Message, Usage

logger = logging.getLogger(__name__)


def _estimate_tokens(text: str) -> int:
    """Roughly estimate token count when upstream platform does not report usage."""
    stripped = text.strip()
    if not stripped:
        return 0
    return max(1, len(stripped) // 4)


def _format_sse_chunk(
    completion_id: str,
    created: int,
    model: str,
    delta: dict[str, str],
    finish_reason: str | None = None,
) -> str:
    payload = {
        "id": completion_id,
        "object": "chat.completion.chunk",
        "created": created,
        "model": model,
        "choices": [
            {
                "index": 0,
                "delta": delta,
                "logprobs": None,
                "finish_reason": finish_reason,
            }
        ],
    }
    return f"data: {json.dumps(payload)}\n\n"


class RequestQueueManager:
    """Serializes requests with an asyncio.Lock so upstream replies never interleave."""

    def __init__(self, adapter: BaseAdapter) -> None:
        self._adapter = adapter
        self._lock = asyncio.Lock()

    def aggregate_messages(self, messages: list[Message]) -> str:
        """Extract only the latest user prompt for stateful memory-backed Instinct."""
        for msg in reversed(messages):
            if msg.role == "user":
                text = msg.extract_text().strip()
                if text:
                    return text

        for msg in reversed(messages):
            text = msg.extract_text().strip()
            if text:
                return text

        raise ValueError(
            "At least one message with non-empty text content is required."
        )

    async def process_request(
        self, request: ChatCompletionRequest
    ) -> ChatCompletionResponse:
        """Serialize execution through the adapter and return an OpenAI response."""
        prompt = self.aggregate_messages(request.messages)

        async with self._lock:
            logger.info("Processing queued request (model=%s)", request.model)
            reply_text = await self._adapter.send_message(prompt=prompt)

        prompt_tokens = _estimate_tokens(prompt)
        completion_tokens = _estimate_tokens(reply_text)
        usage = Usage(
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            total_tokens=prompt_tokens + completion_tokens,
        )

        return ChatCompletionResponse(
            model=request.model,
            choices=[
                Choice(
                    index=0,
                    message=Message(role="assistant", content=reply_text),
                    finish_reason="stop",
                )
            ],
            usage=usage,
        )

    async def stream_chat_completion(
        self, request: ChatCompletionRequest
    ) -> AsyncIterator[str]:
        """Stream OpenAI chat.completion.chunk SSE frames under self._lock."""
        prompt = self.aggregate_messages(request.messages)
        completion_id = f"chatcmpl-{uuid.uuid4().hex}"
        created = int(time.time())

        async with self._lock:
            logger.info("Streaming queued request (model=%s)", request.model)
            sent_role = False
            try:
                async for delta in self._adapter.stream_message(prompt=prompt):
                    if not delta:
                        continue
                    delta_obj: dict[str, str] = {"content": delta}
                    if not sent_role:
                        delta_obj = {"role": "assistant", "content": delta}
                        sent_role = True
                    yield _format_sse_chunk(
                        completion_id=completion_id,
                        created=created,
                        model=request.model,
                        delta=delta_obj,
                        finish_reason=None,
                    )

                yield _format_sse_chunk(
                    completion_id=completion_id,
                    created=created,
                    model=request.model,
                    delta={},
                    finish_reason="stop",
                )
                yield "data: [DONE]\n\n"
            except (TimeoutError, RuntimeError, ValueError) as exc:
                logger.warning("Streaming request failed: %s", exc)
                err_payload = {
                    "error": {
                        "message": str(exc),
                        "type": "proxy_error",
                    }
                }
                yield f"data: {json.dumps(err_payload)}\n\n"
                yield "data: [DONE]\n\n"
