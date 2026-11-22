# Instalacja HYDRA NEXUS

Krótka wersja: [README.md](README.md). Modele i dane referencyjne: [docs/MODELS.md](docs/MODELS.md). Pomiary, z
których pochodzą wymagania sprzętowe: [docs/release/JURY_READINESS.md](docs/release/JURY_READINESS.md).

## 1. Sprzęt

Zmierzone na danych syntetycznych (`tests/fuzz/jury_corpus.py`, serwer Linux x86_64, budżet `HYDRA_CPU_BUDGET`):

| Zbiór | Minimum | Komfortowo | Zmierzone |
|---|---|---|---|
| do ~200 zgłoszeń | 4 rdzenie, 8 GB RAM | 8 rdzeni, 16 GB RAM | N=200: 7,2 min przy 4 rdzeniach (szczyt drzewa procesów 3,6 GB), 4,3 min przy 8 (6,4 GB; w Dockerze cgroup 4,85 GiB) |
| ~1000 zgłoszeń | 8 rdzeni, 32 GB RAM (szacunek) | 16 rdzeni, 64 GB RAM | N=1000 przy 8 rdzeniach: 37 min, szczyt 19,2 GB w jednym procesie |

Dysk: około 10 GB z Dockerem (obraz 4,3 GB po rozpakowaniu, modele i dane referencyjne 1,3 GB, stan około 1,5 ×
rozmiar danych wejściowych; N=200 to 226 MB stanu). GPU nie jest potrzebne. Sieć tylko raz: pobranie obrazu i modeli.

## 2. Docker (zalecane)

**Linux x86_64**, Docker Engine 24+ i Compose v2 (sprawdzone: Docker 29.10.0). Usługa `api` używa sieci hosta i
słucha tylko na `127.0.0.1`.

```bash
git clone https://github.com/FOTOhubpl/hydra-nexus-public && cd hydra-nexus-public
export HYDRA_IMAGE_TAG=1.0.0                     # albo zbuduj sam: docker compose build api
docker pull ghcr.io/fotohubpl/hydra-nexus:$HYDRA_IMAGE_TAG
mkdir -p ~/HydraNexus/{models,data/external,cache/linux-x86_64,cache/enrich,state-docker,exports}
docker compose --profile models run --rm models  # modele + CERT/PSL/MISP, sha256 + testy offline
docker compose up -d api
cat ~/HydraNexus/state-docker/ADMIN_CREDENTIALS.txt
```

**macOS / Windows (Docker Desktop)**: obraz jest `linux/amd64`; na Apple Silicon działa w emulacji (wolniej, zalecana
instalacja ze źródeł, §3). Bez sieci hosta GUI uruchamia usługa `gui`, która publikuje port tylko na `127.0.0.1`:

```bash
docker compose --profile gui-ports up -d gui      # zamiast "docker compose up -d api"
```

Profile `docker-compose.yml`: `api` (GUI + API, sieć hosta), `gui` (profil `gui-ports`), `models` (jednorazowe
pobranie), `cli` (analiza bez sieci, `network_mode: none`), `neo4j` (opcjonalny graf, [docs/NEO4J.md](docs/NEO4J.md)),
`bielik` (opcjonalny lokalny analityk AI), `preview` (podgląd UI). Uruchamiaj `api` albo `gui`, nie obie naraz.

Zmienne: `HYDRA_HOST_ROOT` (katalog na hoście, domyślnie `~/HydraNexus`), `HYDRA_API_PORT` (domyślnie 8000),
`HYDRA_CPU_BUDGET` (domyślnie 8; większa maszyna: `$(nproc)`), `HYDRA_ADMIN_PASSWORD` (hasło startowe admina),
`HYDRA_UID`/`HYDRA_GID` (właściciel katalogów, domyślnie 1000).

## 3. Ze źródeł

Wymagania: Python 3.11 przez [uv](https://docs.astral.sh/uv/) (uv sam pobierze 3.11), Node 22.12+ i pnpm 12.15
(`corepack enable` albo `npm i -g pnpm@12.15.0`), [just](https://just.systems/), `ffmpeg`, `tesseract` z językami
`pol` i `eng`, `zstd` (paczka referencyjna), `dig` (opcjonalne wzbogacanie DNS). Debian/Ubuntu:
`sudo apt install ffmpeg tesseract-ocr tesseract-ocr-pol tesseract-ocr-eng zstd dnsutils libgl1 libegl1 libgles2`;
macOS: `brew install uv node pnpm just ffmpeg tesseract tesseract-lang zstd`.

```bash
git clone https://github.com/FOTOhubpl/hydra-nexus-public && cd hydra-nexus-public
uv sync --locked
(cd ui && pnpm install --frozen-lockfile && pnpm build)
just models                     # = just models-fetch + just models-verify
just serve                      # http://127.0.0.1:8000 (API + ui/dist); tryb deweloperski UI: just demo
just check                      # ruff + pytest + typecheck/test UI
just jury-smoke                 # czysty stan, offline, dane syntetyczne: import, analiza, eksport, audyt, CLI
```

Testy wymagające próbki organizatora (`HYDRA_SAMPLE_TEST=1`) i brakujących modeli są pomijane (`skipped`), nie
zawodzą. Dane, modele, stan i pamięć podręczna leżą poza repozytorium w `~/HydraNexus` (`scripts/env.sh`,
zmienne `HYDRA_ROOT`, `HYDRA_DATA_DIR`, `HYDRA_MODELS_DIR`, `HYDRA_STATE_DIR`, `HYDRA_CACHE_DIR`).

## 4. Pierwsze uruchomienie i własne dane

1. Logowanie: użytkownik `admin`, hasło startowe w `ADMIN_CREDENTIALS.txt` w katalogu stanu (Docker:
   `~/HydraNexus/state-docker/`, ze źródeł: `~/HydraNexus/state/`) albo w logu (`docker compose logs api | grep -A3
   HYDRA`). Przy pierwszym logowaniu system wymusza zmianę hasła. Role i tokeny API: [docs/SECURITY_AUTH.md](docs/SECURITY_AUTH.md).
2. Dane: skopiuj zgłoszenia pod `~/HydraNexus/data/` (kontener widzi `/hydra/data`, tylko odczyt); jeden podfolder
   albo ZIP = jedno zgłoszenie. GUI: **Analizuj nowy materiał** (pliki/ZIP albo folder na serwerze).
3. Wyniki: Kampanie, Graf powiązań, Pary i dowody, karta zgłoszenia; eksporty w `~/HydraNexus/exports/`.
4. Dane demonstracyjne bez własnego zbioru: `just demo-auto` (serwer tymczasowy na danych syntetycznych,
   automatyczne wyszukiwanie reklam) albo `uv run python -m tests.fuzz.jury_corpus scale ~/HydraNexus/data/syntetyczne --n 50` (generator zbioru syntetycznego).

## 5. Rozwiązywanie problemów

| Objaw | Rozwiązanie |
|---|---|
| Nie znam hasła / zgubione hasło | `cat ~/HydraNexus/state-docker/ADMIN_CREDENTIALS.txt`; reset: `docker compose exec api hydra user reset-password admin` (ze źródeł: `uv run hydra user reset-password admin`) |
| Logowanie odrzucane po zmianie portu/hosta | otwieraj `http://127.0.0.1:<port>` albo `http://localhost:<port>`; nagłówki Host/Origin spoza loopback są odrzucane celowo |
| Port 8000 zajęty | `HYDRA_API_PORT=8200 docker compose up -d api` (albo `... --profile gui-ports up -d gui`) |
| GUI nieosiągalne na macOS/Windows | Docker Desktop bez sieci hosta: `docker compose --profile gui-ports up -d gui` |
| `Permission denied` w `state-docker`/`models` | utwórz katalogi przed startem (`mkdir -p` z §2) albo `HYDRA_UID=$(id -u) HYDRA_GID=$(id -g) docker compose ...` |
| Mało miejsca na dysku | obraz 4,3 GB + modele 1,3 GB; `docker system df`, usuń stare obrazy (`docker image prune`); stan przenieś `HYDRA_HOST_ROOT=/inny/dysk` |
| "API is running ... owns the database writer" | zatrzymaj GUI (`docker compose stop api`) przed `analyse`/`assign-batch` w profilu `cli` (SQLite ma jednego pisarza) |
| Brak modelu / błąd weryfikacji | cechy tego modelu mają status `unavailable`, analiza działa dalej; HYDRA nie tworzy zastępczych dowodów. `docker compose --profile models run --rm models` albo `just models-verify` wskaże plik z błędem sha256 |
| `cert-domains FAIL` w `models-verify` po instalacji paczki referencyjnej | oczekiwane: paczka podmieniła migawkę CERT pobraną w kroku `models`; dane paczki sprawdza `SHA256SUMS` przy instalacji ([docs/MODELS.md](docs/MODELS.md) §4). Pozostałe modele muszą mieć `ok` |
| "Brak danych referencyjnych offline" w GUI | zainstaluj paczkę referencyjną ([docs/MODELS.md](docs/MODELS.md) §4) albo uruchom usługę `models` |
| Analiza dużego zbioru "stoi" | licznik postępu może nie zmieniać się przez kilka minut w etapach kalibracji i zapisu; proces pracuje dalej ([docs/release/KNOWN_LIMITATIONS.md](docs/release/KNOWN_LIMITATIONS.md)) |

## 6. Funkcje wymagające kont zewnętrznych (opcjonalne, domyślnie wyłączone)

Rdzeń, GUI, eksporty, monitoring w trybie odtwarzania i demonstracje syntetyczne działają bez żadnych kont.

| Funkcja | Co jest potrzebne | Jak włączyć |
|---|---|---|
| Meta Ad Library (pobieranie reklam na żywo) | token Graph API z dostępem do Ad Library | Centrum integracji → Meta Ad Library API → Połącz, albo `META_AD_LIBRARY_TOKEN` w `~/.config/hydra/meta.env` (0600); potem `HYDRA_OFFLINE=0 HYDRA_WATCH_RT=1`. Bez tokenu zakładka pokazuje stan "wymaga tokenu" i demonstrację syntetyczną. [docs/integrations/META_COLLECTION.md](docs/integrations/META_COLLECTION.md) |
| Wzbogacanie na żywo (RDAP, DNS, crt.sh, ASN) | dostęp do sieci, bez kont | `HYDRA_ENRICH=1 HYDRA_OFFLINE=0 docker compose up -d api` |
| URLhaus (monitoring) | klucz `URLHAUS_AUTH_KEY` (abuse.ch) | Centrum integracji albo zmienna środowiskowa; [docs/WATCH.md](docs/WATCH.md) |
| Analityk AI, lokalny | model Bielik GGUF (~2,9 GB, Apache-2.0), bez kont | `just llm-fetch`, `docker compose --profile bielik up -d bielik`, `HYDRA_LLM=1 HYDRA_LLM_BACKEND=local` |
| Analityk AI w chmurze | konto AWS Bedrock albo klucz `ANTHROPIC_API_KEY` | `HYDRA_LLM=1 HYDRA_LLM_BACKEND=bedrock` albo `anthropic`; nigdy nie jest regułą łączenia ani warunkiem analizy |
| Graf Neo4j | lokalny Neo4j Community (bez konta) | `bash scripts/neo4j_password.sh`, `docker compose --profile neo4j up -d`; [docs/NEO4J.md](docs/NEO4J.md) |
| Konektory wychodzące (MISP, SIEM, webhooki) | adres i klucz własnej instancji | Centrum integracji; [docs/INTEGRATIONS.md](docs/INTEGRATIONS.md) |

## 6a. Publiczne demo za tunelem / reverse proxy

Domyślnie API odrzuca każdy nagłówek Host i Origin spoza `127.0.0.1`/`localhost`. Aby udostępnić demo przez tunel
albo reverse proxy (TLS kończony na proxy):

1. API nadal słucha tylko na `127.0.0.1` (usługa `api`, sieć hosta); tunel/proxy łączy się z `127.0.0.1:8000`.
2. Logowanie musi być włączone: `HYDRA_AUTH=1` (domyślnie). Bez niego `HYDRA_PUBLIC_HOSTS` jest ignorowane.
3. `HYDRA_PUBLIC_HOSTS=<host>` (lista nazw po przecinku, bez schematu i portu), np.
   `HYDRA_PUBLIC_HOSTS=demo.example.org docker compose up -d api`: te nazwy są dopuszczone w nagłówkach Host i Origin;
   zapis z innego pochodzenia nadal jest odrzucany (`hydra/api.py`, `tests/security/test_public_hosts.py`).
4. Ustaw silne hasło admina (`HYDRA_ADMIN_PASSWORD` albo wymuszona zmiana przy pierwszym logowaniu) i nie publikuj
   danych, których nie wolno pokazywać.

## 7. Reset do czystego stanu

```bash
docker compose down
mv ~/HydraNexus/state-docker ~/HydraNexus/state-docker.bak-$(date +%Y%m%d-%H%M%S)   # albo rm -rf, jeśli niepotrzebny
mkdir -p ~/HydraNexus/state-docker && docker compose up -d api                       # nowe hasło startowe w ADMIN_CREDENTIALS.txt
```

Ze źródeł: `just reset-state` (przenosi `$HYDRA_STATE_DIR` do kopii z datą, niczego nie kasuje). Modele
(`~/HydraNexus/models`) i dane referencyjne zostają; pamięć podręczna cech: `~/HydraNexus/cache/` (można usunąć).
Całkowite usunięcie: `docker compose down`, `docker image rm ghcr.io/fotohubpl/hydra-nexus:1.0.0`, `rm -rf ~/HydraNexus`.
