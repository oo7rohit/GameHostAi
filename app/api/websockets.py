import json
import logging
from fastapi import APIRouter, Depends, Query, WebSocket, WebSocketDisconnect
from app.core.connection_manager import manager
from app.core.rabbitmq import rabbitmq_client
from app.core.redis import (
    get_room_meta,
    get_room_state,
    set_player_offline,
    set_player_online,
    set_room_meta,
)
from app.engine.games.mafia import MafiaStrategy
from app.engine.state_machine import GameStateMachine, active_games
from app.schemas.messages import ClientAction, ServerEvent
from app.schemas.narration import NarrationResponse
from app.schemas.session import GameName, PlayerSession, RoomInfo

logger = logging.getLogger(__name__)

router = APIRouter()


async def _build_room_payload(room: RoomInfo) -> dict:
    """Build a room snapshot payload suitable for UI consumption."""
    room_state = await get_room_state(room.id)
    players: list[dict] = []
    for player_id, meta in room_state.items():
        if isinstance(meta, dict):
            if meta.get("status") == "offline":
                continue
            players.append(
                {
                    "player_id": player_id,
                    "player_name": meta.get("player_name") or player_id,
                    "is_speaker": bool(meta.get("is_speaker", False)),
                }
            )
        else:
            players.append({"player_id": player_id, "player_name": player_id, "is_speaker": False})

    return {
        "id": room.id,
        "game_name": room.game_name.value,
        "players": players,
    }


def _room_from_request(
    room_id: str,
    game_name: GameName = Query(..., description="Game to play in this room"),
) -> RoomInfo:
    return RoomInfo(id=room_id, game_name=game_name)


def _player_session_from_request(
    player_id: str,
    player_name: str = Query(..., min_length=1, description="Human-readable player name"),
    is_speaker: bool = Query(False, description="Flag indicating if the player is the speaker"),
) -> PlayerSession:
    return PlayerSession(player_id=player_id, player_name=player_name, is_speaker=is_speaker)


@router.websocket("/ws/{room_id}/{player_id}")
async def websocket_endpoint(
    websocket: WebSocket,
    room: RoomInfo = Depends(_room_from_request),
    player: PlayerSession = Depends(_player_session_from_request),
) -> None:
    registered = False
    conn_id: str | None = None

    persisted_room = await get_room_meta(room.id)
    if persisted_room is None:
        await set_room_meta(room)
    elif persisted_room.game_name != room.game_name:
        # Accept then immediately close with a policy violation; don't register this socket.
        await websocket.accept()
        await websocket.send_text(
            ServerEvent(
                event_type="error",
                data={
                    "message": (
                        f"Room {room.id} is configured for game {persisted_room.game_name.value}; "
                        f"cannot join with game {room.game_name.value}."
                    ),
                },
            ).model_dump_json()
        )
        await websocket.close(code=1008)
        return

    try:
        conn_id = await manager.connect(websocket, room.id, player)
        registered = True
        await set_player_online(room.id, player, conn_id)

        room_payload = await _build_room_payload(room)
        join_event = ServerEvent(
            event_type="system_event",
            data={
                "message": f"Player {player.player_name} joined.",
                "room": room_payload,
                "player": player.model_dump(),
            },
        )
        await manager.broadcast_to_room(room.id, join_event)

        while True:
            try:
                data = await websocket.receive_text()
            except (WebSocketDisconnect, RuntimeError):
                # Starlette can raise RuntimeError if the socket is already closed.
                break

            try:
                msg_data = json.loads(data)
                client_action = ClientAction(**msg_data)

                # ----- Game engine integration -------------------------
                if client_action.action_type == "start_game":
                    await _handle_start_game(room)

                elif room.id in active_games:
                    # Forward action to the running state machine
                    state_machine = active_games[room.id]
                    await state_machine.queue_action(player.player_id, client_action.payload)

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
                logger.error(f"Failed to process message from {player.player_id}: {e}")
                err_response = ServerEvent(
                    event_type="error",
                    data={"message": "Invalid payload format."},
                )
                await manager.send_personal_message(err_response, websocket)
    except Exception:
        logger.exception("Unhandled websocket error for room %s player %s", room.id, player.player_id)
    finally:
        if not registered or conn_id is None:
            return
        removed = manager.disconnect(room.id, player.player_id, conn_id)
        if not removed:
            return
        room_payload: dict | None = None
        try:
            await set_player_offline(room.id, player.player_id, conn_id)
        except Exception:
            logger.exception("Failed to mark player offline for room %s player %s", room.id, player.player_id)
        try:
            room_payload = await _build_room_payload(room)
        except Exception:
            logger.exception("Failed to build room payload for disconnect broadcast in room %s", room.id)

        disconnect_event = ServerEvent(
            event_type="system_event",
            data={
                "message": f"Player {player.player_name} left.",
                "room": room_payload or {"id": room.id, "game_name": room.game_name.value, "players": []},
                "player": player.model_dump(),
            },
        )
        await manager.broadcast_to_room(room.id, disconnect_event)
        logger.info("Client %s disconnected", player.player_id)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

async def _handle_start_game(room: RoomInfo) -> None:
    """Create a GameStateMachine for the room and start the game."""
    room_id = room.id
    if room_id in active_games:
        logger.warning("start_game ignored — game already running in room %s", room_id)
        err = ServerEvent(
            event_type="error",
            data={"message": "A game is already running in this room."},
        )
        await manager.broadcast_to_room(room_id, err)
        return

    # Gather active player IDs from Redis room state
    room_state = await get_room_state(room_id)
    player_ids = [
        pid for pid, meta in room_state.items()
        if not (isinstance(meta, dict) and meta.get("status") == "offline")
    ]

    if len(player_ids) < 4:
        err = ServerEvent(
            event_type="error",
            data={"message": f"Need at least 4 players to start (currently {len(player_ids)})."},
        )
        await manager.broadcast_to_room(room_id, err)
        return

    # Strategy selection based on the room's configured game.
    if room.game_name == GameName.MAFIA:
        strategy = MafiaStrategy()
    else:
        err = ServerEvent(
            event_type="error",
            data={"message": f"Unsupported game: {room.game_name.value}"},
        )
        await manager.broadcast_to_room(room_id, err)
        return

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
