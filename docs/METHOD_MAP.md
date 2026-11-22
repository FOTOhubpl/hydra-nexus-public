# Mapa metod HYDRA NEXUS (pkt 5 zadania NASK)

Stan: 2026-11-22, gałąź `docs/method-map` z  (`e2cd22f`). Dokument dla jury, po polsku.

Zadanie NASK zostawia otwarte: "Zespół może wykorzystać metody regułowe, podobieństwo ważone, modele uczenia
maszynowego, embeddingi, silniki klastrujące, grafy zależności lub inne podejścia. Sposób reprezentacji i wizualizacji
relacji również jest dowolny, jednym z możliwych kierunków jest grafowa baza danych, np. Neo4j." Poniżej każde z tych
podejść jest przypisane do kodu HYDRA: plik i funkcja, czy działa domyślnie, oraz liczba zmierzona tam, gdzie
pomiar istnieje (ze źródłem). Brak liczby oznacza, że nie ma pomiaru; nic nie jest szacowane.

Oznaczenia: **domyślnie** = działa przy `hydra analyse` bez flag; **opt-in** = wymaga flagi lub pliku modelu.
Bity to heurystyka rzadkości (log2), nie prawdopodobieństwo.

Główne źródło liczb: przebieg ewaluacji **`eval-20261122T032044`** na commicie **`0fe34d8`**
(`eval/run.py`; tabela wyników nie jest publikowana, bo opisuje dane organizatora; tu tylko agregaty). Korpusy: `sample` = 10 incydentów NASK (etykiety to hipoteza zespołu, liczby in-sample), `sample+syn`
= sample + 45 wariantów syntetycznych (`eval/variants.py`). Żadna liczba nie jest estymatą wyniku na zbiorze ukrytym.

## 1. Metody regułowe

| Element | Kod | Domyślnie | Pomiar |
|---|---|---|---|
| Poziomy dowodu (tiers): operator, platform, kit, asset, persona, phrase, boilerplate, label | `hydra/synapse.py`: `TIERS`, `gene_tier`, `is_kit`, `level`; tabele `hydra/genome/webparse.py` (`TIERS`, `dependency_group`), `hydra/genome/txt.py` (`TIER_BY_KIND`, `dep_group`) | tak | ablacja `no_tiers`: F1 kampanii 1.000 → **0.427**, 333 fałszywe scalenia (sekcja 9) |
| Reguła dwóch rodzin / operatora: link kampanii tylko gdy wsparcie >= próg i (bity operatora >= 8 albo >= 2 niezależne silne rodziny) | `hydra/synapse.py:score_pair` (`OPERATOR_STRONG_BITS = 8`, `STRONG_FAMILY_BITS = 8`) | tak | część mechanizmu `no_tiers` (brak osobnej ablacji) |
| Ostrzegawcze listy MISP obniżają poziom wspólnej infrastruktury (CDN, chmura) | `hydra/genome/webparse.py:_apply_warninglist` | tak | brak osobnej liczby |
| Reguła decyzji przypisania: `assign` gdy najlepsza kampania ma >= 10 bitów i przewagę >= 3 nad drugą; inaczej `abstain` (szara strefa >= 6 bitów) albo `new_campaign` | `hydra/assign.py:_decide` (`MARGIN 3.0`, `ABSTAIN_MIN 6.0`), progi `hydra/synapse.py:Thresholds` (link 10, kit 6) | tak | LOO `sample+syn` 1.000 [0.93, 1.00] (n=55), odrzucenie nowych 1.000 (n=15); `sample` LOO 1.000 (n=10) |
| Analityk jako reguła: must-link / cannot-link; przypisanie, które łączy cannot-link, wstrzymuje się | `hydra/assign.py:bridge_abstain`, `hydra/review.py` | tak | brak liczby |
| Opis reguł wejścia (URL, zrzut, plik) i otwartego zbioru | `docs/METHODOLOGY.md` §9 | dokumentacja | czasy ścieżek wejścia (syntetyczne) w §9 |

## 2. Podobieństwo ważone

| Element | Kod | Domyślnie | Pomiar |
|---|---|---|---|
| Bity rzadkości: u = (df + A·u0)/(N + A), bity = clip(log2(m/u), 0, 14); reguły hubów i rozproszenia | `hydra/entropy.py:Rarity` (`discrete_bits`, `u_estimate`), `build_rarity` | tak | ablacja `no_rarity` (stałe 4 bity): F1 1.000 → **0.721**, 96 fałszywych scaleń, LOO 0.836 |
| Limity rodzin (VIS 8, TXT 8, ID 6, INF/META 24, NAR 0), `GROUP_CAP 14`, `REST_WEIGHT 0.25`, kara sprzeczności -4 | `hydra/synapse.py` (`FAMILY_CAMPAIGN_CAP`, `FAMILY_CAP`, `GROUP_CAP`) | tak | ablacja `no_caps`: bez zmian na `sample+syn` (F1 1.000), czyli zabezpieczenie, nie źródło wyniku na tym korpusie |
| Wynik pary: suma bitów po rodzinach, poziomy, limity, sprzeczności, hipoteza (same_campaign / shared_template / shared_asset / independent) | `hydra/synapse.py:score_pair` (model `rarity_bits@1`) | tak | drabina bazowa B2 poniżej |
| Tło prior z korpusów publicznych | `hydra/priors/__init__.py` (`HYDRA_BACKGROUND_PRIOR`, domyślnie `mixed`) | tak | corpus-demo: linki do legalnych stron 426 → 270 (`docs/METHODOLOGY.md`, internal notes, not published) |

Drabina bazowa na `sample+syn` (te same cechy, ten sam budżet strojenia, próg z `syn-dev`; run `eval-20261122T032044`, §B2):

| Metoda | F1 kampanii | Fałszywe scalenia | LOO |
|---|---|---|---|
| B0 pHash | 0.907 | 0 | 0.946 |
| B1 tekst (e5) | 0.979 | 0 | 0.964 |
| B2 suma naiwna | 0.891 | 24 | 0.909 |
| B3 bity rzadkości bez poziomów i limitów | 0.616 | 123 | 0.673 |
| LR_fusion (regresja logistyczna) | 0.683 | 88 | 0.782 |
| **HYDRA: bity + poziomy + limity (próg domyślny 10)** | **1.000** | **0** | **1.000** |

## 3. Modele uczenia maszynowego

Wszystkie modele działają lokalnie (offline, `HYDRA_OFFLINE=1` domyślnie), ładują się leniwie; bez wag ekstraktor
zgłasza `ModelUnavailable` i gen jest "nieobserwowany", nie karą. Wersje i sumy: `docs/MODEL_REGISTRY.md`.

| Model | Kod | Domyślnie | Rola |
|---|---|---|---|
| DINOv2-small (obraz) | `hydra/ml/models.py:load_image_embedder`, `hydra/genome/vis.py:get_embedder` | tak, gdy wagi są | embedding wizualny -> bity (sekcja 4) |
| e5-small (tekst) | `hydra/ml/models.py:load_text_embedder`, `hydra/genome/txt.py:_embedder` | tak | embedding tekstu -> bity, limit 2 bity |
| RapidOCR | `hydra/ml/ocr.py:ocr`, `hydra/genome/ocr_pool.py:ocr_file` | tak | tekst ze zrzutów i klatek |
| faster-whisper | `hydra/genome/aud.py:_asr_model` (ekstraktor `aud_asr`) | tak | transkrypcja nagrań |
| YuNet + SFace (twarze) | `hydra/genome/ident.py` (`cv2.FaceDetectorYN`, `FaceRecognizerSF`) | tak | gen persony; link dopiero przy cos >= 0.60 (`FACE_MIN_COS`) |
| AudioSeal (znak wodny audio) | `hydra/genome/dfk.py:audioseal_profile` (ekstraktor `dfk_audio_wm`) | tak, rodzina META, 0 bitów | informacja, nie dowód powiązania; obraz/wideo opt-in `HYDRA_DFK_IMAGE=1` / `HYDRA_DFK_VIDEO=1` |
| Fuzja LR + izotoniczna | `hydra/synapse.py:FusionModel` (`lr_isotonic@1`), `hydra/train.py:train_fusion` / `load_fusion` | **opt-in** (`hydra train`, potem `hydra analyse --fusion <plik>`) | na `sample+syn` F1 0.683 i 88 fałszywych scaleń wobec 1.000 / 0 dla reguł (§B2 wyżej); wcześniej, przy pretreningu na korpusie publicznym, AUC 0.651 wobec 0.648, ale F1 kampanii 0.0 (`docs/METHODOLOGY.md`, internal notes, not published). Dlatego wyłączona |
| Kalibracja wyświetlania (izotoniczna na wsparciu pary) | internal notes, not published (RUN-C7, commit `c7f4817`) | tylko wyświetlanie, nie decyzja | 5375 par (908 pozytywnych), 10 podziałów rozłącznych po kampaniach: Brier 0.0475 wobec 0.0595, ECE 0.0475 wobec 0.0656; lepiej w 8 z 10 podziałów |

## 4. Embeddingi

| Element | Kod | Domyślnie | Pomiar |
|---|---|---|---|
| Kosinus DINOv2 / e5 zamieniany na bity z poprawką na liczbę porównań: u_pair = min(1, k·l·P_tło), bity = log2(m/u_pair), limit 10 | `hydra/synapse.py:score_pair` (pętla wektorów) -> `hydra/entropy.py:Rarity.vector_bits`; tło `entropy.build_background` | tak | brak osobnej liczby |
| Limity: obraz 2 bity, chyba że cos >= 0.95 (prawie duplikat); tekst zawsze 2 bity | `hydra/synapse.py` (`WEAK_VEoperatorR_BITS`, `IMAGE_NEAR_DUP_COS`) | tak | ablacja rodzin: -VIS bez zmiany F1, -TXT -0.046 (§B4) |
| Wyszukiwanie najbliższych | `hydra/ml/models.py:cosine_topk` (zamiast FAISS) | tak | retrieval przy 231 incydentach: recall 0.941 (internal notes, not published, run `eval-20261121T204751`) |

## 5. Silniki klastrujące

| Element | Kod | Domyślnie | Pomiar |
|---|---|---|---|
| Spójne składowe po krawędziach same_campaign | `hydra/nexus.py:cluster` | tak | F1 kampanii `sample` 1.000 (0 z 41 fałszywych par), `sample+syn` 1.000 (0 z 1361) |
| Strażnik rozmiaru: składowa > max(60, n/3) i gęstość < 0.3 jest dzielona Leidenem (graspologic), awaryjnie Louvain | `hydra/nexus.py:split_component` | tak | ablacja `no_guards`: bez zmian na `sample+syn` (zabezpieczenie skali) |
| Grupy kampanii (wspólny kit / platforma): >= 2 grupy kitu lub 1 rzadka (df <= 3); limity 50 kampanii / 200 członków | `hydra/nexus.py:group_link_supported` | tak | F1 grup `sample` 1.000, `sample+syn` 1.000 (run `0fe34d8`); `no_tiers` obniża do 0.714 |

## 6. Grafy zależności

| Element | Kod | Domyślnie | Pomiar |
|---|---|---|---|
| Graf par (incydent - incydent, bity, hipoteza) | `hydra/nexus.py:cluster` (`networkx.Graph`) | tak | jak w sekcji 5 |
| Mosty i punkty artykulacji kampanii (krawędź / członek, którego usunięcie dzieli kampanię), stabilność bez jednej rodziny | `hydra/campaign_support.py:member_support` | tak | zasila kolejkę przeglądu (`hydra/review_queue.py`) |
| Rodowód: najsilniejszy wcześniejszy link w porządku obserwacji | `hydra/lineage.py:build_lineage`, `lineage_for_campaign` | tak (na żądanie) | brak liczby |
| Drzewo mutacji (minimalna arborescencja) | `hydra/lineage_mutation.py:mutation_tree` | opt-in `HYDRA_ADV_LINEAGE=1` | 3 krawędzie, zgodność z drzewem linków 0.75, 0.41 s (internal notes, not published E-ADV-CAMP-1) |
| Raport spójności kampanii; bramka dzieląca | `hydra/consistency.py:consistency_report`, `apply_gate` | raport tak; bramka opt-in `HYDRA_ADV_GATE=1` | partycja `sample` identyczna z bramką i bez; raport 0.006 s (E-ADV-CAMP-1) |
| Grafowa baza danych Neo4j | `hydra/export_neo4j.py` | na żądanie | dowód end-to-end: `docs/NEO4J.md` |

## 7. Inne podejścia

| Element | Kod | Domyślnie | Pomiar |
|---|---|---|---|
| Przypisanie w zbiorze otwartym (assign / abstain / new_campaign) | `hydra/assign.py:assign` -> `_decide` | tak | sekcja 1; replay tylko z przeszłości `sample` 0.667 (n=9), `sample+syn` 0.982 |
| Predykcja konformalna (split conformal, eps 0.1) | `hydra/conformal.py:calibrate`, `apply_conformal` | tak, decyduje dopiero przy >= 20 elementach kalibracji | na `sample` (5 elementów) decyduje reguła progowa; ryzyko selektywne w §C3 runu `0fe34d8` |
| KONTRA: laboratorium kontrfaktyczne (co musiałoby się zmienić, żeby decyzja się odwróciła) | `hydra/kontra.py:run_experiment`, `flip_search`, `next_observations` | na żądanie (API) | syntetycznie, 24 incydenty: zbiór odwracający znaleziony 8/24, średni rozmiar 1.5, 19.95 ms na incydent; planer bez zmierzonej przewagi (internal notes, not published) |
| Kolejka przeglądu według zysku informacji | `hydra/queue_gain.py:annotate`, `p_confirm` | tak (`HYDRA_QUEUE_ORDER`, domyślnie `gain`) | 12 syntetycznych światów, średnie F1 po k=1..30: sparse 0.712 wobec 0.586 losowo, noisy 0.798 wobec 0.659 (internal notes, not published) |
| Opcjonalna warstwa LLM analityka (ADR-008) | `hydra/llm/` | opt-in `HYDRA_LLM=1`, nigdy reguła łączenia | nie dotyczy wyniku |

## 8. Reprezentacja i wizualizacja relacji

| Element | Kod | Domyślnie | Uwagi |
|---|---|---|---|
| Graf z poziomami szczegółu (przegląd, widok, ścieżka, eksport zaznaczenia) | `hydra/graph_lod.py:build_base`, `GraphBase.overlay`; API `hydra/api_graph.py` (`/explore`, `/overview`, `/viewport`, `/path`, `/export`) | tak (UI) | brak liczby |
| Różnice wariantów z nakładką geometrii (homografia, dopasowane fragmenty) | `hydra/vis_geom.py:verify`, `hydra/api_geom.py:geometry`; UI `VariantDiffPage.tsx`, `GeometryOverlay.tsx` | wyświetlanie tak; weryfikacja przycięć `HYDRA_ADV_CROP` domyślnie od 2026-11-22; geometria w wyniku opt-in `HYDRA_ADV_GEOM=1` | hidden_sim: trafność przypisania 0.941 -> 0.983 (112 -> 117 z 119), przycięte kreacje 2/7 -> 7/7 (internal notes, not published) |
| Neo4j (Cypher, CSV, `neo4j-admin`) | `hydra/export_neo4j.py:build_graph`, `export_run`, `inline_cypher`; CLI `hydra export-neo4j`; API `GET /v1/export/neo4j`; `docker compose --profile neo4j` | na żądanie | import i 5 zapytań wykonanych na serwerze na danych syntetycznych: `docs/NEO4J.md` |
| STIX 2.1 / MISP | `hydra/export_stix.py:export_stix` (walidacja `stix2.parse`); `hydra/integrations/exports.py:misp_event` | na żądanie | brak liczby |

## 9. Dowód, że wybór metod ma znaczenie: ablacje mechanizmów

Run `eval-20261122T032044`, commit `0fe34d8`, korpus `sample+syn` (SYNTETYCZNY mix), `eval/run.py:mechanism_ablation`
(§C3 wyniku). Każdy wiersz wyłącza jeden mechanizm, resztę zostawia.

| Wyłączony mechanizm | F1 kampanii | Fałszywe scalenia kampanii | F1 grup | Fałszywe scalenia grup | LOO |
|---|---|---|---|---|---|
| (pełny system, §B1) | 1.000 | 0 | 1.000 | 0 | 1.000 |
| `no_rarity` (stałe 4 bity zamiast rzadkości) | **0.721** | 96 | 1.000 | 0 | 0.836 |
| `no_tiers` (bez poziomów dowodu i reguły operatora) | **0.427** | 333 | 0.714 | 176 | 0.654 |
| `no_caps` (bez limitów rodzin) | 1.000 | 0 | 1.000 | 0 | 1.000 |
| `no_guards` (bez strażnika rozmiaru) | 1.000 | 0 | 1.000 | 0 | 1.000 |

Ablacja rodzin (§B4, ten sam run): bez INF F1 0.630 (-0.370), bez TXT 0.954, bez META 0.988, bez VIS / ID / NAR bez
zmian. Wniosek: wynik niosą rzadkość i poziomy dowodu na genach infrastruktury; limity i strażnik rozmiaru nie
zmieniają wyniku na tym korpusie i są zabezpieczeniem na większą skalę. Liczby są in-sample (progi ustalone przy
pracy na tych samych 10 incydentach), nie estymują zbioru ukrytego.
