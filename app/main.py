from __future__ import annotations
import json
import re
from pathlib import Path

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from openai import OpenAI

from app.game_engine import load_world, load_state, save_state, reset_state, build_gm_prompt
from app.config import XAI_API_KEY, GROK_MODEL, GROK_VOICE, IMAGE_STYLES
from app.image_service import generate_image, image_path, resolve_variant, build_image_log

app = FastAPI()

FRONTEND = Path(__file__).parent.parent / "frontend"
app.mount("/static", StaticFiles(directory=str(FRONTEND)), name="static")


@app.get("/")
def index():
    return FileResponse(str(FRONTEND / "index.html"))


@app.get("/api/location-image/{loc_id}")
async def location_image(loc_id: str, force: bool = False):
    world = load_world()
    state = load_state()
    if loc_id not in world["locations"]:
        return JSONResponse({"error": "unknown location"}, status_code=404)

    location = world["locations"][loc_id]
    file_id, label = resolve_variant(loc_id, location, state["flags"])
    path = image_path(file_id)

    if not path.exists() or force:
        variants = location.get("image_variants", [])
        prompt_extra = next(
            (v["prompt_extra"] for v in variants if v["file"] == file_id),
            location.get("atmosphere", loc_id)
        )
        ok = await generate_image(file_id, location, prompt_extra, force=force)
        if not ok:
            return JSONResponse({"error": "generation failed"}, status_code=503)

    return FileResponse(str(path), media_type="image/png")


@app.get("/api/debug/images")
def debug_images():
    world = load_world()
    state = load_state()
    result = []
    for loc_id, location in world["locations"].items():
        log = build_image_log(loc_id, location, state["flags"])
        result.append({"loc_id": loc_id, "loc_name": location["name"], "variants": log})
    return result


@app.get("/api/image-styles")
def image_styles():
    return IMAGE_STYLES


@app.get("/debug")
def debug_page():
    return FileResponse(str(FRONTEND / "debug.html"))


@app.get("/api/debug/graph")
def debug_graph():
    state = load_state()
    world = load_world()
    flags = state["flags"]

    troll_defeated = flags.get("troll_state") in ("troll_pokonany", "troll_przekupiony")
    hidden_path = flags.get("hidden_path_unlocked", False)
    brama_open = flags.get("brama_state") == "otwarta"

    # Wszystkie możliwe krawędzie z wagami
    edges = [
        {"from": "las",              "to": "polana",           "label": "wschód",   "weight": 1},
        {"from": "polana",           "to": "las",              "label": "zachód",   "weight": 1},
        {"from": "las",              "to": "most",             "label": "północ",   "weight": 1},
        {"from": "most",             "to": "las",              "label": "południe", "weight": 1},
        {"from": "most",             "to": "zamek",            "label": "północ",   "weight": 1 if troll_defeated else 0},
        {"from": "zamek",            "to": "most",             "label": "południe", "weight": 1},
        {"from": "las",              "to": "domek_pustelnika", "label": "zachód",   "weight": 1 if hidden_path else 0},
        {"from": "domek_pustelnika", "to": "las",              "label": "wschód",   "weight": 1},
        {"from": "zamek",            "to": "wnetrze_zamku",    "label": "północ",   "weight": 1 if brama_open else 0},
    ]

    nodes = [
        {"id": loc_id, "label": loc["name"],
         "current": loc_id == state["current_location"]}
        for loc_id, loc in world["locations"].items()
    ]
    nodes.append({"id": "wnetrze_zamku", "label": "Wnętrze Zamku", "current": False})

    return {
        "nodes": nodes,
        "edges": edges,
        "state": state,
        "flags": flags,
    }


def enrich_state(state: dict, world: dict) -> dict:
    """Dodaje do stanu pola z world.yaml potrzebne frontendowi."""
    loc_id = state["current_location"]
    loc = world["locations"].get(loc_id, {})
    state["current_location_name"] = loc.get("name", loc_id)
    state["atmosphere"] = loc.get("atmosphere", "")
    file_id, _ = resolve_variant(loc_id, loc, state["flags"])
    state["active_image"] = file_id
    return state


@app.post("/api/reset")
def reset():
    state = reset_state()
    world = load_world()
    return {"state": enrich_state(state, world), "intro": world["intro"]}


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

    return {"narrative": narrative, "state": enrich_state(updated_state, world)}


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
    # Usuń blok JSON z narracji; usuń też ewentualny surowy JSON na końcu
    narrative = re.sub(r"```json.*?```", "", raw, flags=re.DOTALL)
    narrative = re.sub(r"\{[^{}]*\"new_location\"[^{}]*\}", "", narrative)
    narrative = narrative.strip()

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

    # Fallback: troll pokonany
    troll_keywords = ["ustępuje", "chowa się", "ucieka", "nie chcę kłopotów", "droga wolna", "przekupiony", "zasnął"]
    if (state["current_location"] == "most"
            and state["flags"].get("troll_state") == "blokuje_most"
            and any(kw in narrative.lower() for kw in troll_keywords)):
        state["flags"]["troll_state"] = "troll_pokonany"

    # Fallback: więzień uwolniony
    wiezien_keywords = ["klatka się otwiera", "klatka otwiera", "benedykt wychodzi", "ukryta ścieżka", "pustelnik hieronima"]
    if (state["current_location"] == "zamek"
            and not state["flags"].get("hidden_path_unlocked")
            and any(kw in narrative.lower() for kw in wiezien_keywords)):
        state["flags"]["wiezien_state"] = "uwolniony"
        state["flags"]["hidden_path_unlocked"] = True

    # Fallback: hasło wypowiedziane przy bramie
    if (state["current_location"] == "zamek"
            and state["flags"].get("brama_state") != "otwarta"
            and ("brama" in narrative.lower() and ("otwiera się" in narrative.lower() or "stoi otworem" in narrative.lower()))):
        state["flags"]["brama_state"] = "otwarta"

    # Fallback: pustelnik zdradził hasło
    if (state["current_location"] == "domek_pustelnika"
            and not state["flags"].get("haslo_znane")
            and "bum bara doom" in narrative.lower()):
        state["flags"]["haslo_znane"] = True
        state["flags"]["pustelnik_state"] = "pomoglismy"

    state["turn"] += 1
    state["history"].append({"turn": state["turn"], "gm": narrative})
    return narrative, state
