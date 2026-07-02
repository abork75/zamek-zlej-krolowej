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


def reset_state() -> dict:
    state = {
        "current_location": "las",
        "inventory": [],
        "flags": {"troll_state": "blokuje_most", "straznik_state": "blokuje_przejscie", "pora_dnia": "dzień"},
        "history": [],
        "turn": 0,
    }
    save_state(state)
    return state


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


def find_items(target: str, location: dict) -> list[dict]:
    """Zwraca itemy pasujące do target — najpierw exact match po id, potem fuzzy name/hint."""
    if not target:
        return []
    t = target.lower()
    # Exact match po id (klasyfikator zwraca item_id)
    by_id = [item for item in location.get("items", []) if item.get("id", "").lower() == t]
    if by_id:
        return by_id
    # Fuzzy match po name i hint
    return [
        item for item in location.get("items", [])
        if t in item["name"].lower()
        or t in item.get("hint", "").lower()
        or item["name"].lower() in t
        or item.get("hint", "").lower() in t
    ]


def find_item(target: str, location: dict) -> dict | None:
    """Zwraca pierwszy pasujący item (dla TAKE)."""
    results = find_items(target, location)
    return results[0] if results else None


def build_gm_prompt(player_input: str, state: dict, world: dict, intent_context: str = "") -> str:
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
        scripted = "\n".join(
            f"  - TRIGGER: {s['trigger']}\n    OUTCOME (użyj DOSŁOWNIE jeśli trigger pasuje, nie parafrazuj): {s['outcome'].strip()}\n    [flags_update: {s.get('flags', {})}]"
            for s in npc.get("scripted_solutions", [])
        )
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
    for i in location.get("items", []):
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
