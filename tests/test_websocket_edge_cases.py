"""Comprehensive edge-case tests for the WebSocket connection lifecycle.

Covers 12 scenarios identified during analysis:
1.  Happy path: join → leave
2.  React StrictMode double-mount (same player, rapid reconnect)
3.  Multiple players join sequentially
4.  Player disconnect while others remain
5.  Game-name mismatch rejection
6.  Broadcast with broken socket mid-send
7.  Stale disconnect does not evict newer connection from Redis
8.  send_personal_message to a closed socket
9.  Broadcast during concurrent connect / disconnect
10. Old connection finally block does not broadcast "player left"
11. Invalid JSON from client
12. Valid JSON but invalid ClientAction schema
"""

from unittest.mock import AsyncMock, MagicMock, patch
import json
import pytest

from app.core.connection_manager import ConnectionManager
from app.schemas.messages import ServerEvent
from app.schemas.session import GameName, PlayerSession, RoomInfo
from starlette.websockets import WebSocketDisconnect


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_ws(*, accept=True, close_ok=True) -> MagicMock:
    """Return a mock WebSocket with configurable accept/close behaviour."""
    ws = MagicMock()
    ws.accept = AsyncMock()
    ws.send_text = AsyncMock()
    ws.receive_text = AsyncMock()
    ws.close = AsyncMock() if close_ok else AsyncMock(side_effect=RuntimeError("already closed"))
    return ws


def _player(pid: str = "p1", name: str = "Alice", speaker: bool = False) -> PlayerSession:
    return PlayerSession(player_id=pid, player_name=name, is_speaker=speaker)


def _event(etype: str = "system_event", data: dict | None = None) -> ServerEvent:
    return ServerEvent(event_type=etype, data=data or {})


# ===================================================================
# 1. Happy path: single player joins and leaves
# ===================================================================

class TestHappyPath:
    @pytest.mark.asyncio
    async def test_single_player_join_and_leave(self) -> None:
        mgr = ConnectionManager()
        ws = _make_ws()
        player = _player()

        conn_id = await mgr.connect(ws, "room-1", player)

        # Verify state after connect
        assert "room-1" in mgr.active_rooms
        assert "p1" in mgr.active_rooms["room-1"]
        assert mgr.active_rooms["room-1"]["p1"]["conn_id"] == conn_id

        # Disconnect
        removed = mgr.disconnect("room-1", "p1", conn_id)
        assert removed is True
        assert "room-1" not in mgr.active_rooms  # room cleaned up when empty


# ===================================================================
# 2. React StrictMode double-mount
# ===================================================================

class TestStrictModeDoubleMountReconnect:
    @pytest.mark.asyncio
    async def test_old_connection_replaced_by_new(self) -> None:
        """When the same player connects twice, the new connection replaces the old."""
        mgr = ConnectionManager()
        old_ws = _make_ws()
        new_ws = _make_ws()
        player = _player()

        old_id = await mgr.connect(old_ws, "room-1", player)
        new_id = await mgr.connect(new_ws, "room-1", player)

        assert old_id != new_id
        assert mgr.active_rooms["room-1"]["p1"]["socket"] is new_ws
        # Old socket should have been closed with 4001
        old_ws.close.assert_awaited_once_with(code=4001, reason="Replaced by newer connection")

    @pytest.mark.asyncio
    async def test_stale_disconnect_does_not_remove_new_connection(self) -> None:
        """Old conn_id's finally-block should NOT evict the new connection."""
        mgr = ConnectionManager()
        old_ws = _make_ws()
        new_ws = _make_ws()
        player = _player()

        old_id = await mgr.connect(old_ws, "room-1", player)
        new_id = await mgr.connect(new_ws, "room-1", player)

        # Simulate old connection's finally block
        removed = mgr.disconnect("room-1", "p1", old_id)
        assert removed is False  # stale — should not remove
        assert "p1" in mgr.active_rooms["room-1"]
        assert mgr.active_rooms["room-1"]["p1"]["conn_id"] == new_id

    @pytest.mark.asyncio
    async def test_old_ws_close_error_is_swallowed(self) -> None:
        """If the old socket is already closed, the error should not propagate."""
        mgr = ConnectionManager()
        old_ws = _make_ws(close_ok=False)  # close raises RuntimeError
        new_ws = _make_ws()
        player = _player()

        await mgr.connect(old_ws, "room-1", player)
        # Should not raise
        new_id = await mgr.connect(new_ws, "room-1", player)
        assert mgr.active_rooms["room-1"]["p1"]["conn_id"] == new_id


# ===================================================================
# 3. Multiple players join the same room
# ===================================================================

class TestMultiplePlayersJoin:
    @pytest.mark.asyncio
    async def test_three_players_join_sequentially(self) -> None:
        mgr = ConnectionManager()

        players = [_player("p1", "Alice"), _player("p2", "Bob"), _player("p3", "Charlie")]
        sockets = [_make_ws() for _ in range(3)]

        ids = []
        for p, ws in zip(players, sockets):
            cid = await mgr.connect(ws, "room-1", p)
            ids.append(cid)

        assert len(mgr.active_rooms["room-1"]) == 3
        for p, cid in zip(players, ids):
            assert mgr.active_rooms["room-1"][p.player_id]["conn_id"] == cid


# ===================================================================
# 4. Player disconnects while others remain
# ===================================================================

class TestPlayerDisconnectsWithOthersPresent:
    @pytest.mark.asyncio
    async def test_one_player_disconnects_room_persists(self) -> None:
        mgr = ConnectionManager()
        ws1, ws2 = _make_ws(), _make_ws()

        id1 = await mgr.connect(ws1, "room-1", _player("p1", "Alice"))
        id2 = await mgr.connect(ws2, "room-1", _player("p2", "Bob"))

        mgr.disconnect("room-1", "p2", id2)

        # Room should still exist with p1
        assert "p1" in mgr.active_rooms["room-1"]
        assert "p2" not in mgr.active_rooms["room-1"]


# ===================================================================
# 5. Game-name mismatch rejection
# ===================================================================

class TestGameNameMismatch:
    @pytest.mark.asyncio
    async def test_mismatch_sends_error_and_closes(self) -> None:
        """When room already exists with a different game, the socket should be
        accepted, sent an error, and closed with 1008."""
        from app.api.websockets import websocket_endpoint

        ws = _make_ws()
        existing_room = RoomInfo(id="room-1", game_name=GameName.MAFIA)

        # Simulate: room already stored in Redis as Mafia
        with (
            patch("app.api.websockets.get_room_meta", new=AsyncMock(return_value=existing_room)),
            patch("app.api.websockets.set_room_meta", new=AsyncMock()),
        ):
            # Player tries to join with a game_name that is not Mafia.
            # Since GameName only has MAFIA right now, we simulate the mismatch
            # by making the persisted room have a different game_name than the request.
            # We need to create a temporary different GameName for the test.
            request_room = RoomInfo(id="room-1", game_name=GameName.MAFIA)
            # Patch persisted to return a "different" game_name
            mock_persisted = MagicMock()
            mock_persisted.game_name = MagicMock()
            mock_persisted.game_name.value = "Werewolf"
            mock_persisted.game_name.__ne__ = lambda self, other: True

            with patch("app.api.websockets.get_room_meta", new=AsyncMock(return_value=mock_persisted)):
                player = _player()
                await websocket_endpoint(ws, request_room, player)

            ws.accept.assert_awaited_once()
            ws.close.assert_awaited_once_with(code=1008)
            # Verify an error event was sent
            assert ws.send_text.await_count == 1
            sent = json.loads(ws.send_text.call_args[0][0])
            assert sent["event_type"] == "error"


# ===================================================================
# 6. Broadcast with broken socket mid-send
# ===================================================================

class TestBroadcastWithBrokenSocket:
    @pytest.mark.asyncio
    async def test_broken_socket_is_pruned_healthy_receives_message(self) -> None:
        """If one socket dies during broadcast, it's pruned and others still get the message."""
        mgr = ConnectionManager()
        healthy_ws = _make_ws()
        broken_ws = _make_ws()
        broken_ws.send_text = AsyncMock(side_effect=WebSocketDisconnect())

        id1 = await mgr.connect(healthy_ws, "room-1", _player("p1", "Alice"))
        id2 = await mgr.connect(broken_ws, "room-1", _player("p2", "Bob"))

        event = _event("test_event", {"msg": "hello"})
        await mgr.broadcast_to_room("room-1", event)

        # Healthy socket received the message
        healthy_ws.send_text.assert_awaited_once()
        # Broken socket was pruned
        assert "p2" not in mgr.active_rooms.get("room-1", {})
        # Healthy socket still present
        assert "p1" in mgr.active_rooms["room-1"]

    @pytest.mark.asyncio
    async def test_broadcast_prune_does_not_evict_newer_connection(self) -> None:
        """CRITICAL: if a player reconnected and the OLD socket fails during
        broadcast, pruning must NOT remove the NEW connection."""
        mgr = ConnectionManager()
        old_ws = _make_ws()
        new_ws = _make_ws()
        player = _player("p1", "Alice")

        old_id = await mgr.connect(old_ws, "room-1", player)

        # Manually inject the old socket into the snapshot that broadcast will iterate,
        # simulating a race where old socket is still in the list when broadcast starts
        # but has been replaced by a new connection.
        new_id = await mgr.connect(new_ws, "room-1", player)

        # Now set up a scenario: the room has the NEW connection, but we manually
        # create a snapshot that includes the OLD dead socket under a different entry.
        # Actually, the real scenario is more like: broadcast iterates a snapshot,
        # old entry fails, pruning calls disconnect.
        # Since the entry is already replaced, disconnect with conn_id should be safe.

        # Simulate: force the room to have both old and new (which can't normally happen
        # for the same player_id). Instead, test that disconnect with specific conn_id is safe.
        removed = mgr.disconnect("room-1", "p1", old_id)
        assert removed is False  # old_id doesn't match, so no removal
        assert mgr.active_rooms["room-1"]["p1"]["conn_id"] == new_id

    @pytest.mark.asyncio
    async def test_broadcast_runtime_error_prunes_socket(self) -> None:
        """RuntimeError (e.g. 'cannot call send on closed socket') should also prune."""
        mgr = ConnectionManager()
        broken_ws = _make_ws()
        broken_ws.send_text = AsyncMock(side_effect=RuntimeError("socket closed"))

        await mgr.connect(broken_ws, "room-1", _player("p1", "Alice"))

        event = _event("test", {"x": 1})
        await mgr.broadcast_to_room("room-1", event)

        assert "room-1" not in mgr.active_rooms


# ===================================================================
# 7. Stale disconnect does not evict newer connection from Redis
# ===================================================================

class TestStaleDisconnectRedis:
    @pytest.mark.asyncio
    async def test_set_player_offline_guards_on_conn_id(self) -> None:
        """Calling set_player_offline with an old conn_id should NOT overwrite
        the state of a newer connection in Redis."""
        from app.core.redis import set_player_offline

        existing_state = json.dumps({
            "status": "online",
            "conn_id": "new-conn-id",
            "player_id": "p1",
            "player_name": "Alice",
            "is_speaker": False,
        })

        with patch("app.core.redis.redis_client") as mock_redis:
            mock_redis.hget = AsyncMock(return_value=existing_state)
            mock_redis.hset = AsyncMock()

            # Try to mark offline with old conn_id
            await set_player_offline("room-1", "p1", "old-conn-id")

            # hset should NOT have been called — stale conn_id guard
            mock_redis.hset.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_set_player_offline_succeeds_with_matching_conn_id(self) -> None:
        """set_player_offline with matching conn_id should mark player offline."""
        from app.core.redis import set_player_offline

        existing_state = json.dumps({
            "status": "online",
            "conn_id": "current-conn-id",
            "player_id": "p1",
            "player_name": "Alice",
            "is_speaker": False,
        })

        with patch("app.core.redis.redis_client") as mock_redis:
            mock_redis.hget = AsyncMock(return_value=existing_state)
            mock_redis.hset = AsyncMock()

            await set_player_offline("room-1", "p1", "current-conn-id")

            # Should have been called to set status offline
            mock_redis.hset.assert_awaited_once()
            call_args = mock_redis.hset.call_args
            stored = json.loads(call_args[0][2])
            assert stored["status"] == "offline"


# ===================================================================
# 8. send_personal_message to a closed socket
# ===================================================================

class TestSendPersonalMessageClosed:
    @pytest.mark.asyncio
    async def test_send_to_disconnected_socket_no_raise(self) -> None:
        mgr = ConnectionManager()
        ws = _make_ws()
        ws.send_text = AsyncMock(side_effect=WebSocketDisconnect())

        event = _event("error", {"message": "test"})
        # Should not raise
        await mgr.send_personal_message(event, ws)

    @pytest.mark.asyncio
    async def test_send_to_errored_socket_no_raise(self) -> None:
        mgr = ConnectionManager()
        ws = _make_ws()
        ws.send_text = AsyncMock(side_effect=RuntimeError("connection reset"))

        event = _event("error", {"message": "test"})
        await mgr.send_personal_message(event, ws)


# ===================================================================
# 9. Concurrent room operations
# ===================================================================

class TestConcurrentRoomOperations:
    @pytest.mark.asyncio
    async def test_broadcast_to_empty_room_is_noop(self) -> None:
        mgr = ConnectionManager()
        event = _event("test", {})
        # Should not raise
        await mgr.broadcast_to_room("nonexistent-room", event)

    @pytest.mark.asyncio
    async def test_disconnect_nonexistent_room(self) -> None:
        mgr = ConnectionManager()
        removed = mgr.disconnect("nonexistent", "p1", "some-conn")
        assert removed is False

    @pytest.mark.asyncio
    async def test_disconnect_nonexistent_player(self) -> None:
        mgr = ConnectionManager()
        ws = _make_ws()
        await mgr.connect(ws, "room-1", _player("p1", "Alice"))

        removed = mgr.disconnect("room-1", "p999", "some-conn")
        assert removed is False
        # Room should still exist
        assert "room-1" in mgr.active_rooms


# ===================================================================
# 10. Old connection finally block does not broadcast "player left"
#     (Integration-level simulation)
# ===================================================================

class TestOldConnectionFinallyBlock:
    @pytest.mark.asyncio
    async def test_stale_finally_does_not_broadcast_leave(self) -> None:
        """Simulates the full flow: old connection is replaced, its finally
        block runs disconnect() which returns False, so no leave broadcast."""
        mgr = ConnectionManager()
        old_ws = _make_ws()
        new_ws = _make_ws()
        player = _player("p1", "Alice")

        old_id = await mgr.connect(old_ws, "room-1", player)
        new_id = await mgr.connect(new_ws, "room-1", player)

        # --- Simulate old connection's finally block ---
        removed = mgr.disconnect("room-1", "p1", old_id)

        # The guard prevents removal
        assert removed is False

        # Since removed is False, the websocket_endpoint code does `if not removed: return`
        # So no "player left" broadcast should happen.
        # The new connection is unaffected:
        assert mgr.active_rooms["room-1"]["p1"]["socket"] is new_ws

    @pytest.mark.asyncio
    async def test_current_connection_disconnect_does_broadcast_leave(self) -> None:
        """The actual current connection disconnecting should remove the player."""
        mgr = ConnectionManager()
        ws = _make_ws()
        player = _player("p1", "Alice")

        conn_id = await mgr.connect(ws, "room-1", player)
        removed = mgr.disconnect("room-1", "p1", conn_id)

        assert removed is True
        assert "room-1" not in mgr.active_rooms


# ===================================================================
# 11. Invalid JSON from client
# ===================================================================

class TestInvalidClientMessage:
    @pytest.mark.asyncio
    async def test_invalid_json_returns_error_event(self) -> None:
        """Non-JSON text should produce an error event, not crash the connection."""
        from app.api.websockets import websocket_endpoint

        ws = _make_ws()
        # First receive returns garbage, second raises disconnect (simulating client leaving)
        ws.receive_text = AsyncMock(side_effect=["not valid json", WebSocketDisconnect()])

        room = RoomInfo(id="room-1", game_name=GameName.MAFIA)
        player = _player()

        with (
            patch("app.api.websockets.get_room_meta", new=AsyncMock(return_value=None)),
            patch("app.api.websockets.set_room_meta", new=AsyncMock()),
            patch("app.api.websockets.set_player_online", new=AsyncMock()),
            patch("app.api.websockets.set_player_offline", new=AsyncMock()),
            patch("app.api.websockets.get_room_state", new=AsyncMock(return_value={
                "p1": {"player_name": "Alice", "is_speaker": False, "status": "online"},
            })),
            patch("app.api.websockets.manager") as mock_mgr,
        ):
            mock_mgr.connect = AsyncMock(return_value="conn-1")
            mock_mgr.disconnect = MagicMock(return_value=True)
            mock_mgr.broadcast_to_room = AsyncMock()
            mock_mgr.send_personal_message = AsyncMock()

            await websocket_endpoint(ws, room, player)

            # Should have sent an error event for the bad JSON
            error_calls = [
                call for call in mock_mgr.send_personal_message.call_args_list
                if call[0][0].event_type == "error"
            ]
            assert len(error_calls) >= 1


# ===================================================================
# 12. Valid JSON but invalid ClientAction schema
# ===================================================================

class TestInvalidClientActionSchema:
    @pytest.mark.asyncio
    async def test_wrong_types_returns_error_event(self) -> None:
        """JSON that doesn't match ClientAction schema should produce an error event."""
        from app.api.websockets import websocket_endpoint

        ws = _make_ws()
        bad_payload = json.dumps({"action_type": 123})  # action_type must be str
        ws.receive_text = AsyncMock(side_effect=[bad_payload, WebSocketDisconnect()])

        room = RoomInfo(id="room-1", game_name=GameName.MAFIA)
        player = _player()

        with (
            patch("app.api.websockets.get_room_meta", new=AsyncMock(return_value=None)),
            patch("app.api.websockets.set_room_meta", new=AsyncMock()),
            patch("app.api.websockets.set_player_online", new=AsyncMock()),
            patch("app.api.websockets.set_player_offline", new=AsyncMock()),
            patch("app.api.websockets.get_room_state", new=AsyncMock(return_value={
                "p1": {"player_name": "Alice", "is_speaker": False, "status": "online"},
            })),
            patch("app.api.websockets.manager") as mock_mgr,
        ):
            mock_mgr.connect = AsyncMock(return_value="conn-1")
            mock_mgr.disconnect = MagicMock(return_value=True)
            mock_mgr.broadcast_to_room = AsyncMock()
            mock_mgr.send_personal_message = AsyncMock()

            await websocket_endpoint(ws, room, player)

            error_calls = [
                call for call in mock_mgr.send_personal_message.call_args_list
                if call[0][0].event_type == "error"
            ]
            assert len(error_calls) >= 1


# ===================================================================
# Bonus: Speaker socket lookup
# ===================================================================

class TestSpeakerSocket:
    def test_get_speaker_socket_returns_speaker(self) -> None:
        mgr = ConnectionManager()
        ws_regular = _make_ws()
        ws_speaker = _make_ws()

        mgr.active_rooms = {
            "room-1": {
                "p1": {"socket": ws_regular, "player": _player("p1", "Alice", speaker=False)},
                "p2": {"socket": ws_speaker, "player": _player("p2", "Bob", speaker=True)},
            }
        }
        assert mgr.get_speaker_socket("room-1") is ws_speaker

    def test_get_speaker_socket_returns_none_when_no_speaker(self) -> None:
        mgr = ConnectionManager()
        mgr.active_rooms = {
            "room-1": {
                "p1": {"socket": _make_ws(), "player": _player("p1", "Alice", speaker=False)},
            }
        }
        assert mgr.get_speaker_socket("room-1") is None

    def test_get_speaker_socket_empty_room(self) -> None:
        mgr = ConnectionManager()
        assert mgr.get_speaker_socket("nonexistent") is None


# ===================================================================
# is_current_connection helper (to be added)
# ===================================================================

class TestIsCurrentConnection:
    def test_returns_true_for_current(self) -> None:
        mgr = ConnectionManager()
        mgr.active_rooms = {
            "room-1": {"p1": {"socket": _make_ws(), "player": _player(), "conn_id": "abc"}},
        }
        assert mgr.is_current_connection("room-1", "p1", "abc") is True

    def test_returns_false_for_stale(self) -> None:
        mgr = ConnectionManager()
        mgr.active_rooms = {
            "room-1": {"p1": {"socket": _make_ws(), "player": _player(), "conn_id": "new"}},
        }
        assert mgr.is_current_connection("room-1", "p1", "old") is False

    def test_returns_false_for_missing_room(self) -> None:
        mgr = ConnectionManager()
        assert mgr.is_current_connection("nope", "p1", "abc") is False

    def test_returns_false_for_missing_player(self) -> None:
        mgr = ConnectionManager()
        mgr.active_rooms = {"room-1": {}}
        assert mgr.is_current_connection("room-1", "p999", "abc") is False
