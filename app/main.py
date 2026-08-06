from __future__ import annotations
import json
import logging
import random
import re
from datetime import datetime
from pathlib import Path

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from openai import OpenAI

from app.game_engine import load_world, load_state, save_state, reset_state, build_gm_prompt, check_condition, resolve_exits, resolve_description, find_item, find_items, _item_visible, check_world_events, apply_city_arrest_mechanic, apply_threshold_mechanic, record_npc_dialogue
from app.config import XAI_API_KEY, GROK_MODEL, GROK_VOICE, IMAGE_STYLES, get_game_dir, get_active_game, set_active_game, list_games, NPC_DIALOGUE_MEMORY_ENABLED
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


@app.get("/api/cut-scene/{filename}")
async def cut_scene_file(filename: str):
    path = get_game_dir() / "cut_scenes" / filename
    if not path.exists() or not path.is_file():
        return JSONResponse({"error": "not found"}, status_code=404)
    return FileResponse(str(path), media_type="video/mp4")


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
    inventory = state.get("inventory", [])

    # Krawędzie generowane z world.yaml — bez hardkodowanych warunków
    edges = []
    for loc_id, loc in world["locations"].items():
        for direction, exit_def in loc.get("exits", {}).items():
            if exit_def is None:
                continue
            if isinstance(exit_def, dict):
                target = exit_def["target"]
                req = exit_def.get("requires")
                weight = 1 if (not req or check_condition(req, flags, inventory)) else 0
                edge_type = "entry_only" if exit_def.get("entry_only") else None
            else:
                target = exit_def
                weight = 1
                edge_type = None
            edge = {"from": loc_id, "to": target, "label": direction, "weight": weight}
            if edge_type:
                edge["type"] = edge_type
            edges.append(edge)
            trap = exit_def.get("trap") if isinstance(exit_def, dict) else None
            if trap and trap.get("target") in world["locations"]:
                edges.append({"from": loc_id, "to": trap["target"], "label": "pułapka", "weight": 1, "type": "trap"})

    # Krawędzie scripted move_to (z NPC scripted_solutions)
    for loc_id, loc in world["locations"].items():
        for npc in loc.get("npcs", []):
            for ss in npc.get("scripted_solutions", []):
                if ss.get("move_to") and ss["move_to"] in world["locations"]:
                    edges.append({"from": loc_id, "to": ss["move_to"], "label": ss.get("id", "scripted"), "weight": 1, "type": "scripted"})

    # Krawędzie move_sequence (np. burzowe piaski — "ślepa" sekwencja ruchów)
    for loc_id, loc in world["locations"].items():
        move_seq = loc.get("move_sequence")
        if not move_seq:
            continue
        if move_seq.get("success_target") in world["locations"]:
            edges.append({"from": loc_id, "to": move_seq["success_target"], "label": "sekwencja OK", "weight": 1, "type": "scripted"})
        if move_seq.get("fail_target") in world["locations"]:
            edges.append({"from": loc_id, "to": move_seq["fail_target"], "label": "sekwencja błąd", "weight": 1, "type": "scripted"})

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
    accessible, blocked = resolve_exits(loc, state["flags"], state.get("inventory", []), state["flags"].get(f"wejscie_{loc_id}"))
    # move_sequence nierozwiązane: interfejs musi pokazywać WSZYSTKIE 4 kierunki (gracz nie
    # wie który krok jest "poprawny"), a nie statyczne exits (te opisują stan PO rozwiązaniu)
    move_seq = loc.get("move_sequence")
    if move_seq and not state["flags"].get(move_seq.get("solved_flag") or "__brak__"):
        accessible = {d: loc_id for d in ("północ", "południe", "wschód", "zachód")}
        blocked = {}
    # Wyjścia z pułapką są widoczne nawet gdy warunek nie spełniony — gracz musi móc w nie wejść
    trap_exits = [
        direction for direction, exit_def in loc.get("exits", {}).items()
        if isinstance(exit_def, dict)
        and exit_def.get("trap")
        and direction not in accessible
    ]
    state["available_exits"] = list(accessible.keys()) + trap_exits
    state["blocked_exits"] = {d: m for d, m in blocked.items() if d not in trap_exits}

    # TYMCZASOWE — liczniki do testów mechaniki aresztowania, usunąć razem z frontendem po testach
    mechanic = world.get("mechanics", {}).get("city_arrest")
    if mechanic:
        count = sum(1 for f in mechanic["track_flags"] if state["flags"].get(f))
        threshold_full = mechanic.get("threshold_full", mechanic.get("threshold", 4))
        threshold_partial = mechanic.get("threshold_partial", 3)
        min_transitions = mechanic.get("min_transitions", 10)
        transitions = state.get("transitions", 0)
        state["debug_arrest_counter"] = (
            f"{count}/{threshold_full} wskazówek | {transitions}/{min_transitions} kroków "
            f"(próg 2: {threshold_partial}+ wskazówek)"
        )
    state["debug_step_counter"] = str(state.get("transitions", 0))

    # hidden_items — gracz JE MA (liczą się do wszystkich requires/inventory_all itd.,
    # dlatego filtrowanie robimy dopiero tutaj, na samym końcu, na potrzeby wyświetlenia,
    # a nie wcześniej gdzie inventory jest używane do logiki gry np. w resolve_exits)
    hidden_defs = world.get("hidden_items", {})
    if hidden_defs:
        state["inventory"] = [
            i for i in state["inventory"]
            if i not in hidden_defs or check_condition(hidden_defs[i], state["flags"], state["inventory"])
        ]

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
    description = resolve_description(location, state["flags"], state.get("inventory", []))
    last_narrative = state["history"][-1]["gm"] if state["history"] else description
    return {"state": enrich_state(state, world), "narrative": last_narrative}


@app.get("/api/start-locations")
def start_locations():
    world = load_world()
    return world.get("start_locations", [])


@app.post("/api/reset")
def reset(start: str | None = None):
    world = load_world()
    state = reset_state(world)
    if start and start in world["locations"]:
        state["current_location"] = start
        start_def = next((s for s in world.get("start_locations", []) if s["id"] == start), None)
        if start_def and start_def.get("initial_flags"):
            state["flags"].update(start_def["initial_flags"])
        if start_def and start_def.get("initial_inventory"):
            state["inventory"] = list(start_def["initial_inventory"])
        save_state(state)
    intro_key = f"intro_{state['current_location']}"
    intro = world.get(intro_key) or world.get("intro", "")
    return {"state": enrich_state(state, world), "intro": intro.strip()}


def _check_requires(req: dict, state: dict) -> bool:
    # deleguje w całości do check_condition (obsługuje inventory/inventory_missing/
    # inventory_all/flags_all/flag) — wcześniej ta funkcja była uboższą kopią i gubiła
    # inventory_all/flags_all, przez co np. warunek "4 flagi naraz" nigdy by nie przeszedł
    return check_condition(req, state["flags"], state["inventory"])


class ClassificationUnavailable(Exception):
    """Klasyfikacja LLM nie powiodła się (błąd/przeciążenie API) — odróżnij to od
    legalnego 'brak dopasowania', bo twarde bloki (no_match_message) nie mogą
    traktować awarii infrastruktury jak potwierdzonego wyniku klasyfikacji."""


_PL_DIACRITICS = str.maketrans("ąćęłńóśźż", "acelnoszz")


def _normalize_pl(text: str) -> str:
    return text.lower().translate(_PL_DIACRITICS)


def _match_exact_sequence(player_input: str, sequence: list) -> bool:
    """Sprawdza czy w tekście gracza występują WSZYSTKIE elementy sekwencji, w podanej
    kolejności — dowolny tekst przed/pomiędzy/po jest ignorowany (nie trzeba wypowiadać
    słów bezpośrednio obok siebie). Każdy element może być pojedynczym słowem albo listą
    synonimów (np. ["złoty", "złoto"]) — wystarczy że pasuje którykolwiek z nich.
    Używane dla haseł/sekwencji o jednej, dosłownej poprawnej odpowiedzi (np. kolejność
    kolorów, hasło) — takich rzeczy nie powinniśmy oceniać przez LLM (zawodne, kosztowne),
    tylko sprawdzać deterministycznie w kodzie."""
    text = _normalize_pl(player_input)
    pos = 0
    for step in sequence:
        candidates = step if isinstance(step, list) else [step]
        best_end = None
        best_start = None
        for cand in candidates:
            m = re.search(rf"\b{re.escape(_normalize_pl(cand))}\b", text[pos:])
            if m and (best_start is None or m.start() < best_start):
                best_start, best_end = m.start(), m.end()
        if best_end is None:
            return False
        pos += best_end
    return True


def _match_deterministic_solution(player_input: str, npc: dict, state: dict) -> dict | None:
    """Przed wywołaniem LLM sprawdza scripted_solutions z polem exact_sequence —
    dopasowanie w 100% deterministyczne, zero kosztu i zero losowości modelu."""
    for s in npc.get("scripted_solutions", []):
        seq = s.get("exact_sequence")
        if not seq:
            continue
        if s.get("flags") is None or not _check_requires(s.get("requires", {}), state):
            continue
        if _match_exact_sequence(player_input, seq):
            log.info(f"[NPC OUTCOME] {npc['id']} matched (deterministyczne): {s['id']}")
            return s
    return None


def _append_story_progress(outcome_text: str, progress_key: str | None, state: dict, world: dict) -> str:
    """Dopisuje do tekstu dynamiczną listę już poznanych/brakujących historii mieszkańców
    lasu, na podstawie realnego stanu ekwipunku — używane zarówno przez scripted_solutions
    (rozmowa z Ivanem) jak i eventy (wejście do chatki z niepełnym kompletem)."""
    if not progress_key:
        return outcome_text
    progress_mechanic = world.get("mechanics", {}).get(progress_key)
    if not progress_mechanic:
        return outcome_text
    track_items = progress_mechanic["track_items"]
    labels = progress_mechanic.get("labels", {})
    known = [labels.get(i, i) for i in track_items if i in state["inventory"]]
    missing = [labels.get(i, i) for i in track_items if i not in state["inventory"]]
    if not missing:
        return outcome_text + "\n\n(Księga zna już historie wszystkich mieszkańców lasu.)"
    if known:
        return outcome_text + (
            f"\n\n(Księga zna już historię: {', '.join(known)}. "
            f"Wciąż brakuje jej opowieści: {', '.join(missing)}.)"
        )
    return outcome_text + f"\n\n(Nie znasz jeszcze żadnej z nich: {', '.join(missing)}.)"


async def classify_npc_outcome(player_input: str, npc: dict, location: dict, state: dict, client, is_talk: bool = False) -> dict | None:
    """Klasyfikuje czy akcja gracza zmienia stan NPC. Zwraca dopasowany scripted_solution (cały obiekt) albo None."""
    current_state = state["flags"].get(f"{npc['id']}_state", npc.get("state", ""))
    creative = npc.get("creative_solutions_hint", "")
    # id -> {label, flags, solution}. Klasyfikator odpowiada identyfikatorem (semantycznym),
    # nie numerem pozycji — przy wielu podobnych opcjach naraz (np. 6 zagadek) model gubił się
    # licząc pozycje w liście i zwracał "brak" nawet dla poprawnej odpowiedzi. Dopasowanie po
    # nazwie zamiast po indeksie to ten sam trik co przy npc_id w classify_intent.
    options: dict[str, dict] = {}
    action_hints = []
    talk_allowed_ids = set()
    for s in npc.get("scripted_solutions", []):
        if s.get("flags") is not None and _check_requires(s.get("requires", {}), state):
            sid = s["id"]
            options[sid] = {"label": s["trigger"], "flags": s["flags"], "solution": s}
            if s.get("action_hint"):
                action_hints.append(f"{sid}: {s['action_hint']}")
            if s.get("allow_talk"):
                talk_allowed_ids.add(sid)

    if not options:
        return None  # żadna scripted_solution nie przeszła requires — nic nie może odpalić

    options_text = "\n".join(f"{sid}: {o['label']}" for sid, o in options.items())

    hints_section = ""
    if action_hints:
        hints_section = "BEZWZGLĘDNE WARUNKI (niespełniony = \"brak\", bez wyjątków):\n" + "\n".join(action_hints) + "\n"

    talk_rule = ""
    if is_talk:
        talk_exceptions = ""
        if talk_allowed_ids:
            ids_list = ", ".join(sorted(talk_allowed_ids))
            talk_exceptions = f"\n- WYJĄTEK: {ids_list} mogą odpalić przez dialog (gracz wyraźnie wręcza/ofiarowuje przedmiot słownie)."
        talk_rule = f"""
WAŻNE — gracz MÓWI DO NPC (forma dialogowa, nie akcja fizyczna):
- Triggery wymagające fizycznej akcji (dawanie przedmiotu, atakowanie, czekanie, używanie czegoś)
  NIE mogą odpalić przez sam dialog. Przykład: "dam ci złoto" ≠ danie złota → brak.
- Triggery które SĄ dialogiem (blefowanie, podszywanie się, przekonywanie, straszenie słownie)
  MOGĄ odpalić przez dialog. Przykład: "jestem lordem X" → pasuje do triggera blefowania → odpowiedni identyfikator.{talk_exceptions}
"""

    prompt = f"""Klasyfikator wyników interakcji z NPC. Odpowiedz TYLKO identyfikatorem opcji, dokładnie jak w liście, albo słowem "brak".

NPC: {npc['name']} (stan: {current_state})
Ekwipunek gracza: {', '.join(state['inventory']) or 'brak'}
{talk_rule}
Opcje wyniku (identyfikator: opis):
{options_text}

{hints_section}
Kreatywne rozwiązania (wybierz NAJBLIŻSZY identyfikator z listy):
{creative}

ZASADA: dopasuj trigger TYLKO jeśli gracz używa przedmiotów które faktycznie ma w ekwipunku.
Jeśli gracz tworzy/wyczarowuje/przywołuje/wymyśla przedmioty których nie ma — zawsze "brak".

Akcja gracza: "{player_input}"

Odpowiedź: tylko identyfikator dokładnie jak w liście (np. "zagadka_ksiezyc") albo "brak" jeśli żadna opcja nie pasuje"""

    for attempt in range(3):
        try:
            resp = client.chat.completions.create(
                model=GROK_MODEL,
                messages=[{"role": "user", "content": prompt}],
                max_tokens=20,
                temperature=0,
            )
            raw = resp.choices[0].message.content.strip().strip('"').strip("'").strip(".")
            if raw.lower() == "brak":
                log.info(f"[NPC OUTCOME] {npc['id']}: brak dopasowania")
                return None
            matched_id = raw if raw in options else next((sid for sid in options if sid in raw), None)
            if matched_id is None:
                print(f"[NPC OUTCOME] attempt {attempt+1}: nierozpoznana odpowiedź '{raw}', retry")
                continue
            log.info(f"[NPC OUTCOME] {npc['id']} matched: {matched_id}")
            return options[matched_id]["solution"]
        except Exception as e:
            log.info(f"[NPC OUTCOME ERROR] attempt {attempt+1}: {e}")
    log.info(f"[NPC OUTCOME] wszystkie próby nieudane (błąd API) — zgłaszam niedostępność klasyfikacji")
    raise ClassificationUnavailable()


async def classify_intent(player_input: str, location: dict, state: dict, client) -> dict:
    items_visible = [
        f"{i.get('id', '?')}: {i.get('hint', i['name'])}"
        for i in location.get("items", [])
    ]
    inventory_ids = state["inventory"]
    npcs_visible = [f"{n['id']}: {n['name']}" for n in location.get("npcs", [])]
    has_npc = bool(npcs_visible)
    has_crowd = bool(location.get("crowd")) and has_npc
    crowd_block = ""
    crowd_format_field = ""
    if has_crowd:
        crowd_block = """
=== TŁUM W TEJ LOKACJI ===
Oprócz wymienionych NPC, w tle jest bezimienny tłum/przechodnie. Przy TALK rozstrzygnij dodatkowo:
  - gracz zwraca się do KONKRETNEGO NPC z listy (po imieniu/funkcji, kontynuacja rozmowy z nim/nią) → crowd_target: false
  - gracz zwraca się OGÓLNIE bez adresata, ALBO wprost wskazuje kogoś innego/nieznajomego/z tłumu
    ("pytam kogoś innego", "zaczepiam przechodnia", "ignoruję ją, pytam kogoś z tłumu") → crowd_target: true
  Przy niejasności, gdy nic nie wskazuje na tłum — wybierz crowd_target: false (domyślnie zwraca się do obecnego NPC).
"""
        crowd_format_field = ', "crowd_target": true_lub_false'
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
  - deklaracja intencji BEZ konkretnej, samodzielnej akcji, wymagająca zgody/reakcji innej postaci:
    "chcę przejść", "mogę tu zostać?", "zamierzam się nie poddać" — gdy dotyczy czegoś czego gracz
    nie może zrobić sam, tylko NPC musi na to pozwolić lub zareagować
  - negocjacja, prośba, groźba słowna, przekonywanie
  UWAGA: pytanie o PRZEDMIOT z lokacji to EXAMINE, nie TALK ("co jest w szafie" → EXAMINE szafa)
  UWAGA: jeśli "chcę/mogę/zamierzam/spróbuję" poprzedza konkretny czasownik akcji który gracz
  wykonuje SAM, bez niczyjej zgody (wziąć, otworzyć, zbadać, wejść, użyć) — klasyfikuj wg TEGO
  czasownika (TAKE/USE/EXAMINE/MOVE), NIE jako TALK.
  Przykłady: "chcę wziąć złoto" → TAKE, "chcę zbadać szafę" → EXAMINE, "chcę wejść do środka" → MOVE

MOVE — gracz przemieszcza się w kierunku świata:
  Przykłady: "idę na północ", "wchodzę", "wracam", "przekraczam most"

EXAMINE — gracz bada/ogląda coś, LUB podaje sam przedmiot/miejsce bez czasownika:
  Przykłady: "badam ścianę", "przyglądam się kamieniom", "sprawdzam drzwi"
  WAŻNE: samo słowo lub krótka fraza bez czasownika = ZAWSZE EXAMINE, nigdy TAKE
  Przykłady: "miecz", "sakiewka", "kamień", "stary kij" → EXAMINE

TAKE — gracz podnosi/bierze przedmiot Z LOKACJI (musi być wśród przedmiotów lokacji powyżej) —
  WYMAGA wyraźnego czasownika brania:
  Przykłady: "biorę miecz", "podnoszę kij", "zabieram sakiewkę", "weź miecz"
  Samo wymienienie przedmiotu bez czasownika NIE jest TAKE
  WAŻNE: jeśli przedmiot z komendy "weź/bierz X" NIE występuje wśród przedmiotów lokacji, ale
  JEST w ekwipunku gracza, a w lokacji jest NPC — to rozkaz w 2. osobie skierowany do NPC
  (gracz oferuje/wręcza przedmiot który już ma), NIE branie z ziemi. Klasyfikuj jako OTHER.
  Przykład: gracz ma "kostki cukru" w ekwipunku, mówi do centaura "bierz cukier" → OTHER, nie TAKE.

USE — gracz używa przedmiotu z ekwipunku na czymś:
  Przykłady: "używam miecza", "rzucam kij w trolla"

OTHER — gracz opisuje czynność fizyczną nie będącą ruchem/braniem/badaniem:
  Przykłady: "uderzam trolla", "czekam", "atakuję", "kładę się spać", "daję trolowi sakiewkę"
  Uwaga: "daję [komuś] [coś]" to OTHER (akcja), "dam ci [coś]" to TALK (deklaracja)
{crowd_block}
=== FORMAT ODPOWIEDZI ===
{{"intent": "TALK|MOVE|TAKE|EXAMINE|USE|OTHER", "item_id": "id_lub_null", "npc_id": "id_npc_lub_null"{crowd_format_field}}}

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


async def split_compound(player_input: str, client) -> list[str]:
    """Detect if input contains two commands. Returns [cmd] or [cmd1, cmd2] (capped at 2)."""
    prompt = f"""Oceń czy gracz wydał DWIE RÓŻNE AKCJE w jednym zdaniu.
ZŁOŻONE = dwa różne CZASOWNIKI akcji (weź + otwórz, zbadaj + weź).
NIE ZŁOŻONE = jeden czasownik + lista przedmiotów połączonych "i" (weź X i Y i Z = jedna akcja TAKE).

Akcja: "{player_input}"

Odpowiedz TYLKO w JSON (bez markdown):
- Jedno polecenie: {{"compound": false, "actions": ["{player_input}"]}}
- Dwie akcje: {{"compound": true, "actions": ["pierwsza akcja", "druga akcja"]}}

Przykłady ZŁOŻONYCH (dwa czasowniki):
- "weź klucz i otwórz drzwi" → compound: true, ["weź klucz", "otwórz drzwi"]
- "zbadaj szafę i weź sukienkę" → compound: true, ["zbadaj szafę", "weź sukienkę"]

Przykłady NIE-ZŁOŻONYCH (jeden czasownik, lista przedmiotów):
- "weź miecz i tarczę" → compound: false
- "weź miecz tarczę kolczugę i złoto" → compound: false
- "biorę miecz i sakiewkę" → compound: false
- "czy jest tu klucz i co z nim zrobić?" → compound: false
- "wszystko!" → compound: false
- "atakuję trolla" → compound: false"""
    try:
        resp = client.chat.completions.create(
            model=GROK_MODEL,
            messages=[{"role": "user", "content": prompt}],
            max_tokens=80,
            temperature=0,
        )
        raw = resp.choices[0].message.content.strip()
        raw = re.sub(r"^```\w*\s*", "", raw)
        raw = re.sub(r"\s*```$", "", raw)
        data = json.loads(raw)
        actions = data.get("actions") or [player_input]
        return [a for a in actions[:2] if a]
    except Exception as e:
        log.info(f"[COMPOUND SPLIT ERROR] {e}")
        return [player_input]


# Słowa kluczowe dla auto_reveal — deterministyczny check tematu zamiast dopasowania triggera przez LLM.
_AUTO_REVEAL_KEYWORDS = ("karawan", "królow", "krolow", "żołd", "zold")

_TAKE_STOPWORDS = {"i", "z", "ze", "w", "do", "na", "od", "o", "a", "ta", "to", "ten", "tą", "tę"}


def _item_mentioned(item: dict, lower_input: str) -> bool:
    """Sprawdza czy gracz wymienił ten przedmiot po nazwie/hincie — dla brania kilku konkretnych rzeczy naraz.
    Dopasowanie po rdzeniu słowa (bez końcówki odmiany), żeby "złoto" złapało "złotem" itp."""
    text = f"{item['name']} {item.get('hint', '')}".lower()
    words = [w for w in re.findall(r"\w+", text) if len(w) >= 4 and w not in _TAKE_STOPWORDS]
    stems = {w[:max(4, len(w) - 2)] for w in words}
    return any(stem in lower_input for stem in stems)


async def _execute_intent_step(
    player_input: str,
    state: dict,
    world: dict,
    location: dict,
    client,
) -> tuple[str, dict | None, dict | None, dict, bool, str | None, bool, str]:
    """
    Single intent: classify + dispatch. Mutates state (flags, inventory) in place.
    Returns (intent_context, scripted_solution_fired, scripted_solution_obj, authoritative_flags, early_return, talk_npc_id, crowd_talk, intent).
    early_return=True means return intent_context directly without calling narrator.
    talk_npc_id is set when intent==TALK and a target NPC was resolved (used to record dialogue memory).
    crowd_talk=True means the player addressed the ambient crowd (no specific NPC) — caller must
    hard-revert any flags/inventory/location changes the narrator's JSON might hallucinate for this turn.
    intent is the raw classified intent (TALK/MOVE/TAKE/EXAMINE/USE/OTHER) — caller uses it to decide
    whether the narrator's own JSON inventory_add should be trusted at all (only for TAKE; granting an
    item during EXAMINE/OTHER just because Grok's narration happened to mention it is not something the
    player asked for).
    """
    classified = await classify_intent(player_input, location, state, client)
    intent = classified.get("intent", "OTHER")
    item_id = classified.get("item_id")
    npc_id = classified.get("npc_id")
    log.info(f"[INTENT] {intent} item_id={item_id} npc_id={npc_id}")

    intent_context = ""
    authoritative_flags: dict = {}
    scripted_solution_fired = None
    scripted_solution_obj = None
    talk_npc_id: str | None = None
    crowd_talk = False

    # Set flags from recognized item
    if item_id:
        for item in find_items(item_id, location, state["flags"], state.get("inventory", [])):
            flags_to_set = item.get("examine_sets_flag", {})
            if not flags_to_set:
                continue
            set_on = item.get("set_on")
            # "otwórz szafę"/"zajrzyj do szafy" bywa klasyfikowane jako USE/OTHER, nie tylko EXAMINE —
            # traktuj to samo, żeby Grok zawsze dostał realną zawartość zamiast zmyślać
            if set_on == "examine" and intent not in ("EXAMINE", "USE", "OTHER"):
                continue
            new_flags = {k: v for k, v in flags_to_set.items() if state["flags"].get(k) != v}
            if new_flags:
                state["flags"].update(new_flags)
                print(f"[FLAGS] set from item_id='{item_id}' (intent={intent}): {new_flags}")

    _TAKE_ALL_KEYWORDS = ("wszystko", "wszystkie", "wszystkich", "resztę", "całość")
    if intent == "TAKE":
        # "wszystko co jest w X" — klasyfikator czasem zwraca item_id=kontener (np. "szafa"), ale
        # gracz chce ZAWARTOŚĆ, nie sam kontener. Fraza "weź wszystko" ma pierwszeństwo przed
        # dopasowaniem pojedynczego (nie do wzięcia) przedmiotu-kontenera.
        is_take_all_phrasing = any(kw in player_input.lower() for kw in _TAKE_ALL_KEYWORDS)
        item = None if is_take_all_phrasing else find_item(item_id, location, state["flags"], state.get("inventory", []))
        if item and item.get("takeable") and item["name"] not in state["inventory"]:
            state["inventory"].append(item["name"])
            intent_context = f"gracz właśnie podniósł '{item['name']}' — opisz jak go bierze, potwierdź że trzyma go w rękach"
        elif item and not item.get("takeable", True):
            intent_context = f"gracz próbuje wziąć '{item.get('hint', item_id)}' — to jest element otoczenia, nie można zabrać, wyjaśnij krótko dlaczego"
        elif item and item["name"] in state["inventory"]:
            intent_context = f"gracz próbuje wziąć '{item['name']}' — już to ma przy sobie"
        elif not item:
            takeable = [
                i for i in location.get("items", [])
                if i.get("takeable") and i["name"] not in state["inventory"]
                and _item_visible(i, state["flags"], state.get("inventory", []))
            ]
            lower_input = player_input.lower()
            if any(kw in lower_input for kw in _TAKE_ALL_KEYWORDS):
                matched = takeable
            else:
                # gracz mógł wymienić kilka konkretnych przedmiotów naraz (np. "biorę miecz i złoto") —
                # dopasuj po słowach z nazwy/hinta, żeby wziąć dokładnie to co wymienił, nie mniej nie więcej
                matched = [i for i in takeable if _item_mentioned(i, lower_input)]
            if matched:
                names = [i["name"] for i in matched]
                state["inventory"].extend(names)
                # deterministyczne potwierdzenie zamiast narracji Groka — przy "weź wszystko" Grok
                # potrafił opisać pustą szafę mimo poprawnie zaktualizowanego stanu (zob. feedback_zamek_deterministic_llm_state)
                if len(names) == 1:
                    confirm = f"Bierzesz {names[0]} i chowasz do ekwipunku."
                else:
                    confirm = f"Bierzesz wszystko: {', '.join(names)} — i chowasz do ekwipunku."
                return confirm, None, None, {}, True, None, False, intent
            elif not takeable:
                intent_context = "nie ma tu nic do zabrania (wszystko już wzięte lub nie ma przedmiotów)"

    elif intent in ("EXAMINE", "USE", "OTHER") and item_id:
        items = find_items(item_id, location, state["flags"], state.get("inventory", []))
        if items:
            def _readable_desc(item: dict) -> str:
                # unlock_requires (np. inventory: "księga run") — bez spełnienia warunku
                # EXAMINE pokazuje locked_description zamiast prawdziwego opisu
                if item.get("unlock_requires") and not _check_requires(item["unlock_requires"], state):
                    return item.get("locked_description", item["description"])
                return item["description"]
            descriptions = "\n".join(
                f"- {item['name']}: {_readable_desc(item).strip()}"
                for item in items if item.get("description")
            )
            if descriptions:
                intent_context = (
                    f"gracz ogląda '{items[0].get('hint', item_id)}' — podaj TYLKO opis wyglądu przedmiotu.\n"
                    f"ZAKAZ: nie pisz że gracz chwyta, podnosi, bierze, trzyma — przedmiot leży na miejscu.\n"
                    f"Użyj DOKŁADNIE tych opisów:\n{descriptions}"
                )
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

    # Trap bypass — tylko gdy w lokacji jest pułapka I gracz próbuje przejść
    _BYPASS_KEYWORDS = {"omijam", "omij", "przechodzę", "przejdź", "ominąć", "prześlizguję",
                        "przeskakuję", "ostrożnie", "wchodzę", "dalej", "naprzód"}
    _has_bypass_intent = intent == "MOVE" or any(kw in player_input.lower() for kw in _BYPASS_KEYWORDS)
    if intent not in ("TAKE", "EXAMINE") and not intent_context:
        trap_item = next(
            (i for i in location.get("items", [])
             if i.get("is_trap") and i.get("examine_sets_flag")),
            None
        )
        if trap_item and _has_bypass_intent:
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

    # TALK
    if intent == "TALK":
        location_npcs = location.get("npcs", [])
        crowd_target = bool(classified.get("crowd_target"))
        target_npc = None
        named_someone_absent = False
        if npc_id:
            target_npc = next((n for n in location_npcs if n["id"] == npc_id), None)
            named_someone_absent = target_npc is None
        # fallback na "pierwszego NPC w lokacji" ma sens TYLKO gdy gracz nie nazwał nikogo
        # konkretnego (npc_id puste) — jeśli nazwał kogoś po imieniu kogo tu nie ma (np. zwraca
        # się do centaura stojąc przy driadzie), NIE podstawiaj cicho innej postaci, bo to
        # wygląda jak losowa "teleportacja" rozmowy do kogoś zupełnie innego
        if target_npc is None and not crowd_target and not named_someone_absent:
            fallback_candidates = [n for n in location_npcs if not n.get("is_object")]
            if fallback_candidates:
                target_npc = fallback_candidates[0]
        if named_someone_absent and target_npc is None:
            return f"Nie ma tu nikogo takiego jak '{npc_id}'.", None, None, {}, True, None, False, intent

        if target_npc is None:
            if location.get("crowd"):
                crowd_talk = True
                hint = (location.get("crowd_reaction_hint") or "").strip()
                intent_context = (
                    "Gracz próbuje zagadnąć kogoś w tłumie/przechodniów — nie zwraca się do żadnego "
                    "konkretnego, ustalonego NPC z tej lokacji.\n"
                    "Opisz BARDZO KRÓTKO (1-2 zdania) obojętność lub brak reakcji, w tonie zgodnym z atmosferą "
                    "miejsca" + (f": {hint}" if hint else "") + ".\n"
                    "ZAKAZ: żadnej nazwanej postaci, żadnej informacji fabularnej, żadnej zmiany stanu gry — "
                    "to czysta atmosfera bez konsekwencji mechanicznych."
                )
            else:
                return "Tu nikogo nie ma — pytasz w pustkę, ale nikt nie odpowiada.", None, None, {}, True, None, False, intent
        else:
            talk_npc_id = target_npc["id"]
            npc_flag_key = f"{target_npc['id']}_state"
            current_npc_state = state["flags"].get(npc_flag_key, target_npc.get("state", ""))
            unavailable_msg = target_npc.get("unavailable_states", {}).get(current_npc_state)
            # deterministyczne exact_sequence (np. hint o Czarodziejce) musi działać NIEZALEŻNIE
            # od stanu NPC — inaczej NPC który już "skończył" swój wątek (np. wywabiony centaur)
            # blokuje pytania które celowo mają działać właśnie PO zakończeniu tego wątku
            if unavailable_msg and _match_deterministic_solution(player_input, target_npc, state) is None:
                return unavailable_msg, None, None, {}, True, talk_npc_id, False, intent

            # auto_reveal: niektóre scripted_solutions mają zawsze odpalić się przy pierwszej
            # rozmowie z tym NPC, bez polegania na dopasowaniu triggera przez classify_npc_outcome
            # (używane dla kluczowych fragmentów fabuły, które muszą wyjść niezawodnie).
            player_input_lower = player_input.lower()
            auto_reveal = next(
                (s for s in target_npc.get("scripted_solutions", [])
                 if s.get("auto_reveal")
                 and any(state["flags"].get(k) != v for k, v in s.get("flags", {}).items())
                 and any(kw in player_input_lower for kw in s.get("auto_reveal_keywords", _AUTO_REVEAL_KEYWORDS))),
                None
            )
            if auto_reveal:
                scripted_solution_fired = auto_reveal["flags"]
                scripted_solution_obj = auto_reveal
                state["flags"].update(auto_reveal["flags"])
                authoritative_flags.update(auto_reveal["flags"])
                intent_context = f"WYNIK AKCJI — użyj DOSŁOWNIE jako narrację:\n{auto_reveal['outcome'].strip()}"
            else:
                intent_context = (
                    f"Gracz zwraca się do: {target_npc['name']}.\n"
                    f"Odpowiedz WYŁĄCZNIE jako {target_npc['name']} — mów w pierwszej osobie, "
                    f"zgodnie z charakterem opisanym w sekcji NPC. Nie opisuj co NPC robi, mów jego głosem."
                )

    # NPC outcome classification
    # UWAGA: "or not intent_context" — jeśli TAKE/EXAMINE nic nie znalazło (np. gracz napisał
    # samą nazwę przedmiotu z ekwipunku jak "pochodnia" zamiast "użyj pochodni na X"), nie
    # zostawiamy Groka z pustym kontekstem przy aktywnym NPC — dajemy klasyfikacji NPC szansę
    # (i jej twardym blokom no_match_message) zamiast pozwolić Grokowi zmyślić całą scenę
    if not crowd_talk and (intent not in ("TAKE", "EXAMINE") or not item_id or not intent_context):
        for npc in location.get("npcs", []):
            # scripted_solution z pustymi flags (nic nie zmienia stanu — np. powtórzenie
            # historii) nigdy nie "kończy się" jak zwykłe rozwiązania, więc liczy się jako
            # aktywne dopóki jego requires są spełnione, niezależnie od reszty NPC
            has_active = any(
                s.get("flags") is not None and (
                    not s["flags"] and _check_requires(s.get("requires", {}), state)
                    or any(state["flags"].get(k) != v for k, v in s["flags"].items())
                )
                for s in npc.get("scripted_solutions", [])
            )
            if not has_active:
                continue
            # exact_sequence (hasła/sekwencje o jednej dosłownej odpowiedzi) sprawdzamy
            # deterministycznie PRZED wywołaniem LLM — zero kosztu, zero losowości modelu
            matched_solution = _match_deterministic_solution(player_input, npc, state)
            if matched_solution is None:
                try:
                    matched_solution = await classify_npc_outcome(player_input, npc, location, state, client, is_talk=(intent == "TALK"))
                except ClassificationUnavailable:
                    # API padło/przeciążone — to NIE jest potwierdzone "brak dopasowania", więc nie wolno
                    # tego przepuścić przez no_match_message (zwróciłoby fałszywy, stanowczy komunikat
                    # o porażce mimo że gracz mógł zrobić wszystko poprawnie)
                    return (
                        "Coś na chwilę przerwało kontakt ze światem gry — spróbuj powtórzyć swoją ostatnią akcję.",
                        None, None, {}, True, talk_npc_id, False, intent,
                    )
            if matched_solution is not None:
                scripted_solution_fired = matched_solution["flags"]
                scripted_solution_obj = matched_solution
                if matched_solution["flags"]:
                    state["flags"].update(matched_solution["flags"])
                    authoritative_flags.update(matched_solution["flags"])
                # wymuszamy kanoniczny outcome zamiast prosić Groka o "użyj dosłownie" —
                # ta sama klasa ryzyka co przy drzwiach (Grok czasem parafrazuje/zmyśla mimo
                # jasnej instrukcji), tylko tu chodzi o sukces zamiast porażki. Porażkę
                # (brak trafienia) — patrz gałąź no_match_message/no_match_messages poniżej.
                outcome_text = matched_solution["outcome"].strip()
                # reveals_riddle_hints — NPC (np. kobold) zdradza do max_riddle_hints odpowiedzi
                # na zagadki, których gracz JESZCZE nie rozwiązał (sprawdzane dynamicznie w
                # kodzie, nie zaszyte na sztywno w YAML — więc zawsze pasuje do realnego postępu
                # gracza, nawet jeśli część zagadek rozwiązał samodzielnie wcześniej)
                reveal_key = matched_solution.get("reveals_riddle_hints")
                if reveal_key:
                    hint_mechanic = world.get("mechanics", {}).get(reveal_key)
                    if hint_mechanic:
                        unsolved = [f for f in hint_mechanic["track_flags"] if not state["flags"].get(f)]
                        max_hints = matched_solution.get("max_riddle_hints", len(unsolved))
                        hints_data = hint_mechanic.get("hints", {})
                        words = [
                            hints_data[f]["odpowiedz"]
                            for f in unsolved[:max_hints] if f in hints_data
                        ]
                        if words:
                            outcome_text += "\n\n\"Znam słowa, które mogą ci się przydać: " + ", ".join(words) + ".\""
                        else:
                            fallback = matched_solution.get(
                                "no_hints_fallback",
                                "Ale widzę, że już wszystkie rozwiązałeś sam — nie mam ci nic do dodania.",
                            )
                            outcome_text += "\n\n\"" + fallback + "\""
                # list_story_progress (Ivan) — księga run "sama zbiera" historie mieszkańców
                # lasu niezależnie od kolejności zwiedzania; zamiast statycznej listy 4 imion
                # zawsze pokazujemy dynamicznie co gracz już zna, a czego jeszcze brakuje
                outcome_text = _append_story_progress(outcome_text, matched_solution.get("list_story_progress"), state, world)
                # progowe mechaniki (np. 6/6 zagadek dziada) sprawdzane od razu po każdym
                # dopasowaniu — gracz od razu widzi ile ma zaliczonych, a jeśli TA odpowiedź
                # akurat przekroczyła próg, dowiaduje się że może przejść, bez czekania na
                # kolejną turę. Deterministyczne — licznik liczony w kodzie, nie przez Groka.
                riddle_mechanic = world.get("mechanics", {}).get("dziad_zagadki")
                if riddle_mechanic and any(f in matched_solution["flags"] for f in riddle_mechanic["track_flags"]):
                    sets_flag = riddle_mechanic["sets_flag"]
                    already_passed = state["flags"].get(sets_flag)
                    apply_threshold_mechanic(state, world, "dziad_zagadki")
                    count = sum(1 for f in riddle_mechanic["track_flags"] if state["flags"].get(f))
                    threshold = riddle_mechanic.get("threshold", 1)
                    if not already_passed and state["flags"].get(sets_flag):
                        outcome_text += (
                            f"\n\nDziad kiwa głową z uznaniem. \"To już {count} z {threshold} potrzebnych. "
                            "Wystarczy. Możesz przejść. Ale wiedz — moje zagadki to nic. Kto nie zna "
                            "kolorów we właściwej kolejności, ten i tak nic dalej nie zobaczy.\" "
                            "Odsuwa się z drogi."
                        )
                    else:
                        outcome_text += f"\n\n(Rozwiązałeś już {count} z {threshold} potrzebnych zagadek.)"
                for item in matched_solution.get("inventory_remove", []):
                    state["inventory"] = [i for i in state["inventory"] if i != item]
                for item in matched_solution.get("inventory_add", []):
                    if item not in state["inventory"]:
                        state["inventory"].append(item)
                # move_to na scripted_solution nie działał przy wymuszonym, wczesnym zwrocie —
                # ścieżka early_return nigdy nie docierała do kodu w main.py który go stosuje,
                # więc np. wykopanie tunelu (podkop_start) ustawiało flagę ale nie przenosiło gracza
                if matched_solution.get("move_to") and matched_solution["move_to"] in world["locations"]:
                    state["current_location"] = matched_solution["move_to"]
                    new_loc = world["locations"][matched_solution["move_to"]]
                    new_desc = resolve_description(new_loc, state["flags"], state.get("inventory", []))
                    outcome_text = outcome_text + "\n\n" + new_desc.strip()
                return outcome_text, scripted_solution_fired, scripted_solution_obj, authoritative_flags, True, talk_npc_id, False, intent
            elif npc.get("no_match_message") or npc.get("no_match_messages"):
                # deterministyczny twardy blok — nie ufamy Grokowi że poprawnie opisze "nadal
                # zablokowane"; potwierdzone w testach (niedźwiedź), że narracyjnie halucynuje sukces
                # mimo jasnej instrukcji "nie opisuj że NPC ustąpił". no_match_messages (lista) daje
                # pulę wariantów do losowania, żeby wiele nieudanych prób z rzędu nie brzmiało identycznie.
                pool = npc.get("no_match_messages") or [npc["no_match_message"]]
                return random.choice(pool), None, None, {}, True, talk_npc_id, False, intent
            break

    return intent_context, scripted_solution_fired, scripted_solution_obj, authoritative_flags, False, talk_npc_id, crowd_talk, intent


DIRECTION_ALIASES = {
    "północ": "północ", "polnoc": "północ", "n": "północ", "north": "północ",
    "południe": "południe", "poludnie": "południe", "s": "południe", "south": "południe",
    "wschód": "wschód", "wschod": "wschód", "e": "wschód", "east": "wschód",
    "podejdź bliżej": "podejdź bliżej", "podejdz blizej": "podejdź bliżej",
    "zachód": "zachód", "zachod": "zachód", "w": "zachód", "west": "zachód",
    "wejście": "wejście", "wyjście": "wyjście",
    "wieża": "wieża",
    "góra": "góra", "gora": "góra", "up": "góra",
    "dół": "dół", "dol": "dół", "down": "dół",
    "przód": "przód", "przod": "przód", "dalej": "przód", "naprzód": "przód", "naprzod": "przód", "forward": "przód",
}

REVERSE_DIRECTION = {"północ": "południe", "południe": "północ", "wschód": "zachód", "zachód": "wschód"}


async def _detect_residual_move(player_input: str, handled: list[str], location: dict, client) -> str | None:
    """Split_compound tnie polecenia złożone do max 2 akcji — trzeci czasownik (np. "...i wychodzę
    z pokoju") może zniknąć z formalnej listy. Zwykle to nieszkodliwe, bo Grok i tak widzi pełny
    player_input przy renarracji i sam emituje MOVE:. Ale gdy OBIE sformalizowane akcje są już
    wymuszone (early_return), Grok w ogóle nie jest wywoływany — więc tu, tylko dla tego przypadku,
    osobno sprawdzamy czy w pełnym tekście jest dodatkowa, nieobsłużona intencja ruchu."""
    exits_list = list(location.get("exits", {}).keys())
    if not exits_list:
        return None
    handled_text = " / ".join(h for h in handled if h)
    prompt = f"""Gracz napisał: "{player_input}"
Już obsłużone fragmenty tej wypowiedzi: {handled_text}
Dostępne wyjścia w tej lokacji: {", ".join(exits_list)}

Czy OPRÓCZ już obsłużonych fragmentów gracz WYRAŹNIE próbuje też się przemieścić/wyjść/wejść gdzieś
(dodatkowy, nieobsłużony czasownik ruchu, pominięty przy rozbiciu zdania)?
Jeśli tak — odpowiedz TYLKO dokładną nazwą wyjścia z listy dostępnych wyjść, bez niczego więcej.
Jeśli nie — odpowiedz TYLKO słowem: NIC

Odpowiedź:"""
    try:
        resp = client.chat.completions.create(
            model=GROK_MODEL,
            messages=[{"role": "user", "content": prompt}],
            max_tokens=15,
            temperature=0,
        )
        raw = resp.choices[0].message.content.strip()
        return raw if raw in exits_list else None
    except Exception as e:
        log.info(f"[RESIDUAL MOVE ERROR] {e}")
        return None


def _try_move(player_input: str, state: dict, world: dict):
    """Zwraca (narrative, updated_state) jeśli input to kierunek ruchu, else None."""
    direction = DIRECTION_ALIASES.get(player_input.strip().lower())
    if not direction:
        return None

    location = world["locations"].get(state["current_location"], {})

    # move_sequence — "ślepa" sekwencja ruchów (np. burzowe piaski): gracz musi wykonać
    # dokładnie N kroków w ustalonej kolejności, ale wynik (trafił/nie trafił) poznaje
    # dopiero PO ostatnim kroku, nigdy wcześniej — inaczej dałoby się to rozwiązać metodą
    # prób i błędów krok po kroku (sprawdź kierunek, cofnij się jeśli źle) zamiast zgadywać
    # całą sekwencję na raz, co drastycznie zmniejszałoby trudność łamigłówki.
    move_seq = location.get("move_sequence")
    solved_flag = move_seq.get("solved_flag") if move_seq else None
    already_solved = bool(solved_flag and state["flags"].get(solved_flag))
    if move_seq and not already_solved and direction in ("północ", "południe", "wschód", "zachód"):
        required = move_seq["required"]
        progress = state.get("move_sequence_progress", []) + [direction]
        state["turn"] += 1
        if len(progress) < len(required):
            state["move_sequence_progress"] = progress
            narrative = move_seq["neutral_text"].strip()
        else:
            state["move_sequence_progress"] = []
            if progress == required:
                target = move_seq["success_target"]
                if solved_flag:
                    state["flags"][solved_flag] = True
                state["flags"][f"wejscie_{target}"] = REVERSE_DIRECTION.get(direction, direction)
                state["current_location"] = target
                narrative = resolve_description(world["locations"][target], state["flags"], state.get("inventory", [])).strip()
            else:
                target = move_seq["fail_target"]
                state["flags"][f"wejscie_{target}"] = REVERSE_DIRECTION.get(direction, direction)
                state["current_location"] = target
                fail_desc = resolve_description(world["locations"][target], state["flags"], state.get("inventory", [])).strip()
                narrative = move_seq["fail_narrative"].strip() + "\n\n" + fail_desc
        state["history"].append({"turn": state["turn"], "gm": narrative})
        save_state(state)
        return narrative, state

    exits_raw = location.get("exits", {})
    # Alias per-lokacyjny: opis lokacji może fabularnie nazywać wyjście inaczej niż klucz w YAML
    # (np. "drzwi na południe" w opisie, ale exit nazywa się "wyjście") — Grok czasem użyje tej
    # fabularnej nazwy w MOVE:, więc trzeba ją zmapować z powrotem na prawdziwy klucz.
    if direction not in exits_raw:
        for real_direction, exit_def in exits_raw.items():
            if isinstance(exit_def, dict) and direction in exit_def.get("aliases", []):
                direction = real_direction
                break

    entry_direction = state["flags"].get(f"wejscie_{state['current_location']}")
    accessible, blocked_msgs = resolve_exits(location, state["flags"], state.get("inventory", []), entry_direction)

    if direction in accessible:
        target = accessible[direction]
        if target not in world["locations"]:
            return None
        if direction in REVERSE_DIRECTION:
            state["flags"][f"wejscie_{target}"] = REVERSE_DIRECTION[direction]
        state["current_location"] = target
        state["transitions"] = state.get("transitions", 0) + 1
        new_loc = world["locations"][target]
        description = resolve_description(new_loc, state["flags"], state.get("inventory", []))
        state["turn"] += 1
        state["history"].append({"turn": state["turn"], "gm": description.strip()})
        apply_city_arrest_mechanic(state, world)
        event = check_world_events(state, world)
        if event:
            if event.get("target_location"):
                state["current_location"] = event["target_location"]
            if event.get("inventory_clear"):
                state["inventory"] = []
            for item in event.get("inventory_remove", []):
                state["inventory"] = [i for i in state["inventory"] if i != item]
            for item in event.get("inventory_add", []):
                if item not in state["inventory"]:
                    state["inventory"].append(item)
            if event.get("flags"):
                state["flags"].update(event["flags"])
            state["turn"] += 1
            event_narrative = _append_story_progress(event["narrative"].strip(), event.get("list_story_progress"), state, world)
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
            full_msg = msg + "\n\n" + resolve_description(new_loc, state["flags"], state.get("inventory", []))
            state["turn"] += 1
            state["history"].append({"turn": state["turn"], "gm": full_msg})
            save_state(state)
            return full_msg, state
        msg = blocked_msgs.get(direction, f"Nie możesz iść na {direction}.")
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

    # 3. Wykryj złożone polecenie (max 2 akcje)
    actions = await split_compound(player_input, client)
    log.info(f"[COMPOUND] {len(actions)} akcji: {actions}")

    if len(actions) == 2:
        # Złożone polecenie: uruchom pipeline dla każdej akcji sekwencyjnie, jeden narrator na końcu
        ctx1, ss_fired1, ss_obj1, auth1, early1, npc_id1, crowd1, intent1 = await _execute_intent_step(actions[0], state, world, location, client)
        ctx2, ss_fired2, ss_obj2, auth2, early2, npc_id2, crowd2, intent2 = await _execute_intent_step(actions[1], state, world, location, client)

        # early=True oznacza gotowy, wymuszony tekst (blokada, kanoniczny outcome, błąd API) —
        # taki tekst NIE trafia do Groka do renarracji, tylko jest doklejany verbatim na końcu.
        # Skrót (pomiń Groka całkowicie) ma sens TYLKO gdy OBIE akcje są wymuszone — jeśli któraś
        # nie jest (np. MOVE, które nie generuje żadnego ctx, ale wciąż wymaga żeby Grok zobaczył
        # pełny player_input i sam zdecydował o "MOVE: kierunek"), Grok i tak musi się odezwać,
        # nawet z pustą instrukcją dla tej akcji — inaczej ruch/opis po prostu ginie w ciszy.
        forced_texts = [c for c, e in ((ctx1, early1), (ctx2, early2)) if e and c]
        pending = [c for c, e in ((ctx1, early1), (ctx2, early2)) if not e and c]

        if early1 and early2:
            # obie sformalizowane akcje są wymuszone, więc Groka normalnie byśmy tu nie pytali —
            # ale split_compound tnie do max 2 akcji, więc sprawdźmy czy w pełnym tekście nie
            # zgubił się jeszcze trzeci, nieobsłużony czasownik ruchu (np. "...i wychodzę z pokoju")
            residual_direction = await _detect_residual_move(player_input, [actions[0], actions[1]], location, client)
            if residual_direction:
                move_result = _try_move(residual_direction, state, world)
                if move_result:
                    narrative, updated_state = move_result
                    narrative = narrative + "\n\n" + "\n\n".join(forced_texts)
                    updated_state["history"][-1]["gm"] = narrative
                    if NPC_DIALOGUE_MEMORY_ENABLED:
                        if npc_id1:
                            record_npc_dialogue(updated_state, npc_id1, actions[0], narrative)
                        if npc_id2:
                            record_npc_dialogue(updated_state, npc_id2, actions[1], narrative)
                    save_state(updated_state)
                    return {"narrative": narrative, "state": enrich_state(updated_state, world)}

            narrative = "\n\n".join(forced_texts)
            state["turn"] += 1
            state["history"].append({"turn": state["turn"], "gm": narrative})
            if NPC_DIALOGUE_MEMORY_ENABLED:
                if npc_id1:
                    record_npc_dialogue(state, npc_id1, actions[0], narrative)
                if npc_id2:
                    record_npc_dialogue(state, npc_id2, actions[1], narrative)
            save_state(state)
            return {"narrative": narrative, "state": enrich_state(state, world)}

        combined_context = "\n\n".join(f"[Akcja {i+1}] {c}" for i, c in enumerate(pending)) if len(pending) > 1 else (pending[0] if pending else "")

        # tylko PENDING akcje wpływają na scripted_fired przekazywane do Groka — wymuszona
        # (early) akcja ma już swój kanoniczny tekst doklejony osobno (forced_texts), więc
        # nie może "przeciekać" jako scripted_fired=True dla drugiej, luźnej narracji —
        # inaczej Grok widzi w promptcie kanoniczny outcome INNEGO triggera jako "użyj dosłownie"
        # i sam go powtarza w swojej części odpowiedzi (duplikat tekstu)
        any_scripted = (bool(ss_fired1) and not early1) or (bool(ss_fired2) and not early2)
        any_crowd_talk = crowd1 or crowd2
        flags_after_steps = state["flags"].copy()
        inventory_after_steps = list(state["inventory"])
        location_after_steps = state["current_location"]

        prompt = build_gm_prompt(player_input, state, world, intent_context=combined_context, scripted_fired=any_scripted)
        response = client.chat.completions.create(
            model=GROK_MODEL,
            messages=[{"role": "user", "content": prompt}],
            max_tokens=600,
            temperature=0.8,
        )
        gm_response = response.choices[0].message.content.strip()
        log.info(f"[GM RAW] {gm_response[:120]}")

        # Grok zdecydował że to ruch — ta ścieżka (w przeciwieństwie do pojedynczych poleceń) nie
        # miała w ogóle obsługi sentinela MOVE:, więc leciał surowy tekst "MOVE: X" do gracza
        if gm_response.startswith("MOVE:"):
            direction = gm_response.split(":", 1)[1].strip()
            move_result = _try_move(direction, state, world)
            if move_result:
                narrative, updated_state = move_result
                if forced_texts:
                    narrative = narrative + "\n\n" + "\n\n".join(forced_texts)
                    updated_state["history"][-1]["gm"] = narrative
                if NPC_DIALOGUE_MEMORY_ENABLED:
                    if npc_id1:
                        record_npc_dialogue(updated_state, npc_id1, actions[0], narrative)
                    if npc_id2:
                        record_npc_dialogue(updated_state, npc_id2, actions[1], narrative)
                save_state(updated_state)
                return {"narrative": narrative, "state": enrich_state(updated_state, world)}

        narrative, updated_state = _parse_gm_response(gm_response, state, world, player_input, allow_inventory_add=(intent1 == "TAKE" or intent2 == "TAKE"))
        if forced_texts:
            narrative = narrative + "\n\n" + "\n\n".join(forced_texts)
        if any_crowd_talk:
            # rozmowa z tłumem — bez wyjątków, żadna zmiana stanu z JSON-a Groka nie może się utrzymać
            updated_state["flags"] = flags_after_steps
            updated_state["inventory"] = inventory_after_steps
            updated_state["current_location"] = location_after_steps
        elif any_scripted:
            updated_state["flags"] = flags_after_steps
        elif auth1 or auth2:
            updated_state["flags"].update({**auth1, **auth2})
        apply_city_arrest_mechanic(updated_state, world)
        for ss_obj in [ss_obj1, ss_obj2]:
            if ss_obj:
                for item in ss_obj.get("inventory_remove", []):
                    updated_state["inventory"] = [i for i in updated_state["inventory"] if i != item]
                for item in ss_obj.get("inventory_add", []):
                    if item not in updated_state["inventory"]:
                        updated_state["inventory"].append(item)
                if ss_obj.get("move_to") and ss_obj["move_to"] in world["locations"]:
                    updated_state["current_location"] = ss_obj["move_to"]
                    new_loc = world["locations"][ss_obj["move_to"]]
                    new_desc = resolve_description(new_loc, updated_state["flags"], updated_state.get("inventory", []))
                    narrative = narrative + "\n\n" + new_desc.strip()
        if NPC_DIALOGUE_MEMORY_ENABLED:
            if npc_id1:
                record_npc_dialogue(updated_state, npc_id1, actions[0], narrative)
            if npc_id2:
                record_npc_dialogue(updated_state, npc_id2, actions[1], narrative)
        save_state(updated_state)
        return {"narrative": narrative, "state": enrich_state(updated_state, world)}

    # 4. Pojedyncze polecenie — classify + dispatch + narrator
    intent_context, scripted_solution_fired, scripted_solution_obj, authoritative_flags, early_return, talk_npc_id, crowd_talk, intent = \
        await _execute_intent_step(actions[0], state, world, location, client)

    if early_return:
        state["turn"] += 1
        state["history"].append({"turn": state["turn"], "gm": intent_context})
        if talk_npc_id and NPC_DIALOGUE_MEMORY_ENABLED:
            record_npc_dialogue(state, talk_npc_id, actions[0], intent_context)
        save_state(state)
        return {"narrative": intent_context, "state": enrich_state(state, world)}

    # 5. Grok jako narrator z kontekstem intencji
    flags_before_narrator = state["flags"].copy()
    inventory_before_narrator = list(state["inventory"])
    location_before_narrator = state["current_location"]
    prompt = build_gm_prompt(player_input, state, world, intent_context=intent_context, scripted_fired=bool(scripted_solution_fired))
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

    narrative, updated_state = _parse_gm_response(gm_response, state, world, player_input, allow_inventory_add=(intent == "TAKE"))
    if crowd_talk:
        # rozmowa z tłumem — bez wyjątków, żadna zmiana stanu z JSON-a Groka nie może się utrzymać
        updated_state["flags"] = flags_before_narrator
        updated_state["inventory"] = inventory_before_narrator
        updated_state["current_location"] = location_before_narrator
    elif scripted_solution_fired:
        updated_state["flags"] = {**flags_before_narrator, **scripted_solution_fired}
        if scripted_solution_obj:
            for item in scripted_solution_obj.get("inventory_remove", []):
                updated_state["inventory"] = [i for i in updated_state["inventory"] if i != item]
            for item in scripted_solution_obj.get("inventory_add", []):
                if item not in updated_state["inventory"]:
                    updated_state["inventory"].append(item)
    elif authoritative_flags:
        updated_state["flags"].update(authoritative_flags)
    apply_city_arrest_mechanic(updated_state, world)
    if scripted_solution_obj and scripted_solution_obj.get("move_to"):
        target = scripted_solution_obj["move_to"]
        if target in world["locations"]:
            updated_state["current_location"] = target
            new_loc = world["locations"][target]
            new_desc = resolve_description(new_loc, updated_state["flags"], updated_state.get("inventory", []))
            narrative = narrative + "\n\n" + new_desc.strip()
    if talk_npc_id and NPC_DIALOGUE_MEMORY_ENABLED:
        record_npc_dialogue(updated_state, talk_npc_id, actions[0], narrative)
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


def _parse_gm_response(raw: str, state: dict, world: dict, player_input: str = "", allow_inventory_add: bool = True) -> tuple[str, dict]:
    json_match = re.search(r"```json\s*(.*?)\s*```", raw, re.DOTALL)
    # Usuń blok JSON z narracji — z zamknięciem lub bez
    narrative = re.sub(r"```json.*?```", "", raw, flags=re.DOTALL)
    narrative = re.sub(r"```json.*", "", narrative, flags=re.DOTALL)
    narrative = re.sub(r"\{[^{}]*\"new_location\"[^{}]*\}", "", narrative)
    narrative = narrative.strip()

    if json_match:
        try:
            updates = json.loads(json_match.group(1))
            current_loc = world["locations"].get(state["current_location"], {})
            new_loc = updates.get("new_location")
            if new_loc:
                accessible, _ = resolve_exits(current_loc, state["flags"], state.get("inventory", []), state["flags"].get(f"wejscie_{state['current_location']}"))
                if new_loc in accessible.values():
                    state["current_location"] = new_loc
                else:
                    log.info(f"[GM new_location ODRZUCONE] '{new_loc}' nie jest dostępnym wyjściem z '{state['current_location']}' — ignoruję")
            # inventory_add z JSON-a Groka — WALIDOWANE względem realnie istniejących, widocznych
            # przedmiotów w tej lokacji. Pozwala rozpoznać synonimy/kontekst ("pieniądze" -> "mieszek
            # ze złotem"), ale odrzuca wymyślone nazwy (wcześniej Grok potrafił rozbić "miecz i zbroja"
            # z powrotem na osobne "miecz"/"tarcza"/"kolczuga" — takie nazwy po prostu nie istnieją,
            # więc teraz nie przejdą walidacji).
            # DODATKOWO: stosuje się TYLKO gdy allow_inventory_add=True (intencja gracza to faktycznie
            # TAKE) — bez tego Grok potrafił po cichu wrzucić przedmiot do ekwipunku przy zwykłym
            # EXAMINE/OTHER, bo walidacja sprawdzała tylko "czy przedmiot istnieje", nie "czy gracz
            # w ogóle próbował go wziąć".
            if not allow_inventory_add and updates.get("inventory_add"):
                log.info(f"[GM inventory_add ODRZUCONE] intencja != TAKE, ignoruję {updates.get('inventory_add')}")
            else:
                valid_item_names = {
                    i["name"] for i in current_loc.get("items", [])
                    if i.get("takeable") and _item_visible(i, state["flags"], state.get("inventory", []))
                }
                for item in updates.get("inventory_add", []):
                    if item in valid_item_names and item not in state["inventory"]:
                        state["inventory"].append(item)
                    else:
                        log.info(f"[GM inventory_add ODRZUCONE] '{item}' nie jest prawdziwym/dostępnym przedmiotem tutaj — ignoruję")
            # inventory_remove nadal ignorowane — utrata przedmiotu to poważniejsza konsekwencja,
            # dziś nic w grze tego nie potrzebuje (usuwanie idzie przez scripted_solutions, osobno).
            if updates.get("inventory_remove"):
                log.info(f"[GM inventory_remove ODRZUCONE] {updates.get('inventory_remove')} — ignoruję")
            # flags_update z JSON-a Groka celowo IGNOROWANE — jak przy ekwipunku, żadna flaga w grze
            # nie zależy dziś od tego pola (wszystkie idą przez scripted_solutions, examine_sets_flag
            # albo mechanizmy kodowe typu apply_city_arrest_mechanic). Ufanie Grokowi tutaj niosło
            # to samo ryzyko co przy ekwipunku — mógłby np. samodzielnie "wygrać" walkę bez scripted_solution.
            if updates.get("flags_update"):
                log.info(f"[GM flags_update ODRZUCONE] {updates.get('flags_update')} — ignoruję")
        except json.JSONDecodeError:
            pass

    state["turn"] += 1
    state["history"].append({"turn": state["turn"], "gm": narrative})
    return narrative, state
