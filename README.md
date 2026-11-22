# HYDRA NEXUS: Kolekcjoner fejków

Narzędzie analityczne, które z pojedynczych zgłoszeń podejrzanych reklam (zrzuty ekranu, wideo, zapisane strony
docelowe, HAR, metadane platformy, adresy URL, tekst, eksporty czatów) wydobywa cechy wizualne, tekstowe,
infrastrukturalne, wizerunkowe i narracyjne, a następnie:

- wykrywa duplikaty i warianty tej samej reklamy,
- łączy zgłoszenia w kampanie i grupy podobnych kampanii,
- dla nowego materiału wskazuje znaną kampanię albo oznacza go jako **nowy wzorzec** (`assign` / `new_campaign` /
  `abstain`),
- przy każdym powiązaniu pokazuje powody: zgodne cechy, ich rzadkość w zbiorze (bity dowodowe) i miejsce w
  materiale (ramka obrazu, zakres znaków, kod czasu, wpis HAR, ścieżka DOM), z sumą sha256 pliku źródłowego.

Brak danych (niedostępna strona, brak HAR) oznacza "nieznane", a nie dowód przeciw powiązaniu. Rdzeń analizy działa
**offline**; opcjonalne wzbogacanie (RDAP, DNS, crt.sh, ASN) zapisuje każdą odpowiedź z datą i da się je odtworzyć bez
sieci. Projekt powstał podczas HackNation 2026 (21–22.11.2026); streszczenie wymagań wyzwania:
[docs/REQUIREMENTS_SUMMARY.md](docs/REQUIREMENTS_SUMMARY.md). Repozytorium nie zawiera danych organizatora ani wag
modeli.

## Szybki start (5 minut + pobranie modeli), Docker

Wymagania: Linux x86_64, Docker Engine 24+ z Compose v2, 8 rdzeni, 16 GB RAM, około 10 GB wolnego dysku (szczegóły i
macOS/Windows: [INSTALL.md](INSTALL.md)).

```bash
git clone https://github.com/FOTOhubpl/hydra-nexus-public && cd hydra-nexus-public
export HYDRA_IMAGE_TAG=1.0.0
docker pull ghcr.io/fotohubpl/hydra-nexus:$HYDRA_IMAGE_TAG          # obraz bez wag modeli (~1 GB do pobrania)

# katalogi na hoście (domyślnie ~/HydraNexus; inny katalog: export HYDRA_HOST_ROOT=/sciezka)
mkdir -p ~/HydraNexus/{models,data/external,cache/linux-x86_64,cache/enrich,state-docker,exports}

# modele i dane referencyjne: jedyny krok z siecią (przypięte rewizje + sha256, test offline każdego modelu)
docker compose --profile models run --rm models

docker compose up -d api                      # Linux; macOS/Windows (Docker Desktop): docker compose --profile gui-ports up -d gui
cat ~/HydraNexus/state-docker/ADMIN_CREDENTIALS.txt     # login admin + hasło startowe (albo: docker compose logs api | grep -A3 HYDRA)
```

Otwórz **http://127.0.0.1:8000**, zaloguj się jako `admin` hasłem z pliku (system wymusi zmianę hasła), potem
**Analizuj nowy materiał**: przeciągnij pliki/ZIP albo wskaż folder z `~/HydraNexus/data/` (w kontenerze
`/hydra/data/...`). Układ danych: jeden podfolder = jedno zgłoszenie; obsługiwane formaty i kody odrzuconych plików:
[docs/release/JURY_PACKAGE.md](docs/release/JURY_PACKAGE.md). Analiza z wiersza poleceń, bez sieci:

```bash
docker compose stop api                       # SQLite ma jednego pisarza
docker compose --profile cli run --rm cli analyse /hydra/data/<znane-zgloszenia>
docker compose --profile cli run --rm cli assign-batch /hydra/data/<nowe> --out /hydra/exports/wynik
docker compose up -d api                      # wyniki w GUI; pliki: ~/HydraNexus/exports/wynik/assignments.csv
```

Maszyna bez sieci: zamiast kroku `models` przenieś katalogi modeli i danych z innej maszyny albo zainstaluj paczkę danych
referencyjnych `hydra-reference-data-20261122.tar.zst` z wydania v1.0.0 ([docs/MODELS.md](docs/MODELS.md) §3–§4).

Budowa obrazu lokalnie zamiast `docker pull`: `docker compose build api` (ten sam Dockerfile co w GitHub Actions).

## Galeria referencyjna (twarze, logo)

HYDRA **nie dostarcza** galerii twarzy ani obrazów logo: wektory twarzy to dane biometryczne prawdziwych osób, a
logotypy są znakami towarowymi. Na czystej instalacji geny dopasowania do galerii (ID) i ORB-logo są więc
"nieznane" (nigdy kara), a wynik przebiegu pokazuje uwagę `metrics.references`. Bazę buduje użytkownik, lokalnie, z
własnych materiałów:

1. Zaimportuj materiały (Pulpit → "Nowa analiza" albo `hydra analyse <folder>`).
2. Zbuduj bazę: przycisk "Zbuduj galerię referencyjną z zaimportowanych materiałów" (Pulpit, notatka w wyniku
   analizy, Ustawienia → "Baza referencyjna"; rola administrator), albo `hydra reference build [--from-state |
   --from DIR] [--faces] [--logos]` (Docker: `docker compose --profile cli run --rm cli reference build --from
   /hydra/data/<folder>`); API `POST /v1/reference/build`, `GET /v1/reference/status` (liczby wpisów, bez wektorów
   i nazwisk).
3. "Przelicz analizę" (`POST /v1/reference/reanalyse` albo ponownie `hydra analyse`): z pamięci podręcznej genów
   ponownie liczone są tylko ekstraktory `ident` i `logo`.

Twarz trafia do galerii tylko wtedy, gdy obraz ma dokładnie jedną twarz po bramce jakości, a sam materiał podaje imię
i nazwisko (alt, title, podpis, nazwa pliku); wzorzec logo to obraz o identycznej treści w co najmniej 2 incydentach,
nazwany w nazwie pliku lub tekście alt/title (`hydra/reference_build.py`). Nazwy pochodzą wyłącznie z materiałów, bez
wyszukiwania w sieci. Baza powstaje w `$HYDRA_VIS_REFERENCE_DIR` albo `<state>/reference`, poza repozytorium;
usunięcie katalogu wyłącza dopasowania.

## Wyniki ewaluacji

Przebieg `eval-20261122T065957` na kodzie produktu `f450a70` (status PASS, 0 awarii). Szczegółowy raport ewaluacji
nie jest publikowany, bo opisuje materiał organizatora; poniżej tylko liczby zagregowane. REAL = 10 zgłoszeń z próbki
organizatora (wyniki in-sample: progi ustawiono przy analizie tych zgłoszeń, etykiety to hipoteza zespołu);
SYNTHETIC = wygenerowane warianty tych zgłoszeń i publiczne dystraktory; [a, b] = przedział Wilsona 95%.

| Sprawdzenie | REAL (10) | REAL + 45 SYNTHETIC |
|---|---|---|
| Grupowanie w kampanie: F1 par / fałszywe scalenia | 1.000 / 0 z 41 | 1.000 / 0 z 1361 |
| Nowy materiał, przypisanie leave-one-incident-out | 1.000 [0.72, 1.00] (n = 10) | 1.000 [0.93, 1.00] (n = 55) |
| Odłożone kampanie odrzucone jako nowy wzorzec | 1.000 [0.72, 1.00] (n = 10, w tym 5 pojedynczych) | 1.000 [0.91, 1.00] (n = 40) |
| Odtworzenie w kolejności czasu (rzadkość tylko z przeszłości) | **0.667** [0.35, 0.88] (n = 9) | n/d |
| Symulacja zbioru ukrytego (SYNTHETIC): trafne przypisania / odrzucone obce | n/d | 0.983 / 1.000 (119 pozytywnych, 44 negatywne) |
| Wyszukiwanie przy 231 zgłoszeniach (głównie SYNTHETIC): recall kandydatów / F1 kampanii / F1 grup | n/d | 0.956 / 0.992 / **0.453** (13 901 fałszywych scaleń grup) |

Znane ograniczenia tych liczb ([docs/release/KNOWN_LIMITATIONS.md](docs/release/KNOWN_LIMITATIONS.md)):

- Dziesięć zgłoszeń pokazuje, że potok działa od początku do końca, a nie że uogólnia; prawdziwą miarą jest zbiór
  ukryty organizatora.
- Odtworzenie w kolejności czasu daje 0.667: przy rzadkości liczonej tylko z wcześniejszych zgłoszeń wynik jest
  wyraźnie słabszy niż leave-one-out (1.000).
- Grupy kampanii scalają się nadmiernie przy 231 zgłoszeniach (F1 grup 0.453 przy F1 kampanii 0.992). Opcja
  `HYDRA_ADV_GROUPS=1` (tylko potwierdzone mosty, domyślnie wyłączona) daje zmierzone F1 grup 0.848 przy identycznym
  podziale na kampanie. Grupa to wspólny zestaw lub szablon, nie atrybucja.
- Ewaluacja na zamrożonym podziale odłożonym (G1): przygotowano podział i bazowe reguły, ale przebieg HYDRY na tym
  podziale **nie został zweryfikowany**; nie podajemy z niego liczb.
- Bity to wagi dowodowe, nie prawdopodobieństwa; brak kalibracji na odłożonych parach.

## Instalacja ze źródeł

Python 3.11 przez [uv](https://docs.astral.sh/uv/), Node 22.12+ z pnpm 12, [just](https://just.systems/), systemowe
`ffmpeg` i `tesseract` (z językiem `pol`). Pełna lista: [INSTALL.md](INSTALL.md).

```bash
uv sync --locked                      # środowisko Pythona z uv.lock
(cd ui && pnpm install --frozen-lockfile && pnpm build)
just models                           # pobranie + weryfikacja modeli (jedyny krok z siecią)
just serve                            # API + zbudowane GUI na http://127.0.0.1:8000 (tylko loopback)
just check                            # ruff + pytest + typecheck i testy UI
just jury-smoke                       # ścieżka jury od czystego stanu, offline, na danych syntetycznych
```

## Praca offline

Domyślnie `HYDRA_OFFLINE=1`: proces blokuje połączenia poza loopback, modele ładują się tylko z dysku, a
wzbogacanie odtwarza zapisane odpowiedzi. Sieć jest potrzebna raz: do pobrania obrazu i modeli. Przeniesienie na
maszynę bez sieci (kopiowanie modeli, `docker save`/`docker load`, weryfikacja sha256):
[docs/MODELS.md](docs/MODELS.md) §3. Moduły wymagające kont zewnętrznych (token Meta Ad Library, klucze dostawców)
są wyłączone i opcjonalne: [INSTALL.md](INSTALL.md) §6.

## Modele i dane referencyjne

Wagi nie są w repozytorium ani w obrazie. Tabela 12 przypiętych pozycji (rewizja, licencja, rozmiar, sha256),
polecenia pobrania, przeniesienie offline i paczka danych referencyjnych: [docs/MODELS.md](docs/MODELS.md). HYDRA nie
trenuje wag sieci neuronowych; własne artefakty zespołu (haszowany rozkład częstości tła w
`hydra/priors/background_df.json.gz`, leksykony PL, ręcznie ustawione progi) opisuje ten sam dokument, §5.

## Weryfikacja podpisanego eksportu

Eksport zbioru danych (`hydra export-dataset`, w GUI: Eksport) zawiera `SIGNED_MANIFEST.json` i podpis Ed25519
(klucz instalacji w `$HYDRA_STATE_DIR/keys`). Sprawdzenie offline, PASS/FAIL dla podpisu, każdej sumy sha256 oraz
plików brakujących i nadmiarowych:

```bash
uv run hydra verify-export <dataset.zip|folder>         # ze źródeł
docker compose --profile cli run --rm cli export-dataset latest /hydra/exports/ds    # w Dockerze: eksport...
docker compose --profile cli run --rm cli verify-export /hydra/exports/ds           # ...i weryfikacja
```

API: `POST /v1/exports/verify`. Opis: [docs/INTEGRATIONS.md](docs/INTEGRATIONS.md), [docs/API.md](docs/API.md).

## Dokumentacja

| Temat | Dokument |
|---|---|
| Instalacja, sprzęt, rozwiązywanie problemów, reset | [INSTALL.md](INSTALL.md) |
| Modele i dane referencyjne | [docs/MODELS.md](docs/MODELS.md) |
| Metodyka, mapa metod | [docs/METHODOLOGY.md](docs/METHODOLOGY.md), [docs/METHOD_MAP.md](docs/METHOD_MAP.md) |
| Pakiet dla jury, pomiary, ograniczenia | [docs/release/JURY_PACKAGE.md](docs/release/JURY_PACKAGE.md), [docs/release/JURY_READINESS.md](docs/release/JURY_READINESS.md), [docs/release/KNOWN_LIMITATIONS.md](docs/release/KNOWN_LIMITATIONS.md) |
| API, integracje, Neo4j, monitoring | [docs/API.md](docs/API.md), [docs/INTEGRATIONS.md](docs/INTEGRATIONS.md), [docs/NEO4J.md](docs/NEO4J.md), [docs/WATCH.md](docs/WATCH.md) |
| Logowanie i role | [docs/SECURITY_AUTH.md](docs/SECURITY_AUTH.md) |
| Źródła danych, licencje, ujawnienia | [docs/DATA_SOURCES.md](docs/DATA_SOURCES.md), [docs/LICENSES.md](docs/LICENSES.md), [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md), [docs/DISCLOSURE.md](docs/DISCLOSURE.md) |
| Architektura, decyzje | [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md), [docs/adr/](docs/adr/) |

## Licencja

Udostępnione do oceny w HackNation 2026. Wszelkie prawa zastrzeżone. Komponenty zewnętrzne zachowują swoje licencje
([THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md), szczegóły: [docs/LICENSES.md](docs/LICENSES.md)).

English: published for evaluation in HackNation 2026. All rights reserved. Third-party components keep their own
licences.

---

## English

HYDRA NEXUS links scam-ad incidents (screenshots, videos, saved landing pages, HAR, platform metadata, URLs, text,
chat exports) into campaigns. It extracts visual, text, infrastructure, likeness and narrative features, detects
duplicates and variants, groups incidents into campaigns, assigns new material to a known campaign or flags a new
pattern, and explains every link with matching features, their rarity (evidence bits) and their location in the
material. The core runs offline; the repository contains no organiser data and no model weights.

Quickstart (Linux x86_64, Docker): clone this repository, `docker pull ghcr.io/fotohubpl/hydra-nexus:1.0.0`, set
`HYDRA_IMAGE_TAG=1.0.0`, create `~/HydraNexus/{models,data/external,cache/linux-x86_64,cache/enrich,state-docker,exports}`,
run `docker compose --profile models run --rm models` once (pinned weights, sha256-verified), then
`docker compose up -d api` (Docker Desktop: `docker compose --profile gui-ports up -d gui`). The first admin password
is in `~/HydraNexus/state-docker/ADMIN_CREDENTIALS.txt`; open http://127.0.0.1:8000 and import your data under
"Analizuj nowy materiał". From source: `uv sync --locked`, `cd ui && pnpm install --frozen-lockfile && pnpm build`,
`just models`, `just serve`; quality gate `just check`; clean-state offline smoke `just jury-smoke`. Signed exports:
`hydra verify-export <zip>`. Details: [INSTALL.md](INSTALL.md), [docs/MODELS.md](docs/MODELS.md),
[docs/METHODOLOGY.md](docs/METHODOLOGY.md).
