# GameHostAI Backend

## Prerequisites
- Docker Desktop installed and running
- `uv` installed (Python package manager)

## Running the Application Locally

To fully start the application for local testing, you need to run the following components:

### 1. Start required infrastructure services
The application requires PostgreSQL, Redis, and RabbitMQ. Start them using Docker Compose:
```bash
docker compose up -d
```

### 2. Start the FastAPI Backend
In a new terminal window at the project root (`/Users/rohitagrawal/Desktop/Programming/GameHostAI`), start the core game engine and API server:
```bash
uv run uvicorn app.main:app --reload
```
The API will be available at `http://127.0.0.1:8000`.

### 3. Start the Narration Worker
The narration pipeline runs as a separate standalone worker. In another terminal window at the project root, start it with:
```bash
uv run python -m app.workers.narration_worker
```
