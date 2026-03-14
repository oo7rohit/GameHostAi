from pydantic import BaseModel, Field
from typing import Any, Dict, Optional

class ClientAction(BaseModel):
    action_type: str = Field(..., description="Action type, e.g., 'join_room', 'vote', 'echo'")
    payload: Dict[str, Any] = Field(default_factory=dict, description="Action payload data")

class ServerEvent(BaseModel):
    event_type: str = Field(..., description="Event type, e.g., 'system_event', 'echo_reply', 'error'")
    phase: Optional[str] = Field(None, description="Current game phase, e.g., 'day', 'night'")
    data: Dict[str, Any] = Field(default_factory=dict, description="Event payload data")
