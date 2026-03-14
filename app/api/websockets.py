import json
import logging
from fastapi import APIRouter, WebSocket, WebSocketDisconnect, Query
from app.core.connection_manager import manager
from app.core.rabbitmq import rabbitmq_client
from app.core.redis import set_player_online, set_player_offline, get_room_state
from app.engine.games.mafia import MafiaStrategy
from app.engine.state_machine import GameStateMachine, active_games
from app.schemas.messages import ClientAction, ServerEvent
from app.schemas.narration import NarrationResponse

logger = logging.getLogger(__name__)

router = APIRouter()


@router.websocket("/ws/{room_id}/{player_id}")
async def websocket_endpoint(
    websocket: WebSocket,
    room_id: str,
    player_id: str,
    is_speaker: bool = Query(False, description="Flag indicating if the player is the speaker"),
) -> None:
    await manager.connect(websocket, room_id, player_id, is_speaker)
    await set_player_online(room_id, player_id, is_speaker)

    join_event = ServerEvent(
        event_type="system_event",
        data={"message": f"Player {player_id} joined room {room_id}"},
    )
    await manager.broadcast_to_room(room_id, join_event)

    try:
        while True:
            data = await websocket.receive_text()

            try:
                msg_data = json.loads(data)
                client_action = ClientAction(**msg_data)

                # ----- Game engine integration -------------------------
                if client_action.action_type == "start_game":
                    await _handle_start_game(room_id)

                elif room_id in active_games:
                    # Forward action to the running state machine
                    state_machine = active_games[room_id]
                    await state_machine.queue_action(player_id, client_action.payload)

                else:
                    # No active game — echo back for now
                    response = ServerEvent(
                        event_type="echo_reply",
                        data={
                            "original_action": client_action.action_type,
                            "echoed_payload": client_action.payload,
                        },
                    )
                    await manager.send_personal_message(response, websocket)

            except Exception as e:
                logger.error(f"Failed to process message from {player_id}: {e}")
                err_response = ServerEvent(
                    event_type="error",
                    data={"message": "Invalid payload format."},
                )
                await manager.send_personal_message(err_response, websocket)

    except WebSocketDisconnect:
        manager.disconnect(room_id, player_id)
        await set_player_offline(room_id, player_id)

        disconnect_event = ServerEvent(
            event_type="system_event",
            data={"message": f"Player {player_id} disconnected from room {room_id}"},
        )
        await manager.broadcast_to_room(room_id, disconnect_event)
        logger.info(f"Client {player_id} disconnected")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

async def _handle_start_game(room_id: str) -> None:
    """Create a GameStateMachine for the room and start the game."""
    if room_id in active_games:
        logger.warning("start_game ignored — game already running in room %s", room_id)
        err = ServerEvent(
            event_type="error",
            data={"message": "A game is already running in this room."},
        )
        await manager.broadcast_to_room(room_id, err)
        return

    # Gather player IDs from Redis room state
    room_state = await get_room_state(room_id)
    player_ids = list(room_state.keys())

    if len(player_ids) < 4:
        err = ServerEvent(
            event_type="error",
            data={"message": f"Need at least 4 players to start (currently {len(player_ids)})."},
        )
        await manager.broadcast_to_room(room_id, err)
        return

    # Create the state machine with the Mafia strategy
    strategy = MafiaStrategy()
    state_machine = GameStateMachine(
        room_id=room_id,
        strategy=strategy,
        conn_mgr=manager,
        rabbitmq_publisher=rabbitmq_client,
    )
    active_games[room_id] = state_machine

    await state_machine.start_game(player_ids)
    logger.info("GameStateMachine created and game started for room %s", room_id)


# ---------------------------------------------------------------------------
# Narration response handler (called by the RabbitMQ consumer)
# ---------------------------------------------------------------------------

async def handle_narration_response(response: NarrationResponse) -> None:
    """Process a NarrationResponse consumed from RabbitMQ.

    1. Verify that the room's current turn_number matches the response
       to prevent stale narration from reaching the wrong phase.
    2. Broadcast narration_text to ALL players in the room.
    3. Send an AUDIO_TRIGGER event ONLY to the speaker connection.
    """
    room_id = response.room_id

    # --- Staleness check ---------------------------------------------------
    state_machine = active_games.get(room_id)
    if state_machine is None:
        logger.warning("Narration response for unknown room %s — discarding.", room_id)
        return

    if state_machine.turn_number != response.turn_number:
        logger.warning(
            "Stale narration for room %s: response turn %d != current turn %d — discarding.",
            room_id,
            response.turn_number,
            state_machine.turn_number,
        )
        return

    # --- Broadcast narration text to all players ---------------------------
    narration_event = ServerEvent(
        event_type="narration",
        phase=state_machine.game_state.get("phase"),
        data={"narration_text": response.narration_text},
    )
    await manager.broadcast_to_room(room_id, narration_event)

    # --- AUDIO_TRIGGER to speaker only ------------------------------------
    if response.audio_url:
        speaker_socket = manager.get_speaker_socket(room_id)
        if speaker_socket is not None:
            audio_event = ServerEvent(
                event_type="AUDIO_TRIGGER",
                phase=state_machine.game_state.get("phase"),
                data={"audio_url": response.audio_url},
            )
            await manager.send_personal_message(audio_event, speaker_socket)
            logger.info("AUDIO_TRIGGER sent to speaker in room %s.", room_id)
        else:
            logger.debug("No speaker connected in room %s — skipping AUDIO_TRIGGER.", room_id)
