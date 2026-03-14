from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
import asyncio
import logging
from contextlib import asynccontextmanager

from app.core.config import settings
from app.core.redis import redis_client, check_redis_connection
from app.core.rabbitmq import rabbitmq_client
from app.api.websockets import router as websocket_router, handle_narration_response

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Startup: Check redis connection
    is_connected = await check_redis_connection()
    if is_connected:
        logger.info("Successfully connected to Redis.")
    else:
        logger.warning("Failed to connect to Redis.")

    # Startup: Connect to RabbitMQ and start response consumer
    consumer_task: asyncio.Task | None = None
    try:
        await rabbitmq_client.connect()
        logger.info("Successfully connected to RabbitMQ.")
        consumer_task = asyncio.create_task(
            rabbitmq_client.consume_narration_responses(handle_narration_response)
        )
        logger.info("Narration response consumer started.")
    except Exception:
        logger.warning("Failed to connect to RabbitMQ — narration pipeline disabled.")

    yield

    # Shutdown: Cancel consumer and close connections
    if consumer_task is not None and not consumer_task.done():
        consumer_task.cancel()
        try:
            await consumer_task
        except asyncio.CancelledError:
            pass
        logger.info("Narration response consumer stopped.")

    await rabbitmq_client.close()
    await redis_client.aclose()


app = FastAPI(
    title=settings.PROJECT_NAME,
    lifespan=lifespan
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:5173", "http://127.0.0.1:5173"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(websocket_router)

@app.get("/")
async def root():
    return {"message": "GameHostAI Core Engine Base Backend Running"}
