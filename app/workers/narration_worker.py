"""Standalone narration worker — runs outside the FastAPI server.

Consumes ``NarrationRequest`` messages from RabbitMQ, generates
template-based narration text (to be replaced with an LLM call later),
and publishes ``NarrationResponse`` messages back.

Run with:
    python -m app.workers.narration_worker
"""

import asyncio
import logging

import aio_pika
from aio_pika import connect_robust, Message, DeliveryMode

from app.core.config import settings
from app.schemas.narration import NarrationRequest, NarrationResponse

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Queue names (must match app.core.rabbitmq)
NARRATION_REQUESTS_QUEUE = "narration_requests"
NARRATION_RESPONSES_QUEUE = "narration_responses"

# ---------------------------------------------------------------------------
# Template-based narration stubs
# ---------------------------------------------------------------------------
NARRATION_TEMPLATES: dict[str, str] = {
    "game_start": "Welcome to the town of Salem. The sun sets on the first day, and darkness creeps in...",
    "night_kill": "The town sleeps uneasily... {killed} was found dead at dawn.",
    "night_save": "The town wakes to find everyone alive. The healer was busy last night.",
    "day_vote": "{eliminated} has been voted out by the town. The crowd watches in silence.",
    "day_no_elimination": "The town could not reach a consensus. No one was eliminated today.",
    "game_over": "The game is over! {winner} wins!",
}

FALLBACK_TEXT = "The story continues..."


def generate_narration_text(request: NarrationRequest) -> str:
    """Produce narration text from a template.

    Falls back to a generic string for unknown event contexts.
    """
    template = NARRATION_TEMPLATES.get(request.event_context)
    if template is None:
        return FALLBACK_TEXT

    try:
        return template.format(**request.context_data)
    except KeyError:
        # context_data missing expected keys — use template as-is
        return template


def generate_audio_url(request: NarrationRequest) -> str | None:
    """Return a placeholder audio file path (stub for future TTS)."""
    return f"/audio/{request.room_id}/turn_{request.turn_number}.wav"


async def process_request(request: NarrationRequest) -> NarrationResponse:
    """Generate a NarrationResponse from a request with fallback handling."""
    try:
        narration_text = generate_narration_text(request)
        audio_url = generate_audio_url(request)
    except Exception:
        logger.exception(
            "Error generating narration for room %s — using fallback",
            request.room_id,
        )
        narration_text = FALLBACK_TEXT
        audio_url = None

    return NarrationResponse(
        room_id=request.room_id,
        narration_text=narration_text,
        audio_url=audio_url,
        turn_number=request.turn_number,
    )


# ---------------------------------------------------------------------------
# Worker loop
# ---------------------------------------------------------------------------

async def run_worker() -> None:
    """Connect to RabbitMQ and consume narration requests indefinitely."""
    connection = await connect_robust(settings.RABBITMQ_URL)
    channel = await connection.channel()

    # QoS: fair dispatch across multiple workers
    await channel.set_qos(prefetch_count=1)

    request_queue = await channel.declare_queue(
        NARRATION_REQUESTS_QUEUE, durable=True
    )
    # Ensure response queue exists
    await channel.declare_queue(NARRATION_RESPONSES_QUEUE, durable=True)

    logger.info("Narration worker started. Waiting for messages...")

    async with request_queue.iterator() as queue_iter:
        async for message in queue_iter:
            async with message.process():
                try:
                    request = NarrationRequest.model_validate_json(message.body)
                    logger.info(
                        "Received request: room=%s ctx=%s turn=%d",
                        request.room_id,
                        request.event_context,
                        request.turn_number,
                    )

                    response = await process_request(request)

                    # Publish response back
                    body = response.model_dump_json().encode()
                    resp_message = Message(
                        body=body, delivery_mode=DeliveryMode.PERSISTENT
                    )
                    await channel.default_exchange.publish(
                        resp_message, routing_key=NARRATION_RESPONSES_QUEUE
                    )
                    logger.info(
                        "Published response for room %s turn %d",
                        response.room_id,
                        response.turn_number,
                    )
                except Exception:
                    logger.exception("Failed to process narration request")


if __name__ == "__main__":
    asyncio.run(run_worker())
