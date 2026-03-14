"""Unit tests for MafiaStrategy."""

import pytest
from app.engine.games.mafia import (
    MafiaStrategy,
    ROLE_MAFIA,
    ROLE_VILLAGER,
    ROLE_HEALER,
    ROLE_COP,
    PHASE_NIGHT,
    PHASE_DAY,
)


@pytest.fixture
def strategy() -> MafiaStrategy:
    return MafiaStrategy()


@pytest.fixture
def players() -> list[str]:
    return ["alice", "bob", "charlie", "diana", "eve", "frank"]


# ---------------------------------------------------------------------------
# initialize_game
# ---------------------------------------------------------------------------

class TestInitializeGame:
    def test_all_players_present(self, strategy: MafiaStrategy, players: list[str]) -> None:
        state = strategy.initialize_game(players)
        assert set(state["players"].keys()) == set(players)

    def test_all_players_alive(self, strategy: MafiaStrategy, players: list[str]) -> None:
        state = strategy.initialize_game(players)
        for pdata in state["players"].values():
            assert pdata["alive"] is True

    def test_correct_role_distribution(self, strategy: MafiaStrategy, players: list[str]) -> None:
        state = strategy.initialize_game(players)
        roles = [pdata["role"] for pdata in state["players"].values()]
        # 6 players → 1 mafia (6//4=1), 1 healer, 1 cop, 3 villagers
        assert roles.count(ROLE_MAFIA) == 1
        assert roles.count(ROLE_HEALER) == 1
        assert roles.count(ROLE_COP) == 1
        assert roles.count(ROLE_VILLAGER) == 3

    def test_initial_phase_is_night(self, strategy: MafiaStrategy, players: list[str]) -> None:
        state = strategy.initialize_game(players)
        assert state["phase"] == PHASE_NIGHT

    def test_initial_round_is_one(self, strategy: MafiaStrategy, players: list[str]) -> None:
        state = strategy.initialize_game(players)
        assert state["round"] == 1

    def test_minimum_players_enforced(self, strategy: MafiaStrategy) -> None:
        with pytest.raises(ValueError, match="at least 4 players"):
            strategy.initialize_game(["a", "b", "c"])

    def test_eight_players_two_mafia(self, strategy: MafiaStrategy) -> None:
        state = strategy.initialize_game([f"p{i}" for i in range(8)])
        roles = [pdata["role"] for pdata in state["players"].values()]
        assert roles.count(ROLE_MAFIA) == 2


# ---------------------------------------------------------------------------
# evaluate_phase — Night
# ---------------------------------------------------------------------------

class TestNightResolution:
    def _make_night_state(self, strategy: MafiaStrategy, players: list[str]) -> dict:
        """Produce a deterministic night state."""
        state = strategy.initialize_game(players)
        state["phase"] = PHASE_NIGHT
        return state

    def _find_by_role(self, state: dict, role: str) -> str:
        """Return the first player_id with the given role."""
        for pid, pdata in state["players"].items():
            if pdata["role"] == role:
                return pid
        raise ValueError(f"No player with role {role}")

    def test_mafia_kill_succeeds(self, strategy: MafiaStrategy, players: list[str]) -> None:
        state = self._make_night_state(strategy, players)
        mafia = self._find_by_role(state, ROLE_MAFIA)
        # Pick a villager to kill
        target = self._find_by_role(state, ROLE_VILLAGER)

        actions = {mafia: {"target": target}}
        new_state, _ = strategy.evaluate_phase(state, actions)

        assert new_state["players"][target]["alive"] is False
        assert target in new_state["eliminated"]
        assert new_state["phase"] == PHASE_DAY

    def test_healer_saves_target(self, strategy: MafiaStrategy, players: list[str]) -> None:
        state = self._make_night_state(strategy, players)
        mafia = self._find_by_role(state, ROLE_MAFIA)
        healer = self._find_by_role(state, ROLE_HEALER)
        target = self._find_by_role(state, ROLE_VILLAGER)

        actions = {
            mafia: {"target": target},
            healer: {"target": target},
        }
        new_state, _ = strategy.evaluate_phase(state, actions)

        assert new_state["players"][target]["alive"] is True
        assert target not in new_state["eliminated"]

    def test_cop_investigation_private_message(self, strategy: MafiaStrategy, players: list[str]) -> None:
        state = self._make_night_state(strategy, players)
        cop = self._find_by_role(state, ROLE_COP)
        mafia = self._find_by_role(state, ROLE_MAFIA)

        actions = {cop: {"target": mafia}}
        _, private_messages = strategy.evaluate_phase(state, actions)

        assert cop in private_messages
        assert private_messages[cop]["is_mafia"] is True
        assert private_messages[cop]["target"] == mafia

    def test_cop_investigation_clears_villager(self, strategy: MafiaStrategy, players: list[str]) -> None:
        state = self._make_night_state(strategy, players)
        cop = self._find_by_role(state, ROLE_COP)
        villager = self._find_by_role(state, ROLE_VILLAGER)

        actions = {cop: {"target": villager}}
        _, private_messages = strategy.evaluate_phase(state, actions)

        assert cop in private_messages
        assert private_messages[cop]["is_mafia"] is False

    def test_no_actions_night(self, strategy: MafiaStrategy, players: list[str]) -> None:
        state = self._make_night_state(strategy, players)
        new_state, private_messages = strategy.evaluate_phase(state, {})
        # No kills, no messages, but phase should still advance
        assert new_state["phase"] == PHASE_DAY
        assert private_messages == {}


# ---------------------------------------------------------------------------
# evaluate_phase — Day
# ---------------------------------------------------------------------------

class TestDayResolution:
    def _make_day_state(self, strategy: MafiaStrategy, players: list[str]) -> dict:
        state = strategy.initialize_game(players)
        state["phase"] = PHASE_DAY
        return state

    def _find_by_role(self, state: dict, role: str) -> str:
        for pid, pdata in state["players"].items():
            if pdata["role"] == role:
                return pid
        raise ValueError(f"No player with role {role}")

    def test_majority_vote_eliminates(self, strategy: MafiaStrategy, players: list[str]) -> None:
        state = self._make_day_state(strategy, players)
        alive = [pid for pid, p in state["players"].items() if p["alive"]]
        target = alive[0]

        # Two others vote for the target
        actions = {
            alive[1]: {"target": target},
            alive[2]: {"target": target},
        }
        new_state, _ = strategy.evaluate_phase(state, actions)

        assert new_state["players"][target]["alive"] is False
        assert new_state["phase"] == PHASE_NIGHT
        assert new_state["round"] == 2

    def test_tie_no_elimination(self, strategy: MafiaStrategy, players: list[str]) -> None:
        state = self._make_day_state(strategy, players)
        alive = [pid for pid, p in state["players"].items() if p["alive"]]

        # Equal votes → tie → no elimination
        actions = {
            alive[0]: {"target": alive[1]},
            alive[2]: {"target": alive[3]},
        }
        new_state, _ = strategy.evaluate_phase(state, actions)

        assert new_state["players"][alive[1]]["alive"] is True
        assert new_state["players"][alive[3]]["alive"] is True

    def test_no_votes_day(self, strategy: MafiaStrategy, players: list[str]) -> None:
        state = self._make_day_state(strategy, players)
        new_state, _ = strategy.evaluate_phase(state, {})
        # No elimination, phase advances
        assert new_state["phase"] == PHASE_NIGHT


# ---------------------------------------------------------------------------
# check_win_condition
# ---------------------------------------------------------------------------

class TestWinCondition:
    def test_villagers_win_no_mafia(self, strategy: MafiaStrategy) -> None:
        state = {
            "players": {
                "a": {"role": ROLE_VILLAGER, "alive": True},
                "b": {"role": ROLE_HEALER, "alive": True},
                "c": {"role": ROLE_COP, "alive": True},
                "d": {"role": ROLE_MAFIA, "alive": False},
            }
        }
        assert strategy.check_win_condition(state) == "villagers"

    def test_mafia_wins_equal_numbers(self, strategy: MafiaStrategy) -> None:
        state = {
            "players": {
                "a": {"role": ROLE_MAFIA, "alive": True},
                "b": {"role": ROLE_VILLAGER, "alive": True},
                "c": {"role": ROLE_HEALER, "alive": False},
                "d": {"role": ROLE_COP, "alive": False},
            }
        }
        assert strategy.check_win_condition(state) == "mafia"

    def test_game_continues(self, strategy: MafiaStrategy) -> None:
        state = {
            "players": {
                "a": {"role": ROLE_MAFIA, "alive": True},
                "b": {"role": ROLE_VILLAGER, "alive": True},
                "c": {"role": ROLE_HEALER, "alive": True},
                "d": {"role": ROLE_COP, "alive": True},
            }
        }
        assert strategy.check_win_condition(state) is None
