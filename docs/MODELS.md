# Modele i dane referencyjne

Repozytorium i obraz Dockera **nie zawierają wag modeli**. Wszystkie modele to publiczne, wstępnie wytrenowane
punkty kontrolne przypięte do rewizji i sum sha256 w [`models.lock.json`](../models.lock.json). Pobiera się je
jednorazowo (jedyny krok wymagający sieci); potem system działa offline.

## 1. Co jest pobierane

Źródło tabeli: `models.lock.json` (12 pozycji, łącznie 2 369 813 089 B). Domyślnie pobieranych jest 11 pozycji
(1 235 458 054 B); `e5-base` (1,13 GB) jest opcjonalny (`--with-base`). Pełne sumy sha256 każdego pliku są w
`models.lock.json` (`entries.<id>.files[].sha256`).

| id | źródło (upstream) | przypięta rewizja | licencja | rozmiar [B] | pliki | do czego |
|---|---|---|---|---|---|---|
| `dinov2-small` | [facebook/dinov2-small](https://huggingface.co/facebook/dinov2-small) | `ed25f3a31f01632728cabb09d1542f84ab7b0056` | Apache-2.0 | 88 250 943 | 3 | osadzenia wizualne (podobieństwo kreacji, zrzutów stron) |
| `e5-small` | [intfloat/multilingual-e5-small](https://huggingface.co/intfloat/multilingual-e5-small) | `614241f622f53c4eeff9890bdc4f31cfecc418b3` | MIT | 492 795 290 | 9 | osadzenia tekstu (wielojęzyczne) |
| `e5-base` (opcjonalny) | [intfloat/multilingual-e5-base](https://huggingface.co/intfloat/multilingual-e5-base) | `d128750597153bb5987e10b1c3493a34e5a4502a` | MIT | 1 134 355 035 | 9 | większy wariant osadzeń tekstu |
| `yunet` | [opencv/face_detection_yunet](https://huggingface.co/opencv/face_detection_yunet) | `3cc26e7f1014a5ee5d74a42acee58bafc9d0a310` | MIT | 233 674 | 2 | wykrywanie twarzy |
| `sface` | [opencv/face_recognition_sface](https://huggingface.co/opencv/face_recognition_sface) | `3d7082438a6e4551e840c9b2bb60b71e8da4b524` | Apache-2.0 | 38 707 711 | 2 | porównanie wizerunku |
| `faster-whisper-small` | [Systran/faster-whisper-small](https://huggingface.co/Systran/faster-whisper-small) | `536b0662742c02347bc0e980a01041f333bce120` | MIT | 486 212 372 | 4 | transkrypcja mowy (CTranslate2, CPU int8) |
| `mediapipe-face-landmarker` | [MediaPipe face_landmarker](https://storage.googleapis.com/mediapipe-models/face_landmarker) | ścieżka wersji `float16/1` + sha256 | Apache-2.0 | 3 758 596 | 1 | punkty twarzy (synchronizacja ust w wideo) |
| `audioseal-detector` | [facebook/audioseal](https://huggingface.co/facebook/audioseal) | `3c19eba53390776cf2cc9ed5f6c9ac67ce72ecba` | MIT | 34 593 512 | 1 | wykrywanie znaku wodnego AudioSeal (konwersja do safetensors, sha256 źródła i wyniku w locku) |
| `rapidocr` | [RapidAI/RapidOCR](https://www.modelscope.cn/models/RapidAI/RapidOCR) | `v3.10.0` (PP-OCRv5) | Apache-2.0 | 13 742 597 | 3 | OCR |
| `rapidocr-v6` | [RapidAI/RapidOCR](https://www.modelscope.cn/models/RapidAI/RapidOCR) | `v3.10.0` (PP-OCRv6) | Apache-2.0 | 31 749 509 | 3 | OCR (nowsza wersja) |
| `cert-domains` (dane) | [CERT Polska, lista ostrzeżeń](https://hole.cert.pl/domains/v2/domains.json) | migawka 2026-11-20T18:58:20Z | dowolne użycie, bez sugerowania poparcia CERT Polska | 45 070 965 | 3 | znane domeny oszukańcze |
| `psl` (dane) | [publicsuffix/list](https://github.com/publicsuffix/list) | `d7d7f1fd55166aaf73ad2f526e72634490acd9c8` | MPL-2.0 (plik niezmieniony) | 342 885 | 1 | domena rejestrowalna (eTLD+1) |

Zasady pobierania (`scripts/fetch_models.py`): tylko przypięte rewizje; tylko safetensors/ONNX/TFLite/CTranslate2 i
pliki konfiguracyjne/tokenizera (nigdy pickle `.bin`/`.pt`); bez `trust_remote_code`. Weryfikacja liczy ponownie
sha256 każdego pliku i uruchamia jeden test dymny na model z blokadą gniazd sieciowych (próba pobrania = błąd).
Licencje bibliotek i usług: [LICENSES.md](LICENSES.md), [../THIRD_PARTY_NOTICES.md](../THIRD_PARTY_NOTICES.md).

Uwaga: pakiet `rapidocr` z PyPI zawiera własne domyślne pliki ONNX (Apache-2.0), więc są one obecne w środowisku
Pythona i w obrazie jako część biblioteki; `rapidocr-v6` jest identyczny z plikami z koła (wheel).

## 2. Pobranie i weryfikacja

**Docker** (usługa `models`, profil `models` w `docker-compose.yml`):

```bash
mkdir -p ~/HydraNexus/{models,data/external,cache/linux-x86_64,cache/enrich,state-docker,exports}
docker compose --profile models run --rm models                        # pobranie 11 pozycji + weryfikacja sha256 + testy offline
HYDRA_FETCH_ARGS=--with-base docker compose --profile models run --rm models   # to samo z opcjonalnym e5-base
```

Usługa zapisuje wagi do `~/HydraNexus/models`, dane referencyjne do `~/HydraNexus/data/external`, a kopię locka do
`~/HydraNexus/models/models.lock.json`; na końcu uruchamia `fetch_models.py --verify` na tej kopii.

**Ze źródeł** (uv):

```bash
just models-fetch     # HYDRA_OFFLINE=0: pobranie (idempotentne; pliki już obecne są tylko przeliczane)
just models-verify    # offline: sha256 + test dymny każdego modelu
just models           # oba kroki
```

**Zgodność z przypiętymi sumami z repozytorium.** Krok pobierania zapisuje sumy faktycznie pobranych plików.
Porównanie z wersją w repozytorium (powinna różnić się tylko pozycja `cert-domains`, bo lista CERT jest żywa, chyba
że zainstalowano paczkę referencyjną z sekcji 4; `e5-base` jest pomijany, jeśli go nie pobrano):

```bash
python3 - models.lock.json ~/HydraNexus/models/models.lock.json <<'EOF'
import json, sys
a, b = (json.load(open(p))["entries"] for p in sys.argv[1:3])
for k, e in a.items():
    got = {f["path"]: f["sha256"] for f in b.get(k, {}).get("files", [])}
    bad = [f["path"] for f in e["files"] if got.get(f["path"]) != f["sha256"]]
    print(f"{k:28s}", "brak" if k not in b else ("OK" if not bad else f"RÓŻNICA {bad}"))
EOF
```

## 3. Przeniesienie na maszynę bez sieci (air-gap)

1. Na maszynie z siecią wykonaj krok z sekcji 2 (Docker albo `just models-fetch`).
2. Skopiuj katalogi `~/HydraNexus/models/`, `~/HydraNexus/data/external/` i `~/HydraNexus/cache/<platforma>/tldextract/`
   (np. `tar -C ~/HydraNexus -cf hydra-models.tar models data/external cache`), na docelowej maszynie rozpakuj pod
   `~/HydraNexus` (albo pod `$HYDRA_HOST_ROOT`). Obraz przenieś przez `docker save ghcr.io/fotohubpl/hydra-nexus:1.0.1 |
   gzip > hydra.tar.gz` i `docker load < hydra.tar.gz`.
3. Na maszynie docelowej, bez sieci: `just models-verify` albo
   `docker compose --profile models run --rm --entrypoint python models scripts/fetch_models.py --verify --lock /hydra/models/models.lock.json`
   (sha256 każdego pliku + testy dymne z blokadą gniazd; usługa `models` montuje katalog modeli do zapisu, bo
   weryfikacja zapisuje tam raport `verify-<platforma>.json`). Dodatkowo porównanie z repozytorium z sekcji 2.

## 4. Paczka danych referencyjnych offline

`hydra-reference-data-<data>.tar.zst` (około 4,3 MB) zawiera listy ostrzeżeń MISP (`misp-warninglists`, CC0-1.0,
commit `40e2b570fda2e5443a2d6fa1d1db16c37ab0a956`; listy X4BNet w środku: MIT), Public Suffix List (MPL-2.0, plik
niezmieniony z tekstem licencji) i migawkę listy ostrzeżeń CERT Polska. Nie zawiera list KNF ani rejestru
hazardowego MF (warunki ponownego użycia niezweryfikowane; KNF może wymieniać osoby fizyczne).

- Publikacja: plik jest planowany jako załącznik (GitHub Release asset) wydania `v1.0.0`; zbudowany poleceniem
  `just reference-data-build` (`scripts/reference_data.py build`). Każdy zestaw ma `SOURCE.md` i `SHA256SUMS`,
  korzeń paczki `MANIFEST.json`.
- Instalacja: `just reference-data-install hydra-reference-data-<data>.tar.zst` (rozpakowuje do
  `$HYDRA_DATA_DIR/external`, sprawdza każdą sumę z `SHA256SUMS`, odrzuca niebezpieczne wpisy tar); stan:
  `uv run python scripts/reference_data.py status`. Bez Pythona: `mkdir -p ~/HydraNexus/data/external && tar --zstd -xf
  hydra-reference-data-<data>.tar.zst -C ~/HydraNexus/data/external`, potem `sha256sum -c SHA256SUMS` w każdym
  katalogu.
- Alternatywa online (jednorazowo, z siecią): usługa `models` (CERT + PSL + MISP przez
  `scripts/fetch_misp_warninglists.py --commit 40e2b570fda2e5443a2d6fa1d1db16c37ab0a956`), opcjonalnie
  `scripts/fetch_public_warnings.py` (KNF, MF; poza paczką).

Kiedy używać paczki: na maszynie, która nie może wykonać kroku `models` z sekcji 2 w części danych referencyjnych
(albo dostała same modele). Na maszynie, na której krok `models` już pobrał CERT/PSL/MISP, paczka nie jest potrzebna.
Instalacja paczki **po** kroku `models` podmienia migawkę CERT na tę z paczki; wtedy `models-verify` zgłasza
`cert-domains FAIL` (inna migawka niż zapisana w locku przy pobieraniu), a ponowne uruchomienie usługi `models`
kończy się błędem `cert-domains` (zmierzone 2026-11-22). Rozstrzygająca dla danych z paczki jest kontrola
`SHA256SUMS` wykonywana przez `install`; pozostałe modele weryfikują się normalnie.

Bez paczki system działa, ale nadpisania poziomów z list MISP są wyłączone, a w GUI widać notkę o braku danych
referencyjnych.

## 5. Czego HYDRA nie trenuje, a co zostało przygotowane przez zespół

HYDRA **nie trenuje żadnych wag sieci neuronowych**. Wszystkie modele z sekcji 1 to publiczne punkty kontrolne,
używane bez dostrajania. Własne elementy zespołu:

| element | gdzie | opis |
|---|---|---|
| (a) rozkład częstości tła | `hydra/priors/background_df.json.gz` (w repozytorium, nic do pobierania) | liczby dokumentów na haszowany klucz `sha256(rodzaj + wartość)[:16]`; wartości nie są zapisywane. Zbudowany ze źródeł publicznych oznaczonych jako dopuszczone do dystrybucji (lista w [DISCLOSURE.md](DISCLOSURE.md)), nigdy z danych organizatora; przełącznik `HYDRA_BACKGROUND_PRIOR` (`mixed` domyślnie, `0` wyłącza) |
| (b) polskie leksykony i teksty wzorcowe | `hydra/genome/lexicons/*.json`, kod w `hydra/genome/` | schematy oszustw, frazy-przynęty, marki, osoby publiczne z publicznych źródeł (każdy wpis z URL źródła) |
| (c) progi | kod (`hydra/`), opis w [METHODOLOGY.md](METHODOLOGY.md) | ustawione ręcznie heurystyki (m.in. próg powiązania 10 bitów, zestawu 6); dobrane na 10-incydentowej próbce, więc wyniki na tej próbce są in-sample ([release/KNOWN_LIMITATIONS.md](release/KNOWN_LIMITATIONS.md)) |
| (d) fuzja LR + izotoniczna | `hydra fit-fusion` / `analyse --fusion` | opcjonalna, uruchamiana jawnie na własnych danych z etykietami; nie jest dostarczana ani używana domyślnie |

## 6. Brak modelu

Jeśli modelu brakuje albo nie przeszedł weryfikacji, odpowiadające mu cechy mają status `unavailable`, a analiza
działa dalej na pozostałych cechach. System nigdy nie podstawia wyników zastępczych ani nie tworzy dowodu bez
modelu. `/health` i strona "Status" pokazują listę modeli; `just models-verify` wskazuje plik z błędem.

---

## English summary

No model weights are in the repository or the Docker image. All models are public pretrained checkpoints pinned by
revision and per-file sha256 in `models.lock.json` (12 entries, 2,369,813,089 bytes; 11 fetched by default, `e5-base`
optional via `--with-base`). HYDRA trains no neural weights. Fetch once with network:
`docker compose --profile models run --rm models` (Docker) or `just models-fetch && just models-verify` (source);
verification re-hashes every file and runs one offline smoke test per model under a socket guard. For an air-gapped
machine copy `~/HydraNexus/models`, `data/external` and the tldextract cache, then run `just models-verify`. The
offline reference-data bundle (MISP warninglists CC0 @40e2b570, PSL MPL-2.0, CERT Polska snapshot; no KNF/MF lists) is
planned as a release asset of v1.0.0 (`just reference-data-build`, install with `just reference-data-install <tar>`,
checked against `SHA256SUMS`). Team-made artefacts: the hashed background frequency prior
`hydra/priors/background_df.json.gz` (public sources only, shipped in the repository), Polish lexicons in code,
hand-set heuristic thresholds tuned on the 10-incident sample, and an optional LR fusion that is not shipped or used by
default. A missing model makes its features `unavailable`; it never produces substitute evidence.
