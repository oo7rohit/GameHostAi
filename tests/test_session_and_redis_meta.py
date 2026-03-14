import json
from unittest.mock import AsyncMock, patch

import pytest

from app.core import redis as redis_mod
from app.schemas.session import GameName, PlayerSession, RoomInfo


@pytest.mark.asyncio
async def test_set_player_online_persists_all_player_fields() -> None:
    fake_client = AsyncMock()

    player = PlayerSession(
        player_id="p1",
        player_name="Alice",
        is_speaker=True,
        avatar_url="https://example.test/a.png",  # extra field (future metadata)
    )

    with patch.object(redis_mod, "redis_client", fake_client):
        await redis_mod.set_player_online("room-1", player)

    fake_client.hset.assert_awaited_once()
    hash_key, field, raw_state = fake_client.hset.await_args.args
    assert hash_key == "room:room-1:players"
    assert field == "p1"

    state = json.loads(raw_state)
    assert state["status"] == "online"
    assert state["player_id"] == "p1"
    assert state["player_name"] == "Alice"
    assert state["is_speaker"] is True
    assert state["avatar_url"] == "https://example.test/a.png"


@pytest.mark.asyncio
async def test_set_room_meta_roundtrip() -> None:
    fake_client = AsyncMock()
    room = RoomInfo(id="room-1", game_name=GameName.MAFIA, region="ap-south-1")

    with patch.object(redis_mod, "redis_client", fake_client):
        await redis_mod.set_room_meta(room)

    fake_client.set.assert_awaited_once()
    key, raw = fake_client.set.await_args.args
    assert key == "room:room-1:meta"
    payload = json.loads(raw)
    assert payload["id"] == "room-1"
    assert payload["game_name"] == "Mafia"
    assert payload["region"] == "ap-south-1"


@pytest.mark.asyncio
async def test_get_room_meta_backwards_compatible_legacy_shape() -> None:
    fake_client = AsyncMock()
    fake_client.get = AsyncMock(return_value=json.dumps({"game_name": "Mafia"}))

    with patch.object(redis_mod, "redis_client", fake_client):
        meta = await redis_mod.get_room_meta("room-legacy")

    assert meta is not None
    assert meta.id == "room-legacy"
    assert meta.game_name == GameName.MAFIA

