from enum import Enum

from pydantic import BaseModel, ConfigDict, Field


class GameName(str, Enum):
    MAFIA = "Mafia"


class RoomInfo(BaseModel):
    """Room metadata provided at WebSocket connect time."""

    model_config = ConfigDict(extra="allow")

    id: str = Field(..., description="Unique room identifier")
    game_name: GameName = Field(..., description="Game to play in this room")


class PlayerSession(BaseModel):
    """Player session metadata provided at WebSocket connect time."""

    model_config = ConfigDict(extra="allow")

    player_id: str = Field(..., description="Unique player identifier within the room")
    player_name: str = Field(..., description="Human-readable player display name")
    is_speaker: bool = Field(False, description="Whether this connection is the speaker node")
