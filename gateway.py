import asyncio
import json
from contextlib import asynccontextmanager

import redis.asyncio as redis
from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse

CHANNELS = ["game.state", "narrator.broadcast"]

clients: set[WebSocket] = set()  # active WS connections


async def redis_pump():
    """Single background task: Redis -> enveloped broadcast to all WS."""
    sub = redis.Redis(host="localhost", port=6379, decode_responses=True)
    ps = sub.pubsub()
    await ps.subscribe(*CHANNELS)
    async for msg in ps.listen():
        if msg.get("type") != "message":
            continue
        try:
            data = json.loads(msg["data"])
        except (json.JSONDecodeError, TypeError):
            continue
        envelope = json.dumps({"type": msg["channel"], "data": data})
        for ws in list(clients):  # copy: removal during iteration
            try:
                await ws.send_text(envelope)
            except Exception:  # zombie/disconnected -> drop instantly
                clients.discard(ws)


@asynccontextmanager
async def lifespan(app: FastAPI):
    task = asyncio.create_task(redis_pump())
    yield
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass


app = FastAPI(lifespan=lifespan)


@app.get("/")
async def index():
    return FileResponse("index.html")


@app.websocket("/ws")
async def ws_endpoint(ws: WebSocket):
    await ws.accept()
    clients.add(ws)
    try:
        while True:  # keep alive; client never sends, we only push
            await ws.receive_text()
    except (WebSocketDisconnect, Exception):
        pass
    finally:
        clients.discard(ws)


if __name__ == "__main__":
    import uvicorn

    uvicorn.run("gateway:app", host="0.0.0.0", port=8000)
