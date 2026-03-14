# Phase 4: The AI Narrator Pipeline (RabbitMQ Workers & Speaker Node)
We are continuing our FastAPI/Redis multiplayer game backend. In Phase 3, we built the core game engine (Strategy Pattern + State Machine). Now, we are building the AI narration pipeline — the feature that makes the host anonymous and the game immersive.

**CRITICAL CONSTRAINT:** The narration pipeline must be fully **decoupled** from the game engine via RabbitMQ. The fast game loop must never block waiting for slow AI text generation or TTS audio processing. The engine publishes events; workers consume and respond asynchronously.

# Architectural Requirements

## 1. The Narration Event Schema (`app/schemas/narration.py`)
Define the data contracts for the narration pipeline:
* **`NarrationRequest`:** Published by the game engine to RabbitMQ when a phase resolves. Must include `room_id`, `event_context` (e.g., "night_kill", "day_vote", "game_start"), and `context_data` (dict with relevant game state like who was killed, who was saved, vote tallies, etc.).
* **`NarrationResponse`:** Consumed from RabbitMQ by the API server. Must include `room_id`, `narration_text` (the AI-generated narration string), and an optional `audio_url` (a URL/path to the generated TTS audio file).

## 2. The RabbitMQ Publisher (`app/core/rabbitmq.py`)
Create an async RabbitMQ client wrapper using `aio-pika`.
* Implement a `publish_narration_request(request: NarrationRequest)` function that serialises and publishes to a `narration_requests` queue.
* Implement a `consume_narration_responses(callback)` function that listens on a `narration_responses` queue and invokes a callback for each response.
* Handle connection lifecycle (connect on app startup, graceful close on shutdown) via the FastAPI lifespan.

## 3. The Narration Worker (`app/workers/narration_worker.py`)
Create a standalone worker process (runnable outside the FastAPI server) that:
* Connects to RabbitMQ and consumes from the `narration_requests` queue.
* For each `NarrationRequest`, generates narration text. For now, implement a **template-based stub** (e.g., "The town sleeps uneasily... {player} was found dead at dawn.") — this will be replaced with an LLM call in a future phase.
* Optionally generates a TTS audio stub (just creates a placeholder file path for now).
* Publishes the resulting `NarrationResponse` back to the `narration_responses` queue.

## 4. Engine → RabbitMQ Integration (`app/engine/state_machine.py`)
After the `GameStateMachine` resolves a phase and broadcasts the public state:
* Construct a `NarrationRequest` from the resolution context (who was killed, vote results, phase transitions, game over, etc.).
* Publish it to RabbitMQ via the publisher. This must be **fire-and-forget** — the game loop does not wait for narration.

## 5. The Speaker Node — AUDIO_TRIGGER Events (`app/api/websockets.py`)
When a `NarrationResponse` is consumed from RabbitMQ by the API server:
* Broadcast the `narration_text` to **all** players in the room as a `ServerEvent` with `event_type: "narration"`.
* Send an **`AUDIO_TRIGGER`** event **only** to the player connection flagged as `is_speaker` in that room. This event must include the `audio_url` so the speaker device can play the audio aloud for the group.
* Use the `ConnectionManager`'s existing `is_speaker` metadata to identify the correct socket.

## 6. Update `app/main.py` — Lifespan Wiring
* On startup: initialise the RabbitMQ connection and start consuming `narration_responses` as a background task.
* On shutdown: gracefully close the RabbitMQ connection and cancel the consumer task.

## 7. Robustness & Edge Cases (CRITICAL)
* **Sequence Tracking:** Add a `turn_number` integer to both `NarrationRequest` and `NarrationResponse`. The FastAPI consumer MUST verify that the room's current `turn_number` matches the response before broadcasting the `AUDIO_TRIGGER` to prevent stale audio from playing in the wrong phase.
* **Worker QoS:** In the `narration_worker.py`, you must configure the `aio-pika` channel with `prefetch_count=1` to ensure fair dispatching across multiple worker instances.
* **Fallback Handling:** If the worker encounters an error (e.g., a simulated API timeout), it must yield a fallback `NarrationResponse` with generic text and `audio_url: None` so the game is not permanently blocked.

Please generate the narration schemas, the RabbitMQ publisher/consumer, the narration worker stub, the engine integration, the speaker node AUDIO_TRIGGER logic, and the lifespan updates. Ensure strong typing, full async, and clean separation between the fast game loop and the slow narration pipeline.
