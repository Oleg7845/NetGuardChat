import uvicorn
from fastapi import FastAPI, WebSocket
from fastapi.middleware.cors import CORSMiddleware
from contextlib import asynccontextmanager
import json
import asyncio
import time
from collections import defaultdict

@asynccontextmanager
async def lifespan(app_arg: FastAPI):
    asyncio.create_task(cleanup_task())
    yield
    pass

app = FastAPI(title="NetGuard Chat API", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# box_id -> list of websocket connections
subscribers = defaultdict(list)

# box_id -> list of messages
# каждое сообщение: {"message_id": str, "text": str, "timestamp": float, "delivered_to": set(websocket_ids)}
messages = defaultdict(list)

# box_id -> invitation dict
invitations = {}

CLEANUP_INTERVAL = 3600  # раз в час
INVITATION_TTL = 2 * 24 * 3600  # 2 дня
MESSAGE_TTL = 2 * 24 * 3600  # 2 дня

async def cleanup_task():
    while True:
        now = time.time()
        # чистим старые приглашения
        to_delete_invites = []
        for box_id, invite in invitations.items():
            if now - invite["timestamp"] > INVITATION_TTL:
                to_delete_invites.append(box_id)
        for box_id in to_delete_invites:
            del invitations[box_id]

        # чистим старые сообщения
        for box_id, msg_list in messages.items():
            messages[box_id] = [m for m in msg_list if now - m["timestamp"] <= MESSAGE_TTL]

        await asyncio.sleep(CLEANUP_INTERVAL)

def websocket_id(ws: WebSocket):
    return id(ws)

@app.websocket("/")
async def websocket_endpoint(websocket: WebSocket):
    await websocket.accept()
    subscribed_box_ids = []

    try:
        # ждем подписку
        data = await websocket.receive_text()
        obj = json.loads(data)
        if obj.get("type") == "subscribe":
            subscribed_box_ids = obj.get("box_ids", [])
            for box_id in subscribed_box_ids:
                if websocket not in subscribers[box_id]:
                    subscribers[box_id].append(websocket)

            # отправляем все недоставленные сообщения для каждого box_id
            for box_id in subscribed_box_ids:
                for msg in messages.get(box_id, []):
                    if websocket_id(websocket) not in msg["delivered_to"]:
                        await websocket.send_text(json.dumps({
                            "type": "message",
                            "box_id": box_id,
                            "message_id": msg["message_id"],
                            "text": msg["text"]
                        }))
                        msg["delivered_to"].add(websocket_id(websocket))

        while True:
            data = await websocket.receive_text()
            msg_obj = json.loads(data)
            msg_type = msg_obj.get("type")
            box_id = msg_obj.get("box_id")

            message_id = msg_obj.get("message_id")

            # сразу ACK
            if message_id:
                ack = {"type": "ack", "message_id": message_id}
                await websocket.send_text(json.dumps(ack))

            # обработка приглашений
            if msg_type in ["invite_request", "invite_response"]:
                invitations[box_id] = {"data": msg_obj.get("data"), "timestamp": time.time()}
            elif msg_type == "get_invite":
                invite = invitations.pop(box_id, None)
                if invite:
                    await websocket.send_text(json.dumps({
                        "type": "invite_response",
                        "box_id": box_id,
                        "data": invite["data"]
                    }))
            else:
                # обычное сообщение
                if not box_id or not message_id:
                    continue

                # сохраняем сообщение
                new_msg = {
                    "message_id": message_id,
                    "text": msg_obj.get("text", ""),
                    "timestamp": time.time(),
                    "delivered_to": set([websocket_id(websocket)])
                }
                messages[box_id].append(new_msg)

                # рассылаем всем подписанным клиентам кроме отправителя
                for client in subscribers.get(box_id, []):
                    if client != websocket:
                        await client.send_text(json.dumps({
                            "type": "message",
                            "box_id": box_id,
                            "message_id": message_id,
                            "text": new_msg["text"]
                        }))
                        new_msg["delivered_to"].add(websocket_id(client))

    except Exception as e:
        print("Client disconnected:", e)
    finally:
        # удаляем из подписок
        for box_id in subscribed_box_ids:
            if websocket in subscribers[box_id]:
                subscribers[box_id].remove(websocket)

if __name__ == "__main__":
    uvicorn.run("main:app", host="0.0.0.0", port=5080, workers=1, reload=True)
