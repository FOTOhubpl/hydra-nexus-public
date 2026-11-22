# HYDRA NEXUS 1.0: pakiet dla jury / jury package

Status 2026-11-22 (Europe/Warsaw), wydanie 1.0. Ta strona jest punktem wejścia dla oceniających. Instalacja:
[README.md](../../README.md) (szybki start), [INSTALL.md](../../INSTALL.md) (sprzęt, rozwiązywanie problemów, reset),
[docs/MODELS.md](../MODELS.md) (modele i dane referencyjne). Pomiary: [JURY_READINESS.md](JURY_READINESS.md).
Ograniczenia: [KNOWN_LIMITATIONS.md](KNOWN_LIMITATIONS.md).

## 0. Szybki start (czysty system, własne dane)

System startuje z pustą bazą: bez incydentów i bez żadnych korpusów. Repozytorium i obraz zawierają tylko kod,
wbudowane leksykony i progi oraz prior częstości tła (`hydra/priors/background_df.json.gz`, klucze haszowane).
Wagi modeli pobiera jeden krok z przypiętymi rewizjami i sha256 (`models.lock.json`, docs/MODELS.md).

**Wymagania sprzętowe (z pomiaru, JURY_READINESS.md §3).** Do ~200 incydentów: minimum 4 rdzenie i 8 GB RAM
(zmierzono: N=200 w 7,2 min, szczyt pamięci 3,6 GB), wygodnie 8 rdzeni i 16 GB (N=200 w 4,3 min; Docker
`--cpus 8 --memory 16g`: 6 min, szczyt 4,85 GiB). Około 1000 incydentów: co najmniej 8 rdzeni i 32 GB RAM (zmierzono
37 min i szczyt 19,2 GB w jednym procesie, 30,7 GB łącznie; 16 GB prawdopodobnie nie wystarczy, *szacunek*),
wygodnie 16 rdzeni i 64 GB. Dysk: 10 GB wolnego miejsca (obraz 4,3 GB, modele i dane referencyjne 2,37 GB według `models.lock.json`, stan do 2,6 GB przy
1000 incydentów). GPU niepotrzebne. Licznik postępu w GUI może stać w miejscu przez kilka minut przy dużym zbiorze,
mimo że analiza trwa.

**Logowanie.** Przy pierwszym starcie API zapisuje dane administratora w `ADMIN_CREDENTIALS.txt` (0600) w katalogu
stanu (Docker: `~/HydraNexus/state-docker/ADMIN_CREDENTIALS.txt`, lokalnie `~/HydraNexus/state/ADMIN_CREDENTIALS.txt`)
i wypisuje baner w logu (`docker compose logs api | grep -A3 HYDRA`). Login `admin`, hasło z pliku; przy pierwszym
logowaniu panel wymusza zmianę hasła (min. 12 znaków). Zgubione hasło:
`docker compose --profile cli run --rm cli user reset-password admin` (bez Dockera `uv run hydra user reset-password
admin`). Szczegóły: [SECURITY_AUTH.md](../SECURITY_AUTH.md).

**Kroki.**

| Ścieżka | Polecenie | Wynik |
|---|---|---|
| Obraz | pełny: `docker pull ghcr.io/fotohubpl/hydra-nexus:full` (wagi i dane referencyjne w środku, krok „Modele” odpada, `HYDRA_IMAGE_TAG=full`); lekki: `ghcr.io/fotohubpl/hydra-nexus:1.0.1` (albo `:latest`; budowa ze źródeł: `docker compose build api`) | lekki: obraz bez wag modeli |
| Modele (jednorazowo, sieć) | `docker compose --profile models run --rm models` | pobranie, ponowne haszowanie i test dymny każdego modelu offline |
| Docker (Linux) | `docker compose up -d api`, potem http://127.0.0.1:8000/ | GUI; na pustym Pulpicie karta „Pusta baza: zaimportuj własny zbiór incydentów” |
| Docker Desktop (macOS/Windows) | `docker compose --profile gui-ports up -d gui` (albo `just docker-gui`) | to samo GUI na http://127.0.0.1:8000/ |
| Dane referencyjne offline (tylko maszyna bez sieci, zamiast części danych z kroku „Modele”; po kroku „Modele” niepotrzebne, docs/MODELS.md §4) | `just reference-data-install hydra-reference-data-<data>.tar.zst` (weryfikuje SHA256SUMS; paczka jako załącznik wydania) | listy ostrzeżeń MISP, PSL, lista CERT Polska; bez paczki przebieg działa, a wynik pokazuje uwagę „Brak danych referencyjnych offline” |
| Import w GUI | Pulpit → „Nowa analiza: plik, folder lub ZIP” → przeciągnij folder zbioru (jeden podfolder na incydent), jeden ZIP całego zbioru albo pliki jednego incydentu → „Analizuj” | pełny przebieg: kampanie, pary, graf; lista pominiętych plików z kodami w karcie wyniku |
| Folder na serwerze | dane pod `~/HydraNexus/data/` (w kontenerze `/hydra/data`), w GUI zakładka „Folder na serwerze” | jak wyżej, bez wysyłania plików przez przeglądarkę |
| CLI wsadowo | `docker compose --profile cli run --rm cli analyse /hydra/data/<zbiór>` (bez Dockera: `uv run hydra analyse <folder albo zip>`) | podsumowanie przebiegu, linia `import: …` i lista `POMINIĘTO <plik> [KOD]`; wyniki widoczne w GUI |
| Przypisanie nowych materiałów | `docker compose --profile cli run --rm cli assign-batch /hydra/data/<nowe> --out /hydra/exports/<nazwa>` | `assignments.csv` / `.json`: decyzja `assign` / `new_campaign` / `abstain` / `error` na element |
| Eksport dowodów | GUI: Pulpit → „Eksport”, albo `uv run hydra export-dataset latest <folder>` | ZIP / folder z podpisanym manifestem (sha256 każdego pliku) |
| Weryfikacja | `uv run hydra verify` | ponowne haszowanie oryginałów i łańcuch audytu, exit 0 |
| Powrót do czystego stanu | `just reset-state` (przenosi stan do kopii `.bak-<czas>`, niczego nie usuwa); Docker: zatrzymaj usługę i przenieś `~/HydraNexus/state-docker` | pusta baza przy następnym starcie |
| Samosprawdzenie offline | `just jury-smoke` | pusty stan → import danych syntetycznych i wrogich plików → analiza → 200 na wszystkich endpointach i stronach → eksport z weryfikacją sha256 → łańcuch audytu → CLI; `JURY SMOKE: PASS` |

**Obsługiwane układy wejścia** (testy `tests/fuzz/test_jury_import.py`): folder z podfolderami incydentów, folder
opakowujący jeden taki zbiór, jeden ZIP zbioru (także z folderem w środku), ZIP z ZIP-ami incydentów, ZIP w ZIP-ie
wewnątrz incydentu (2 poziomy), luźne pliki jednego incydentu, mieszanka folderów i ZIP-ów. Typ pliku rozpoznawany
jest po bajtach, nie po rozszerzeniu. Polskie znaki, spacje, długie nazwy (do 255 bajtów) i wielkie litery w
rozszerzeniach są obsługiwane; teksty w cp1250 i ISO-8859-2 są czytane.

**Co zrobić, gdy plik został odrzucony.** Każdy plik dostaje status; odrzucenie jednego pliku nigdy nie przerywa
analizy pozostałych incydentów (wyjątek: KNOWN_LIMITATIONS L15).

| Kod | Znaczenie | Co zrobić |
|---|---|---|
| `FILE_EMPTY` | plik 0 bajtów | nic, plik nie niesie danych |
| `FILE_TOO_LARGE` | ponad limit (`HYDRA_MAX_FILE_MB`, wideo do `HYDRA_MAX_UPLOAD_MB`) | zwiększ limit zmienną środowiskową |
| `ZIP_BOMB_SUSPECTED`, `ZIP_TOO_MANY_ENTRIES`, `ZIP_TOO_LARGE`, `ZIP_NO_SPACE` | archiwum odrzucone w całości przed rozpakowaniem | rozpakuj ręcznie, jeśli archiwum jest zaufane |
| `ZIP_ENTRY_UNREADABLE`, `ZIP_UNREADABLE` | wpis zaszyfrowany hasłem albo uszkodzony | rozpakuj z hasłem i podaj folder |
| `ZIP_PATH_TRAVERSAL`, `ZIP_SYMLINK`, `SYMLINK_NOT_FOLLOWED` | ścieżka poza archiwum / dowiązanie symboliczne: nigdy nie rozpakowywane ani śledzone | podaj prawdziwy plik zamiast dowiązania |
| `LOOSE_ROOT_FILE` | luźny plik w katalogu głównym zbioru (np. README) nie należy do żadnego incydentu | przenieś go do folderu incydentu |
| `HIDDEN_NOT_DATA` | plik ukryty, który nie jest danymi | nic |
| uwaga „Folder … zaimportowany jako jeden incydent” | zbiór zagnieżdżony w kategoriach (`kategoria/incydent_N`) | uruchom `hydra analyse` osobno dla każdego folderu kategorii |

Panel „Import danych” tylko zapisuje pliki w magazynie (status każdego pliku) i nie grupuje w kampanie; do analizy
służy „Nowa analiza”.

**Znane ograniczenia czystego stanu.**

1. Galeria twarzy i wzorce logo nie są dołączone. Bez nich dopasowanie twarzy do osób nazwanych w materiale i
   dopasowanie logo do wzorców są wyłączone: geny „nieznane”, nigdy kara. Bazę buduje się ze znanego zbioru
   w GUI ("Zbuduj galerię referencyjną z zaimportowanych materiałów") albo poleceniem `hydra reference build --from <folder>` (README, "Galeria referencyjna").
2. Migawki danych zewnętrznych (listy ostrzeżeń MISP, PSL, lista CERT Polska) są w osobnej paczce offline
   `hydra-reference-data-<data>.tar.zst` (4,3 MB, `scripts/reference_data.py`, SOURCE.md i licencje w środku). Bez
   niej nadpisania poziomów z list ostrzeżeń są wyłączone, działają listy wbudowane, a wynik pokazuje uwagę
   (`metrics.references`). Listy KNF i MF (hazard) nie są w paczce (warunki ponownego użycia niepotwierdzone);
   pobiera je tylko opcjonalny krok online `models`.
3. Duże zbiory: około 1000 incydentów wymaga około 32 GB RAM i ponad 30 minut.
4. Pozostałe ograniczenia: [KNOWN_LIMITATIONS.md](KNOWN_LIMITATIONS.md).

## 1. What to run (Docker, Linux x86_64; offline after the one-time steps)

| # | Command | Expected output | Measured time (Linux x86_64 server, compose defaults, `HYDRA_CPU_BUDGET=8`) |
|---|---|---|---|
| 1 | `mkdir -p ~/HydraNexus/{models,data/external,cache/linux-x86_64,cache/enrich,state-docker,exports}` and `docker pull ghcr.io/fotohubpl/hydra-nexus:1.0.1` (or `docker compose build api`) | image present; the build imports cv2/torch/mediapipe/onnxruntime/`hydra.api`, so a missing library fails at build time | build from source: 368 s, image 4.3 GB (1.01 GB compressed); needs network once |
| 2 | `docker compose --profile models run --rm models` | exit 0; offline verify prints `ok` for every key | 155 s; about 1.2 GB models + 61 MB reference data in that measurement; needs network once |
| 3 | `docker compose --profile cli run --rm cli analyse /hydra/data/<known-base>` (optionally after `cli reference build --from /hydra/data/<known-base>`, README "Galeria referencyjna") | campaigns, pairs and genes in the state DB; summary with campaign counts | synthetic 40 incidents: 58 s; 10 incidents with video: 441 s (274 s with `HYDRA_CPU_BUDGET=24`, 136 s warm cache) |
| 4 | `docker compose --profile cli run --rm cli assign-batch /hydra/data/<new-items> --out /hydra/exports/<name>` | `exports/<name>/assignments.csv` and `.json`: one row per new item | synthetic 22 items: 53 s (median 1.73 s per item) |
| 5 | `docker compose up -d api` (Linux) or `docker compose --profile gui-ports up -d gui` (Docker Desktop) | GUI on http://127.0.0.1:8000/, `/health` ok | about 3 s |

Shortcut with `just`: `just docker-models`, then `just docker-jury <known-base> <new-items> <name>` (paths relative to
`~/HydraNexus/data`). Without Docker: [docs/PORTABILITY.md](../PORTABILITY.md) §8 and INSTALL.md. Tested platforms:
Linux x86_64 Docker and native macOS arm64. Docker Desktop on macOS/Windows was not tested.

## 2. Where results and explanations appear

| What | Where |
|---|---|
| Decision per new item, campaign id, top candidate, evidence bits, Polish reasons, `new_pattern_id`, `duplicate_of` | `~/HydraNexus/exports/<name>/assignments.csv` / `.json`, `new_patterns.json` |
| Same assignments in the GUI | "Kampanie", incident card, "Pary i dowody", "Graf powiązań"; API `GET /api/v1/assignments` |
| Why two incidents are linked or not (per family, per gene, rarity, counter-arguments) | GUI pair view; API `GET /v1/pairs/{a}/{b}` |
| Campaign export | `GET /v1/campaigns/{id}/export` (JSON/CSV), STIX/XARF/Neo4j exports (`hydra/export_*.py`) |
| Evaluation harness (run on your own labelled data) | `just eval` (`eval/run.py`) |

## 3. Documents

| Document | Content |
|---|---|
| [docs/REQUIREMENTS_SUMMARY.md](../REQUIREMENTS_SUMMARY.md) | the challenge requirements in our own words |
| [docs/METHODOLOGY.md](../METHODOLOGY.md) | method: genes, rarity bits, levels (asset/template/campaign), assignment, abstention |
| [docs/METHOD_MAP.md](../METHOD_MAP.md) | which method lives in which module |
| [docs/DISCLOSURE.md](../DISCLOSURE.md) | external resources, models, datasets, pre-event assets, AI use |
| [docs/LICENSES.md](../LICENSES.md), [THIRD_PARTY_NOTICES.md](../../THIRD_PARTY_NOTICES.md) | licences of dependencies, models and vendored code |
| [docs/MODELS.md](../MODELS.md) | models and reference data: sources, revisions, sha256, download and offline transfer |
| [docs/PORTABILITY.md](../PORTABILITY.md) | runbook, hardware, measured timings |
| [docs/INTEGRATIONS.md](../INTEGRATIONS.md), [docs/API.md](../API.md), [docs/WATCH.md](../WATCH.md), [docs/NEO4J.md](../NEO4J.md), [docs/SECURITY_AUTH.md](../SECURITY_AUTH.md) | connectors and exports, API, live watch, graph read model, login |

## 4. Weryfikacja pakietu dowodowego (export integrity)

Every dataset export (`hydra export-dataset <run> <dir>`, `GET /v1/export/dataset`, UI "Eksport") carries
`SIGNED_MANIFEST.json` (canonical JSON: path, sha256 and size of every file, run id, product version and commit,
audit chain head) and `SIGNED_MANIFEST.sig` (Ed25519). The key is created per installation on first use in
`$HYDRA_STATE_DIR/keys/evidence_ed25519.pem` (0600); its public key is embedded in the manifest and its fingerprint is
shown in the graph view "Eksport przebiegu" panel and by `GET /v1/exports/signing-key`.

```bash
uv run hydra verify-export hydra-dataset-<run>.zip --fingerprint SHA256:<fingerprint shown in the UI>
# PASS: ... | pliki/files N | run <run> | klucz/key SHA256:...      exit 0
# change one byte in any file -> FAIL, "hash nie pasuje / sha256 mismatch: <file>"   exit 1
```

Also detected: missing or extra files, a manifest edited after signing, a signature from another package or key,
a key other than the pinned fingerprint, unsafe member names (`..`, absolute paths, backslashes, symlinks). Without
`--fingerprint` (or `--local-key` on the signing machine) the check proves the package is self-consistent, not who
signed it. The signature proves integrity since export, not the truth of the material. Tests:
`tests/core/test_evidence_sign.py`.

## 5. Optional modules (off by default, offline-safe)

1. Integracje & API (`/integrations`): every connector and webhook is off by default and the demo works fully
   offline. File exports (MISP, STIX 2.1, XARF, Splunk HEC JSON, Elasticsearch bulk, CSV, SQL, Cypher, signed evidence
   package) need no network. A live connector needs an explicit key, then `HYDRA_OFFLINE=0` and `HYDRA_ENRICH=1`, then
   Testuj/Włącz; offline it fails closed with a Polish message. Levels are shown as they are: `live` (12 connectors,
   tested only against fixtures written from the provider documentation, never against a production provider API),
   `export-format`, `planned` (descriptor only). Reference: docs/INTEGRATIONS.md, docs/API.md.
2. Watch na żywo (`/watch`): with `HYDRA_OFFLINE=1` the engine shows "Offline" and polls nothing. The offline replay
   ("Tryb demonstracyjny (odtworzenie)", or `uv run hydra watch replay demo --speed 600`) feeds a synthetic fixture
   (only `.example` / `.test` domains) through the same engine, labelled SYNTETYCZNE / ODTWORZENIE on screen. Live
   polling (CERT Polska, URLhaus with key, OpenPhish, crt.sh with manual keywords, Meta with token) needs
   `HYDRA_OFFLINE=0` plus explicit source enabling. Reference: docs/WATCH.md.
3. Automatic suspicious-ad search (bonus): `just demo-auto` starts a throwaway server with a synthetic corpus and its
   own temporary state.
