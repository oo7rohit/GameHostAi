"""Async RabbitMQ client wrapper using aio-pika.

Provides publish / consume helpers for the narration pipeline and
connection lifecycle methods wired into the FastAPI lifespan.
"""

import asyncio
import logging
from typing import Any, Awaitable, Callable

import aio_pika
from aio_pika import connect_robust, Message, DeliveryMode
from aio_pika.abc import AbstractRobustConnection, AbstractChannel, AbstractQueue

from app.core.config import settings
from app.schemas.narration import NarrationRequest, NarrationResponse

logger = logging.getLogger(__name__)

# Queue names
NARRATION_REQUESTS_QUEUE = "narration_requests"
NARRATION_RESPONSES_QUEUE = "narration_responses"


class RabbitMQClient:
    """Manages a single RabbitMQ connection with publish/consume capabilities."""

    def __init__(self) -> None:
        self._connection: AbstractRobustConnection | None = None
        self._channel: AbstractChannel | None = None

    # ------------------------------------------------------------------ #
    # Lifecycle
    # ------------------------------------------------------------------ #
    async def connect(self) -> None:
        """Open a robust connection and channel."""
        self._connection = await connect_robust(settings.RABBITMQ_URL)
        self._channel = await self._connection.channel()
        # Declare both queues so they exist before any publish/consume
        await self._channel.declare_queue(NARRATION_REQUESTS_QUEUE, durable=True)
        await self._channel.declare_queue(NARRATION_RESPONSES_QUEUE, durable=True)
        logger.info("RabbitMQ connected and queues declared.")

    async def close(self) -> None:
        """Gracefully close the connection."""
        if self._connection and not self._connection.is_closed:
            await self._connection.close()
            logger.info("RabbitMQ connection closed.")

    # ------------------------------------------------------------------ #
    # Publishing
    # ------------------------------------------------------------------ #
    async def publish_narration_request(self, request: NarrationRequest) -> None:
        """Serialise and publish a NarrationRequest to the requests queue."""
        if self._channel is None:
            logger.error("Cannot publish — RabbitMQ channel is not initialised.")
            return

        body = request.model_dump_json().encode()
        message = Message(body=body, delivery_mode=DeliveryMode.PERSISTENT)
        await self._channel.default_exchange.publish(
            message, routing_key=NARRATION_REQUESTS_QUEUE
        )
        logger.info(
            "Published NarrationRequest for room %s (turn %d, ctx=%s)",
            request.room_id,
            request.turn_number,
            request.event_context,
        )

    # ------------------------------------------------------------------ #
    # Consuming
    # ------------------------------------------------------------------ #
    async def consume_narration_responses(
        self,
        callback: Callable[[NarrationResponse], Awaitable[None]],
    ) -> None:
        """Listen on the responses queue and invoke *callback* for each message.

        This method runs indefinitely and is intended to be launched
        as an ``asyncio.Task`` from the FastAPI lifespan.
        """
        if self._channel is None:
            logger.error("Cannot consume — RabbitMQ channel is not initialised.")
            return

        queue: AbstractQueue = await self._channel.declare_queue(
            NARRATION_RESPONSES_QUEUE, durable=True
        )

        async with queue.iterator() as queue_iter:
            async for message in queue_iter:
                async with message.process():
                    try:
                        response = NarrationResponse.model_validate_json(message.body)
                        await callback(response)
                    except Exception:
                        logger.exception("Error processing narration response")


# ---------------------------------------------------------------------------
# Module-level singleton (wired in lifespan)
# ---------------------------------------------------------------------------
rabbitmq_client = RabbitMQClient()
