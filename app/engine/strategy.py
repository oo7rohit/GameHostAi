"""Abstract Base Class defining the contract for all game strategies.

The core state machine delegates ALL game-specific logic to a concrete
strategy implementation.  This keeps the engine entirely game-agnostic.
"""

from abc import ABC, abstractmethod
from typing import Any


class BaseGameStrategy(ABC):
    """Contract that every game (Mafia, Werewolf, etc.) must fulfil."""

    @abstractmethod
    def initialize_game(self, players: list[str]) -> dict[str, Any]:
        """Assign roles and produce the initial game state.

        Args:
            players: Ordered list of player IDs in the room.

        Returns:
            A state dict that the engine will persist in Redis.
            Must contain at minimum:
                - ``players``: mapping of player_id → per-player data
                - ``phase``: the first active sub-phase name
        """
        ...

    @abstractmethod
    def evaluate_phase(
        self,
        current_state: dict[str, Any],
        actions: dict[str, dict[str, Any]],
    ) -> tuple[dict[str, Any], dict[str, dict[str, Any]]]:
        """Resolve all queued actions for the current phase.

        Actions are keyed by ``player_id`` so that each player's most
        recent submission is the only one evaluated (idempotent).

        Args:
            current_state: The authoritative game state from Redis.
            actions: ``{player_id: action_payload}`` collected during the phase.

        Returns:
            A 2-tuple of:
                - **mutated_state**: the updated game state to persist.
                - **private_messages**: ``{player_id: data_dict}`` of
                  messages that should only be sent to that specific player
                  (e.g. Cop investigation results).
        """
        ...

    @abstractmethod
    def check_win_condition(self, current_state: dict[str, Any]) -> str | None:
        """Check whether the game has ended.

        Args:
            current_state: The authoritative game state.

        Returns:
            The name of the winning team (e.g. ``"mafia"``, ``"villagers"``)
            or ``None`` if the game should continue.
        """
        ...
