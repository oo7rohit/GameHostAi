import asyncio
import websockets

async def test_ws(uri):
    try:
        async with websockets.connect(uri) as websocket:
            print(f"Connected to {uri}")
            await websocket.send("test")
    except Exception as e:
        print(f"Failed to connect to {uri}: {e}")

async def main():
    print("Testing /ws/room1/player1 ...")
    await test_ws("ws://localhost:8000/ws/room1/player1?game_name=Mafia&player_name=Player1&is_speaker=false")

if __name__ == "__main__":
    asyncio.run(main())
