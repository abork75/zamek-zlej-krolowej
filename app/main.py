from __future__ import annotations
import json
import re
from pathlib import Path

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from openai import OpenAI

from app.game_engine import load_world, load_state, save_state, reset_state, build_gm_prompt
from app.config import XAI_API_KEY, GROK_MODEL, GROK_VOICE

app = FastAPI()

FRONTEND = Path(__file__).parent.parent / "frontend"
app.mount("/static", StaticFiles(directory=str(FRONTEND)), name="static")


@app.get("/")
def index():
    return FileResponse(str(FRONTEND / "index.html"))


@app.post("/api/reset")
def reset():
    state = reset_state()
    world = load_world()
    return {"state": state, "intro": world["intro"]}


@app.post("/api/chat")
async def chat(body: dict):
    player_input = body.get("message", "")
    state = load_state()
    world = load_world()

    prompt = build_gm_prompt(player_input, state, world)

    client = OpenAI(api_key=XAI_API_KEY, base_url="https://api.x.ai/v1")
    response = client.chat.completions.create(
        model=GROK_MODEL,
        messages=[{"role": "user", "content": prompt}],
        max_tokens=500,
        temperature=0.8,
    )

    gm_response = response.choices[0].message.content
    narrative, updated_state = _parse_gm_response(gm_response, state)
    save_state(updated_state)

    return {"narrative": narrative, "state": updated_state}


@app.websocket("/ws/voice")
async def voice_ws(websocket: WebSocket):
    await websocket.accept()
    state = load_state()
    world = load_world()

    xai_ws_url = "wss://api.x.ai/v1/audio/speech/websocket"

    try:
        import websockets
        async with websockets.connect(
            xai_ws_url,
            additional_headers={"Authorization": f"Bearer {XAI_API_KEY}"},
        ) as grok_ws:
            system_prompt = build_gm_prompt("(gracz dopiero zaczyna — przywitaj go i opisz lokację)", state, world)

            await grok_ws.send(json.dumps({
                "type": "session.update",
                "session": {
                    "model": GROK_VOICE,
                    "voice": "ara",
                    "instructions": system_prompt,
                    "input_audio_format": "pcm16",
                    "output_audio_format": "pcm16",
                    "turn_detection": {"type": "server_vad"},
                }
            }))

            import asyncio

            async def from_client():
                while True:
                    data = await websocket.receive_bytes()
                    await grok_ws.send(json.dumps({
                        "type": "input_audio_buffer.append",
                        "audio": __import__("base64").b64encode(data).decode(),
                    }))

            async def from_grok():
                async for raw in grok_ws:
                    msg = json.loads(raw)
                    await websocket.send_text(raw)

            await asyncio.gather(from_client(), from_grok())

    except WebSocketDisconnect:
        pass
    except Exception as e:
        await websocket.close()


def _parse_gm_response(raw: str, state: dict) -> tuple[str, dict]:
    json_match = re.search(r"```json\s*(.*?)\s*```", raw, re.DOTALL)
    narrative = re.sub(r"```json.*?```", "", raw, flags=re.DOTALL).strip()

    if json_match:
        try:
            updates = json.loads(json_match.group(1))
            if updates.get("new_location"):
                state["current_location"] = updates["new_location"]
            for item in updates.get("inventory_add", []):
                if item not in state["inventory"]:
                    state["inventory"].append(item)
            for item in updates.get("inventory_remove", []):
                state["inventory"] = [i for i in state["inventory"] if i != item]
            state["flags"].update(updates.get("flags_update", {}))
        except json.JSONDecodeError:
            pass

    state["turn"] += 1
    state["history"].append({"turn": state["turn"], "gm": narrative})
    return narrative, state
