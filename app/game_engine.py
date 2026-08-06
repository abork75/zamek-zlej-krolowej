from __future__ import annotations
import json
import yaml
from pathlib import Path

from app.config import get_game_dir, NPC_DIALOGUE_MEMORY_ENABLED, NPC_DIALOGUE_MEMORY_TURNS

STATE_FILE = Path(__file__).parent.parent / "game_state.json"


def load_world() -> dict:
    world_file = get_game_dir() / "world.yaml"
    return yaml.safe_load(world_file.read_text(encoding="utf-8"))


def load_state() -> dict:
    if not STATE_FILE.exists():
        return reset_state()
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
        "npc_dialogue": {},
        "turn": 0,
        "transitions": 0,
    }
    save_state(state)
    return state


def record_npc_dialogue(state: dict, npc_id: str, player_line: str, npc_line: str) -> None:
    """Zapisuje wymianę zdań z NPC, przycinając do ostatnich NPC_DIALOGUE_MEMORY_TURNS."""
    dialogue = state.setdefault("npc_dialogue", {})
    history = dialogue.setdefault(npc_id, [])
    history.append({"player": player_line.strip(), "npc": npc_line.strip()})
    dialogue[npc_id] = history[-NPC_DIALOGUE_MEMORY_TURNS:] if NPC_DIALOGUE_MEMORY_TURNS > 0 else []


def check_world_events(state: dict, world: dict) -> dict | None:
    """
    Sprawdza czy jakiś event z sekcji events w world.yaml powinien się odpalić.
    Zwraca event dict lub None.
    """
    for event in world.get("events", []):
        cond = event.get("condition")
        if not check_condition(cond, state["flags"], state.get("inventory")):
            continue
        trigger_locs = event.get("trigger_locations", [])
        if state["current_location"] not in trigger_locs:
            continue
        return event
    return None


def apply_city_arrest_mechanic(state: dict, world: dict) -> None:
    """
    Sprawdza czy próg zebranych informacji w mieście został osiągnięty.
    Dwie niezależne ścieżki: komplet (4/4) — zawsze; albo 3/4 + minimalna liczba
    przejść między lokacjami (daje graczowi szansę dobić do kompletu zamiast
    czekać bezczynnie na sam upływ czasu). Jeśli któraś spełniona — ustawia flagę siepaczy.
    """
    mechanic = world.get("mechanics", {}).get("city_arrest")
    if not mechanic:
        return
    if state["flags"].get(mechanic["sets_flag"]):
        return  # już ustawione
    count = sum(1 for f in mechanic["track_flags"] if state["flags"].get(f))
    full = count >= mechanic.get("threshold_full", mechanic.get("threshold", 4))
    partial = count >= mechanic.get("threshold_partial", 3) and state.get("transitions", 0) >= mechanic.get("min_transitions", 10)
    if full or partial:
        state["flags"][mechanic["sets_flag"]] = True


def apply_threshold_mechanic(state: dict, world: dict, mechanic_key: str) -> None:
    """
    Prosty licznik progowy (bez wariantu partial/transitions jak przy areszcie) —
    N z track_flags ustawionych => sets_flag. Używane np. przez zagadki leśnego dziada
    (3 z 6 wystarczy, gracz nie musi trafić konkretnych — liczy się sama liczba).
    """
    mechanic = world.get("mechanics", {}).get(mechanic_key)
    if not mechanic:
        return
    if state["flags"].get(mechanic["sets_flag"]):
        return  # już ustawione
    count = sum(1 for f in mechanic["track_flags"] if state["flags"].get(f))
    if count >= mechanic.get("threshold", 1):
        state["flags"][mechanic["sets_flag"]] = True


def check_condition(req: dict, flags: dict, inventory: list | None = None) -> bool:
    """Sprawdza czy warunek przejścia jest spełniony. Wszystkie obecne klucze w req musza przejść (AND)."""
    if not req:
        return True
    inv = inventory or []
    if "inventory" in req and req["inventory"] not in inv:
        return False
    if "inventory_missing" in req and req["inventory_missing"] in inv:
        return False
    if "inventory_all" in req and not all(item in inv for item in req["inventory_all"]):
        return False
    if "inventory_any_missing" in req and all(item in inv for item in req["inventory_any_missing"]):
        # przeciwieństwo inventory_all: przechodzi TYLKO gdy przynajmniej jeden przedmiot
        # jeszcze brakuje — wzajemnie wykluczający się warunek z inventory_all (por. flags_any_false)
        return False
    if "flags_all" in req and not all(flags.get(k) == v for k, v in req["flags_all"].items()):
        return False
    if "flags_any_false" in req and all(flags.get(k) == v for k, v in req["flags_any_false"].items()):
        # przeciwieństwo flags_all: przechodzi TYLKO gdy przynajmniej jedna z flag jeszcze
        # nie pasuje — pozwala napisać warunek "wzajemnie wykluczający się" z flags_all
        return False
    if "flag" in req:
        flag_val = flags.get(req["flag"])
        if "values" in req:
            if flag_val not in req["values"]:
                return False
        elif flag_val != req.get("value"):
            return False
    return True


def resolve_description(location: dict, flags: dict, inventory: list | None = None) -> str:
    """Zwraca opis lokacji pasujący do aktualnych flag i ekwipunku (pierwszy pasujący wariant)."""
    for variant in location.get("description_variants", []):
        cond = variant.get("condition")
        if cond is None or check_condition(cond, flags, inventory):
            return variant["description"]
    # Fallback: stare pole description (kompatybilność wsteczna)
    return location.get("description", "")


def resolve_exits(location: dict, flags: dict, inventory: list | None = None, entry_direction: str | None = None) -> tuple[dict, dict]:
    """
    Zwraca (dostępne_wyjścia, {kierunek: komunikat_blokady}).
    Wyjścia hidden=true są niewidoczne dopóki nie są odblokowane.
    entry_only=true na wyjściu: widoczne TYLKO gdy jego kierunek zgadza się z entry_direction
    (czyli kierunkiem, którym gracz wszedł do obecnej lokacji) — np. lokacja ze skorpionem,
    gdzie da się cofnąć TYLKO tą stroną, którą się weszło, niezależnie ile razy się tu wraca.
    entry_only_unless: opcjonalny warunek (format requires) — jeśli spełniony, ograniczenie
    entry_only przestaje obowiązywać (np. po pokonaniu przeszkody gracz może iść dowolnie,
    a nie tylko stroną którą akurat ostatnio wszedł).
    """
    accessible = {}
    blocked_msgs = {}
    for direction, exit_def in location.get("exits", {}).items():
        if exit_def is None:
            continue
        if isinstance(exit_def, dict):
            if exit_def.get("entry_only") and direction != entry_direction:
                bypass = exit_def.get("entry_only_unless")
                if not (bypass and check_condition(bypass, flags, inventory)):
                    continue
            req = exit_def.get("requires")
            if req:
                flag_part = {k: v for k, v in req.items() if k in ("flag", "value", "values")}
                inv_part = {k: v for k, v in req.items() if k in ("inventory", "inventory_missing", "inventory_all")}
                flag_ok = check_condition(flag_part, flags, inventory)
                inv_ok = check_condition(inv_part, flags, inventory)
                if not (flag_ok and inv_ok):
                    if not exit_def.get("hidden"):
                        default_msg = f"Kierunek {direction} jest zablokowany."
                        if not flag_ok:
                            blocked_msgs[direction] = exit_def.get("blocked_message", default_msg)
                        else:
                            blocked_msgs[direction] = exit_def.get("blocked_message_items", exit_def.get("blocked_message", default_msg))
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
    return not check_condition(hw, flags, inventory)


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
    raw_inventory = state.get("inventory", [])
    inventory = raw_inventory if raw_inventory else ["(brak)"]
    flags = state["flags"]

    entry_direction = flags.get(f"wejscie_{loc_id}")
    exits, blocked_msgs = resolve_exits(location, flags, raw_inventory, entry_direction)
    exits_list = ", ".join(f"{k} → {v}" for k, v in exits.items()) or "(brak wyjść)"
    blocked_info = "\n".join(f"  ⚠ {direction}: {m}" for direction, m in blocked_msgs.items())

    description = resolve_description(location, flags, raw_inventory)

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

        dialogue_block = ""
        if NPC_DIALOGUE_MEMORY_ENABLED:
            recent_dialogue = state.get("npc_dialogue", {}).get(npc["id"], [])
            if recent_dialogue:
                lines = "\n".join(
                    f'  Gracz: "{turn["player"]}"\n  {npc["name"]}: "{turn["npc"]}"'
                    for turn in recent_dialogue
                )
                dialogue_block = (
                    f"Ostatnia rozmowa z {npc['name']} (od najstarszej do najnowszej — nawiązuj do niej dla "
                    f"spójności, ale NIE zmieniaj AKTUALNEGO STANU wbrew temu co ustalono wyżej):\n{lines}\n"
                )

        npc_context += f"""
NPC: {npc['name']} (AKTUALNY STAN: {npc_current_state})
Opis dla GM: {npc['description']}
{dialogue_block}Kanoniczne rozwiązania:
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
8. Jeśli akcja gracza wymaga przedmiotu którego NIE MA jeszcze w polu "Ekwipunek gracza" powyżej — powiedz wprost że musi go najpierw zdobyć/podnieść. NIE wymyślaj technicznych powodów niepowodzenia ("klucz nie pasuje", "zamek zablokowany") — to fałszywe tropy. Jedyny prawdziwy powód to brak przedmiotu w ekwipunku.
9. Opisuj fizycznie spójne akcje — unikaj zdań wewnętrznie sprzecznych (np. "blokuje drogę nie ruszając się z miejsca").

=== RUCH MIĘDZY LOKACJAMI (KRYTYCZNE) ===
Jeśli akcja gracza to próba przemieszczenia się (idę, wchodzę, wychodzę, podejdź, przejdź itp.) — odpowiedz WYŁĄCZNIE jedną linią w formacie:
MOVE: <kierunek>
gdzie <kierunek> to DOKŁADNIE jedna z nazw z listy "Dostępne wyjścia" lub "Zablokowane kierunki" powyżej (np. jeśli lista pokazuje "wyjście", pisz MOVE: wyjście — NIE zgaduj strony świata z opisu lokacji, nawet jeśli opis wspomina "południe" czy "północ").
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
