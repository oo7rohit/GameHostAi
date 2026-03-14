"""Unit tests for the Phase 4 narration pipeline.

Covers: schemas, narration worker text generation, the handle_narration_response
callback (broadcast + AUDIO_TRIGGER + staleness), and state-machine narration
integration.
"""

import asyncio
import pytest
from unittest.mock import AsyncMock, MagicMock, patch

from app.schemas.narration import NarrationRequest, NarrationResponse
from app.schemas.messages import ServerEvent
from app.workers.narration_worker import (
    generate_narration_text,
    generate_audio_url,
    process_request,
)


# ---------------------------------------------------------------------------
# Schema validation
# ---------------------------------------------------------------------------


class TestNarrationSchemas:
    def test_narration_request_required_fields(self) -> None:
        req = NarrationRequest(
            room_id="room-1",
            event_context="night_kill",
            context_data={"killed": "alice"},
            turn_number=1,
        )
        assert req.room_id == "room-1"
        assert req.event_context == "night_kill"
        assert req.turn_number == 1

    def test_narration_request_default_context_data(self) -> None:
        req = NarrationRequest(
            room_id="room-1", event_context="game_start", turn_number=0
        )
        assert req.context_data == {}

    def test_narration_response_optional_audio(self) -> None:
        resp = NarrationResponse(
            room_id="room-1",
            narration_text="Hello",
            turn_number=1,
        )
        assert resp.audio_url is None

    def test_narration_response_with_audio(self) -> None:
        resp = NarrationResponse(
            room_id="room-1",
            narration_text="Hello",
            audio_url="/audio/room-1/turn_1.wav",
            turn_number=1,
        )
        assert resp.audio_url == "/audio/room-1/turn_1.wav"

    def test_narration_request_serialisation_roundtrip(self) -> None:
        req = NarrationRequest(
            room_id="r1",
            event_context="day_vote",
            context_data={"eliminated": "bob"},
            turn_number=3,
        )
        raw = req.model_dump_json()
        restored = NarrationRequest.model_validate_json(raw)
        assert restored == req

    def test_narration_response_serialisation_roundtrip(self) -> None:
        resp = NarrationResponse(
            room_id="r1",
            narration_text="The end.",
            audio_url="/audio/r1/turn_5.wav",
            turn_number=5,
        )
        raw = resp.model_dump_json()
        restored = NarrationResponse.model_validate_json(raw)
        assert restored == resp


# ---------------------------------------------------------------------------
# Worker — text generation
# ---------------------------------------------------------------------------


class TestNarrationWorker:
    def test_night_kill_template(self) -> None:
        req = NarrationRequest(
            room_id="r1",
            event_context="night_kill",
            context_data={"killed": "alice"},
            turn_number=1,
        )
        text = generate_narration_text(req)
        assert "alice" in text
        assert "dead" in text.lower()

    def test_night_save_template(self) -> None:
        req = NarrationRequest(
            room_id="r1",
            event_context="night_save",
            context_data={},
            turn_number=1,
        )
        text = generate_narration_text(req)
        assert "alive" in text.lower()

    def test_day_vote_template(self) -> None:
        req = NarrationRequest(
            room_id="r1",
            event_context="day_vote",
            context_data={"eliminated": "bob"},
            turn_number=2,
        )
        text = generate_narration_text(req)
        assert "bob" in text.lower()

    def test_game_start_template(self) -> None:
        req = NarrationRequest(
            room_id="r1",
            event_context="game_start",
            context_data={},
            turn_number=0,
        )
        text = generate_narration_text(req)
        assert len(text) > 0

    def test_game_over_template(self) -> None:
        req = NarrationRequest(
            room_id="r1",
            event_context="game_over",
            context_data={"winner": "villagers"},
            turn_number=5,
        )
        text = generate_narration_text(req)
        assert "villagers" in text.lower()

    def test_unknown_context_fallback(self) -> None:
        req = NarrationRequest(
            room_id="r1",
            event_context="unknown_event",
            context_data={},
            turn_number=1,
        )
        text = generate_narration_text(req)
        assert text == "The story continues..."

    def test_missing_template_key_graceful(self) -> None:
        """Template expects {killed} but context_data is empty — should not crash."""
        req = NarrationRequest(
            room_id="r1",
            event_context="night_kill",
            context_data={},  # missing 'killed'
            turn_number=1,
        )
        text = generate_narration_text(req)
        # Should return the raw template string (without formatting)
        assert len(text) > 0

    def test_audio_url_stub(self) -> None:
        req = NarrationRequest(
            room_id="room-42",
            event_context="game_start",
            context_data={},
            turn_number=3,
        )
        url = generate_audio_url(req)
        assert url == "/audio/room-42/turn_3.wav"

    @pytest.mark.asyncio
    async def test_process_request_returns_response(self) -> None:
        req = NarrationRequest(
            room_id="r1",
            event_context="game_start",
            context_data={},
            turn_number=0,
        )
        resp = await process_request(req)
        assert isinstance(resp, NarrationResponse)
        assert resp.room_id == "r1"
        assert resp.turn_number == 0
        assert len(resp.narration_text) > 0


# ---------------------------------------------------------------------------
# handle_narration_response callback
# ---------------------------------------------------------------------------


class TestHandleNarrationResponse:
    @pytest.fixture(autouse=True)
    def _setup_active_games(self):
        """Provide a mock state machine in active_games for room 'test-room'."""
        from app.engine.state_machine import active_games

        mock_sm = MagicMock()
        mock_sm.turn_number = 1
        mock_sm.game_state = {"phase": "DAY_VOTING"}

        active_games["test-room"] = mock_sm
        yield
        active_games.pop("test-room", None)

    @pytest.mark.asyncio
    @patch("app.api.websockets.manager")
    async def test_broadcasts_narration_to_all(self, mock_manager: MagicMock) -> None:
        from app.api.websockets import handle_narration_response

        mock_manager.broadcast_to_room = AsyncMock()
        mock_manager.get_speaker_socket = MagicMock(return_value=None)

        response = NarrationResponse(
            room_id="test-room",
            narration_text="The sun rises.",
            turn_number=1,
        )
        await handle_narration_response(response)

        mock_manager.broadcast_to_room.assert_called_once()
        call_args = mock_manager.broadcast_to_room.call_args
        event: ServerEvent = call_args[0][1]
        assert event.event_type == "narration"
        assert event.data["narration_text"] == "The sun rises."

    @pytest.mark.asyncio
    @patch("app.api.websockets.manager")
    async def test_audio_trigger_to_speaker_only(self, mock_manager: MagicMock) -> None:
        from app.api.websockets import handle_narration_response

        mock_speaker_ws = MagicMock()
        mock_manager.broadcast_to_room = AsyncMock()
        mock_manager.get_speaker_socket = MagicMock(return_value=mock_speaker_ws)
        mock_manager.send_personal_message = AsyncMock()

        response = NarrationResponse(
            room_id="test-room",
            narration_text="Night falls.",
            audio_url="/audio/test-room/turn_1.wav",
            turn_number=1,
        )
        await handle_narration_response(response)

        # Should broadcast narration
        mock_manager.broadcast_to_room.assert_called_once()

        # Should also send AUDIO_TRIGGER to speaker
        mock_manager.send_personal_message.assert_called_once()
        audio_call = mock_manager.send_personal_message.call_args
        audio_event: ServerEvent = audio_call[0][0]
        assert audio_event.event_type == "AUDIO_TRIGGER"
        assert audio_event.data["audio_url"] == "/audio/test-room/turn_1.wav"
        assert audio_call[0][1] is mock_speaker_ws

    @pytest.mark.asyncio
    @patch("app.api.websockets.manager")
    async def test_no_audio_trigger_without_url(self, mock_manager: MagicMock) -> None:
        from app.api.websockets import handle_narration_response

        mock_manager.broadcast_to_room = AsyncMock()
        mock_manager.get_speaker_socket = MagicMock(return_value=MagicMock())
        mock_manager.send_personal_message = AsyncMock()

        response = NarrationResponse(
            room_id="test-room",
            narration_text="Silence.",
            audio_url=None,
            turn_number=1,
        )
        await handle_narration_response(response)

        # No AUDIO_TRIGGER when audio_url is None
        mock_manager.send_personal_message.assert_not_called()

    @pytest.mark.asyncio
    @patch("app.api.websockets.manager")
    async def test_stale_turn_number_discards(self, mock_manager: MagicMock) -> None:
        from app.api.websockets import handle_narration_response

        mock_manager.broadcast_to_room = AsyncMock()

        response = NarrationResponse(
            room_id="test-room",
            narration_text="Old narration.",
            turn_number=99,  # does not match mock_sm.turn_number=1
        )
        await handle_narration_response(response)

        # Should NOT broadcast anything
        mock_manager.broadcast_to_room.assert_not_called()

    @pytest.mark.asyncio
    @patch("app.api.websockets.manager")
    async def test_unknown_room_discards(self, mock_manager: MagicMock) -> None:
        from app.api.websockets import handle_narration_response

        mock_manager.broadcast_to_room = AsyncMock()

        response = NarrationResponse(
            room_id="nonexistent-room",
            narration_text="Ghost narration.",
            turn_number=1,
        )
        await handle_narration_response(response)

        mock_manager.broadcast_to_room.assert_not_called()


# ---------------------------------------------------------------------------
# State machine narration integration
# ---------------------------------------------------------------------------


class TestStateMachineNarration:
    @pytest.mark.asyncio
    @patch("app.engine.state_machine.save_game_state", new_callable=AsyncMock)
    async def test_turn_number_increments_on_resolve(self, mock_save: AsyncMock) -> None:
        from app.engine.games.mafia import MafiaStrategy
        from app.engine.state_machine import GameStateMachine, GamePhase

        mock_conn_mgr = MagicMock()
        mock_conn_mgr.broadcast_to_room = AsyncMock()
        mock_conn_mgr.send_personal_message = AsyncMock()
        mock_conn_mgr.active_rooms = {}

        sm = GameStateMachine(
            room_id="r1",
            strategy=MafiaStrategy(),
            conn_mgr=mock_conn_mgr,
        )

        assert sm.turn_number == 0

        players = ["a", "b", "c", "d", "e", "f"]
        await sm.start_game(players)
        assert sm.turn_number == 0  # start_game does not increment

        # Submit all actions to trigger early resolution
        for pid in players:
            await sm.queue_action(pid, {"target": players[0]})

        assert sm.turn_number == 1  # first resolution increments

    @pytest.mark.asyncio
    @patch("app.engine.state_machine.save_game_state", new_callable=AsyncMock)
    async def test_publish_narration_called_on_resolve(self, mock_save: AsyncMock) -> None:
        from app.engine.games.mafia import MafiaStrategy
        from app.engine.state_machine import GameStateMachine

        mock_conn_mgr = MagicMock()
        mock_conn_mgr.broadcast_to_room = AsyncMock()
        mock_conn_mgr.send_personal_message = AsyncMock()
        mock_conn_mgr.active_rooms = {}

        mock_publisher = MagicMock()
        mock_publisher.publish_narration_request = AsyncMock()

        sm = GameStateMachine(
            room_id="r1",
            strategy=MafiaStrategy(),
            conn_mgr=mock_conn_mgr,
            rabbitmq_publisher=mock_publisher,
        )

        players = ["a", "b", "c", "d", "e", "f"]
        await sm.start_game(players)

        # start_game should have published "game_start" narration
        # (fire-and-forget via create_task, so we need to let the event loop run)
        await asyncio.sleep(0)
        mock_publisher.publish_narration_request.assert_called()

        # Check the first call was for game_start
        first_call_req = mock_publisher.publish_narration_request.call_args_list[0][0][0]
        assert first_call_req.event_context == "game_start"
        assert first_call_req.room_id == "r1"

    @pytest.mark.asyncio
    @patch("app.engine.state_machine.save_game_state", new_callable=AsyncMock)
    async def test_no_publish_without_publisher(self, mock_save: AsyncMock) -> None:
        """When rabbitmq_publisher is None, narration should silently skip."""
        from app.engine.games.mafia import MafiaStrategy
        from app.engine.state_machine import GameStateMachine

        mock_conn_mgr = MagicMock()
        mock_conn_mgr.broadcast_to_room = AsyncMock()
        mock_conn_mgr.send_personal_message = AsyncMock()
        mock_conn_mgr.active_rooms = {}

        sm = GameStateMachine(
            room_id="r1",
            strategy=MafiaStrategy(),
            conn_mgr=mock_conn_mgr,
            rabbitmq_publisher=None,  # No RabbitMQ
        )

        players = ["a", "b", "c", "d", "e", "f"]
        # Should not raise even without publisher
        await sm.start_game(players)

        for pid in players:
            await sm.queue_action(pid, {"target": players[0]})

        # If we got here without exceptions, the test passes
        assert sm.turn_number == 1
