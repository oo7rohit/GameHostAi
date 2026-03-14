"""Concrete Mafia game strategy.

Implements role assignment, phase resolution (night kill/heal, day voting),
and win-condition checks for the classic Mafia party game.
"""

import logging
import random
from typing import Any

from app.engine.strategy import BaseGameStrategy

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Role constants
# ---------------------------------------------------------------------------
ROLE_MAFIA = "mafia"
ROLE_VILLAGER = "villager"
ROLE_HEALER = "healer"
ROLE_COP = "cop"

# Sub-phase names used by the state machine
PHASE_NIGHT = "NIGHT_ACTIONS"
PHASE_DAY = "DAY_VOTING"


class MafiaStrategy(BaseGameStrategy):
    """Rules engine for the classic Mafia party game."""

    # ------------------------------------------------------------------ #
    # Initialisation
    # ------------------------------------------------------------------ #
    def initialize_game(self, players: list[str]) -> dict[str, Any]:
        """Assign roles randomly and build the initial game state.

        Role distribution (minimum 4 players):
            * 1 Mafia per 4 players (rounded down, minimum 1)
            * 1 Healer
            * 1 Cop
            * Remaining players are Villagers
        """
        if len(players) < 4:
            raise ValueError("Mafia requires at least 4 players.")

        shuffled = players.copy()
        random.shuffle(shuffled)

        num_mafia = max(1, len(shuffled) // 4)

        role_assignments: dict[str, dict[str, Any]] = {}
        idx = 0

        # Assign Mafia
        for _ in range(num_mafia):
            role_assignments[shuffled[idx]] = {"role": ROLE_MAFIA, "alive": True}
            idx += 1

        # Assign Healer
        role_assignments[shuffled[idx]] = {"role": ROLE_HEALER, "alive": True}
        idx += 1

        # Assign Cop
        role_assignments[shuffled[idx]] = {"role": ROLE_COP, "alive": True}
        idx += 1

        # Remaining players are Villagers
        for i in range(idx, len(shuffled)):
            role_assignments[shuffled[i]] = {"role": ROLE_VILLAGER, "alive": True}

        state: dict[str, Any] = {
            "players": role_assignments,
            "phase": PHASE_NIGHT,
            "round": 1,
            "eliminated": [],
        }

        logger.info("Mafia game initialised with %d players (%d mafia)", len(players), num_mafia)
        return state

    # ------------------------------------------------------------------ #
    # Phase resolution
    # ------------------------------------------------------------------ #
    def evaluate_phase(
        self,
        current_state: dict[str, Any],
        actions: dict[str, dict[str, Any]],
    ) -> tuple[dict[str, Any], dict[str, dict[str, Any]]]:
        """Resolve actions for the current sub-phase.

        Returns ``(mutated_state, private_messages)``.
        """
        phase = current_state["phase"]

        if phase == PHASE_NIGHT:
            return self._resolve_night(current_state, actions)
        elif phase == PHASE_DAY:
            return self._resolve_day(current_state, actions)
        else:
            logger.warning("evaluate_phase called during unknown phase '%s'", phase)
            return current_state, {}

    # ---- Night resolution ------------------------------------------------ #
    def _resolve_night(
        self,
        state: dict[str, Any],
        actions: dict[str, dict[str, Any]],
    ) -> tuple[dict[str, Any], dict[str, dict[str, Any]]]:
        """Resolve simultaneous Mafia kill, Healer save, and Cop investigate."""
        players = state["players"]
        private_messages: dict[str, dict[str, Any]] = {}

        # --- Collect targets from each role --------------------------------
        kill_target: str | None = None
        heal_target: str | None = None
        cop_target: str | None = None

        for player_id, action in actions.items():
            player_data = players.get(player_id)
            if player_data is None or not player_data["alive"]:
                continue

            role = player_data["role"]
            target = action.get("target")

            if role == ROLE_MAFIA:
                # Last mafia action wins (or they could all agree)
                kill_target = target
            elif role == ROLE_HEALER:
                heal_target = target
            elif role == ROLE_COP:
                cop_target = target

        # --- Resolve kill vs heal ------------------------------------------
        killed_player: str | None = None
        if kill_target and kill_target in players and players[kill_target]["alive"]:
            if kill_target != heal_target:
                players[kill_target]["alive"] = False
                killed_player = kill_target
                state["eliminated"].append(kill_target)
                logger.info("Night: %s was killed by the Mafia.", kill_target)
            else:
                logger.info("Night: Healer saved %s from the Mafia.", kill_target)

        # --- Cop investigation (private message) ---------------------------
        if cop_target and cop_target in players:
            is_mafia = players[cop_target]["role"] == ROLE_MAFIA
            # Find the cop player_id to send the result to
            for pid, pdata in players.items():
                if pdata["role"] == ROLE_COP and pdata["alive"]:
                    private_messages[pid] = {
                        "event_type": "investigation_result",
                        "target": cop_target,
                        "is_mafia": is_mafia,
                    }
                    break

        # --- Transition to day -------------------------------------------
        state["phase"] = PHASE_DAY
        state["last_night_result"] = {
            "killed": killed_player,
        }

        return state, private_messages

    # ---- Day resolution -------------------------------------------------- #
    def _resolve_day(
        self,
        state: dict[str, Any],
        actions: dict[str, dict[str, Any]],
    ) -> tuple[dict[str, Any], dict[str, dict[str, Any]]]:
        """Resolve day-time voting to eliminate a player by majority."""
        players = state["players"]

        # Tally votes
        vote_counts: dict[str, int] = {}
        for player_id, action in actions.items():
            player_data = players.get(player_id)
            if player_data is None or not player_data["alive"]:
                continue
            vote_target = action.get("target")
            if vote_target:
                vote_counts[vote_target] = vote_counts.get(vote_target, 0) + 1

        # Determine player with most votes (simple plurality)
        eliminated_player: str | None = None
        if vote_counts:
            max_votes = max(vote_counts.values())
            top_voted = [pid for pid, cnt in vote_counts.items() if cnt == max_votes]

            # If there's a single majority winner, eliminate them
            if len(top_voted) == 1:
                eliminated_player = top_voted[0]
                if eliminated_player in players and players[eliminated_player]["alive"]:
                    players[eliminated_player]["alive"] = False
                    state["eliminated"].append(eliminated_player)
                    logger.info("Day: %s was voted out.", eliminated_player)
            else:
                logger.info("Day: Vote ended in a tie among %s. No elimination.", top_voted)

        # Transition to night, increment round
        state["phase"] = PHASE_NIGHT
        state["round"] += 1
        state["last_day_result"] = {
            "eliminated": eliminated_player,
            "vote_counts": vote_counts,
        }

        return state, {}

    # ------------------------------------------------------------------ #
    # Win condition
    # ------------------------------------------------------------------ #
    def check_win_condition(self, current_state: dict[str, Any]) -> str | None:
        """Mafia wins when alive mafia >= alive non-mafia.

        Villagers win when no mafia remain alive.
        """
        players = current_state["players"]

        alive_mafia = sum(
            1 for p in players.values() if p["alive"] and p["role"] == ROLE_MAFIA
        )
        alive_non_mafia = sum(
            1 for p in players.values() if p["alive"] and p["role"] != ROLE_MAFIA
        )

        if alive_mafia == 0:
            return "villagers"
        if alive_mafia >= alive_non_mafia:
            return "mafia"
        return None
