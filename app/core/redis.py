import json
import logging
from typing import Dict, Any
import redis.asyncio as redis
from app.core.config import settings
from app.schemas.session import GameName, PlayerSession, RoomInfo

logger = logging.getLogger(__name__)

# Global Redis connection pool
redis_client = redis.from_url(settings.REDIS_URL, decode_responses=True)

async def check_redis_connection() -> bool:
    try:
        return await redis_client.ping()
    except Exception:
        return False

async def set_player_online(
    room_id: str,
    player: PlayerSession,
    conn_id: str,
):
    """Stores the player state as a JSON string in a Redis Hash linked to the Room ID."""
    state = {"status": "online", "conn_id": conn_id} | player.model_dump(mode="json", exclude_none=True)
    hash_key = f"room:{room_id}:players"
    await redis_client.hset(hash_key, player.player_id, json.dumps(state))
    logger.info(f"Stored player {player.player_id} state in Redis for room {room_id}")

async def set_player_offline(room_id: str, player_id: str, conn_id: str):
    """Updates the player state to offline in Redis without removing them from the Room."""
    hash_key = f"room:{room_id}:players"
    
    # Retrieve existing state to preserve other metadata like is_speaker
    existing_state_raw = await redis_client.hget(hash_key, player_id)
    state = {}
    if existing_state_raw:
        try:
            state = json.loads(existing_state_raw)
        except json.JSONDecodeError:
            pass

    # Guard against stale disconnect: only mark offline if the connection ids match.
    existing_conn_id = state.get("conn_id")
    if isinstance(existing_conn_id, str) and existing_conn_id != conn_id:
        return
            
    state["status"] = "offline"
    await redis_client.hset(hash_key, player_id, json.dumps(state))
    logger.info(f"Marked player {player_id} offline in Redis for room {room_id}")

async def get_room_state(room_id: str) -> Dict[str, Any]:
    """Returns the current players in a room and their statuses."""
    hash_key = f"room:{room_id}:players"
    raw_data = await redis_client.hgetall(hash_key)
    
    room_state = {}
    for player_id, state_json in raw_data.items():
        try:
            room_state[player_id] = json.loads(state_json)
        except json.JSONDecodeError:
            room_state[player_id] = state_json
            
    return room_state


# ---------------------------------------------------------------------------
# Room metadata
# ---------------------------------------------------------------------------

async def set_room_meta(room: RoomInfo) -> None:
    """Persist room metadata used to keep room configuration stable across reconnects."""
    key = f"room:{room.id}:meta"
    await redis_client.set(key, room.model_dump_json(exclude_none=True))


async def get_room_meta(room_id: str) -> RoomInfo | None:
    """Return persisted room metadata, if any.

    Backwards-compatible with older stored shapes like ``{"game_name": "Mafia"}``.
    """
    key = f"room:{room_id}:meta"
    raw = await redis_client.get(key)
    if raw is None:
        return None
    assert isinstance(raw, str)
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        logger.warning("Corrupted room meta in Redis for room %s", room_id)
        return None
    try:
        if isinstance(parsed, dict) and "id" not in parsed:
            parsed = {"id": room_id, **parsed}
        return RoomInfo.model_validate(parsed)
    except Exception:
        logger.warning("Invalid room meta in Redis for room %s", room_id)
        return None


async def set_room_game_name(room_id: str, game_name: GameName) -> None:
    """Convenience wrapper for setting the room's game selection."""
    await set_room_meta(RoomInfo(id=room_id, game_name=game_name))


async def get_room_game_name(room_id: str) -> GameName | None:
    """Convenience wrapper for retrieving the room's game selection."""
    meta = await get_room_meta(room_id)
    return None if meta is None else meta.game_name


# ---------------------------------------------------------------------------
# Game state persistence
# ---------------------------------------------------------------------------

async def save_game_state(room_id: str, state: Dict[str, Any]) -> None:
    """Serialise and persist the authoritative game state to Redis."""
    key = f"room:{room_id}:game_state"
    await redis_client.set(key, json.dumps(state))
    logger.info(f"Game state saved to Redis for room {room_id}")


async def load_game_state(room_id: str) -> Dict[str, Any] | None:
    """Load the authoritative game state from Redis.

    Returns ``None`` if no state exists for the room.
    """
    key = f"room:{room_id}:game_state"
    raw = await redis_client.get(key)
    if raw is None:
        return None
    assert isinstance(raw, str)
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        logger.error(f"Corrupted game state in Redis for room {room_id}")
        return None
