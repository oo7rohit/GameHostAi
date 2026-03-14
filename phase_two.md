# Phase 2: Real-Time Communication & Ephemeral State Management
We are continuing the development of our scalable multiplayer game engine using FastAPI and Redis. In Phase 1, we set up the directory structure and Docker environment. In this phase, we are building the communication plumbing. 

**CRITICAL CONSTRAINT:** Do not write any game-specific logic (e.g., Mafia rules, Day/Night cycles) in this phase. We are strictly building the data contracts and connection management.

# Architectural Requirements

## 1. Data Contracts (Pydantic Schemas)
Define strict JSON schemas for bidirectional communication to ensure the clients act as "dumb terminals."
* **`ClientAction`:** The standard payload sent from the client to the server (must include `action_type` and a generic `payload` dict).
* **`ServerEvent`:** The standard payload sent from the server to the client (must include `event_type`, current `phase`, and a generic `data` dict).

## 2. The Connection Manager (In-Memory / App Level)
Create a robust `ConnectionManager` class to handle FastAPI `WebSocket` objects.
* It must map connections using a composite key: `Room_ID` + `Player_ID`.
* It needs methods for: `connect`, `disconnect`, `broadcast_to_room`, and `send_personal_message`.
* **The "Dual-Role" Requirement:** The `connect` method must accept an optional `is_speaker` boolean flag and store this metadata alongside the WebSocket object so the server knows which connection in a room should receive `AUDIO_TRIGGER` events later.

## 3. Ephemeral State Store (Redis Integration)
WebSockets are fragile; players will drop connection on mobile networks. The "Truth" of a room's state cannot live in the WebSocket manager's memory.
* Implement a Redis client wrapper (using `redis-py` async).
* When a player connects via WebSocket, the server must push their `Player_ID` and `is_speaker` status to a Redis Hash or Set linked to that `Room_ID`.
* If a WebSocket disconnects, the player remains in the Redis `Room_ID` state. They are simply marked as "offline."

## 4. The WebSocket Router
Create the FastAPI router endpoint (`/ws/{room_id}/{player_id}`) and accept additional room/player metadata via query params (e.g., `game_name`, `player_name`).
* On connection, validate the room in Redis, accept the socket, and broadcast a join event.
* Set up the async receive loop: parse incoming text, validate it against the `ClientAction` schema, and (for now) just echo back an acknowledgment using the `ServerEvent` schema.
* Handle `WebSocketDisconnect` gracefully without crashing the server thread.

Please generate the Pydantic schemas, the Connection Manager class, the Redis utility functions, and the WebSocket router to fulfill these requirements. Keep the code strictly typed and fully asynchronous.
