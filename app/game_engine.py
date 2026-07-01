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
        scripted = "\n".join(
            f"  - {s['trigger']} → {s['outcome']}" for s in npc.get("scripted_solutions", [])
        )
        creative = npc.get("creative_solutions_hint", "")
        npc_context += f"""
NPC: {npc['name']} (stan: {npc.get('state','')})
Opis dla GM: {npc['description']}
Kanoniczne rozwiązania:
{scripted}
Wskazówka dla kreatywnych rozwiązań:
{creative}
"""

    items_here = ", ".join(i["name"] for i in location.get("items", [])) or "(brak)"
    exits_list = ", ".join(
        f"{k} → {v}" for k, v in location.get("exits", {}).items() if v
    )

    return f"""Jesteś Mistrzem Gry w tekstowej grze przygodowej "Zamek Złej Królowej".
Rozmawiasz z graczem głosowo — odpowiadaj żywo, obrazowo, w drugiej osobie liczby pojedynczej.
Odpowiedzi max 3-4 zdania (to głos, nie tekst).

=== STAN GRY ===
Lokacja: {location['name']}
Opis lokacji: {location['description'].strip()}
Atmosfera: {location.get('atmosphere', '')}
Wyjścia: {exits_list}
Przedmioty w lokacji: {items_here}
Ekwipunek gracza: {", ".join(inventory)}
Flagi: {json.dumps(flags, ensure_ascii=False)}
Pora dnia: {flags.get('pora_dnia', 'dzień')}

{npc_context}

=== ZASADY DLA MISTRZA GRY ===
1. Jeśli gracz wykonuje kanoniczne rozwiązanie — zastosuj opisany wynik i zaktualizuj stan.
2. Jeśli gracz próbuje kreatywnego rozwiązania — oceń czy ma sens w tym świecie fantasy.
   Jeśli tak → pozwól zadziałać (uczciwa gra!). Jeśli nie → odmów z humorem i logiką.
3. Ruch między lokacjami: jeśli gracz chce iść w kierunku który jest zablokowany (np. most z trollem),
   powiedz dlaczego nie może przejść.
4. Na końcu odpowiedzi zawsze podaj JSON z aktualizacją stanu w bloku ```json ... ```
   Pola do aktualizacji: new_location (string lub null),
   inventory_add (lista), inventory_remove (lista), flags_update (dict).

=== AKCJA GRACZA ===
{player_input}"""
