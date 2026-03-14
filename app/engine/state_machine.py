"""Game State Machine — manages the lifecycle of a single room's game.

Orchestrates the flow: LOBBY → IN_PROGRESS (sub-phases) → FINISHED.
All game-specific logic is delegated to an injected ``BaseGameStrategy``.
"""

import asyncio
import logging
from enum import Enum
from typing import Any, TYPE_CHECKING

from app.core.connection_manager import ConnectionManager
from app.core.redis import save_game_state, load_game_state
from app.engine.strategy import BaseGameStrategy
from app.schemas.messages import ServerEvent
from app.schemas.narration import NarrationRequest

if TYPE_CHECKING:
    from app.core.rabbitmq import RabbitMQClient

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Top-level game phase enum (distinct from strategy sub-phases)
# ---------------------------------------------------------------------------
class GamePhase(str, Enum):
    LOBBY = "LOBBY"
    IN_PROGRESS = "IN_PROGRESS"
    FINISHED = "FINISHED"


# Phase timer defaults (seconds)
PHASE_DURATIONS: dict[str, int] = {
    "NIGHT_ACTIONS": 30,
    "DAY_VOTING": 60,
}


class GameStateMachine:
    """Manages the state lifecycle of a single room's game session.

    Attributes:
        room_id:   Unique identifier for the room this machine controls.
        strategy:  The injected game-specific rules engine.
        conn_mgr:  Reference to the ``ConnectionManager`` for broadcasting.
    """

    def __init__(
        self,
        room_id: str,
        strategy: BaseGameStrategy,
        conn_mgr: ConnectionManager,
        rabbitmq_publisher: "RabbitMQClient | None" = None,
    ) -> None:
        self.room_id = room_id
        self.strategy = strategy
        self.conn_mgr = conn_mgr
        self.rabbitmq_publisher = rabbitmq_publisher

        self.game_phase: GamePhase = GamePhase.LOBBY
        self.game_state: dict[str, Any] = {}

        # Narration sequence tracking
        self.turn_number: int = 0

        # Action queuing — dict for idempotency (player can update action)
        self.current_phase_actions: dict[str, dict[str, Any]] = {}

        # Phase timer management
        self.timer_task: asyncio.Task[None] | None = None
        self._is_resolving: bool = False

    # ------------------------------------------------------------------ #
    # Game lifecycle
    # ------------------------------------------------------------------ #
    async def start_game(self, players: list[str]) -> None:
        """Initialise the game via the strategy and start the first phase."""
        if self.game_phase != GamePhase.LOBBY:
            logger.warning("start_game called but room %s is not in LOBBY.", self.room_id)
            return

        self.game_state = self.strategy.initialize_game(players)
        self.game_phase = GamePhase.IN_PROGRESS
        self.current_phase_actions = {}
        self._is_resolving = False

        await save_game_state(self.room_id, self.game_state)

        # Broadcast game-started event (public: role details are *not* shared)
        start_event = ServerEvent(
            event_type="game_started",
            phase=self.game_state.get("phase"),
            data={"message": "The game has begun!", "round": self.game_state.get("round", 1)},
        )
        await self.conn_mgr.broadcast_to_room(self.room_id, start_event)

        # Send each player their private role assignment
        for player_id, pdata in self.game_state.get("players", {}).items():
            role_event = ServerEvent(
                event_type="role_assignment",
                phase=self.game_state.get("phase"),
                data={"role": pdata["role"]},
            )
            await self._send_to_player(player_id, role_event)

        # Publish game_start narration request
        self._publish_narration("game_start", {"players": list(self.game_state["players"].keys())})

        # Start the phase timer
        self._start_phase_timer()
        logger.info("Game started in room %s", self.room_id)

    # ------------------------------------------------------------------ #
    # Action queuing (idempotent)
    # ------------------------------------------------------------------ #
    async def queue_action(self, player_id: str, action: dict[str, Any]) -> None:
        """Queue (or overwrite) a player's action for the current phase.

        If all alive players have submitted, triggers early resolution.
        """
        if self.game_phase != GamePhase.IN_PROGRESS:
            logger.warning("Action ignored — room %s is in %s.", self.room_id, self.game_phase.value)
            return

        self.current_phase_actions[player_id] = action
        logger.info(
            "Room %s: queued action from %s (total: %d)",
            self.room_id,
            player_id,
            len(self.current_phase_actions),
        )

        # Acknowledge the action to the player
        ack_event = ServerEvent(
            event_type="action_acknowledged",
            phase=self.game_state.get("phase"),
            data={"message": "Your action has been recorded."},
        )
        await self._send_to_player(player_id, ack_event)

        # Check for early resolution
        alive_players = [
            pid for pid, pdata in self.game_state.get("players", {}).items()
            if pdata["alive"]
        ]
        if len(self.current_phase_actions) >= len(alive_players):
            logger.info("Room %s: all alive players submitted — early resolution.", self.room_id)
            # Cancel the timer first, then resolve
            timer = self.timer_task
            if timer is not None and not timer.done():
                timer.cancel()
            await self._resolve_phase()

    # ------------------------------------------------------------------ #
    # Phase resolution
    # ------------------------------------------------------------------ #
    async def _resolve_phase(self) -> None:
        """Resolve the current phase using the strategy and broadcast results.

        Protected by ``_is_resolving`` flag to prevent double-execution from
        a timer + early-resolution race condition.
        """
        if self._is_resolving:
            logger.debug("Room %s: _resolve_phase skipped (already resolving).", self.room_id)
            return
        self._is_resolving = True

        try:
            current_phase = self.game_state.get("phase", "unknown")
            logger.info("Room %s: resolving phase '%s'", self.room_id, current_phase)

            # Increment turn counter
            self.turn_number += 1

            # Delegate to the strategy
            new_state, private_messages = self.strategy.evaluate_phase(
                self.game_state,
                self.current_phase_actions,
            )
            self.game_state = new_state

            # Persist to Redis
            await save_game_state(self.room_id, self.game_state)

            # Broadcast public state update
            public_event = ServerEvent(
                event_type="phase_resolved",
                phase=self.game_state.get("phase"),
                data={
                    "round": self.game_state.get("round", 1),
                    "eliminated": self.game_state.get("eliminated", []),
                    "last_night_result": self.game_state.get("last_night_result"),
                    "last_day_result": self.game_state.get("last_day_result"),
                },
            )
            await self.conn_mgr.broadcast_to_room(self.room_id, public_event)

            # Send private messages to specific players
            for player_id, msg_data in private_messages.items():
                pm_event = ServerEvent(
                    event_type=msg_data.get("event_type", "private_message"),
                    phase=self.game_state.get("phase"),
                    data=msg_data,
                )
                await self._send_to_player(player_id, pm_event)

            # Fire-and-forget narration request based on resolved phase
            self._publish_narration_for_phase(current_phase)

            # Check win condition
            winner = self.strategy.check_win_condition(self.game_state)
            if winner:
                self.game_phase = GamePhase.FINISHED
                win_event = ServerEvent(
                    event_type="game_over",
                    phase="FINISHED",
                    data={
                        "winner": winner,
                        "players": self.game_state.get("players", {}),
                    },
                )
                await self.conn_mgr.broadcast_to_room(self.room_id, win_event)
                self._publish_narration("game_over", {"winner": winner})
                logger.info("Room %s: game over — %s wins!", self.room_id, winner)
            else:
                # Reset actions and start the next phase timer
                self.current_phase_actions = {}
                self._is_resolving = False
                self._start_phase_timer()

                # Broadcast new phase announcement
                new_phase_event = ServerEvent(
                    event_type="phase_started",
                    phase=self.game_state.get("phase"),
                    data={"message": f"Phase '{self.game_state.get('phase')}' has begun."},
                )
                await self.conn_mgr.broadcast_to_room(self.room_id, new_phase_event)
        except Exception:
            self._is_resolving = False
            raise

    # ------------------------------------------------------------------ #
    # Phase timer
    # ------------------------------------------------------------------ #
    def _start_phase_timer(self) -> None:
        """Launch an async task that resolves the phase after a timeout."""
        current_sub_phase = self.game_state.get("phase", "")
        duration = PHASE_DURATIONS.get(current_sub_phase, 60)
        self.timer_task = asyncio.create_task(self._phase_timer(duration))
        logger.info("Room %s: phase timer started (%ds for %s)", self.room_id, duration, current_sub_phase)

    async def _phase_timer(self, duration: int) -> None:
        """Wait for ``duration`` seconds then resolve the phase."""
        try:
            await asyncio.sleep(duration)
            logger.info("Room %s: phase timer expired.", self.room_id)
            await self._resolve_phase()
        except asyncio.CancelledError:
            logger.debug("Room %s: phase timer cancelled.", self.room_id)

    # ------------------------------------------------------------------ #
    # Helpers
    # ------------------------------------------------------------------ #
    async def _send_to_player(self, player_id: str, event: ServerEvent) -> None:
        """Send a personal message to a specific player via ConnectionManager."""
        room_conns = self.conn_mgr.active_rooms.get(self.room_id, {})
        conn_data = room_conns.get(player_id)
        if conn_data:
            await self.conn_mgr.send_personal_message(event, conn_data["socket"])
        else:
            logger.debug("Room %s: player %s not connected, skipping message.", self.room_id, player_id)

    # ------------------------------------------------------------------ #
    # Narration helpers
    # ------------------------------------------------------------------ #
    def _publish_narration(self, event_context: str, context_data: dict[str, Any]) -> None:
        """Fire-and-forget a NarrationRequest to RabbitMQ."""
        if self.rabbitmq_publisher is None:
            return

        request = NarrationRequest(
            room_id=self.room_id,
            event_context=event_context,
            context_data=context_data,
            turn_number=self.turn_number,
        )
        asyncio.create_task(self.rabbitmq_publisher.publish_narration_request(request))

    def _publish_narration_for_phase(self, resolved_phase: str) -> None:
        """Build the appropriate narration context from the last resolution."""
        if resolved_phase == "NIGHT_ACTIONS":
            night_result = self.game_state.get("last_night_result", {})
            killed = night_result.get("killed")
            if killed:
                self._publish_narration("night_kill", {"killed": killed})
            else:
                self._publish_narration("night_save", {})
        elif resolved_phase == "DAY_VOTING":
            day_result = self.game_state.get("last_day_result", {})
            eliminated = day_result.get("eliminated")
            if eliminated:
                self._publish_narration("day_vote", {
                    "eliminated": eliminated,
                    "vote_counts": day_result.get("vote_counts", {}),
                })
            else:
                self._publish_narration("day_no_elimination", {
                    "vote_counts": day_result.get("vote_counts", {}),
                })


# ---------------------------------------------------------------------------
# Global registry of active games (room_id → GameStateMachine)
# ---------------------------------------------------------------------------
active_games: dict[str, GameStateMachine] = {}
