from unittest.mock import AsyncMock, patch

import pytest

from app.api.websockets import _build_room_payload
from app.schemas.session import GameName, RoomInfo


@pytest.mark.asyncio
async def test_build_room_payload_includes_players_from_redis_state() -> None:
    room = RoomInfo(id="room-1", game_name=GameName.MAFIA)
    fake_state = {
        "uuid-1": {"player_name": "Rohit", "is_speaker": True, "status": "online"},
        "uuid-2": {"player_name": "Mohit", "is_speaker": False, "status": "online"},
    }

    with patch("app.api.websockets.get_room_state", new=AsyncMock(return_value=fake_state)):
        payload = await _build_room_payload(room)

    assert payload["id"] == "room-1"
    assert payload["game_name"] == "Mafia"
    assert {"player_id": "uuid-1", "player_name": "Rohit", "is_speaker": True} in payload["players"]
    assert {"player_id": "uuid-2", "player_name": "Mohit", "is_speaker": False} in payload["players"]

