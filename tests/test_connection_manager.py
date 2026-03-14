from unittest.mock import AsyncMock, MagicMock

import pytest

from app.core.connection_manager import ConnectionManager
from app.schemas.session import PlayerSession
from starlette.websockets import WebSocketDisconnect


@pytest.mark.asyncio
async def test_connection_manager_stores_player_session() -> None:
    mgr = ConnectionManager()
    ws = MagicMock()
    ws.accept = AsyncMock()

    player = PlayerSession(player_id="p1", player_name="Alice", is_speaker=False, level=3)
    await mgr.connect(ws, "room-1", player)

    assert "room-1" in mgr.active_rooms
    assert "p1" in mgr.active_rooms["room-1"]
    assert mgr.active_rooms["room-1"]["p1"]["socket"] is ws
    assert mgr.active_rooms["room-1"]["p1"]["player"] == player


def test_get_speaker_socket_uses_player_session() -> None:
    mgr = ConnectionManager()
    speaker_ws = MagicMock()
    other_ws = MagicMock()

    mgr.active_rooms = {
        "room-1": {
            "p1": {"socket": other_ws, "player": PlayerSession(player_id="p1", player_name="Bob", is_speaker=False)},
            "p2": {"socket": speaker_ws, "player": PlayerSession(player_id="p2", player_name="Alice", is_speaker=True)},
        }
    }

    assert mgr.get_speaker_socket("room-1") is speaker_ws


@pytest.mark.asyncio
async def test_broadcast_prunes_disconnected_socket() -> None:
    mgr = ConnectionManager()

    ws = MagicMock()
    ws.send_text = AsyncMock(side_effect=WebSocketDisconnect())

    mgr.active_rooms = {
        "room-1": {
            "p1": {"socket": ws, "player": PlayerSession(player_id="p1", player_name="Alice", is_speaker=False)},
        }
    }

    # Should not raise; should prune the dead connection.
    await mgr.broadcast_to_room("room-1", MagicMock(model_dump_json=lambda: "{}"))
    assert "room-1" not in mgr.active_rooms
