# Frontend Development Prompt — GameHostAI React + TypeScript Client

You are building the **frontend client** for **GameHostAI**, a real-time multiplayer party game engine. The backend is already fully built with FastAPI, Redis, and RabbitMQ. The frontend must be a **"dumb terminal"** — it renders UI based on JSON events pushed from the server over WebSockets and sends structured JSON actions back. It does **not** contain any game logic.

Use **TypeScript** and **React** (with Vite as the bundler). Use a modern component library or hand-crafted CSS for a polished, immersive party-game aesthetic (dark theme, vibrant accents, smooth animations).

---

## 1. High-Level Architecture

```
┌───────────────────────────────┐         WebSocket (JSON)         ┌─────────────────────────┐
│        React Client           │ ◄─────────────────────────────► │   FastAPI Backend        │
│  (TypeScript / Vite / React)  │   ws://<host>/ws/{room}/{player}?game_name=Mafia&player_name=Alice │   (Python 3.10+)        │
│                               │                                  │   Redis · RabbitMQ      │
│  • Renders UI from ServerEvent│                                  │   GameStateMachine      │
│  • Sends ClientAction payloads│                                  │   MafiaStrategy         │
│  • Plays audio (speaker node) │                                  │   Narration Worker      │
└───────────────────────────────┘                                  └─────────────────────────┘
```

The client connects via a **single WebSocket** per player per room. All communication is through two JSON schemas described below.

---

## 2. WebSocket Connection

### Endpoint
```
ws://<BACKEND_HOST>/ws/{room_id}/{player_id}?game_name=Mafia&player_name=Alice&is_speaker=false
```

| Path / Query Param | Type    | Description |
|---------------------|---------|-------------|
| `room_id`           | string  | Unique room identifier |
| `player_id`         | string  | Unique player identifier |
| `game_name`         | enum    | Query param. Currently only `Mafia` is supported. |
| `player_name`       | string  | Query param. Human-readable player display name. |
| `is_speaker`        | boolean | Query param, default `false`. Set to `true` for the **one** device in the room that will play narration audio aloud for the group (the "Speaker Node"). |

### Connection Lifecycle
- On connect: the backend accepts the socket, registers the player in Redis, and broadcasts a `system_event` to all room members.
- On disconnect: the backend marks the player as "offline" in Redis (they remain in the room state) and broadcasts a disconnect `system_event`.
- The frontend should implement **automatic reconnection** with exponential backoff. The backend preserves player state across disconnects.

---

## 3. Data Contracts (JSON Schemas)

### 3a. [ClientAction](file:///Users/rohitagrawal/Desktop/Programming/GameHostAI/app/schemas/messages.py#4-7) — Client → Server
```typescript
interface ClientAction {
  action_type: string;   // e.g. "start_game", "vote", "night_action"
  payload: Record<string, any>;  // action-specific data
}
```

Supported `action_type` values the backend currently handles:

| `action_type`   | `payload`                     | When to Send | Description |
|------------------|-------------------------------|--------------|-------------|
| `"start_game"`  | `{}` (empty)                  | In Lobby     | Only the host should trigger this. Requires ≥ 4 players in the room. |
| *(any other)*   | `{ "target": "<player_id>" }` | During game  | Forwarded to the [GameStateMachine](file:///Users/rohitagrawal/Desktop/Programming/GameHostAI/app/engine/state_machine.py#40-311) action queue. Used for voting (day) and night actions (mafia kill, healer save, cop investigate). |

### 3b. [ServerEvent](file:///Users/rohitagrawal/Desktop/Programming/GameHostAI/app/schemas/messages.py#8-12) — Server → Client
```typescript
interface ServerEvent {
  event_type: string;          // identifies the event kind
  phase: string | null;        // current game sub-phase, e.g. "NIGHT_ACTIONS", "DAY_VOTING"
  data: Record<string, any>;   // event-specific payload
}
```

**Complete catalogue of `event_type` values the backend sends:**

| `event_type`            | [phase](file:///Users/rohitagrawal/Desktop/Programming/GameHostAI/app/engine/state_machine.py#253-261)         | `data` shape | Description |
|-------------------------|-----------------|--------------|-------------|
| `"system_event"`        | `null`          | `{ message: string }` | Player join/disconnect notifications |
| `"echo_reply"`          | `null`          | `{ original_action: string, echoed_payload: object }` | Echo when no game is running (lobby idle) |
| `"error"`               | `null`          | `{ message: string }` | Validation errors, not enough players, game already running, etc. |
| `"game_started"`        | `"NIGHT_ACTIONS"` | `{ message: string, round: number }` | Broadcast to all players when the game begins. First phase is always Night. |
| `"role_assignment"`     | `"NIGHT_ACTIONS"` | `{ role: string }` | **Private** — sent only to this player. Role is one of: `"mafia"`, `"villager"`, `"healer"`, `"cop"`. |
| `"action_acknowledged"` | current phase   | `{ message: string }` | Confirms the server received the player's action. |
| `"phase_resolved"`      | next phase      | `{ round: number, eliminated: string[], last_night_result: object \| null, last_day_result: object \| null }` | Broadcast after a phase timer expires or all alive players have acted. |
| `"phase_started"`       | new phase       | `{ message: string }` | Broadcast to announce the new phase has begun. |
| `"investigation_result"` | `"DAY_VOTING"` | `{ event_type: "investigation_result", target: string, is_mafia: boolean }` | **Private** — sent only to the Cop player after a night phase resolves. |
| `"narration"`           | current phase   | `{ narration_text: string }` | AI-generated narrative text, broadcast to all players. |
| `"AUDIO_TRIGGER"`       | current phase   | `{ audio_url: string }` | **Sent ONLY to the speaker node** (`is_speaker=true`). Contains a URL to a TTS audio file to play aloud. |
| `"game_over"`           | `"FINISHED"`    | `{ winner: string, players: object }` | Game ended. `winner` is `"mafia"` or `"villagers"`. `players` is the full player map with roles revealed. |
| `"private_message"`     | current phase   | varies        | Generic fallback for any private server→player messages. |

---

## 4. Game Flow & State Machine (What the Backend Does)

The backend manages a [GameStateMachine](file:///Users/rohitagrawal/Desktop/Programming/GameHostAI/app/engine/state_machine.py#40-311) per room with three top-level phases:

```
LOBBY  ──(start_game)──►  IN_PROGRESS  ──(win condition met)──►  FINISHED
                            │
                   ┌────────┴────────┐
                   │  Sub-phases:    │
                   │  NIGHT_ACTIONS  │◄──┐
                   │  DAY_VOTING     │───┘ (cycles each round)
                   └─────────────────┘
```

### Phase Timers
- **NIGHT_ACTIONS**: 30 seconds
- **DAY_VOTING**: 60 seconds
- If all alive players submit actions before the timer expires, the phase resolves **early**.

### Mafia Game Rules (Implemented in [MafiaStrategy](file:///Users/rohitagrawal/Desktop/Programming/GameHostAI/app/engine/games/mafia.py#28-233))
- **Minimum players**: 4
- **Roles**: `mafia` (1 per 4 players), `healer` (1), `cop` (1), `villager` (remaining)
- **Night Phase**: Mafia chooses a kill target, Healer chooses someone to protect, Cop investigates a player. All actions resolve simultaneously.
- **Day Phase**: All alive players vote to eliminate someone. Simple plurality wins. Ties result in no elimination.
- **Win Conditions**: Villagers win if all Mafia are eliminated. Mafia wins if alive Mafia ≥ alive non-Mafia.

### Action Idempotency
Players can re-submit their action during a phase — only the **last** submission is used. The frontend should allow players to change their selection before the timer expires.

---

## 5. AI Narration Pipeline (Backend-Driven)

After each phase resolution, the backend publishes a [NarrationRequest](file:///Users/rohitagrawal/Desktop/Programming/GameHostAI/app/schemas/narration.py#11-30) to RabbitMQ. A worker generates narration text (currently template-based; will be LLM-powered later) and optionally a TTS audio URL, then publishes a [NarrationResponse](file:///Users/rohitagrawal/Desktop/Programming/GameHostAI/app/schemas/narration.py#32-46) back.

**What the frontend receives:**
1. A `"narration"` event (broadcast to all players) with `{ narration_text: string }` — display this as immersive story text.
2. An `"AUDIO_TRIGGER"` event (sent **only** to the `is_speaker` connection) with `{ audio_url: string }` — the speaker device must auto-play this audio file.

**Staleness protection**: The backend checks `turn_number` before broadcasting narration. The frontend does not need to handle this — stale narration will simply never arrive.

---

## 6. Frontend Pages & Components to Build

### 6a. Landing / Home Page
- Input fields for **Player Name** and **Room Code** (both generate UUIDs or short codes).
- A toggle/checkbox: **"This device is the speaker"** (sets `is_speaker=true` on the WebSocket connection).
- "Join Room" button that opens the WebSocket connection.

### 6b. Lobby Screen
- Shows the list of connected players (derived from `system_event` join/disconnect messages).
- A "Start Game" button (visible to all, but conceptually the "host" presses it). Sends `ClientAction { action_type: "start_game", payload: {} }`.
- Displays errors from the server (e.g., "Need at least 4 players").

### 6c. Role Reveal Screen
- After `"game_started"`, the client receives a private `"role_assignment"` event.
- Display the player's role with a thematic animation (e.g., a card flip).
- Roles: **Mafia** (🔪), **Villager** (🏘️), **Healer** (💉), **Cop** (🔍).
- A "Continue" button or auto-advance after a few seconds.

### 6d. Night Phase Screen (`phase === "NIGHT_ACTIONS"`)
- **Mafia players**: Show a list of alive non-Mafia players to choose a kill target.
- **Healer**: Show a list of alive players to choose who to protect.
- **Cop**: Show a list of alive players to choose who to investigate.
- **Villagers**: Show a "waiting" screen — villagers have no night action.
- A **countdown timer** showing the remaining seconds (starts at 30s).
- On selecting a target, send: `ClientAction { action_type: "night_action", payload: { target: "<player_id>" } }`.
- Show an "Action Recorded" confirmation when `"action_acknowledged"` is received.
- Allow changing the selection before the timer expires (re-sending overwrites the previous action).

### 6e. Day Phase Screen (`phase === "DAY_VOTING"`)
- Display the result of the previous night: who was killed (from `phase_resolved.data.last_night_result.killed`) or that everyone survived.
- Show narration text if a `"narration"` event was received.
- Display a list of alive players to vote for elimination.
- A **countdown timer** (starts at 60s).
- On selecting a target, send: `ClientAction { action_type: "vote", payload: { target: "<player_id>" } }`.
- Allow changing the vote before time expires.
- Show `"action_acknowledged"` confirmation.

### 6f. Phase Resolution Overlay
- When `"phase_resolved"` arrives, briefly show the results:
  - Night → Day: who died or the healer saved them.
  - Day → Night: who was voted out, or a tie occurred (no elimination).
- Transition to the next phase screen.

### 6g. Cop Investigation Result (Private)
- After night resolves, the Cop receives an `"investigation_result"` event.
- Display a private modal/toast: **"{target} is [Mafia / Not Mafia]"**.

### 6h. Game Over Screen
- When `"game_over"` arrives, display:
  - The winning team (`data.winner`: `"mafia"` or `"villagers"`).
  - Full role reveal for all players (`data.players` contains `{ player_id: { role, alive } }` for every player).
- A "Return to Lobby" button.

### 6i. Narration Display
- When a `"narration"` event arrives, show the [narration_text](file:///Users/rohitagrawal/Desktop/Programming/GameHostAI/app/workers/narration_worker.py#42-56) as an immersive overlay or dedicated narration bar (think a dramatic text crawl or typewriter effect).
- This can appear during any phase.

### 6j. Speaker Node Audio Player (Conditional)
- **Only** on the device connected with `is_speaker=true`.
- When an `"AUDIO_TRIGGER"` event arrives, auto-play the audio file at `data.audio_url` using the Web Audio API or an `<audio>` element.
- Show a small speaker icon/indicator that the device is in speaker mode.

---

## 7. Features the Backend Does NOT Have Yet (Frontend Must Mock / Stub)

The following features are **not implemented** on the backend. The frontend should either mock them entirely on the client side or build the UI with placeholder data, ready to be wired to real endpoints later.

### 7a. User Authentication & Profiles
- **No auth** exists. There are no login endpoints, no JWT tokens, no user accounts.
- **Mock**: Let the player type any name. Generate a UUID for `player_id` client-side. Store in local storage for reconnection.
- **No profile avatars** — mock with generated initials or placeholder avatars.

### 7b. Room Creation / Room Listing API
- **No REST endpoint** for creating or listing rooms. The backend only tracks rooms when a WebSocket connects.
- **Mock**: Generate room codes client-side (e.g., 6-character alphanumeric). The room is implicitly "created" when the first player connects via WebSocket.

### 7c. Player List REST Endpoint
- There is **no REST endpoint** to fetch the player list for a room. Player presence is tracked via WebSocket `system_event` broadcasts.
- **Build**: Maintain a local state array of players by listening to `system_event` messages (join/disconnect). This is the intended design.

### 7d. Game History / Replay
- **No game history** is persisted. PostgreSQL models exist in the directory structure but are empty ([app/models/__init__.py](file:///Users/rohitagrawal/Desktop/Programming/GameHostAI/app/models/__init__.py) only).
- **Mock**: Optionally build a local game log by recording all received [ServerEvent](file:///Users/rohitagrawal/Desktop/Programming/GameHostAI/app/schemas/messages.py#8-12) messages for the session.

### 7e. In-Game Chat / Discussion
- **No chat system** exists. The backend has no chat-related `action_type` handling.
- **Mock**: Build a local-only chat UI where messages are broadcast to the room by wrapping them in a [ClientAction](file:///Users/rohitagrawal/Desktop/Programming/GameHostAI/app/schemas/messages.py#4-7) with a custom `action_type` (e.g., `"chat"`). The backend will currently echo these back as `echo_reply` if no game is running, or ignore them during a game. Consider implementing a frontend-only chat using a separate communication channel or just a visual placeholder.

### 7f. Game Configuration / Settings
- **No configurable game settings**. Phase durations are hardcoded (Night: 30s, Day: 60s). Role distribution is automatic.
- **Mock**: Build a settings UI in the lobby (number of mafia, phase durations, etc.) but do not expect the backend to honour these values. Display them as "coming soon" or store locally.

### 7g. Multiple Game Types
- The architecture supports multiple game strategies, but **only Mafia ([MafiaStrategy](file:///Users/rohitagrawal/Desktop/Programming/GameHostAI/app/engine/games/mafia.py#28-233)) is implemented**.
- **Mock**: Show a game-type selector in the lobby with "Mafia" as the only available option and others greyed out (e.g., "Werewolf — Coming Soon", "Secret Hitler — Coming Soon").

### 7h. Spectator Mode
- **No spectator support**. Every WebSocket connection is treated as a player.
- **Mock**: Optionally build a spectator view that connects to the WebSocket but does not send any [ClientAction](file:///Users/rohitagrawal/Desktop/Programming/GameHostAI/app/schemas/messages.py#4-7) messages.

### 7i. Real TTS Audio
- The [audio_url](file:///Users/rohitagrawal/Desktop/Programming/GameHostAI/app/workers/narration_worker.py#58-61) in `AUDIO_TRIGGER` is currently a **placeholder path** (e.g., `/audio/{room_id}/turn_{turn_number}.wav`). No real audio file is generated.
- **Mock**: When an `AUDIO_TRIGGER` is received, either use the browser's `SpeechSynthesis` API to read [narration_text](file:///Users/rohitagrawal/Desktop/Programming/GameHostAI/app/workers/narration_worker.py#42-56) aloud as a fallback, or play a placeholder sound effect.

### 7j. Push Notifications / Mobile Optimization
- **No push notifications** exist backed.
- **Mock**: Use browser Notification API for phase transitions if the tab is not focused.

---

## 8. Recommended Tech Stack & Libraries

| Concern | Recommendation |
|---------|---------------|
| Framework | React 18+ with TypeScript |
| Bundler | Vite |
| Routing | React Router v6 |
| State Management | Zustand or React Context — keep it lightweight |
| WebSocket | Native `WebSocket` API, wrapped in a custom hook with reconnection logic |
| Styling | CSS Modules, Styled Components, or Tailwind CSS — must be visually polished |
| Audio | Web Audio API or `<audio>` element for speaker node |
| Animations | Framer Motion for page transitions, role reveals, and narration typewriter effects |
| Timers | `setInterval` synced to server phase events (timer starts when phase_started arrives) |
| Testing | Vitest + React Testing Library |

---

## 9. State Management Design

```typescript
interface GameStore {
  // Connection
  roomId: string | null;
  playerId: string | null;
  isSpeaker: boolean;
  isConnected: boolean;

  // Lobby
  players: string[];         // built from system_event join/disconnect

  // Game
  gamePhase: "LOBBY" | "NIGHT_ACTIONS" | "DAY_VOTING" | "FINISHED" | null;
  myRole: "mafia" | "villager" | "healer" | "cop" | null;
  round: number;
  eliminated: string[];
  lastNightResult: { killed: string | null } | null;
  lastDayResult: { eliminated: string | null; vote_counts: Record<string, number> } | null;

  // Narration
  narrationText: string | null;
  audioUrl: string | null;       // speaker-only

  // Cop-only
  investigationResult: { target: string; is_mafia: boolean } | null;

  // Game Over
  winner: string | null;
  finalPlayers: Record<string, { role: string; alive: boolean }> | null;

  // Actions
  myAction: { target: string } | null;    // what I've submitted this phase
  actionAcknowledged: boolean;
}
```

---

## 10. WebSocket Hook Contract

```typescript
function useWebSocket(roomId: string, playerId: string, isSpeaker: boolean) {
  // Returns:
  //   isConnected: boolean
  //   sendAction: (action: ClientAction) => void
  //   lastEvent: ServerEvent | null
  //
  // Internally:
  //   - Connects to ws://<host>/ws/{roomId}/{playerId}?game_name=Mafia&player_name={playerName}&is_speaker={isSpeaker}
  //   - Parses incoming JSON as ServerEvent
  //   - Dispatches to the game store based on event_type
  //   - Implements reconnection with exponential backoff
  //   - Serialises and sends ClientAction as JSON text
}
```

---

## 11. Key Implementation Notes

1. **The frontend has ZERO game logic.** It does not know Mafia rules. It renders what the server tells it and sends user selections back.
2. **Phase transitions are server-driven.** The client never decides when to change phase — it reacts to `phase_resolved` and `phase_started` events.
3. **Timers are approximate.** Start a local countdown when `phase_started` arrives (30s for night, 60s for day). The server is the authority — if the phase resolves early, the client resets.
4. **Role-based UI branching:** After receiving `role_assignment`, store the role and conditionally render night-phase UI:
   - Mafia → kill target selector
   - Healer → save target selector
   - Cop → investigate target selector
   - Villager → passive waiting screen
5. **Dead players cannot act.** Track eliminated players from `phase_resolved.data.eliminated` and disable action UI for them. They should still see game events (spectate mode).
6. **Private messages:** `role_assignment`, `investigation_result`, and `action_acknowledged` are sent only to the relevant player — no special handling needed since the server already filters.
7. **The [phase](file:///Users/rohitagrawal/Desktop/Programming/GameHostAI/app/engine/state_machine.py#253-261) field on [ServerEvent](file:///Users/rohitagrawal/Desktop/Programming/GameHostAI/app/schemas/messages.py#8-12)** always reflects the *current* sub-phase after the event. Use it to drive the main game screen router.
8. **Backend URL**: The backend runs at `http://localhost:8000` by default (`uvicorn app.main:app`). WebSocket at `ws://localhost:8000/ws/...`.

---

## 12. Suggested File Structure

```
src/
├── main.tsx
├── App.tsx
├── index.css
├── types/
│   ├── schemas.ts          # ClientAction, ServerEvent interfaces
│   └── game.ts             # GameStore, role types, phase types
├── hooks/
│   ├── useWebSocket.ts     # WebSocket connection + reconnection
│   └── useGameStore.ts     # Zustand store or context
├── components/
│   ├── Layout.tsx
│   ├── NarrationOverlay.tsx
│   ├── PlayerList.tsx
│   ├── CountdownTimer.tsx
│   ├── RoleCard.tsx
│   ├── VoteSelector.tsx
│   ├── ActionSelector.tsx
│   ├── SpeakerAudioPlayer.tsx
│   └── ErrorToast.tsx
├── pages/
│   ├── HomePage.tsx         # Join / create room
│   ├── LobbyPage.tsx        # Pre-game lobby
│   ├── GamePage.tsx          # Main game view (switches sub-components by phase)
│   ├── NightPhasePage.tsx
│   ├── DayPhasePage.tsx
│   ├── RoleRevealPage.tsx
│   └── GameOverPage.tsx
└── utils/
    ├── audio.ts             # Speaker audio playback helpers
    └── ids.ts               # UUID / room code generation
```

---

## 13. Summary of Backend Capabilities vs. Frontend Mocks

| Feature | Backend Status | Frontend Approach |
|---------|---------------|-------------------|
| WebSocket real-time communication | ✅ Fully implemented | Wire directly |
| Room join/leave via WebSocket | ✅ Fully implemented | Wire directly |
| Player presence tracking (Redis) | ✅ Fully implemented | Build from `system_event` |
| Game start (≥4 players) | ✅ Fully implemented | Wire directly |
| Role assignment (private) | ✅ Fully implemented | Wire directly |
| Night phase actions (kill/heal/investigate) | ✅ Fully implemented | Wire directly |
| Day phase voting | ✅ Fully implemented | Wire directly |
| Phase timers (30s / 60s) | ✅ Fully implemented | Local countdown synced to server events |
| Early phase resolution | ✅ Fully implemented | React to `phase_resolved` |
| Cop investigation results (private) | ✅ Fully implemented | Wire directly |
| Win condition detection | ✅ Fully implemented | Wire directly |
| AI narration text broadcast | ✅ Implemented (template stubs) | Wire directly, display as story text |
| Speaker node AUDIO_TRIGGER | ✅ Implemented (placeholder URLs) | Mock with SpeechSynthesis or placeholder audio |
| Reconnection handling | ✅ Backend preserves state | Implement reconnection logic on frontend |
| User authentication | ❌ Not implemented | Mock with local name + UUID |
| Room creation REST API | ❌ Not implemented | Generate room codes client-side |
| Player list REST API | ❌ Not implemented | Build from WebSocket events |
| In-game chat | ❌ Not implemented | Mock or placeholder |
| Game history / replay | ❌ Not implemented | Optional local log |
| Game configuration UI | ❌ Not implemented | Build UI, store locally, display as "coming soon" |
| Multiple game types | ❌ Only Mafia | Show selector with others greyed out |
| Spectator mode | ❌ Not implemented | Optional mock |
| Real TTS audio files | ❌ Placeholder URLs | Use browser SpeechSynthesis as fallback |
| Push notifications | ❌ Not implemented | Use browser Notification API |
