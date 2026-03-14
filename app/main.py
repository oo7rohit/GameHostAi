from fastapi import FastAPI
import logging
from contextlib import asynccontextmanager

from app.core.config import settings
from app.core.redis import redis_client, check_redis_connection
from app.api.websockets import router as websocket_router

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
        
    yield
    # Shutdown
    await redis_client.aclose()


app = FastAPI(
    title=settings.PROJECT_NAME,
    lifespan=lifespan
)

app.include_router(websocket_router)

@app.get("/")
async def root():
    return {"message": "GameHostAI Core Engine Base Backend Running"}
