from __future__ import annotations
import json
import yaml
from pathlib import Path

WORLD_FILE = Path(__file__).parent.parent / "world.yaml"
STATE_FILE = Path(__file__).parent.parent / "game_state.json"


def load_world() -> dict:
    return yaml.safe_load(WORLD_FILE.read_text(encoding="utf-8"))


def load_state() -> dict:
    return json.loads(STATE_FILE.read_text(encoding="utf-8"))


def save_state(state: dict) -> None:
    STATE_FILE.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")


def reset_state(world: dict | None = None) -> dict:
    if world is None:
        world = load_world()
    start = world.get("start_location", "las")
    initial_flags = {"troll_state": "blokuje_most", "straznik_state": "blokuje_przejscie", "niedzwiedz_state": "blokuje_sciezke", "pora_dnia": "dzień"}
    state = {
        "current_location": start,
        "inventory": [],
        "flags": initial_flags,
        "history": [],
        "turn": 0,
    }
    save_state(state)
    return state


def check_world_events(state: dict, world: dict) -> dict | None:
    """
    Sprawdza czy jakiś event z sekcji events w world.yaml powinien się odpalić.
    Zwraca event dict lub None.
    """
    for event in world.get("events", []):
        cond = event.get("condition")
        if not check_condition(cond, state["flags"]):
            continue
        trigger_locs = event.get("trigger_locations", [])
        if state["current_location"] not in trigger_locs:
            continue
        return event
    return None


def apply_city_arrest_mechanic(state: dict, world: dict) -> None:
    """
    Sprawdza czy próg zebranych informacji w mieście został osiągnięty.
    Jeśli tak — ustawia flagę siepaczy.
    """
    mechanic = world.get("mechanics", {}).get("city_arrest")
    if not mechanic:
        return
    if state["flags"].get(mechanic["sets_flag"]):
        return  # już ustawione
    count = sum(1 for f in mechanic["track_flags"] if state["flags"].get(f))
    if count >= mechanic["threshold"]:
        state["flags"][mechanic["sets_flag"]] = True


def check_condition(req: dict, flags: dict) -> bool:
    """Sprawdza czy warunek przejścia jest spełniony."""
    if not req:
        return True
    flag_val = flags.get(req["flag"])
    if "values" in req:
        return flag_val in req["values"]
    return flag_val == req["value"]


def resolve_description(location: dict, flags: dict) -> str:
    """Zwraca opis lokacji pasujący do aktualnych flag (pierwszy pasujący wariant)."""
    for variant in location.get("description_variants", []):
        cond = variant.get("condition")
        if cond is None or check_condition(cond, flags):
            return variant["description"]
    # Fallback: stare pole description (kompatybilność wsteczna)
    return location.get("description", "")


def resolve_exits(location: dict, flags: dict) -> tuple[dict, list[str]]:
    """
    Zwraca (dostępne_wyjścia, lista_komunikatów_blokad).
    Wyjścia hidden=true są niewidoczne dopóki nie są odblokowane.
    """
    accessible = {}
    blocked_msgs = []
    for direction, exit_def in location.get("exits", {}).items():
        if exit_def is None:
            continue
        if isinstance(exit_def, dict):
            req = exit_def.get("requires")
            if req and not check_condition(req, flags):
                if not exit_def.get("hidden"):
                    blocked_msgs.append(exit_def.get("blocked_message", f"Kierunek {direction} jest zablokowany."))
                continue
            accessible[direction] = exit_def["target"]
        else:
            accessible[direction] = exit_def
    return accessible, blocked_msgs


def _item_visible(item: dict, flags: dict, inventory: list | None = None) -> bool:
    """Zwraca False jeśli item ma hidden_when i warunek jest spełniony, lub jeśli item jest już w ekwipunku."""
    if inventory and item.get("takeable") and item.get("name") in inventory:
        return False
    hw = item.get("hidden_when")
    if not hw:
        return True
    return not check_condition(hw, flags)


def find_items(target: str, location: dict, flags: dict | None = None, inventory: list | None = None) -> list[dict]:
    """Zwraca itemy pasujące do target — najpierw exact match po id, potem fuzzy name/hint."""
    if not target:
        return []
    t = target.lower()
    visible = [i for i in location.get("items", []) if _item_visible(i, flags or {}, inventory)]
    # Exact match po id (klasyfikator zwraca item_id)
    by_id = [item for item in visible if item.get("id", "").lower() == t]
    if by_id:
        return by_id
    # Fuzzy match po name i hint
    return [
        item for item in visible
        if t in item["name"].lower()
        or t in item.get("hint", "").lower()
        or item["name"].lower() in t
        or item.get("hint", "").lower() in t
    ]


def find_item(target: str, location: dict, flags: dict | None = None, inventory: list | None = None) -> dict | None:
    """Zwraca pierwszy pasujący item (dla TAKE)."""
    results = find_items(target, location, flags, inventory)
    return results[0] if results else None


def build_gm_prompt(player_input: str, state: dict, world: dict, intent_context: str = "", scripted_fired: bool = False) -> str:
    loc_id = state["current_location"]
    location = world["locations"][loc_id]
    inventory = state["inventory"] if state["inventory"] else ["(brak)"]
    flags = state["flags"]

    exits, blocked_msgs = resolve_exits(location, flags)
    exits_list = ", ".join(f"{k} → {v}" for k, v in exits.items()) or "(brak wyjść)"
    blocked_info = "\n".join(f"  ⚠ {m}" for m in blocked_msgs)

    description = resolve_description(location, flags)

    # Zbierz NPC i ich ukryte rozwiązania dla GM-a
    npc_context = ""
    for npc in location.get("npcs", []):
        npc_current_state = flags.get(f"{npc['id']}_state", npc.get("state", ""))
        if scripted_fired:
            scripted = "\n".join(
                f"  - TRIGGER: {s['trigger']}\n    OUTCOME (użyj DOSŁOWNIE jeśli trigger pasuje, nie parafrazuj): {s['outcome'].strip()}\n    [flags_update: {s.get('flags', {})}]"
                for s in npc.get("scripted_solutions", [])
            )
        else:
            scripted = ("(WAŻNE: system gry sprawdził akcję gracza — żaden trigger nie pasował. "
                        "NPC pozostaje w aktualnym stanie i NADAL BLOKUJE DROGĘ. "
                        "NIE opisuj że gracz przeszedł, przeskoczył, wymknął się lub ominął NPC. "
                        "NIE opisuj że NPC ustąpił, cofnął się lub zniknął. "
                        "Opisz nieudaną próbę i że droga jest nadal zablokowana.)")
        creative = npc.get("creative_solutions_hint", "")
        npc_context += f"""
NPC: {npc['name']} (AKTUALNY STAN: {npc_current_state})
Opis dla GM: {npc['description']}
Kanoniczne rozwiązania:
{scripted}
Wskazówka dla kreatywnych rozwiązań:
{creative}
"""

    hint_groups: dict[str, list[str]] = {}
    mechanic_lines = []
    for i in [x for x in location.get("items", []) if _item_visible(x, flags, state.get("inventory", []))]:
        hint = i.get("hint", i["name"])
        hint_groups.setdefault(hint, []).append(i["name"])
        for flag, expected in i.get("examine_sets_flag", {}).items():
            if flags.get(flag) != expected:
                mechanic_lines.append(
                    f"  ✗ '{hint}' — gracz NIE zbadał, nie wie co to jest (nie narruj efektów zbadania ani przeskoczenia)"
                )
            else:
                mechanic_lines.append(f"  ✓ '{hint}' — gracz już to zbadał, wie co to jest")
    items_here = ", ".join(
        f"{hint} (po zbadaniu: {', '.join(names)})" for hint, names in hint_groups.items()
    ) or "(brak)"
    mechanic_context = "\n".join(mechanic_lines)

    return f"""Jesteś Mistrzem Gry w tekstowej grze przygodowej "Zamek Złej Królowej".
Rozmawiasz z graczem głosowo — odpowiadaj żywo, obrazowo, w drugiej osobie liczby pojedynczej.
Odpowiedzi max 3-4 zdania (to głos, nie tekst).

=== AKTUALNA LOKACJA GRACZA ===
Gracz jest TERAZ w: {location['name']} (id: {loc_id})
Opis: {description.strip()}
Atmosfera: {location.get('atmosphere', '')}
Dostępne wyjścia: {exits_list}
{f"Zablokowane kierunki:{chr(10)}{blocked_info}" if blocked_msgs else ""}
Przedmioty TUTAJ: {items_here}
{f"Stan zbadania przedmiotów:{chr(10)}{mechanic_context}" if mechanic_context else ""}
Ekwipunek gracza: {", ".join(inventory)}
Pora dnia: {flags.get('pora_dnia', 'dzień')}

{npc_context}

=== BOHATER (ABSOLUTNE OGRANICZENIA) ===
Gracz wciela się w wojownika w skórzanej zbroi. Dozwolone: walka fizyczna, wyważanie, wspinanie, ukrywanie się, przekupstwo, blef, zastraszanie, negocjacje.
ZABRONIONE — jeśli gracz próbuje poniższego, odpowiedz TYLKO: "Nie jesteś czarownikiem." i nic więcej:
- czary, zaklęcia, magia wszelkiego rodzaju
- przywoływanie przedmiotów, złota, broni z niczego
- telekineza, latanie, niewidzialność, nadludzkie zdolności
- wskrzeszanie, przywoływanie duchów lub demonów

=== NIEPRECYZYJNE KOMENDY ===
- "atakuj/uderz/walcz" bez wskazania czym → bohater atakuje gołymi rękami. Opisz realistycznie — gołe pięści przeciwko uzbrojonemu/silnemu wrogowi są nieskuteczne i ryzykowne.
- "podnieś/weź/bierz" gdy w lokacji jest więcej niż jeden przedmiot → zapytaj gracza w narracji co dokładnie chce zabrać.
- "poczekaj/czekaj" bez kontekstu wobec konkretnego NPC lub sytuacji → opisz że bohater stoi i czeka, nic szczególnego się nie dzieje.

=== KRYTYCZNE ZASADY ===
1. NIGDY nie wymyślaj przedmiotów, postaci ani miejsc których nie ma w opisie lokacji.
2. Gracz może iść TYLKO w kierunkach z listy "Dostępne wyjścia". Zablokowane kierunki — opisz blokadę.
3. NPC istnieje TYLKO we własnej lokacji.
4. Jeśli gracz próbuje czegoś niemożliwego — powiedz to krótko i zapytaj co chce zrobić.
5. Kanoniczne rozwiązania: gdy akcja gracza pasuje do TRIGGER — użyj tekstu z OUTCOME DOSŁOWNIE, słowo w słowo. To jedyny sposób żeby gracz dostał kluczowe informacje fabularne.
6. Kreatywne rozwiązania: jeśli mają logiczny sens w tym świecie — pozwól zadziałać.
7. Gdy gracz wchodzi do nowej lokacji — ZAWSZE opisz ją zgodnie z polem "Opis" powyżej. Nie pomijaj kluczowych elementów sceny (NPC, przedmioty, atmosfera).

=== RUCH MIĘDZY LOKACJAMI (KRYTYCZNE) ===
Jeśli akcja gracza to próba przemieszczenia się (idę, wchodzę, wychodzę, podejdź, przejdź itp.) — odpowiedz WYŁĄCZNIE jedną linią w formacie:
MOVE: <kierunek>
gdzie <kierunek> to jedno z: północ, południe, wschód, zachód, wejście, wyjście
Przykłady: "idę na północ" → MOVE: północ | "wchodzę do środka" → MOVE: wejście | "wracam" → MOVE: południe
Jeśli kierunek jest zablokowany — NIE pisz MOVE, opisz blokadę normalnie.
Jeśli gracz robi COKOLWIEK innego niż ruch — NIE pisz MOVE, odpowiedz normalnie z JSON.

=== FORMAT ODPOWIEDZI DLA AKCJI (nie-ruch) ===
Narracja (3-4 zdania), potem ZAWSZE blok JSON:
```json
{{"new_location": null, "inventory_add": [], "inventory_remove": [], "flags_update": {{}}}}
```

=== AKCJA GRACZA ===
{f"[INTENCJA GRACZA (użyj tego kontekstu): {intent_context}]" + chr(10) if intent_context else ""}{player_input}"""
