import logging
from typing import Dict
from fastapi import WebSocket
from starlette.websockets import WebSocketDisconnect
from app.schemas.messages import ServerEvent
from app.schemas.session import PlayerSession

logger = logging.getLogger(__name__)

class ConnectionManager:
    def __init__(self):
        # Dict[room_id, Dict[player_id, {"socket": WebSocket, "player": PlayerSession}]]
        self.active_rooms: Dict[str, Dict[str, dict]] = {}

    async def connect(self, websocket: WebSocket, room_id: str, player: PlayerSession):
        await websocket.accept()
        
        if room_id not in self.active_rooms:
            self.active_rooms[room_id] = {}
            
        self.active_rooms[room_id][player.player_id] = {
            "socket": websocket,
            "player": player,
        }
        logger.info(
            "Player %s connected to room %s (speaker: %s)",
            player.player_id,
            room_id,
            player.is_speaker,
        )

    def disconnect(self, room_id: str, player_id: str):
        if room_id in self.active_rooms:
            if player_id in self.active_rooms[room_id]:
                del self.active_rooms[room_id][player_id]
                logger.info(f"Player {player_id} disconnected from room {room_id}")
            if not self.active_rooms[room_id]:
                del self.active_rooms[room_id]

    async def broadcast_to_room(self, room_id: str, message: ServerEvent):
        if room_id in self.active_rooms:
            msg_json = message.model_dump_json()
            # Iterate a snapshot so we can safely remove broken connections.
            for player_id, conn_data in list(self.active_rooms[room_id].items()):
                socket: WebSocket = conn_data["socket"]
                try:
                    await socket.send_text(msg_json)
                except WebSocketDisconnect:
                    # Expected during fast refresh / React StrictMode / tab close.
                    logger.info("Broadcast prune: %s disconnected from %s", player_id, room_id)
                    self.disconnect(room_id, player_id)
                except Exception as e:
                    logger.error("Error broadcasting to %s in %s: %r", player_id, room_id, e)
                    self.disconnect(room_id, player_id)

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
