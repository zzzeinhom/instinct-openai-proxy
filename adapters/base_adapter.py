"""Unified abstract base adapter for stateful messaging platforms."""

from abc import ABC, abstractmethod
from collections.abc import AsyncIterator


class BaseAdapter(ABC):
    """Abstract interface implemented by all messaging platform adapters."""

    @abstractmethod
    async def startup(self) -> None:
        """Initialize client resources and verify platform readiness."""

    @abstractmethod
    async def shutdown(self) -> None:
        """Gracefully close underlying platform clients and clean up state."""

    @abstractmethod
    async def send_message(self, prompt: str) -> str:
        """Send a prompt to the upstream bot and return the combined reply."""

    async def stream_message(self, prompt: str) -> AsyncIterator[str]:
        """Yield incremental text deltas as the upstream bot streams its reply."""
        reply = await self.send_message(prompt=prompt)
        if reply:
            yield reply
