# Changelog — Sesja deweloperska (lipiec 2026)

## Naprawione błędy silnika

### `check_condition` — obsługa ekwipunku
- Dodano obsługę `inventory` i `inventory_missing` jako typy warunków obok `flag`/`values`/`value`
- Dodano fallback `if "flag" not in req: return True` zapobiegający `KeyError`
- Naprawiono `/api/debug/graph` który crashował na tych warunkach

### `resolve_description` i `resolve_exits`
- Obie funkcje teraz przyjmują i przekazują `inventory` do `check_condition`
- Opisy lokacji i dostępność wyjść reagują poprawnie na przedmioty w ekwipunku

### `image_service.py`
- `_check_image_condition` obsługuje oba klucze: `inventory` i `inventory_contains`

### `inventory_add` w silniku
- Dodano obsługę `inventory_add` w `scripted_solutions` (dwa miejsca w main.py: ścieżka złożona i pojedyncza)

### `move_to` w `scripted_solutions`
- Po przeniesieniu gracza do nowej lokacji narracja jest uzupełniana opisem tej lokacji

### `DIRECTION_ALIASES`
- Dodano: `wieża`, `góra` / `gora` / `up`, `dół` / `dol` / `down`

### Debug map
- Naprawiono CSS canvas (`display: flex` na rodzicu, `flex: 1` na canvas)
- Zastąpiono Proxy dla `NODE_POS` funkcją `getNodePos(id)` — stabilniejsze renderowanie
- Dodano overlay diagnostyczny dla pustego canvasu

---

## Nowe funkcje silnika

### Scripted edges w debuggerze
- `/api/debug/graph` zwraca teraz krawędzie z `"type": "scripted"` dla `move_to` w `scripted_solutions`
- `debug.html` rysuje je jako przerywane czerwone linie
- Połączenia cross-area (cel poza aktywnym obszarem mapy) → przerywana strzałka z etykietą `→ <lokacja>`

---

## Nowe lokacje (zamek-dev)

### Wieża (3 poziomy)
- `wiezba_parter` — parter z dwoma strażnikami, wejście z dziedzińca
- `wiezba` — pracownia alchemika, item `zestaw fiolek`
- `wiezba_gore` — górna izba, item `klucz do celi`
- Nawigacja: `góra` / `dół` między poziomami

### Skrzydło cel (3 lokacje)
- `cela_korytarz` — korytarz, wejście ze wschodu dziedzińca
- `cela_przy_kratach` — przed celą, rozmowa z więźniem przez kraty (NPC), otwieranie kluczem
- `cela_zamknieta` — wnętrze celi, podawanie przedmiotów, podkop ze ścianą

### Tunel ucieczki
- `tunel_ucieczki` — nowa lokacja po przebiciu ściany celi łomem + próbką substancji
- Wyjście do `las2` (inny obszar)

### Las za zamkiem
- `las2` — nowy obszar, zwykły liściasty las, nowa zakładka w debuggerze
- Wyjście na wschód (placeholder `miasto_las2` do uzupełnienia)

---

## Zmiany w world.yaml

### `dziedziniec_zamku`
- `description_variants` — opis zmienia się po odejściu strażnika (flaga `straznik_state`)
- Wyjście `wieża → wiezba_parter`

### `korytarz`
- Zapadnia prowadzi do `loch` (poprawka — wcześniej prowadziła do dziedzińca)

### `loch`
- Uproszczony do mechaniki v1: zbadaj zachodnią ścianę → ukryte przejście → `kanaly`

### `kanaly`
- Wyjście: `wschód → dziedziniec_zamku`

### `magazyn_ciemny`
- `description_variants` na podstawie posiadania pochodni w ekwipunku
- Wyjście do `korytarz` wymaga pochodni (`inventory: "pochodnia"`)

### `magazyn_glowny`
- Beczka jako NPC z `scripted_solutions`: bez fiolek → odmowa; z fiolkami → `inventory_add: ["próbka substancji"]`
- Item `próbka substancji` ukryty dopóki nie zostanie napełniony (`hidden_when: {flag: fiolki_uzyte, value: null}`)

### `start_locations`
- Dodano start "🏰 Zamek" z `initial_flags: {straznik_state: straznik_oszukany}` — gracz zaczyna bez strażnika na dziedzińcu

---

## Mapa debuggera (map_config.json)

Przeprojektowany układ węzłów dla poziomu `zamek`:
- Kolumna zachodnia (magazyny): `magazyn_glowny` → `korytarz` → `magazyn_ciemny`
- Środek-lewo (pułapka): `loch` → `kanaly` → (wschód) dziedziniec
- Centrum: `dziedziniec_zamku` ↔ `korytarz_polnocny` ↔ `sala_tronowa`
- Wieża (prawo od centrum): `wiezba_parter` → `wiezba` → `wiezba_gore`
- Skrzydło cel (dalej na wschód): `cela_korytarz` → `cela_przy_kratach` → `cela_zamknieta` → `tunel_ucieczki`
- Nowy obszar `las2` z własną zakładką

---

## Do zrobienia

- [ ] Generacja obrazków dla nowych lokacji: `wiezba_parter`, `wiezba`, `wiezba_gore`, `cela_przy_kratach`, `cela_zamknieta` (prompt `cela_otwarta`)
- [ ] Mechanika odzyskania ekwipunku po areszcie (miecz, zbroja w worku gdzieś w zamku)
- [ ] `las2` → dalszy ciąg gry (połączenie z miastem lub nowy obszar)
- [ ] Połączenie `kanaly` z `las` (stary tunel v1) vs `dziedziniec` — zdecydować docelowy flow
