from __future__ import annotations
import json
import logging
import re
from datetime import datetime
from pathlib import Path

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from openai import OpenAI

from app.game_engine import load_world, load_state, save_state, reset_state, build_gm_prompt, check_condition, resolve_exits, resolve_description, find_item, find_items, _item_visible, check_world_events, apply_city_arrest_mechanic
from app.config import XAI_API_KEY, GROK_MODEL, GROK_VOICE, IMAGE_STYLES, get_game_dir, get_active_game, set_active_game, list_games
from app.image_service import generate_image, generate_image_i2i, image_path, resolve_variant, build_image_log, _find_base_variant

app = FastAPI()

LOG_DIR = Path(__file__).parent.parent / "logs"
LOG_DIR.mkdir(exist_ok=True)
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
    handlers=[
        logging.FileHandler(LOG_DIR / "game.log", encoding="utf-8"),
        logging.StreamHandler(),
    ],
)
log = logging.getLogger("game")


@app.on_event("startup")
async def on_startup():
    log.info(f"[SERVER START] active game: {get_active_game()}")

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
    inventory = state.get("inventory", [])
    file_id, label = resolve_variant(loc_id, location, state["flags"], inventory)
    path = image_path(file_id)

    if not path.exists() or force:
        variants = location.get("image_variants", [])
        active_variant = next((v for v in variants if v["file"] == file_id), None)
        prompt_extra = (active_variant or {}).get("prompt_extra", location.get("atmosphere", loc_id))

        is_base = active_variant and active_variant.get("base")
        if is_base:
            ok = await generate_image(file_id, location, prompt_extra, force=force)
        else:
            base_variant = _find_base_variant(location)
            base_path = image_path(base_variant["file"]) if base_variant else None
            if base_path:
                ok = await generate_image_i2i(file_id, base_path, location, prompt_extra, force=force)
            else:
                ok = await generate_image(file_id, location, prompt_extra, force=force)

        if not ok:
            return JSONResponse({"error": "generation failed"}, status_code=503)

    return FileResponse(str(path), media_type="image/png")


@app.get("/api/debug/solutions")
def debug_solutions():
    world = load_world()
    state = load_state()
    result = []
    for loc_id, location in world["locations"].items():
        for npc in location.get("npcs", []):
            npc_state = state["flags"].get(f"{npc['id']}_state", npc.get("state", ""))
            solutions = []
            for s in npc.get("scripted_solutions", []):
                req = s.get("requires", {})
                passes = True
                reason = ""
                if req:
                    inv = state["inventory"]
                    if "inventory" in req and req["inventory"] not in inv:
                        passes = False
                        reason = f"brak w ekwipunku: {req['inventory']}"
                    elif "inventory_missing" in req and req["inventory_missing"] in inv:
                        passes = False
                        reason = f"gracz MA (a nie powinien): {req['inventory_missing']}"
                    elif "flag" in req:
                        from app.game_engine import check_condition
                        if not check_condition(req, state["flags"]):
                            passes = False
                            reason = f"flaga {req['flag']} != {req.get('value', req.get('values'))}"
                solutions.append({
                    "id": s.get("id", "?"),
                    "trigger": s.get("trigger", ""),
                    "requires": req,
                    "flags": s.get("flags"),
                    "passes": passes,
                    "reason": reason,
                })
            result.append({
                "loc_id": loc_id,
                "loc_name": location["name"],
                "npc_id": npc["id"],
                "npc_name": npc["name"],
                "npc_state": npc_state,
                "solutions": solutions,
            })
    return result


@app.get("/api/debug/images")
def debug_images():
    world = load_world()
    state = load_state()
    result = []
    for loc_id, location in world["locations"].items():
        variants = build_image_log(loc_id, location, state["flags"], state.get("inventory", []))
        result.append({"loc_id": loc_id, "loc_name": location["name"], "variants": variants})
    return result


@app.get("/api/image-styles")
def image_styles():
    return IMAGE_STYLES


@app.get("/api/map-config")
def map_config():
    cfg_file = get_game_dir() / "map_config.json"
    if not cfg_file.exists():
        return JSONResponse({"levels": {}})
    import json as _json
    return _json.loads(cfg_file.read_text(encoding="utf-8"))


@app.get("/api/debug/games")
def debug_games():
    return {"active": get_active_game(), "games": list_games()}


@app.post("/api/debug/set-game")
def debug_set_game(body: dict):
    name = body.get("game", "")
    if not set_active_game(name):
        return JSONResponse({"error": f"Gra '{name}' nie istnieje"}, status_code=404)
    log.info(f"[GAME SWITCH] → {name}")
    return {"ok": True, "active": name}


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
    file_id, _ = resolve_variant(loc_id, loc, state["flags"], state.get("inventory", []))
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
def reset(start: str | None = None):
    world = load_world()
    state = reset_state(world)
    if start and start in world["locations"]:
        state["current_location"] = start
        save_state(state)
    intro_key = f"intro_{state['current_location']}"
    intro = world.get(intro_key) or world.get("intro", "")
    return {"state": enrich_state(state, world), "intro": intro.strip()}


def _check_requires(req: dict, state: dict) -> bool:
    if not req:
        return True
    inv = state["inventory"]
    if "inventory" in req and req["inventory"] not in inv:
        return False
    if "inventory_missing" in req and req["inventory_missing"] in inv:
        return False
    if "flag" in req:
        return check_condition(req, state["flags"])
    return True


async def classify_npc_outcome(player_input: str, npc: dict, location: dict, state: dict, client, is_talk: bool = False) -> dict:
    """Klasyfikuje czy akcja gracza zmienia stan NPC. Zwraca flags_update lub {}."""
    current_state = state["flags"].get(f"{npc['id']}_state", npc.get("state", ""))
    creative = npc.get("creative_solutions_hint", "")
    options = [{"label": "brak zmiany", "flags": {}}]
    action_hints = []
    talk_allowed_options = set()
    for s in npc.get("scripted_solutions", []):
        if s.get("flags") is not None and _check_requires(s.get("requires", {}), state):
            option_num = len(options)
            options.append({"label": s["trigger"], "flags": s["flags"]})
            if s.get("action_hint"):
                action_hints.append(f"Opcja {option_num}: {s['action_hint']}")
            if s.get("allow_talk"):
                talk_allowed_options.add(option_num)

    if len(options) == 1:
        return None  # żadna scripted_solution nie przeszła requires — nic nie może odpalić

    options_text = "\n".join(f"{i}: {o['label']}" for i, o in enumerate(options))

    hints_section = ""
    if action_hints:
        hints_section = "BEZWZGLĘDNE WARUNKI (niespełniony = opcja 0, bez wyjątków):\n" + "\n".join(action_hints) + "\n"

    talk_rule = ""
    if is_talk:
        talk_exceptions = ""
        if talk_allowed_options:
            nums = ", ".join(str(n) for n in sorted(talk_allowed_options))
            talk_exceptions = f"\n- WYJĄTEK: opcje {nums} mogą odpalić przez dialog (gracz wyraźnie wręcza/ofiarowuje przedmiot słownie)."
        talk_rule = f"""
WAŻNE — gracz MÓWI DO NPC (forma dialogowa, nie akcja fizyczna):
- Triggery wymagające fizycznej akcji (dawanie przedmiotu, atakowanie, czekanie, używanie czegoś)
  NIE mogą odpalić przez sam dialog. Przykład: "dam ci złoto" ≠ danie złota → opcja 0.
- Triggery które SĄ dialogiem (blefowanie, podszywanie się, przekonywanie, straszenie słownie)
  MOGĄ odpalić przez dialog. Przykład: "jestem lordem X" → pasuje do triggera blefowania → opcja N.{talk_exceptions}
"""

    prompt = f"""Klasyfikator wyników interakcji z NPC. Odpowiedz TYLKO jedną cyfrą.

NPC: {npc['name']} (stan: {current_state})
Ekwipunek gracza: {', '.join(state['inventory']) or 'brak'}
{talk_rule}
Opcje wyniku:
{options_text}

{hints_section}
Kreatywne rozwiązania (wybierz NAJBLIŻSZĄ opcję z listy):
{creative}

ZASADA: dopasuj trigger TYLKO jeśli gracz używa przedmiotów które faktycznie ma w ekwipunku.
Jeśli gracz tworzy/wyczarowuje/przywołuje/wymyśla przedmioty których nie ma — zawsze opcja 0.

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
            log.info(f"[NPC OUTCOME] {npc['id']} option {idx}: {options[idx]['flags']}")
            if idx == 0:
                return None  # opcja 0 = brak zmiany, żadne scripted_solution nie odpaliło
            return options[idx]["flags"]  # {} lub {flagi} — scripted_solution odpaliło
        except Exception as e:
            log.info(f"[NPC OUTCOME ERROR] attempt {attempt+1}: {e}")
    log.info(f"[NPC OUTCOME] wszystkie próby nieudane, zwracam None")
    return None


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

TALK — gdy gracz MÓWI DO kogoś lub PYTA kogoś (NPC w lokacji, NPC = {has_npc}):
  - zwrot bezpośredni: wołacz, "ty", "ci", "cię", rozkaz skierowany do osoby
    Przykłady: "hej trolu", "przepuść mnie", "co wiesz o zamku?", "dam ci sakiewkę"
  - pytanie skierowane DO NPC lub o NPC: "co o tym wiesz?", "powiedz mi", "czy przepuścisz?"
  - deklaracja intencji zamiast akcji: "chcę", "mogę", "zamierzam", "dam ci", "spróbuję"
  - negocjacja, prośba, groźba słowna, przekonywanie
  UWAGA: pytanie o PRZEDMIOT z lokacji to EXAMINE, nie TALK ("co jest w szafie" → EXAMINE szafa)
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
            log.info(f"[INTENT] {result.get('intent')} item_id={result.get('item_id')} npc_id={result.get('npc_id')}")
            return result
        except Exception as e:
            log.info(f"[INTENT ERROR] attempt {attempt+1}: {e}")
    log.info("[INTENT] wszystkie próby nieudane, fallback OTHER")
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
        apply_city_arrest_mechanic(state, world)
        event = check_world_events(state, world)
        if event:
            state["current_location"] = event["target_location"]
            if event.get("inventory_clear"):
                state["inventory"] = []
            state["turn"] += 1
            event_narrative = event["narrative"].strip()
            state["history"].append({"turn": state["turn"], "gm": event_narrative})
            save_state(state)
            return description.strip() + "\n\n" + event_narrative, state
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
    log.info(f"[INTENT] {intent} item_id={item_id} npc_id={npc_id}")

    # 3. Dispatch — modyfikuj stan i buduj kontekst dla narratora
    intent_context = ""

    # Ustaw flagi dla rozpoznanego itemu
    # set_on: examine → tylko gdy EXAMINE intent
    # brak set_on → przy każdej wzmiance (np. "przeskocz zapadnię")
    if item_id:
        for item in find_items(item_id, location, state["flags"], state.get("inventory", [])):
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
        item = find_item(item_id, location, state["flags"], state.get("inventory", []))
        if item and item.get("takeable") and item["name"] not in state["inventory"]:
            state["inventory"].append(item["name"])
            intent_context = f"gracz właśnie podniósł '{item['name']}' — opisz jak go bierze, potwierdź że trzyma go w rękach"
        elif item and not item.get("takeable", True):
            intent_context = f"gracz próbuje wziąć '{item.get('hint', item_id)}' — to jest element otoczenia, nie można zabrać, wyjaśnij krótko dlaczego"
        elif item and item["name"] in state["inventory"]:
            intent_context = f"gracz próbuje wziąć '{item['name']}' — już to ma przy sobie"

    elif intent == "EXAMINE":
        items = find_items(item_id, location, state["flags"], state.get("inventory", []))
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
                # Jeśli badany przedmiot odsłania inne itemy (examine_sets_flag), wylistuj co zostało w środku
                examined_flags = items[0].get("examine_sets_flag", {})
                if examined_flags:
                    revealed_flag = list(examined_flags.keys())[0]
                    container_items = [
                        i for i in location.get("items", [])
                        if i.get("hidden_when", {}).get("flag") == revealed_flag
                        and _item_visible(i, state["flags"], state.get("inventory", []))
                    ]
                    if container_items:
                        names = ", ".join(i["name"] for i in container_items)
                        intent_context += f"\nBEZWZGLĘDNIE WYMIEŃ Z NAZWY przedmioty które gracz widzi w środku: {names}"
                    else:
                        intent_context += "\nW środku jest już pusto — wszystko zostało wzięte. Powiedz to graczowi wprost."

    # 3b. Obsługa prób ominięcia pułapki — sprawdź czy w lokacji jest item z set_on: examine
    if intent not in ("TAKE", "EXAMINE") and not intent_context:
        trap_item = next(
            (i for i in location.get("items", [])
             if i.get("set_on") == "examine" and i.get("examine_sets_flag")),
            None
        )
        if trap_item:
            flag_key = list(trap_item["examine_sets_flag"].keys())[0]
            if state["flags"].get(flag_key):
                for k, v in (trap_item.get("bypass_sets_flag") or {}).items():
                    state["flags"][k] = v
                intent_context = (
                    f"gracz omija '{trap_item['name']}' — wie już jak to zrobić (zbadał wcześniej). "
                    f"Opisz że bezpiecznie mija pułapkę trzymając się ściany i staje przed masywnyni drzwiami. "
                    f"NIE opisuj ruchu do następnej lokacji — gracz musi sam wpisać kierunek."
                )
            else:
                intent_context = (
                    f"gracz próbuje ominąć niebezpieczny fragment posadzki nie wiedząc gdzie dokładnie jest pułapka. "
                    f"Próba kończy się NIEPOWODZENIEM — gracz cofa się w ostatniej chwili lub traci równowagę. "
                    f"NIE opisuj że przeszedł. Zasugeruj że warto najpierw dokładniej zbadać posadzkę."
                )

    # 3c. TALK — rozmowa z NPC
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
    scripted_solution_fired = None  # None = brak, {} lub {flagi} = scripted_solution odpaliło
    scripted_solution_obj = None    # pełny obiekt scripted_solution (do inventory_remove)
    if intent not in ("TAKE", "EXAMINE") or not item_id:
        for npc in location.get("npcs", []):
            has_active = any(
                s.get("flags") is not None and any(state["flags"].get(k) != v for k, v in s["flags"].items())
                for s in npc.get("scripted_solutions", [])
            )
            if not has_active:
                continue
            npc_flags = await classify_npc_outcome(player_input, npc, location, state, client, is_talk=(intent == "TALK"))
            if npc_flags is not None:
                scripted_solution_fired = npc_flags
                if npc_flags:
                    state["flags"].update(npc_flags)
                    authoritative_flags.update(npc_flags)
                # Znajdź pasujący scripted_solution (outcome + inventory_remove)
                for s in npc.get("scripted_solutions", []):
                    if s.get("flags") == npc_flags:
                        scripted_solution_obj = s
                        intent_context = f"WYNIK AKCJI — użyj DOSŁOWNIE jako narrację:\n{s['outcome'].strip()}"
                        break
                if not intent_context:
                    intent_context = f"interakcja z {npc['name']} — opisz naturalnie, nie zmieniaj stanu"
            break  # jeden NPC na turę

    # 5. Grok jako narrator z kontekstem intencji
    prompt = build_gm_prompt(player_input, state, world, intent_context=intent_context, scripted_fired=scripted_solution_fired is not None)
    response = client.chat.completions.create(
        model=GROK_MODEL,
        messages=[{"role": "user", "content": prompt}],
        max_tokens=500,
        temperature=0.8,
    )
    gm_response = response.choices[0].message.content.strip()
    log.info(f"[GM RAW] {gm_response[:120]}")

    # Grok zdecydował że to ruch — przekazujemy do deterministycznego mechanizmu
    if gm_response.startswith("MOVE:"):
        direction = gm_response.split(":", 1)[1].strip()
        move_result = _try_move(direction, state, world)
        if move_result:
            narrative, updated_state = move_result
            return {"narrative": narrative, "state": enrich_state(updated_state, world)}

    flags_before_narrator = state["flags"].copy()
    narrative, updated_state = _parse_gm_response(gm_response, state, player_input)
    if scripted_solution_fired is not None:
        # Scripted solution odpaliło — przywróć flagi do stanu sprzed narracji
        # i nałóż TYLKO to co scripted_solution zdecydowało. Grok nie może nic dodać.
        updated_state["flags"] = {**flags_before_narrator, **scripted_solution_fired}
        # Deterministycznie usuń przedmioty zużyte przez scripted_solution (narrator nie może ich przywrócić)
        if scripted_solution_obj:
            for item in scripted_solution_obj.get("inventory_remove", []):
                updated_state["inventory"] = [i for i in updated_state["inventory"] if i != item]
    elif authoritative_flags:
        updated_state["flags"].update(authoritative_flags)
    apply_city_arrest_mechanic(updated_state, world)
    # Scripted solution może przenieść gracza do innej lokacji
    if scripted_solution_obj and scripted_solution_obj.get("move_to"):
        target = scripted_solution_obj["move_to"]
        if target in world["locations"]:
            updated_state["current_location"] = target
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
    # Usuń blok JSON z narracji — z zamknięciem lub bez
    narrative = re.sub(r"```json.*?```", "", raw, flags=re.DOTALL)
    narrative = re.sub(r"```json.*", "", narrative, flags=re.DOTALL)
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

    state["turn"] += 1
    state["history"].append({"turn": state["turn"], "gm": narrative})
    return narrative, state
