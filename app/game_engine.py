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
        "flags": {"troll_state": "blokuje_most", "pora_dnia": "dzień"},
        "history": [],
        "turn": 0,
    }
    save_state(state)
    return state


def build_gm_prompt(player_input: str, state: dict, world: dict) -> str:
    loc_id = state["current_location"]
    location = world["locations"][loc_id]
    inventory = state["inventory"] if state["inventory"] else ["(brak)"]
    flags = state["flags"]

    # Zbierz NPC i ich ukryte rozwiązania dla GM-a
    npc_context = ""
    for npc in location.get("npcs", []):
        # Stan NPC bierz z aktualnych flag gry, nie z YAML
        npc_current_state = flags.get("troll_state", npc.get("state", ""))
        scripted = "\n".join(
            f"  - {s['trigger']} → {s['outcome']}" for s in npc.get("scripted_solutions", [])
        )
        creative = npc.get("creative_solutions_hint", "")
        npc_context += f"""
NPC: {npc['name']} (AKTUALNY STAN: {npc_current_state})
Opis dla GM: {npc['description']}
Kanoniczne rozwiązania:
{scripted}
Wskazówka dla kreatywnych rozwiązań:
{creative}
WAŻNE: jeśli stan to "troll_pokonany" lub "troll_przekupiony" — troll nie blokuje już przejścia.
"""

    items_here = ", ".join(i["name"] for i in location.get("items", [])) or "(brak)"

    # Buduj listę wyjść z uwzględnieniem flag
    exits = {}
    for direction, target in location.get("exits", {}).items():
        if direction == "zachód" and loc_id == "las" and not hidden_path:
            continue  # ukryta ścieżka niewidoczna
        if direction == "północ" and loc_id == "zamek" and not brama_open:
            continue  # brama zamknięta
        if target:
            exits[direction] = target
    exits_list = ", ".join(f"{k} → {v}" for k, v in exits.items()) or "(brak wyjść)"

    # Użyj opisu z ukrytą ścieżką jeśli odblokowana
    if loc_id == "las" and hidden_path:
        description = location.get("description_with_path", location["description"])
    else:
        description = location["description"]

    troll_defeated = flags.get("troll_state") in ("troll_pokonany", "troll_przekupiony")
    troll_blocks = loc_id == "most" and not troll_defeated
    hidden_path = flags.get("hidden_path_unlocked", False)
    brama_open = flags.get("brama_state") == "otwarta"

    return f"""Jesteś Mistrzem Gry w tekstowej grze przygodowej "Zamek Złej Królowej".
Rozmawiasz z graczem głosowo — odpowiadaj żywo, obrazowo, w drugiej osobie liczby pojedynczej.
Odpowiedzi max 3-4 zdania (to głos, nie tekst).

=== AKTUALNA LOKACJA GRACZA ===
Gracz jest TERAZ w: {location['name']} (id: {loc_id})
Opis: {description.strip()}
Atmosfera: {location.get('atmosphere', '')}
Dostępne wyjścia z tej lokacji: {exits_list}
Przedmioty TUTAJ (tylko te może wziąć): {items_here}
Ekwipunek gracza: {", ".join(inventory)}
Pora dnia: {flags.get('pora_dnia', 'dzień')}

{npc_context}

=== KRYTYCZNE ZASADY ===
1. NIGDY nie wymyślaj przedmiotów, postaci ani miejsc których nie ma w opisie lokacji.
   Gracz widzi TYLKO to co jest wymienione powyżej.
2. NPC (np. troll) istnieje TYLKO we własnej lokacji. Jeśli gracza nie ma przy moście — troll jest poza zasięgiem.
3. Ruch: gracz może iść TYLKO w kierunkach z listy wyjść tej lokacji.
   {"UWAGA: przejście na północ przez most jest ZABLOKOWANE przez trolla — gracz musi go najpierw pokonać lub ominąć." if troll_blocks else ""}
4. Jeśli gracz próbuje czegoś niemożliwego — powiedz to krótko i zapytaj co chce zrobić.
5. Kanoniczne rozwiązania: zastosuj dokładnie opisany wynik.
6. Kreatywne rozwiązania: jeśli mają logiczny sens w tym świecie — pozwól zadziałać.

=== FORMAT ODPOWIEDZI (OBOWIĄZKOWY) ===
Najpierw narracja (3-4 zdania).
Potem ZAWSZE na końcu blok JSON — nawet jeśli nic się nie zmienia:
```json
{{"new_location": null, "inventory_add": [], "inventory_remove": [], "flags_update": {{}}}}
```

=== AKCJA GRACZA ===
{player_input}"""
