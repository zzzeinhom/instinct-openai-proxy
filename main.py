"""FastAPI application exposing an OpenAI-compatible proxy over messaging bots."""

from contextlib import asynccontextmanager
import logging
import secrets
import time
from typing import Any

from fastapi import APIRouter, Depends, FastAPI, Header, HTTPException, Request, status
from fastapi.responses import StreamingResponse
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
import uvicorn

from adapters import BaseAdapter, TelegramAdapter, WhatsAppAdapter
from config import settings
from core import ChatCompletionRequest, ChatCompletionResponse, RequestQueueManager

logging.basicConfig(
    level=getattr(logging, settings.LOG_LEVEL.upper(), logging.INFO),
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
)
logger = logging.getLogger(__name__)

_bearer_scheme = HTTPBearer(auto_error=False)


def _create_adapter() -> BaseAdapter:
    platform = settings.ACTIVE_PLATFORM.upper()
    if platform == "TELEGRAM":
        return TelegramAdapter(settings=settings)
    if platform == "WHATSAPP":
        return WhatsAppAdapter(settings=settings)
    raise ValueError(f"Unsupported ACTIVE_PLATFORM: {settings.ACTIVE_PLATFORM}")


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Manage platform adapter startup and clean shutdown."""
    adapter = _create_adapter()
    await adapter.startup()
    app.state.adapter = adapter
    app.state.queue_manager = RequestQueueManager(adapter=adapter)
    logger.info("Proxy initialized with platform=%s", settings.ACTIVE_PLATFORM)
    try:
        yield
    finally:
        await adapter.shutdown()


async def verify_api_key(
    credentials: HTTPAuthorizationCredentials | None = Depends(_bearer_scheme),
) -> None:
    """Enforce Bearer token authentication on /v1/* routes when PROXY_API_KEY is set."""
    expected_key = settings.PROXY_API_KEY
    if not expected_key:
        return

    if (
        credentials is None
        or credentials.scheme.lower() != "bearer"
        or not secrets.compare_digest(credentials.credentials, expected_key)
    ):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or missing Bearer API key.",
            headers={"WWW-Authenticate": "Bearer"},
        )


app = FastAPI(
    title="instinct-openai-proxy",
    version="0.3.0",
    lifespan=lifespan,
)

v1_router = APIRouter(
    prefix="/v1",
    dependencies=[Depends(verify_api_key)],
)


@v1_router.get("/models")
async def list_models() -> dict[str, Any]:
    """Return a minimal OpenAI-compatible model listing for client compatibility."""
    return {
        "object": "list",
        "data": [
            {
                "id": "instinct",
                "object": "model",
                "created": int(time.time()),
                "owned_by": "instinct-openai-proxy",
            }
        ],
    }


@v1_router.post("/chat/completions", response_model=None)
async def create_chat_completion(
    payload: ChatCompletionRequest,
    request: Request,
) -> ChatCompletionResponse | StreamingResponse:
    """Process a streaming or non-streaming OpenAI chat completion request."""
    queue_manager: RequestQueueManager | None = getattr(
        request.app.state, "queue_manager", None
    )
    if queue_manager is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Proxy queue manager is not initialized.",
        )

    if payload.stream:
        try:
            queue_manager.aggregate_messages(payload.messages)
        except ValueError as exc:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=str(exc),
            ) from exc

        return StreamingResponse(
            queue_manager.stream_chat_completion(payload),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache",
                "Connection": "keep-alive",
                "X-Accel-Buffering": "no",
            },
        )

    try:
        return await queue_manager.process_request(payload)
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=str(exc),
        ) from exc
    except TimeoutError as exc:
        raise HTTPException(
            status_code=status.HTTP_504_GATEWAY_TIMEOUT,
            detail=str(exc),
        ) from exc
    except RuntimeError as exc:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=str(exc),
        ) from exc
    except Exception as exc:
        logger.exception("Unhandled error while processing chat completion")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Internal proxy error while processing request.",
        ) from exc


app.include_router(v1_router)


@app.post("/webhooks/whatsapp")
async def whatsapp_webhook(
    payload: dict[str, Any],
    request: Request,
    x_webhook_secret: str | None = Header(default=None, alias="X-Webhook-Secret"),
) -> dict[str, str]:
    """Receive inbound WAHA webhook events outside /v1/* Bearer auth."""
    expected_secret = settings.WAHA_WEBHOOK_SECRET
    if expected_secret and (
        x_webhook_secret is None
        or not secrets.compare_digest(x_webhook_secret, expected_secret)
    ):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or missing webhook secret.",
        )

    adapter = getattr(request.app.state, "adapter", None)
    if isinstance(adapter, WhatsAppAdapter):
        await adapter.handle_webhook_payload(payload)
    return {"status": "ok"}


@app.get("/health")
async def health_check() -> dict[str, str]:
    """Basic health endpoint outside /v1/* auth scope."""
    return {"status": "ok", "platform": settings.ACTIVE_PLATFORM}


if __name__ == "__main__":
    uvicorn.run(
        "main:app",
        host=settings.HOST,
        port=settings.PORT,
        reload=False,
    )
