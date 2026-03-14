"""Data contracts for the AI narration pipeline.

These schemas flow through RabbitMQ between the game engine and the
narration worker.
"""

from pydantic import BaseModel, Field
from typing import Any, Dict, Optional


class NarrationRequest(BaseModel):
    """Published by the game engine when a phase resolves."""

    room_id: str = Field(..., description="Room this narration belongs to")
    event_context: str = Field(
        ...,
        description=(
            "Semantic label for the event, e.g. 'night_kill', 'night_save', "
            "'day_vote', 'game_start', 'game_over'"
        ),
    )
    context_data: Dict[str, Any] = Field(
        default_factory=dict,
        description="Relevant game state (who was killed, vote tallies, etc.)",
    )
    turn_number: int = Field(
        ...,
        description="Monotonically increasing counter used to reject stale responses",
    )


class NarrationResponse(BaseModel):
    """Consumed by the API server from the narration_responses queue."""

    room_id: str = Field(..., description="Room this narration belongs to")
    narration_text: str = Field(
        ..., description="AI-generated narration string"
    )
    audio_url: Optional[str] = Field(
        None, description="URL/path to the generated TTS audio file"
    )
    turn_number: int = Field(
        ...,
        description="Must match the room's current turn_number before broadcast",
    )
