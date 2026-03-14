# Project Overview
We are building the backend for a highly scalable, real-time multiplayer party game engine. The first game supported will be "Mafia," but the architecture must be completely game-agnostic. The client devices (mobile/web) are "dumb terminals" that only render UI components based on JSON schemas sent by the server. 

# Core Tech Stack
* **Framework:** FastAPI (Python 3.10+)
* **In-Memory Store:** Redis (for live WebSocket session management, temporary game state, and timers)
* **Persistent Database:** PostgreSQL (for user profiles, game history, and static game rulesets/JSON configurations)
* **Message Queue:** RabbitMQ (for decoupling the fast game engine from slow, asynchronous tasks like AI text generation and TTS audio processing)
* **ORM:** SQLAlchemy (Async)

# Architectural Requirements
1. **Event-Driven WebSockets:** Real-time bidirectional communication. The server pushes state changes; the client pushes user action payloads.
2. **Abstract Game State Machine:** The core engine manages `Rooms`, `Sessions`, and `Phases` (e.g., Day/Night) without knowing the specific rules of the game.
3. **Strategy Pattern for Game Logic:** Game-specific rules (like Mafia) are implemented as isolated Strategies. The core engine evaluates player actions against the active Strategy.
4. **The "Dual-Role" Speaker Node:** The backend must support assigning a specific `is_speaker` flag to a player's connection. This node receives standard game state updates AND separate `AUDIO_TRIGGER` events to play narration via a background thread, allowing the host to play anonymously.
5. **Simultaneous Actions:** The engine must handle concurrent incoming WebSocket payloads during a phase (e.g., multiple Mafia voting, Healer acting) and resolve them simultaneously when the phase timer expires.

# Phase 1: Project Initialization & Scaffolding
I want to start by setting up the foundational backend infrastructure. Please generate the following:

1. A clean, production-ready directory structure for this FastAPI project (separating routers, core game logic, models, schemas, and worker tasks).
2. A `docker-compose.yml` file that spins up PostgreSQL, Redis, and RabbitMQ.
3. The foundational `main.py` file with the FastAPI app initialization and a basic WebSocket router that can handle a client connecting, joining a "Room" (tracked in Redis), and echoing messages. 

Keep the code modular, strictly typed (using Pydantic), and fully asynchronous.