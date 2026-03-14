"""Unit tests for GameStateMachine.

Uses mocked Redis and ConnectionManager to test the lifecycle in isolation.
"""

import asyncio
import pytest
from unittest.mock import AsyncMock, MagicMock, patch

from app.engine.games.mafia import MafiaStrategy, PHASE_NIGHT, PHASE_DAY, ROLE_MAFIA, ROLE_VILLAGER
from app.engine.state_machine import GameStateMachine, GamePhase


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def mock_conn_mgr() -> MagicMock:
    """A mock ConnectionManager with async methods."""
    mgr = MagicMock()
    mgr.broadcast_to_room = AsyncMock()
    mgr.send_personal_message = AsyncMock()
    mgr.active_rooms = {}
    return mgr


@pytest.fixture
def players() -> list[str]:
    return ["alice", "bob", "charlie", "diana", "eve", "frank"]


@pytest.fixture
def state_machine(mock_conn_mgr: MagicMock) -> GameStateMachine:
    strategy = MafiaStrategy()
    return GameStateMachine(room_id="test-room", strategy=strategy, conn_mgr=mock_conn_mgr)


# ---------------------------------------------------------------------------
# Lifecycle
# ---------------------------------------------------------------------------

class TestGameLifecycle:
    @pytest.mark.asyncio
    @patch("app.engine.state_machine.save_game_state", new_callable=AsyncMock)
    async def test_initial_phase_is_lobby(
        self, mock_save: AsyncMock, state_machine: GameStateMachine
    ) -> None:
        assert state_machine.game_phase == GamePhase.LOBBY

    @pytest.mark.asyncio
    @patch("app.engine.state_machine.save_game_state", new_callable=AsyncMock)
    async def test_start_game_transitions_to_in_progress(
        self, mock_save: AsyncMock, state_machine: GameStateMachine, players: list[str]
    ) -> None:
        await state_machine.start_game(players)
        assert state_machine.game_phase == GamePhase.IN_PROGRESS
        assert state_machine.game_state["phase"] == PHASE_NIGHT

    @pytest.mark.asyncio
    @patch("app.engine.state_machine.save_game_state", new_callable=AsyncMock)
    async def test_start_game_persists_state(
        self, mock_save: AsyncMock, state_machine: GameStateMachine, players: list[str]
    ) -> None:
        await state_machine.start_game(players)
        mock_save.assert_called_once_with("test-room", state_machine.game_state)

    @pytest.mark.asyncio
    @patch("app.engine.state_machine.save_game_state", new_callable=AsyncMock)
    async def test_start_game_broadcasts_and_sends_roles(
        self, mock_save: AsyncMock, state_machine: GameStateMachine, players: list[str], mock_conn_mgr: MagicMock
    ) -> None:
        # Set up mock active_rooms so _send_to_player can find sockets
        mock_sockets = {}
        for pid in players:
            mock_ws = MagicMock()
            mock_sockets[pid] = {"socket": mock_ws, "is_speaker": False}
        mock_conn_mgr.active_rooms = {"test-room": mock_sockets}

        await state_machine.start_game(players)
        
        # Should broadcast game_started event
        mock_conn_mgr.broadcast_to_room.assert_called()
        # Should send personal role messages to each player
        assert mock_conn_mgr.send_personal_message.call_count == len(players)

    @pytest.mark.asyncio
    @patch("app.engine.state_machine.save_game_state", new_callable=AsyncMock)
    async def test_start_game_ignores_if_not_lobby(
        self, mock_save: AsyncMock, state_machine: GameStateMachine, players: list[str]
    ) -> None:
        state_machine.game_phase = GamePhase.IN_PROGRESS
        await state_machine.start_game(players)
        mock_save.assert_not_called()


# ---------------------------------------------------------------------------
# Action queuing
# ---------------------------------------------------------------------------

class TestActionQueuing:
    @pytest.mark.asyncio
    @patch("app.engine.state_machine.save_game_state", new_callable=AsyncMock)
    async def test_queue_action_stores_idempotently(
        self, mock_save: AsyncMock, state_machine: GameStateMachine, players: list[str]
    ) -> None:
        await state_machine.start_game(players)
        mock_save.reset_mock()

        # Queue two actions from the same player — second should overwrite
        await state_machine.queue_action("alice", {"target": "bob"})
        await state_machine.queue_action("alice", {"target": "charlie"})

        assert state_machine.current_phase_actions["alice"] == {"target": "charlie"}

    @pytest.mark.asyncio
    @patch("app.engine.state_machine.save_game_state", new_callable=AsyncMock)
    async def test_queue_action_ignored_when_not_in_progress(
        self, mock_save: AsyncMock, state_machine: GameStateMachine
    ) -> None:
        # Still in LOBBY
        await state_machine.queue_action("alice", {"target": "bob"})
        assert len(state_machine.current_phase_actions) == 0


# ---------------------------------------------------------------------------
# Phase resolution
# ---------------------------------------------------------------------------

class TestPhaseResolution:
    @pytest.mark.asyncio
    @patch("app.engine.state_machine.save_game_state", new_callable=AsyncMock)
    async def test_early_resolution_when_all_alive_submit(
        self, mock_save: AsyncMock, state_machine: GameStateMachine, players: list[str]
    ) -> None:
        await state_machine.start_game(players)
        mock_save.reset_mock()

        # Submit actions from all alive players to trigger early resolution
        for pid in players:
            await state_machine.queue_action(pid, {"target": players[0]})

        # Phase should have resolved and transitioned
        assert state_machine.game_state["phase"] == PHASE_DAY
        # save_game_state should have been called for the resolution
        assert mock_save.called

    @pytest.mark.asyncio
    @patch("app.engine.state_machine.save_game_state", new_callable=AsyncMock)
    async def test_is_resolving_prevents_double_execution(
        self, mock_save: AsyncMock, state_machine: GameStateMachine, players: list[str]
    ) -> None:
        await state_machine.start_game(players)
        mock_save.reset_mock()

        # Manually set _is_resolving
        state_machine._is_resolving = True
        await state_machine._resolve_phase()

        # Should have returned early — no save call
        mock_save.assert_not_called()

    @pytest.mark.asyncio
    @patch("app.engine.state_machine.save_game_state", new_callable=AsyncMock)
    async def test_timer_task_cancelled_on_early_resolution(
        self, mock_save: AsyncMock, state_machine: GameStateMachine, players: list[str]
    ) -> None:
        await state_machine.start_game(players)

        # Verify timer was started
        assert state_machine.timer_task is not None
        timer = state_machine.timer_task
        assert not timer.done()

        # Trigger early resolution
        for pid in players:
            await state_machine.queue_action(pid, {"target": players[0]})

        # Let event loop process the cancellation
        await asyncio.sleep(0)

        # Original timer should be cancelled
        assert timer.cancelled()
