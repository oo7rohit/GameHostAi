import logging
from typing import Dict
from fastapi import WebSocket
from app.schemas.messages import ServerEvent

logger = logging.getLogger(__name__)

class ConnectionManager:
    def __init__(self):
        # Dict[room_id, Dict[player_id, {"socket": WebSocket, "is_speaker": bool}]]
        self.active_rooms: Dict[str, Dict[str, dict]] = {}

    async def connect(self, websocket: WebSocket, room_id: str, player_id: str, is_speaker: bool = False):
        await websocket.accept()
        
        if room_id not in self.active_rooms:
            self.active_rooms[room_id] = {}
            
        self.active_rooms[room_id][player_id] = {
            "socket": websocket,
            "is_speaker": is_speaker
        }
        logger.info(f"Player {player_id} connected to room {room_id} (speaker: {is_speaker})")

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
            for player_id, conn_data in self.active_rooms[room_id].items():
                socket: WebSocket = conn_data["socket"]
                try:
                    await socket.send_text(msg_json)
                except Exception as e:
                    logger.error(f"Error broadcasting to {player_id} in {room_id}: {e}")

    async def send_personal_message(self, message: ServerEvent, websocket: WebSocket):
        try:
            await websocket.send_text(message.model_dump_json())
        except Exception as e:
            logger.error(f"Error sending personal message: {e}")

manager = ConnectionManager()
