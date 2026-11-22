# HYDRA Watch na żywo (WATCH-RT)

Stan: 2026-11-22 (Europe/Warsaw), gałąź. Moduł `/watch` w interfejsie, kod `hydra/watch/rt/`,
API `/v1/watch/rt/*`, migracja bazy v6 (`hydra/db.py`, internal notes, not published „Watch realtime”). Klasyczny Watch (nagrania
CERT Polska, ocena nazw w bitach, `/v1/watch/*`) działa bez zmian, opis w internal notes, not published.

**Domyślnie wyłączony i offline.** Bez `HYDRA_OFFLINE=0` Watch niczego nie pobiera i nie używa danych zastępczych;
pokazuje stan „Offline” z wyjaśnieniem. Dla jury jest tryb demonstracyjny (odtworzenie), który działa w pełni
offline i jest na ekranie wyraźnie oznaczony jako **SYNTETYCZNE / ODTWORZENIE**.

## 1. Co robi

1. Co najmniej raz na sekundę silnik sprawdza, które źródła mają termin odpytania, i odpytuje je w wątku roboczym.
2. Każdy nowy element (domena z listy CERT, URL z URLhaus/OpenPhish, certyfikat z crt.sh, reklama z Meta) jest
   normalizowany do wskaźników: host, domena rejestrowalna, URL, IP, telefon, IBAN (tylko solony skrót), portfel
   krypto, identyfikator trackera, identyfikator reklamy i strony.
3. Wskaźniki są wyszukiwane dokładnie (słownik w pamięci) na liście obserwowanych. Trafienie zapisuje się jako
   dopasowanie `watch_matches` w tej samej transakcji co element.
4. Interfejs dostaje zdarzenia na żywo przez istniejący strumień `GET /v1/stream`: `watch.poll`, `watch.item`,
   `watch.match`, `watch.source_state` (oraz `watch.engine` przy zmianie stanu silnika).
5. Analityk potwierdza lub odrzuca dopasowanie, otwiera kampanię albo tworzy incydent. „Utwórz incydent” wysyła
   adres przez publiczną ścieżkę `hydra.batch.assign_batch` (decyzja assign / new_campaign / abstain oraz
   `new_pattern_id`). Punktacja kampanii nie jest zmieniana. Opcja `HYDRA_WATCH_AUTO_ASSIGN=1` robi to
   automatycznie dla silnych dopasowań na żywo (domyślnie wyłączona). Z danych odtworzenia incydent nie powstaje.

## 2. Źródła, interwały, co jest wysyłane

| Źródło | Domyślny interwał | Minimum | Co wychodzi z komputera | Uwagi |
|---|---|---|---|---|
| CERT Polska, lista ostrzeżeń (`actions_<rok>.log`) | 60 s | 60 s | nic o naszych wskaźnikach: pobranie przyrostu publicznego pliku (nagłówek `Range`) | przy pierwszym odpytaniu ostatnie 16 KB; potem tylko nowe bajty; pozycja w bazie |
| URLhaus (abuse.ch), najnowsze URL | 60 s | 60 s | nic o naszych wskaźnikach; klucz `Auth-Key` w nagłówku | wymaga klucza `URLHAUS_AUTH_KEY` (Integracje); bez klucza stan „Brak klucza” |
| OpenPhish, feed społecznościowy | 3600 s | 3600 s | nic o naszych wskaźnikach: pobranie `feed.txt` z `If-None-Match` | feed aktualizowany co 12 h; warunki użytkowania OpenPhish nie zezwalają wprost na użycie komercyjne (niezweryfikowane), dlatego źródło jest domyślnie wyłączone |
| crt.sh (Certificate Transparency) | 60 s | 60 s | **ręcznie wpisane słowa kluczowe CT** (najwyżej 3 na odpytanie, rotacja) | nigdy wartości wyprowadzone z kampanii; usługa bez SLA (częste 502) |
| Meta Ad Library | 21600 s | 3600 s | ręcznie wpisane słowa kluczowe (istniejący harmonogram `hydra.scheduler`) | tylko z tokenem `HYDRA_META_TOKEN` i `HYDRA_ENRICH=1` |

Interwał jest konfigurowalny dla każdego źródła (UI albo `hydra watch enable <źródło> --interval N`), ale nigdy
krótszy niż minimum wynikające z polityki dostawcy i limitu konektora w rejestrze integracji
(`rate_limit_per_min` × liczba zapytań na odpytanie). Do terminu doliczany jest losowy rozrzut 0–10 %.

Każde zapytanie sieciowe przechodzi przez `hydra.integrations.http.request`: wyłącznik `HYDRA_OFFLINE`, flaga
włączenia konektora, kontrola adresu docelowego (`netguard`: tylko publiczne http/https, bez przekierowań), limit
zapytań konektora i wpis audytu `INTEGRATION_REQUEST` (host, ścieżka bez parametrów, status, czas, rozmiary).
Włączenie źródła w Watch włącza też odpowiadający mu konektor w Integracjach (jawna zgoda operatora).

## 3. Stany i odporność

- Silnik: **Na żywo**, **Offline** (HYDRA_OFFLINE=1: nic nie jest odpytywane), **Odtworzenie**, **Wstrzymany**,
  **Wyłączony**.
- Źródło: Działa, Oczekuje, Wyłączone, Offline, Brak klucza, Ponawianie (po błędzie), Wstrzymane po błędach
  (bezpiecznik).
- Błąd sieci lub HTTP: kolejna próba po `interwał × 2^n` (maks. 1 h). Po 5 kolejnych błędach bezpiecznik otwiera
  się na 15 min; potem jedna próba kontrolna; sukces zeruje licznik. Brak klucza i przekroczenie limitu nie liczą
  się do bezpiecznika.
- Walidatory `ETag` / `Last-Modified` są zapisywane per źródło; odpowiedź 304 to „bez zmian”.
- Udane odpytanie bez nowych pozycji daje stan **Brak nowych wpisów** (to nie błąd). Lista CERT nie rośnie co
  minutę: plik jest generowany na nowo co kilka minut (zmienia się `Last-Modified`), ale jego rozmiar i treść
  zostają te same, a serwer odpowiada `416` z `Content-Range: bytes */<rozmiar>` równym naszej pozycji.
- CERT: gdy rozmiar z `Content-Range` jest mniejszy niż nasza pozycja (plik skrócony / podmieniony) albo odpowiedź
  `206` zaczyna się od innego bajtu niż żądany, silnik od razu resynchronizuje pozycję (rozmiar − 16 KB) i odpytuje
  ponownie; duplikaty odrzuca `external_id`. Zdarzenie trafia do audytu (`WATCH_SOURCE_RESYNC`) i do opisu
  odpytania („plik CERT skrócony: resynchronizacja”).
- Karta źródła pokazuje zdanie w rodzaju „Lista CERT bez nowych wpisów od 02:47 (ostatni wpis 03:01:55 czasu PL).
  Plik sprawdzany co minutę.”, rozmiar pliku i `Last-Modified` serwera, najnowszy wpis, czas ostatniego nowego
  wpisu, liczbę nowych w 24 h i wykres nowych pozycji na godzinę; „Odpytaj teraz” odpowiada np. „Sprawdzono: brak
  nowych wpisów (plik bez zmian, 31,6 MB).”
- Jeden silnik na katalog stanu: blokada pliku `<state>/watch/rt/engine.lock` (API albo `hydra watch run`).

## 4. Lista obserwowanych

- **Z kampanii**: przycisk „Przebuduj z kampanii” (`POST /v1/watch/rt/watchlist/rebuild`, `hydra watch rebuild`)
  czyta geny członków kampanii ostatniego przebiegu analizy (tylko odczyt): hosty lądowania, domeny, URL wejścia,
  telefony operatora (bez infolinii i numerów platform), skróty IBAN, portfele, trackery, identyfikatory reklam i
  stron, adresy IP serwerów. Hosty platform (np. serwisy społecznościowe, hosting) są pomijane. Te wartości nigdy
  nie opuszczają komputera.
- **Ręczne**: dowolny rodzaj, z etykietą. Słowa kluczowe CT są jedynymi wartościami wysyłanymi do crt.sh.
- Siła dopasowania: **silne** dla hosta, URL, telefonu, IBAN, portfela, trackera, identyfikatora reklamy/strony;
  **słabe** dla domeny rejestrowalnej i adresu IP (wspólny hosting, darmowe subdomeny).
- „Rzadkość w korpusie” przy dopasowaniu: `bits = log2((N + 1) / (df + 1))`, gdzie `df` to liczba incydentów z tą
  wartością, `N` liczba incydentów z genami. To heurystyka informacyjna, nie prawdopodobieństwo i nie reguła
  łączenia.

## 5. Tryb demonstracyjny (odtworzenie)

- UI: panel „Tryb demonstracyjny (odtworzenie)”, wybór nagrania i prędkości (1× = jedno odpytanie na minutę,
  10×, 60×, 600×). CLI: `uv run hydra watch replay demo --speed 60` albo `hydra watch replay <katalog>`.
- Nagranie `hydra/watch/rt/fixtures/demo` jest **syntetyczne** (generator `scripts/watch_rt_fixture.py`): 12
  odpytań w natywnych formatach źródeł, wyłącznie domeny w zarezerwowanych TLD `.example` / `.test`, syntetyczna
  lista obserwowanych (pochodzenie „odtworzenie”). Dodatkowo w czwartym odpytaniu trzy wpisy w formacie CERT niosą
  prawdziwe hosty z lokalnej listy „z kampanii” (gdy jest zbudowana), żeby pokazać dopasowanie do istniejących
  kampanii; element jest syntetyczny, wyszukiwanie jest prawdziwe.
- Odtworzenie używa tych samych adapterów, zapisu i dopasowania co tryb na żywo; nie otwiera żadnego połączenia.
  Wiersze mają `replay=1`, banner SYNTETYCZNE / ODTWORZENIE jest stale widoczny, widoki na żywo nie mieszają
  danych odtworzenia. Start odtworzenia usuwa poprzednie wiersze odtworzenia (dane na żywo zostają).
- Własne nagranie: katalog z `manifest.json` (`ticks`: lista słowników źródło → plik) i plikami odpowiedzi.
- Nagrywanie na żywo: `HYDRA_WATCH_RECORD=1` zapisuje każdą udaną odpowiedź źródła jako jedno odpytanie w
  `<state>/watch/rt/recordings/live-<data>/` (`synthetic: false`, poza repozytorium). Takie nagranie pojawia się na
  liście nagrań i jest odtwarzane z etykietą ODTWORZENIE (bez „SYNTETYCZNE”), nigdy jako dane na żywo.

## 6. Dane i retencja

Tabele (migracja v6): `watch_sources` (stan, termin, walidatory, kursor, błędy, bezpiecznik), `watch_items`
(źródło, `external_id`, pierwszy/ostatni raz widziany, skrót treści, wskaźniki; unikalne `(source, external_id)`),
`watch_watchlist`, `watch_matches` (element → wpis listy/kampania, rodzaj i wartość wskaźnika, siła, bity, status
nowe/potwierdzone/odrzucone), `watch_runs` (każde odpytanie: czas, pobrane, nowe, dopasowane, błąd).
Retencja (co godzinę): elementy bez dopasowań starsze niż `HYDRA_WATCH_RETENTION_DAYS` (domyślnie 30) i
odpytania starsze niż `HYDRA_WATCH_RUNS_RETENTION_DAYS` (14) są usuwane; elementy z dopasowaniem zostają jako
dowód. Wszystkie zapisy idą przez jedynego `Writer` procesu.

## 7. Uruchomienie

```bash
. scripts/env.sh
uv run hydra watch replay demo --speed 600      # offline, syntetyczne
HYDRA_OFFLINE=0 HYDRA_WATCH_RT=1 just serve      # silnik startuje z API; źródła włącza się w /watch
HYDRA_OFFLINE=0 uv run hydra watch enable cert-pl && HYDRA_OFFLINE=0 uv run hydra watch run   # demon CLI
uv run hydra watch status
```

## 8. Pomiary

Zmierzone raz (Apple Silicon Mac, `uv run python scripts/watch_rt_bench.py --n 10000`, syntetyczny feed 10 tys. elementów):
dopasowanie jednego elementu p50 1,96 µs / p95 2,13 µs; całe odpytanie 10 tys. elementów z zapisem w bazie 1268 ms;
pusta pętla harmonogramu p50 0,23 ms / p95 0,27 ms; szczyt pamięci (tracemalloc) 27,7 MB, RSS procesu 107 MB.
Szczegóły: internal notes, not published „Watch realtime”. Odpytań na żywo u prawdziwych dostawców nie wykonano podczas budowy.
