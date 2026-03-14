# Phase 3: Core Game Engine (Abstract State Machine & Strategy Pattern)
We are continuing our FastAPI/Redis multiplayer game backend. In Phase 2, we built the `ConnectionManager` and Ephemeral State (Redis). Now, we are building the core game engine.

**CRITICAL CONSTRAINT:** The core state machine MUST NOT contain any game-specific logic. All game rules must be injected via the Strategy Pattern.

# Architectural Requirements

## 1. The Strategy Interface (`app/engine/strategy.py`)
Create an Abstract Base Class (`BaseGameStrategy`) using Python's `abc` module. This is the contract that any specific game (like Mafia) must fulfill.
* Define abstract methods:
  * `initialize_game(players: list) -> dict`: Assigns roles and sets up the initial game state.
  * `evaluate_phase(current_state: dict, actions: list) -> dict`: Takes all queued actions from a phase, resolves them according to the rules, and returns the mutated game state.
  * `check_win_condition(current_state: dict) -> str | None`: Returns the winning team or None if the game continues.

## 2. The Concrete Strategy (`app/engine/games/mafia.py`)
Implement the first concrete strategy: `MafiaStrategy` inheriting from `BaseGameStrategy`.
* Implement the methods to handle basic Mafia logic (assigning Mafia/Villager/Healer/Cop, resolving a Night phase where a kill and a heal happen simultaneously, and checking if Mafia equals Villagers for a win).

## 3. The Abstract Game State Machine (`app/engine/state_machine.py`)
Create the `GameStateMachine` class. This manages the lifecycle of a single room.
* It must hold a reference to the active `BaseGameStrategy`.
* It must manage standard phases: `LOBBY`, `IN_PROGRESS` (with sub-phases like `DAY_VOTING`, `NIGHT_ACTIONS`), and `FINISHED`.
* **Action Queuing:** Implement a mechanism to collect incoming `ClientAction` payloads from the WebSocket router during an active phase. 
* **Phase Resolution:** When a timer expires (or all players submit an action), the state machine passes the queued actions to the Strategy's `evaluate_phase()` method, updates the authoritative state in Redis, and broadcasts the new state back to the `ConnectionManager`.

## 4. Router Integration (`app/api/websockets.py`)
Update the Phase 2 WebSocket router to actually pass incoming validated `ClientAction` payloads into the `GameStateMachine`'s action queue for that specific room, rather than just echoing them back.

Please generate the abstract interfaces, the concrete Mafia strategy, the state machine class, and the updated router integration. Ensure strong typing and asynchronous methods where Redis interaction is required.