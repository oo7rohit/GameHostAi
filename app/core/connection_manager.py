import logging
from typing import Dict
from uuid import uuid4
from fastapi import WebSocket
from starlette.websockets import WebSocketDisconnect
from app.schemas.messages import ServerEvent
from app.schemas.session import PlayerSession

logger = logging.getLogger(__name__)

class ConnectionManager:
    def __init__(self):
        # Dict[room_id, Dict[player_id, {"socket": WebSocket, "player": PlayerSession, "conn_id": str}]]
        self.active_rooms: Dict[str, Dict[str, dict]] = {}

    async def connect(self, websocket: WebSocket, room_id: str, player: PlayerSession) -> str:
        conn_id = uuid4().hex

        if room_id not in self.active_rooms:
            self.active_rooms[room_id] = {}

        # If a previous connection exists for this player, best-effort close it.
        # In React 18 StrictMode, the client often already closed it, so we must
        # swallow close errors to avoid breaking the new connection.
        existing = self.active_rooms[room_id].get(player.player_id)
        if existing is not None:
            old_socket: WebSocket = existing["socket"]
            try:
                await old_socket.close(code=4001, reason="Replaced by newer connection")
            except Exception:
                pass

        await websocket.accept()

        self.active_rooms[room_id][player.player_id] = {
            "socket": websocket,
            "player": player,
            "conn_id": conn_id,
        }
        logger.info(
            "Player %s connected to room %s (speaker: %s)",
            player.player_id,
            room_id,
            player.is_speaker,
        )
        return conn_id

    def disconnect(self, room_id: str, player_id: str, conn_id: str | None = None) -> bool:
        """Remove a connection.

        If conn_id is provided, removal only happens if it matches the stored conn_id.
        This prevents stale cleanup (StrictMode double-mount) from deleting a newer connection.
        """
        room_conns = self.active_rooms.get(room_id)
        if not room_conns:
            return False

        existing = room_conns.get(player_id)
        if existing is None:
            return False

        if conn_id is not None and existing.get("conn_id") != conn_id:
            return False

        del room_conns[player_id]
        logger.info("Player %s disconnected from room %s", player_id, room_id)
        if not room_conns:
            del self.active_rooms[room_id]
        return True

    def is_current_connection(self, room_id: str, player_id: str, conn_id: str) -> bool:
        """Check whether *conn_id* is still the active connection for a player."""
        room_conns = self.active_rooms.get(room_id)
        if not room_conns:
            return False
        existing = room_conns.get(player_id)
        if existing is None:
            return False
        return existing.get("conn_id") == conn_id

    async def broadcast_to_room(self, room_id: str, message: ServerEvent):
        if room_id in self.active_rooms:
            msg_json = message.model_dump_json()
            # Iterate a snapshot so we can safely remove broken connections.
            for player_id, conn_data in list(self.active_rooms[room_id].items()):
                socket: WebSocket = conn_data["socket"]
                snapshot_conn_id: str | None = conn_data.get("conn_id")
                try:
                    await socket.send_text(msg_json)
                except WebSocketDisconnect:
                    # Expected during fast refresh / React StrictMode / tab close.
                    logger.info("Broadcast prune: %s disconnected from %s", player_id, room_id)
                    self.disconnect(room_id, player_id, conn_id=snapshot_conn_id)
                except Exception as e:
                    logger.error("Error broadcasting to %s in %s: %r", player_id, room_id, e)
                    self.disconnect(room_id, player_id, conn_id=snapshot_conn_id)

    async def send_personal_message(self, message: ServerEvent, websocket: WebSocket):
        try:
            await websocket.send_text(message.model_dump_json())
        except WebSocketDisconnect:
            # Caller will usually handle cleanup; avoid noisy stack traces.
            return
        except Exception as e:
            logger.error(f"Error sending personal message: {e}")

    def get_speaker_socket(self, room_id: str) -> WebSocket | None:
        """Return the WebSocket for the player flagged as speaker, or None."""
        room_conns = self.active_rooms.get(room_id, {})
        for player_id, conn_data in room_conns.items():
            player: PlayerSession | None = conn_data.get("player")
            if player is not None and player.is_speaker:
                return conn_data["socket"]
        return None

manager = ConnectionManager()
