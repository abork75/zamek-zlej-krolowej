from __future__ import annotations
import json
import re
from pathlib import Path

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from openai import OpenAI

from app.game_engine import load_world, load_state, save_state, reset_state, build_gm_prompt, check_condition, resolve_exits, resolve_description, find_item, find_items
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

    # Krawędzie generowane z world.yaml — bez hardkodowanych warunków
    edges = []
    for loc_id, loc in world["locations"].items():
        for direction, exit_def in loc.get("exits", {}).items():
            if exit_def is None:
                continue
            if isinstance(exit_def, dict):
                target = exit_def["target"]
                req = exit_def.get("requires")
                weight = 1 if (not req or check_condition(req, flags)) else 0
            else:
                target = exit_def
                weight = 1
            edges.append({"from": loc_id, "to": target, "label": direction, "weight": weight})

    nodes = [
        {"id": loc_id, "label": loc["name"],
         "current": loc_id == state["current_location"]}
        for loc_id, loc in world["locations"].items()
    ]

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
    accessible, blocked = resolve_exits(loc, state["flags"])
    # Wyjścia z pułapką są widoczne nawet gdy warunek nie spełniony — gracz musi móc w nie wejść
    trap_exits = [
        direction for direction, exit_def in loc.get("exits", {}).items()
        if isinstance(exit_def, dict)
        and exit_def.get("trap")
        and direction not in accessible
    ]
    state["available_exits"] = list(accessible.keys()) + trap_exits
    state["blocked_exits"] = {}
    return state


@app.post("/api/move")
async def move(body: dict):
    direction = body.get("direction", "")
    state = load_state()
    world = load_world()
    result = _try_move(direction, state, world)
    if result:
        narrative, updated_state = result
        return {"narrative": narrative, "state": enrich_state(updated_state, world)}
    return JSONResponse({"error": "invalid move"}, status_code=400)


@app.post("/api/debug/teleport")
async def debug_teleport(body: dict):
    loc_id = body.get("location", "")
    world = load_world()
    if loc_id not in world["locations"]:
        return JSONResponse({"error": "unknown location"}, status_code=404)
    state = load_state()
    state["current_location"] = loc_id
    save_state(state)
    return {"ok": True, "location": loc_id}


@app.get("/api/state")
def get_state():
    state = load_state()
    world = load_world()
    loc_id = state["current_location"]
    location = world["locations"].get(loc_id, {})
    description = resolve_description(location, state["flags"])
    last_narrative = state["history"][-1]["gm"] if state["history"] else description
    return {"state": enrich_state(state, world), "narrative": last_narrative}


@app.post("/api/reset")
def reset():
    state = reset_state()
    world = load_world()
    return {"state": enrich_state(state, world), "intro": world["intro"]}


async def classify_npc_outcome(player_input: str, npc: dict, location: dict, state: dict, client, is_talk: bool = False) -> dict:
    """Klasyfikuje czy akcja gracza zmienia stan NPC. Zwraca flags_update lub {}."""
    if not any(s.get("flags") for s in npc.get("scripted_solutions", [])):
        return {}

    current_state = state["flags"].get(f"{npc['id']}_state", npc.get("state", ""))
    creative = npc.get("creative_solutions_hint", "")
    options = [{"label": "brak zmiany", "flags": {}}]
    for s in npc.get("scripted_solutions", []):
        if s.get("flags"):
            options.append({"label": s["trigger"], "flags": s["flags"]})

    options_text = "\n".join(f"{i}: {o['label']}" for i, o in enumerate(options))

    talk_rule = ""
    if is_talk:
        talk_rule = """
WAŻNE — gracz MÓWI DO NPC (forma dialogowa, nie akcja fizyczna):
- Triggery wymagające fizycznej akcji (dawanie przedmiotu, atakowanie, czekanie, używanie czegoś)
  NIE mogą odpalić przez sam dialog. Przykład: "dam ci złoto" ≠ danie złota → opcja 0.
- Triggery które SĄ dialogiem (blefowanie, podszywanie się, przekonywanie, straszenie słownie)
  MOGĄ odpalić przez dialog. Przykład: "jestem lordem X" → pasuje do triggera blefowania → opcja N.
"""

    prompt = f"""Klasyfikator wyników interakcji z NPC. Odpowiedz TYLKO jedną cyfrą.

NPC: {npc['name']} (stan: {current_state})
Ekwipunek gracza: {', '.join(state['inventory']) or 'brak'}
{talk_rule}
Opcje wyniku:
{options_text}

Kreatywne rozwiązania (wybierz NAJBLIŻSZĄ opcję z listy):
{creative}

Akcja gracza: "{player_input}"

Odpowiedź: tylko jedna cyfra (numer opcji)"""

    for attempt in range(3):
        try:
            resp = client.chat.completions.create(
                model=GROK_MODEL,
                messages=[{"role": "user", "content": prompt}],
                max_tokens=5,
                temperature=0,
            )
            raw = resp.choices[0].message.content.strip()
            m = re.search(r"\d", raw)
            if not m:
                print(f"[NPC OUTCOME] attempt {attempt+1}: brak cyfry w odpowiedzi '{raw}', retry")
                continue
            idx = int(m.group())
            if idx >= len(options):
                print(f"[NPC OUTCOME] attempt {attempt+1}: idx={idx} poza zakresem (max {len(options)-1}), retry")
                continue
            print(f"[NPC OUTCOME] {npc['id']} option {idx}: {options[idx]['flags']}")
            return options[idx]["flags"]
        except Exception as e:
            print(f"[NPC OUTCOME ERROR] attempt {attempt+1}: {e}")
    print(f"[NPC OUTCOME] wszystkie próby nieudane, zwracam {{}}")
    return {}


async def classify_intent(player_input: str, location: dict, state: dict, client) -> dict:
    items_visible = [
        f"{i.get('id', '?')}: {i.get('hint', i['name'])}"
        for i in location.get("items", [])
    ]
    inventory_ids = state["inventory"]
    npcs_visible = [f"{n['id']}: {n['name']}" for n in location.get("npcs", [])]
    has_npc = bool(npcs_visible)
    prompt = f"""Klasyfikator akcji gracza w grze tekstowej. Odpowiedz TYLKO w JSON, bez markdown.

Kontekst lokacji:
  Nazwa: {location.get('name', '')}
  Przedmioty (id: opis): {', '.join(items_visible) or 'brak'}
  Ekwipunek gracza: {', '.join(inventory_ids) or 'brak'}
  NPC w lokacji (id: nazwa): {', '.join(npcs_visible) or 'brak'}

Akcja gracza: "{player_input}"

=== KLUCZ KLASYFIKACJI — FORMA GRAMATYCZNA, nie semantyka ===

TALK — gdy gracz MÓWI DO kogoś lub PYTA (nieważne o co):
  - zwrot bezpośredni: wołacz, "ty", "ci", "cię", rozkaz skierowany do osoby
    Przykłady: "hej trolu", "przepuść mnie", "co wiesz o zamku?", "dam ci sakiewkę"
  - pytanie (znak "?", słowa: co, kto, gdzie, kiedy, czy, jak, ile)
    Uwaga: pytanie zawsze = TALK jeśli jest NPC w lokacji (NPC = {has_npc})
  - deklaracja intencji zamiast akcji: "chcę", "mogę", "zamierzam", "dam ci", "spróbuję"
  - negocjacja, prośba, groźba słowna, przekonywanie

MOVE — gracz przemieszcza się w kierunku świata:
  Przykłady: "idę na północ", "wchodzę", "wracam", "przekraczam most"

EXAMINE — gracz bada/ogląda coś, LUB podaje sam przedmiot/miejsce bez czasownika:
  Przykłady: "badam ścianę", "przyglądam się kamieniom", "sprawdzam drzwi"
  WAŻNE: samo słowo lub krótka fraza bez czasownika = ZAWSZE EXAMINE, nigdy TAKE
  Przykłady: "miecz", "sakiewka", "kamień", "stary kij" → EXAMINE

TAKE — gracz podnosi/bierze przedmiot z lokacji — WYMAGA wyraźnego czasownika brania:
  Przykłady: "biorę miecz", "podnoszę kij", "zabieram sakiewkę", "weź miecz"
  Samo wymienienie przedmiotu bez czasownika NIE jest TAKE

USE — gracz używa przedmiotu z ekwipunku na czymś:
  Przykłady: "używam miecza", "rzucam kij w trolla"

OTHER — gracz opisuje czynność fizyczną nie będącą ruchem/braniem/badaniem:
  Przykłady: "uderzam trolla", "czekam", "atakuję", "kładę się spać", "daję trolowi sakiewkę"
  Uwaga: "daję [komuś] [coś]" to OTHER (akcja), "dam ci [coś]" to TALK (deklaracja)

=== FORMAT ODPOWIEDZI ===
{{"intent": "TALK|MOVE|TAKE|EXAMINE|USE|OTHER", "item_id": "id_lub_null", "npc_id": "id_npc_lub_null"}}

npc_id: id NPC jeśli gracz adresuje konkretną postać z listy, null jeśli zwraca się ogólnie lub pyta ogólnie"""
    valid_intents = {"TAKE", "EXAMINE", "USE", "TALK", "MOVE", "OTHER"}
    for attempt in range(3):
        try:
            resp = client.chat.completions.create(
                model=GROK_MODEL,
                messages=[{"role": "user", "content": prompt}],
                max_tokens=60,
                temperature=0,
            )
            raw = resp.choices[0].message.content.strip()
            raw = re.sub(r"^```\w*\s*", "", raw)
            raw = re.sub(r"\s*```$", "", raw)
            result = json.loads(raw)
            if result.get("intent") not in valid_intents:
                print(f"[INTENT] attempt {attempt+1}: nieznana intencja '{result.get('intent')}', retry")
                continue
            print(f"[INTENT] {result.get('intent')} item_id={result.get('item_id')} npc_id={result.get('npc_id')}")
            return result
        except Exception as e:
            print(f"[INTENT ERROR] attempt {attempt+1}: {e}")
    print("[INTENT] wszystkie próby nieudane, fallback OTHER")
    return {"intent": "OTHER", "item_id": None}


DIRECTION_ALIASES = {
    "północ": "północ", "n": "północ", "north": "północ",
    "południe": "południe", "s": "południe", "south": "południe",
    "wschód": "wschód", "e": "wschód", "east": "wschód",
    "podejdź bliżej": "podejdź bliżej", "podejdz blizej": "podejdź bliżej",
    "zachód": "zachód", "w": "zachód", "west": "zachód",
    "wejście": "wejście", "wyjście": "wyjście",
}


def _try_move(player_input: str, state: dict, world: dict):
    """Zwraca (narrative, updated_state) jeśli input to kierunek ruchu, else None."""
    direction = DIRECTION_ALIASES.get(player_input.strip().lower())
    if not direction:
        return None

    location = world["locations"].get(state["current_location"], {})
    accessible, blocked_msgs = resolve_exits(location, state["flags"])

    if direction in accessible:
        target = accessible[direction]
        if target not in world["locations"]:
            return None
        state["current_location"] = target
        new_loc = world["locations"][target]
        description = resolve_description(new_loc, state["flags"])
        state["turn"] += 1
        state["history"].append({"turn": state["turn"], "gm": description.strip()})
        save_state(state)
        return description.strip(), state

    # Kierunek istnieje ale zablokowany — sprawdź czy jest pułapka
    raw_exit = location.get("exits", {}).get(direction)
    if isinstance(raw_exit, dict):
        trap = raw_exit.get("trap")
        if trap and trap.get("target") in world["locations"]:
            state["current_location"] = trap["target"]
            msg = trap.get("message", "Wpadasz w pułapkę!")
            new_loc = world["locations"][trap["target"]]
            full_msg = msg + "\n\n" + resolve_description(new_loc, state["flags"])
            state["turn"] += 1
            state["history"].append({"turn": state["turn"], "gm": full_msg})
            save_state(state)
            return full_msg, state
        msg = raw_exit.get("blocked_message", f"Nie możesz iść na {direction}.")
        state["turn"] += 1
        state["history"].append({"turn": state["turn"], "gm": msg})
        save_state(state)
        return msg, state

    return None


@app.post("/api/chat")
async def chat(body: dict):
    player_input = body.get("message", "")
    state = load_state()
    world = load_world()

    # 1. Deterministyczny ruch — zawsze pierwszy, bez API call
    move_result = _try_move(player_input, state, world)
    if move_result:
        narrative, updated_state = move_result
        return {"narrative": narrative, "state": enrich_state(updated_state, world)}

    location = world["locations"].get(state["current_location"], {})
    client = OpenAI(api_key=XAI_API_KEY, base_url="https://api.x.ai/v1")

    # 2. Zakończenie gry — specjalne lokacje z polem "ending"
    if location.get("ending"):
        ending_type = location["ending"]
        ending_prompt = _build_ending_prompt(player_input, state, location, ending_type)
        resp = client.chat.completions.create(
            model=GROK_MODEL,
            messages=[{"role": "user", "content": ending_prompt}],
            max_tokens=600,
            temperature=0.9,
        )
        narrative = resp.choices[0].message.content.strip()
        state["flags"]["game_ended"] = ending_type
        state["turn"] += 1
        state["history"].append({"turn": state["turn"], "gm": narrative})
        save_state(state)
        return {"narrative": narrative, "state": enrich_state(state, world), "ending": ending_type}

    # 3. Klasyfikacja intencji
    classified = await classify_intent(player_input, location, state, client)
    intent = classified.get("intent", "OTHER")
    item_id = classified.get("item_id")
    npc_id = classified.get("npc_id")
    print(f"[INTENT] {intent} item_id={item_id} npc_id={npc_id}")

    # 3. Dispatch — modyfikuj stan i buduj kontekst dla narratora
    intent_context = ""

    # Ustaw flagi dla rozpoznanego itemu
    # set_on: examine → tylko gdy EXAMINE intent
    # brak set_on → przy każdej wzmiance (np. "przeskocz zapadnię")
    if item_id:
        for item in find_items(item_id, location):
            flags_to_set = item.get("examine_sets_flag", {})
            if not flags_to_set:
                continue
            set_on = item.get("set_on")
            if set_on == "examine" and intent != "EXAMINE":
                continue
            new_flags = {k: v for k, v in flags_to_set.items() if state["flags"].get(k) != v}
            if new_flags:
                state["flags"].update(new_flags)
                print(f"[FLAGS] set from item_id='{item_id}' (intent={intent}): {new_flags}")

    if intent == "TAKE":
        item = find_item(item_id, location)
        if item and item.get("takeable") and item["name"] not in state["inventory"]:
            state["inventory"].append(item["name"])
            intent_context = f"gracz właśnie podniósł '{item['name']}' — opisz jak go bierze, potwierdź że trzyma go w rękach"
        elif item and not item.get("takeable", True):
            intent_context = f"gracz próbuje wziąć '{item.get('hint', item_id)}' — to jest element otoczenia, nie można zabrać, wyjaśnij krótko dlaczego"
        elif item and item["name"] in state["inventory"]:
            intent_context = f"gracz próbuje wziąć '{item['name']}' — już to ma przy sobie"

    elif intent == "EXAMINE":
        items = find_items(item_id, location)
        if items:
            descriptions = "\n".join(
                f"- {item['name']}: {item['description'].strip()}"
                for item in items if item.get("description")
            )
            if descriptions:
                intent_context = (
                    f"gracz ogląda '{items[0].get('hint', item_id)}' — podaj TYLKO opis wyglądu przedmiotu.\n"
                    f"ZAKAZ: nie pisz że gracz chwyta, podnosi, bierze, trzyma — przedmiot leży na miejscu.\n"
                    f"Użyj DOKŁADNIE tych opisów:\n{descriptions}"
                )

    # 3b. TALK — rozmowa z NPC
    if intent == "TALK":
        location_npcs = location.get("npcs", [])
        # Wybierz NPC: adresowany wprost lub pierwszy w lokacji
        target_npc = None
        if npc_id:
            target_npc = next((n for n in location_npcs if n["id"] == npc_id), None)
        if target_npc is None and location_npcs:
            target_npc = location_npcs[0]

        if target_npc is None:
            narrative = "Tu nikogo nie ma — pytasz w pustkę, ale nikt nie odpowiada."
            state["turn"] += 1
            state["history"].append({"turn": state["turn"], "gm": narrative})
            save_state(state)
            return {"narrative": narrative, "state": enrich_state(state, world)}

        # Sprawdź czy NPC jest dostępny
        npc_flag_key = f"{target_npc['id']}_state"
        current_npc_state = state["flags"].get(npc_flag_key, target_npc.get("state", ""))
        unavailable_msg = target_npc.get("unavailable_states", {}).get(current_npc_state)
        if unavailable_msg:
            state["turn"] += 1
            state["history"].append({"turn": state["turn"], "gm": unavailable_msg})
            save_state(state)
            return {"narrative": unavailable_msg, "state": enrich_state(state, world)}

        # NPC dostępny — nakieruj narratora żeby odpowiedział w charakterze NPC
        intent_context = (
            f"Gracz zwraca się do: {target_npc['name']}.\n"
            f"Odpowiedz WYŁĄCZNIE jako {target_npc['name']} — mów w pierwszej osobie, "
            f"zgodnie z charakterem opisanym w sekcji NPC. Nie opisuj co NPC robi, mów jego głosem."
        )

    # 4. NPC outcome classification — deterministyczne wyniki interakcji z NPC
    authoritative_flags = {}  # flagi z klasyfikatora — nadpiszą JSON Groka po narracji
    if intent not in ("TAKE", "EXAMINE") or not item_id:
        for npc in location.get("npcs", []):
            has_active = any(
                s.get("flags") and any(state["flags"].get(k) != v for k, v in s["flags"].items())
                for s in npc.get("scripted_solutions", [])
            )
            if not has_active:
                continue
            npc_flags = await classify_npc_outcome(player_input, npc, location, state, client, is_talk=(intent == "TALK"))
            if npc_flags:
                state["flags"].update(npc_flags)
                authoritative_flags.update(npc_flags)
                for s in npc.get("scripted_solutions", []):
                    s_flags = s.get("flags", {})
                    if s_flags and all(npc_flags.get(k) == v for k, v in s_flags.items()):
                        intent_context = f"WYNIK AKCJI — użyj DOSŁOWNIE jako narrację:\n{s['outcome'].strip()}"
                        break
                if not intent_context:
                    intent_context = f"interakcja z {npc['name']} zmieniła stan: {npc_flags} — opisz to naturalnie"
            break  # jeden NPC na turę

    # 5. Grok jako narrator z kontekstem intencji
    prompt = build_gm_prompt(player_input, state, world, intent_context=intent_context)
    response = client.chat.completions.create(
        model=GROK_MODEL,
        messages=[{"role": "user", "content": prompt}],
        max_tokens=500,
        temperature=0.8,
    )
    gm_response = response.choices[0].message.content.strip()
    print(f"[GM RAW] {gm_response[:120]}")

    # Grok zdecydował że to ruch — przekazujemy do deterministycznego mechanizmu
    if gm_response.startswith("MOVE:"):
        direction = gm_response.split(":", 1)[1].strip()
        move_result = _try_move(direction, state, world)
        if move_result:
            narrative, updated_state = move_result
            return {"narrative": narrative, "state": enrich_state(updated_state, world)}

    narrative, updated_state = _parse_gm_response(gm_response, state, player_input)
    if authoritative_flags:
        updated_state["flags"].update(authoritative_flags)
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


def _build_ending_prompt(player_input: str, state: dict, location: dict, ending_type: str) -> str:
    npc = location.get("npcs", [{}])[0]
    inventory = ", ".join(state["inventory"]) or "brak"
    return f"""Jesteś Mistrzem Gry kończącym przygodę w grze "Zamek Złej Królowej".
Gracz właśnie wszedł do sali tronowej i stanął twarzą w twarz z Królową Marzeną.

Napisz scenę zakończenia w 5-7 zdaniach. Styl: literacki, filmowy, trochę ironiczny.

Zasady tej sceny:
- Królowa jest piękna, inteligentna i całkowicie przejmuje kontrolę nad sytuacją
- Niezależnie co gracz mówi lub robi (akcja gracza: "{player_input}") — Królowa obraca to na swoją korzyść
- Scena kończy się tym że bohater zostaje przy Królowej z własnej woli — inne wyjście przestaje go interesować
- To happy end — tylko inny niż planowany. Bohater nie żałuje.
- Ekwipunek gracza: {inventory} — możesz go użyć komicznie/dramatycznie w scenie
- Zakończ scenę jednym krótkim, zapadającym w pamięć zdaniem pointą

Napisz TYLKO narrację, bez JSON, bez komentarzy."""


def _parse_gm_response(raw: str, state: dict, player_input: str = "") -> tuple[str, dict]:
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

    # Normalizacja: jeśli Grok ustawił troll_przekupiony/troll_pokonany jako bool-flagę zamiast troll_state
    if state["flags"].get("troll_przekupiony") and state["flags"].get("troll_state") == "blokuje_most":
        state["flags"]["troll_state"] = "troll_przekupiony"
    if state["flags"].get("troll_pokonany") and state["flags"].get("troll_state") == "blokuje_most":
        state["flags"]["troll_state"] = "troll_pokonany"

    # Fallback: więzień uwolniony (klasyfikator NPC nie obsługuje multi-flag outcome dla więźnia)
    wiezien_keywords = ["benedykt wychodzi", "znika między drzewami", "znika w lesie",
                        "pęka z", "wygina się", "wyważasz", "klatka staje otworem",
                        "klatka się otwiera", "krata pęka", "zawiasy", "wolny"]
    if (state["current_location"] == "klatka_wieznia"
            and not state["flags"].get("hidden_path_unlocked")
            and any(kw in narrative.lower() for kw in wiezien_keywords)):
        state["flags"]["wiezien_state"] = "uwolniony"
        state["flags"]["hidden_path_unlocked"] = True

    # Fallback: brama zamku
    brama_keywords = ["brama otwiera się", "brama stoi otworem", "brama otwarta",
                      "brama powoli się otwiera", "wrota się otwierają", "wrota stoją otworem"]
    if (state["current_location"] == "zamek"
            and state["flags"].get("brama_state") != "otwarta"
            and any(kw in narrative.lower() for kw in brama_keywords)):
        state["flags"]["brama_state"] = "otwarta"

    # Fallback: pustelnik zdradził hasło (hasło wypowiedziane lub przekonany inaczej)
    if (state["current_location"] == "wnetrze_hatki"
            and not state["flags"].get("haslo_znane")
            and "bum bara dum" in narrative.lower()):
        state["flags"]["haslo_znane"] = True
        state["flags"]["pustelnik_state"] = "pomoglismy"

    state["turn"] += 1
    state["history"].append({"turn": state["turn"], "gm": narrative})
    return narrative, state
