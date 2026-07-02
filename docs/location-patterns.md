# Taksonomia Wzorców Lokacji — Zamek Złej Królowej

Praktyczny przewodnik przy rozbudowie gry o nowe lokacje. Każda nowa lokacja powinna być zaklasyfikowana do wzorca przed implementacją — to określa poziom testowania jakiego wymaga.

---

## Poziom 1 — Wzorce Atomowe

Wytestowane, bezpieczne. Jeśli nowa lokacja pasuje do wzorca atomowego który już działa w grze, implementacja sprowadza się do konfiguracji YAML i treści promptów.

---

### W1 — Korytarz

**Definicja:** Lokacja przejściowa. Brak NPC, brak przedmiotów. Tylko wyjścia i opis atmosfery.

**Przykład w grze:** `korytarz`

**YAML minimum:**
```yaml
nazwa_lokacji:
  name: "Nazwa"
  description_variants:
    - condition: null
      description: "Opis."
  atmosphere: "klimat"
  exits:
    północ: inna_lokacja
    południe: poprzednia_lokacja
  items: []
  npcs: []
```

**Ryzyko:** Niskie. Narrator ma minimalny kontekst, trudno o halucynacje.

**Test akceptacyjny:**
- [ ] Wejście z każdej strony daje poprawny opis
- [ ] Narrator nie wymyśla przedmiotów ani postaci

---

### W2 — Skarbiec

**Definicja:** Lokacja z przedmiotami do zbadania i/lub wzięcia. Brak NPC.

**Przykład w grze:** `polana` (miecz, sakiewka)

**Kluczowe pola YAML:**
```yaml
items:
  - id: miecz
    name: "zardzewiały miecz"
    hint: "błyszczący przedmiot przy głazie"
    description: "Stary miecz z runami..."
    takeable: true
    examine_sets_flag: {}      # opcjonalnie
    set_on: examine            # opcjonalnie — tylko przy EXAMINE
```

**Ryzyko:** Niskie. Możliwy błąd: narrator sugeruje podniesienie przy EXAMINE.
Mitygacja: `intent_context` zawiera ZAKAZ pisania "chwytasz/bierzesz" przy EXAMINE.

**Test akceptacyjny:**
- [ ] Samo słowo `miecz` → EXAMINE, nie TAKE
- [ ] `biorę miecz` → TAKE, przedmiot w ekwipunku
- [ ] `badam miecz` → opis z pola `description`, bez sugestii podniesienia
- [ ] Przedmiot z `takeable: false` → wyjaśnienie dlaczego nie można zabrać

---

### W3 — Strażnik

**Definicja:** NPC blokujący wyjście. Odblokowanie wymaga wykonania jednej z `scripted_solutions`.

**Przykład w grze:** `most` (troll), `dziedziniec_zamku` (strażnik)

**Kluczowe pola YAML:**
```yaml
exits:
  północ:
    target: następna_lokacja
    requires: {flag: npc_state, values: [stan_A, stan_B, stan_C]}
    blocked_message: "NPC blokuje drogę."

npcs:
  - id: nazwa_npc
    state: "stan_blokujący"
    unavailable_states:
      stan_A: "Komunikat gdy NPC jest w stanie A."
    scripted_solutions:
      - id: rozwiazanie_1
        trigger: "opis akcji gracza"
        outcome: |
          Tekst narracyjny — używany DOSŁOWNIE.
        flags: {npc_state: stan_A}
```

**Ryzyko:** Średnie.
- Classifier NPC może nie rozpoznać kreatywnego rozwiązania → dodaj jako `scripted_solution`
- Grok narrator może nadpisać flagę własną wartością → `authoritative_flags` chroni przed tym
- TALK (deklaracja) nie może odpalić scripted_solution wymagającej fizycznej akcji

**Test akceptacyjny:**
- [ ] Wszystkie `scripted_solutions` odpalają i ustawiają flagę
- [ ] Kreatywne rozwiązania z `creative_solutions_hint` mają swoje `scripted_solutions`
- [ ] Deklaracja "dam ci X" ≠ danie X (TALK nie odpala akcji fizycznej)
- [ ] Blef słowny odpala przy TALK jeśli trigger to dialog
- [ ] `unavailable_states` dają deterministyczny komunikat

---

### W4 — Rozmówca

**Definicja:** NPC z którym gracz może rozmawiać. Może mieć quest (daj mi X → dam ci Y) lub tylko narrację/informacje.

**Przykład w grze:** `wnetrze_hatki` (pustelnik Hieronim)

**Kluczowe pola YAML:**
```yaml
npcs:
  - id: npc_id
    name: "Imię NPC"
    description: |
      Rozbudowany opis charakteru: jak mówi, co go interesuje,
      czego nie lubi, jaką wiedzę posiada, jakim stylem odpowiada.
      Im bogatszy opis — tym lepsze odpowiedzi Groka.
    scripted_solutions:
      - id: quest_wykonany
        trigger: "gracz daje NPC wymagany przedmiot"
        outcome: |
          Narracja + kluczowa informacja fabularna.
        flags: {npc_id_state: stan_po_queście}
```

**Ryzyko:** Niskie dla swobodnej rozmowy, średnie dla questu.
- Kluczowe informacje fabularne MUSZĄ być w `scripted_solutions` → determinizm
- Swobodna rozmowa (filozofia, wiedza ogólna) może być Grokowa

**Test akceptacyjny:**
- [ ] NPC odpowiada w charakterze (ton, słownictwo zgodne z `description`)
- [ ] Quest: danie wymaganego przedmiotu odpala `scripted_solution` z kluczową info
- [ ] Po queście NPC może powtórzyć informację jeśli pytany ponownie
- [ ] Pytania niezwiązane z grą (gwiazdy, przyroda) → filozoficzna/charakterystyczna odpowiedź

---

### W5 — Pułapka

**Definicja:** Wyjście z ukrytym zagrożeniem. Wymaga zbadania otoczenia żeby przejść bezpiecznie.

**Przykład w grze:** `korytarz` (zapadnia)

**Kluczowe pola YAML:**
```yaml
exits:
  północ:
    target: cel_jesli_bezpieczne
    requires: {flag: flaga_zauważenia, value: true}
    hidden: false
    trap:
      target: lokacja_pułapki
      message: "Opis wpadnięcia w pułapkę."
    blocked_message: "Można iść, ale coś niepokoi."

items:
  - id: element_pułapki
    name: "kamienne płyty"
    hint: "podejrzane rysy w posadzce"
    description: "Przyjrzawszy się uważnie..."
    takeable: false
    examine_sets_flag: {flaga_zauważenia: true}
    set_on: examine    # tylko EXAMINE ustawia flagę, nie samo wspomnienie
```

**Ryzyko:** Średnie.
- Narrator może opisać "przeskakujesz pułapkę" zanim gracz ją zauważył → `mechanic_context` w prompcie blokuje to przez ✗/✓
- `set_on: examine` zapewnia że samo wspomnienie pułapki jej nie aktywuje

**Test akceptacyjny:**
- [ ] Bez zbadania: wejście w kierunek odpala `trap` → gracz w lokacji pułapki
- [ ] Po zbadaniu (EXAMINE): flaga ustawiona, wyjście odblokowane
- [ ] Narrator nie opisuje "przeskakujesz" przed zbadaniem
- [ ] Ponowne wejście w kierunek po zbadaniu → przejście normalne

---

### W6 — Więzień / Informator

**Definicja:** NPC uwięziony lub ograniczony. Przekazuje kluczową wskazówkę fabularną. Może być uwolniony.

**Przykład w grze:** `klatka_wieznia` (Benedykt)

**Różnica od W4:** NPC nie może opuścić lokacji, ma ograniczoną wiedzę, często zna jeden kluczowy sekret.

**Ryzyko:** Niskie-średnie. Uwaga na fallback lokacji po uwolnieniu — gracz nie powinien wracać do pustej klatki i widzieć NPC.

**Test akceptacyjny:**
- [ ] Kluczowa wskazówka przekazana deterministycznie przez `scripted_solution`
- [ ] Po uwolnieniu NPC znika z lokacji (flaga + `description_variants`)

---

### W7 — Ending

**Definicja:** Finałowa lokacja kończąca grę. Trigger zakończenia przy wejściu lub po interakcji.

**Przykład w grze:** `sala_tronowa`

**Kluczowe pola YAML:**
```yaml
sala_tronowa:
  ending: "queen_seduction"
```

**Ryzyko:** Niskie — logika w `_build_ending_prompt()`, nie w naratorze.

**Test akceptacyjny:**
- [ ] Wejście do lokacji odpala sekwencję wideo + epilog
- [ ] Przycisk "Zagraj ponownie" resetuje stan

---

## Poziom 2 — Wzorce Kompozytowe

Kombinacje wzorców atomowych. **Wymagają osobnego scenariusza testowego** — złożoność nie jest addytywna, klasyfikator musi jednocześnie rozróżniać konteksty.

| Kompozycja | Główne ryzyko | Przykład w grze |
|---|---|---|
| W3 + W2 (Strażnik + Skarbiec) | EXAMINE itemu triggeruje NPC outcome? | — |
| W4 + W2 (Rozmówca + Quest item) | Narrator opisuje danie przedmiotu zanim gracz to zrobi? | `wnetrze_hatki` |
| W5 + W3 (Pułapka + Strażnik) | Po pułapce gracz wraca — czy NPC stan zachowany? | — |
| W3 + W6 (Strażnik + Informator) | Dwie grupy `scripted_solutions` — klasyfikator się nie myli? | `zamek` (brama + więzień) |

**Protokół testowy dla kompozytów:**
1. Przetestuj każdy wzorzec składowy osobno
2. Przetestuj interferencję: wykonaj akcję dla wzorca A, sprawdź czy wzorzec B się nie aktywował
3. Przetestuj w odwrotnej kolejności

---

## Poziom 3 — Złożone

Trzy lub więcej wzorców atomowych jednocześnie. **Wymagają pełnego QA** z napisanymi scenariuszami przed wdrożeniem.

Aktualnie brak w grze — traktować jako red flag przy projektowaniu.

---

## Checklista dla nowej lokacji

```
[ ] Określ wzorzec (W1-W7) lub kompozycję (Poziom 2/3)
[ ] Napisz world.yaml zgodnie ze schematem wzorca
[ ] Sprawdź czy wszystkie wyjścia niestandardowe są w DIRECTION_ALIASES (main.py)
[ ] Dodaj ikonę kierunku do DIR_ICONS (game.js)
[ ] Dodaj węzeł do NODE_POS w debug.html
[ ] Przetestuj wg checklisty wzorca
[ ] Dla Poziomu 2+: napisz i wykonaj scenariusz interferencji
```

---

## Zasady ogólne

**Każde kreatywne rozwiązanie które odblokowuje exit MUSI być `scripted_solution`.**
Jeśli zostawisz je jako "kreatywne" bez scripted_solution, Grok narruje efekt ale nie ustawia flagi → gracz utknął mimo "wygranej" rozmowy.

**Bogatszy `description` NPC = lepsze odpowiedzi.**
Grok nie ma własnej wiedzy o postaciach — wszystko czerpie z `description`. Skąpe opisy = generyczne odpowiedzi.

**Nowe flagi w exitach trzeba dodać do `reset_state()` w game_engine.py.**
Inaczej po resecie gry flaga ma wartość `None` zamiast stanu startowego.
