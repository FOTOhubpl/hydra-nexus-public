# Integracje & API

Stan: 2026-11-22 (Europe/Warsaw), gałąź. Fakty o dostawcach sprawdzone na publicznych stronach
2026-11-22 02:20–03:00; to, czego nie dało się potwierdzić, jest oznaczone „niezweryfikowane”. Dokumentacja REST
(angielska): `docs/API.md`. Ta sama treść w skrócie jest w aplikacji: Integracje & API → Dokumentacja.

## 1. Po co

HYDRA NEXUS działa samodzielnie i offline. Moduł Integracje & API pozwala analitykowi:

- przekazać wyniki dalej w formatach, które znają narzędzia CERT/SOC (MISP, STIX 2.1/OpenCTI, XARF, Splunk HEC,
  Elasticsearch `_bulk`, CSV, SQL, Neo4j, podpisany pakiet dowodowy);
- opcjonalnie wzbogacić wskaźniki o publiczne źródła (CERT Polska, crt.sh, RDAP, DNS, URLhaus, VirusTotal,
  urlscan.io, OpenPhish, Safe Browsing, AbuseIPDB) i bibliotekę reklam Meta;
- powiadamiać własne systemy o zdarzeniach (podpisane webhooki HMAC-SHA256);
- wywoływać lokalne REST API z poziomu przeglądarki (Konsola API) i czytać jego dokumentację.

## 2. Zasada: wszystko wyłączone domyślnie, działa offline

1. Każdy konektor i każdy webhook jest domyślnie wyłączony. Instalacja bez kluczy niczego nie wysyła.
2. Domyślnie `HYDRA_OFFLINE=1` (`scripts/env.sh`). Wtedy każde wywołanie sieciowe integracji kończy się od razu
   statusem `offline` i komunikatem po polsku. Nie ma cichego przejścia na sieć ani „zastępczych” wyników.
3. Eksport do plików (format eksportu) i podgląd na sucho działają offline: plik powstaje lokalnie.
4. Konektor `live` wymaga: klucza (jeśli dostawca go wymaga), `HYDRA_OFFLINE=0`, dla wzbogacania także
   `HYDRA_ENRICH=1`, a potem jawnego kliknięcia Testuj i Włącz. Test nigdy nie uruchamia się sam.
5. Webhook na adres loopback (127.0.0.1, ::1, localhost) działa także offline, bo dane nie opuszczają komputera.
   Każdy inny adres wymaga `HYDRA_OFFLINE=0`, włączonego endpointu i pozytywnej kontroli netguard; inaczej
   dostawa ma status `blocked_offline`.

Poziomy implementacji (pole `level`, etykiety w UI):

| Poziom | Etykieta | Znaczenie |
|---|---|---|
| `live` | Na żywo (live) | Zaimplementowany; testowany na fiksturach z dokumentacji, bez wywołań produkcyjnych (fikstury przygotowane ręcznie z publicznej dokumentacji dostawcy; testy nie łączą się z siecią; żaden z 12 klientów nie był uruchomiony przeciw produkcyjnemu API dostawcy). Wymaga trybu online i włączenia. |
| `export-format` | Format eksportu | HYDRA tworzy plik w formacie importu dostawcy lokalnie (pobranie lub podgląd na sucho). Wysyłka tylko tam, gdzie jest klient (`live_impl`). |
| `planned` | Planowane | Tylko opis. Brak przycisków Połącz/Testuj/Uruchom/Eksport; API odpowiada 409. |

Statusy konektora: Skonfigurowane (`configured`), Nieskonfigurowane (`not_configured`), Offline (`offline`: konektor
live skonfigurowany, ale `HYDRA_OFFLINE=1`), Wyłączone (`disabled`), Planowane (`planned`).

## 3. Cztery zakładki (`/integrations?tab=hub|console|webhooks|docs`)

1. **Centrum integracji** (`hub`, domyślna). Karty konektorów pogrupowane w kategorie, z poziomem, statusem,
   danymi wysyłanymi i licencją. Przyciski:
   - **Połącz** — rozwija formularz klucza bezpośrednio w karcie (ten sam w karcie pakietu, karcie konektora i
     szczegółach): ramka „Wymaga klucza API” (co klucz odblokowuje, skąd go wziąć, plan i limity, link do dostawcy,
     „Jak zdobyć klucz” do zakładki Dokumentacja), pole maskowane z przyciskami Pokaż/Ukryj i Wklej, pola adresu instancji
     (MISP / Elasticsearch / Splunk), „Zapisz i testuj” (zapis sekretu → test → włączenie, `POST
     /v1/integrations/{id}/connect`) z wynikiem w miejscu (✓ Połączono / ⏸ Klucz zapisany offline / ✕ powód) oraz
     „Usuń klucz”. API zwraca tylko „zapisany” i 4 ostatnie znaki.
   - **Pobierz pakiet** — plik w formacie dostawcy (`GET /v1/integrations/{id}/export`), działa offline.
   - **Podgląd ładunku (na sucho)** — co dokładnie zostałoby wysłane, dokąd, ile bajtów, sha256, jakie pola i redakcje; nic nie jest
     wysyłane (`POST /v1/integrations/{id}/preview`).
   - **Pakiety jednym kliknięciem** (presets) — kilka konektorów naraz: Pakiet CERT/MISP, Pakiet SIEM, Pakiet reklamowy: Meta Ad
     Library, Pakiet reputacji (lista w §4). Każda karta ma pasek postępu i podsumowanie („3 z 5 gotowe, 2 wymagają
     klucza”), wiersz na konektor ze stanem opisanym tekstem i symbolem (✓ Gotowe, ! Wymaga klucza, ⏸ Offline, ✕ Błąd
     testu, – Planowany) i formularzem klucza w wierszu. Przycisk karty to „Uzupełnij klucze (N)” (otwiera pierwszy
     brakujący formularz), a gdy klucze są komplet — „Zastosuj pakiet”; po zapisaniu ostatniego brakującego klucza pakiet
     stosuje się sam. Klucze konektorów formatu eksportu są opcjonalne (tylko do wysyłki; plik działa bez nich).
   Szuflada konektora pokazuje kroki konfiguracji, uwagi weryfikacyjne, ostatni test i ostatnie zdarzenia audytu.
2. **Konsola API** (`console`). Wywołania lokalnego REST API z przeglądarki na podstawie `/openapi.json`, z gotowymi
   przykładami (curl, Python, JavaScript). Historia zapisuje tylko metodę, ścieżkę, status i czas; nagłówki typu
   authorization / x-api-key / *token* / *secret* i pola ciała typu secret/token/key/password są usuwane.
3. **Webhooki** (`webhooks`). Endpointy odbiorców, wybór zdarzeń, generowanie sekretu w przeglądarce
   (`crypto.getRandomValues`, serwer nigdy go nie odsyła), ping testowy, lista dostaw z ponawianiem, wbudowany
   odbiornik testowy na loopback (`/v1/integrations/webhooks/sink`), który sprawdza podpis.
4. **Dokumentacja** (`docs`). Ten opis w skrócie oraz referencja endpointów generowana z `/openapi.json`.

## 4. Konektory

Tabela wygenerowana z `hydra/integrations/registry.py` (krotka `CONNEoperatorRS`) — źródłem prawdy jest kod; kolumny
odpowiadają polom `id`, `name`, `category`, `direction`, `level`, `formats`, `data_out_pl`, `auth`/`secrets`,
`rate_limit_pl`/`rate_limit_per_min`, `licence_pl`, `docs_url`. „Limit HYDRA” to limit po stronie klienta
(okno 60 s), nie limit dostawcy.

<!-- BEGIN CONNEoperatorR TABLE (generated from hydra/integrations/registry.py) -->
| id | Nazwa | Kategoria | Kierunek | Poziom | Formaty | Dane wysyłane | Uwierzytelnienie (sekrety) | Limity | Licencja / ToS | Źródło |
|---|---|---|---|---|---|---|---|---|---|---|
| `meta-adlibrary` | Meta Ad Library API | Przejrzystość reklam | pobieranie | live | — | Do Meta trafiają słowa kluczowe z listy obserwacji i identyfikatory stron (page_id), kraj PL. | token (`META_AD_LIBRARY_TOKEN`) | Limit Graph API nie jest podany liczbowo; przekroczenie zwraca błąd 613 (HYDRA ponawia z wycofaniem 1/2/4 s). Limit HYDRA: 30/min. | Warunki Meta Platform (niezweryfikowane). Reklamy, które nie dotarły do UE, są zwracane tylko gdy dotyczą polityki/spraw społecznych (sprawdzone 2026-11-22). | https://developers.facebook.com/docs/graph-api/reference/ads_archive/ |
| `google-ads-transparency` | Google Ads Transparency Center | Przejrzystość reklam | pobieranie | planowane | — | Nic nie jest wysyłane. | brak | Brak publicznie podanego limitu. | Warunki Google. Zbiór BigQuery `bigquery-public-data.google_political_ads` obejmuje tylko reklamy polityczne. | https://adstransparency.google.com/ |
| `tiktok-commercial-content` | TikTok Commercial Content API | Przejrzystość reklam | pobieranie | planowane | — | Nic nie jest wysyłane. | token | Brak publicznie podanego limitu. | Warunki TikTok for Developers; na obecnym etapie dane tylko z krajów UE. | https://developers.tiktok.com/products/commercial-content-api |
| `linkedin-ad-library` | LinkedIn Ad Library | Przejrzystość reklam | pobieranie | planowane | — | Nic nie jest wysyłane. | token | Brak publicznie podanego limitu. | — | https://www.linkedin.com/ad-library/ |
| `x-ads-repository` | X Ads Repository | Przejrzystość reklam | pobieranie | planowane | — | Nic nie jest wysyłane. | token | Brak publicznie podanego limitu. | — | https://ads.x.com/ads-repository |
| `urlscan` | urlscan.io | Wzbogacanie | pobieranie | live | — | Do urlscan.io trafia nazwa domeny (zapytanie wyszukiwania). | klucz API (`URLSCAN_API_KEY`) | Limity zależą od planu konta (niezweryfikowane liczbowo); HYDRA ogranicza do 30/min. Limit HYDRA: 30/min. | Warunki urlscan.io; wyniki wyszukiwania publicznych skanów. | https://docs.urlscan.io/pages/api-intro |
| `virustotal` | VirusTotal | Wzbogacanie | pobieranie | live | — | Do VirusTotal trafia domena, URL (jako identyfikator base64url), IP lub SHA-256 pliku. Same pliki nigdy nie są wysyłane. | klucz API (`VT_API_KEY`) | Publiczne API: 4 zapytania/min, 500/dzień (sprawdzone 2026-11-22). Limit HYDRA: 4/min. | Publiczne API nie może być używane w produktach komercyjnych ani procesach biznesowych, które nie wnoszą nowych plików (sprawdzone 2026-11-22); do zastosowań komercyjnych Premium API. | https://docs.virustotal.com/reference/public-vs-premium-api |
| `urlhaus` | URLhaus (abuse.ch) | Threat intelligence | pobieranie | live | — | Do abuse.ch trafia host (domena/IP) albo URL. | klucz API (`URLHAUS_AUTH_KEY`) | Limit na minutę nie jest podany; wyniki ograniczone do 100 URL na host (sprawdzone 2026-11-22). HYDRA ogranicza do 30/min. Limit HYDRA: 30/min. | Warunki abuse.ch (nie sprawdzone na stronie API). | https://urlhaus-api.abuse.ch/ |
| `phishtank` | PhishTank | Threat intelligence | pobieranie | planowane | — | Nic nie jest wysyłane. | klucz API | Bez klucza tylko kilka pobrań dziennie (sprawdzone 2026-11-22). | Warunki Cisco Talos (nie przeczytane, niezweryfikowane). | https://www.phishtank.com/developer_info.php |
| `openphish` | OpenPhish (feed społecznościowy) | Threat intelligence | pobieranie | live | — | Nic o wskaźnikach: tylko zwykłe pobranie publicznego pliku (GET feed.txt). | brak | Feed społecznościowy aktualizowany co 12 h (sprawdzone 2026-11-22); HYDRA pobiera najwyżej raz na godzinę. Limit HYDRA: 1/min. | Wymaga akceptacji Terms of Use OpenPhish; użycie komercyjne nie jest wprost dozwolone (plany komercyjne są płatne), niezweryfikowane. | https://openphish.com/phishing_feeds.html |
| `google-safe-browsing` | Google Safe Browsing v4 | Threat intelligence | pobieranie | live | — | Do Google trafiają pełne URL-e (z usuniętymi parametrami zawierającymi sekrety). | klucz API (`GOOGLE_SAFE_BROWSING_KEY`) | Limit nie jest podany na stronie (niezweryfikowane); HYDRA ogranicza do 60/min. Limit HYDRA: 60/min. | Safe Browsing API tylko do użytku niekomercyjnego; do zastosowań komercyjnych Web Risk API (Google Cloud). Wersja v4 oznaczona jako przestarzała (sprawdzone 2026-11-22). | https://developers.google.com/safe-browsing/v4 |
| `cert-pl` | CERT Polska — Lista ostrzeżeń | Threat intelligence | pobieranie | live | — | Nic: wyszukiwanie w lokalnej migawce. Odświeżenie migawki to zwykłe pobranie publicznego pliku (scripts). | brak | Plik aktualizowany co 5 minut (sprawdzone 2026-11-22); wyszukiwanie lokalne bez limitu. | Do dowolnego użytku, także komercyjnego, bez gwarancji; przypisanie nie może sugerować poparcia NASK (sprawdzone 2026-11-22). | https://cert.pl/lista-ostrzezen/ |
| `crtsh` | crt.sh (Certificate Transparency) | Wzbogacanie | pobieranie | live | — | Do crt.sh trafia nazwa domeny. | brak | Bezpłatna usługa bez SLA (częste 502); HYDRA: 1 zapytanie/3 s. Limit HYDRA: 20/min. | Dane CT są publiczne z założenia (RFC 6962); warunki usługi crt.sh niezweryfikowane. | https://crt.sh/ |
| `rdap` | RDAP (rdap.org) | Wzbogacanie | pobieranie | live | — | Do rdap.org i rejestru trafia domena lub IP. | brak | rdap.org: 10 zapytań / 10 s, nadmiar kończy się HTTP 429 (sprawdzone 2026-11-22). Limit HYDRA: 60/min. | Dane RDAP są publiczne (ICANN), pola rejestrującego zanonimizowane (RODO). | https://about.rdap.org/ |
| `dns` | DNS (publiczne resolwery) | Wzbogacanie | pobieranie | live | — | Do resolwera DNS trafia nazwa domeny. | brak | HYDRA: 20 zapytań/s. Limit HYDRA: 600/min. | Publiczny resolwer 1.1.1.1 (polityka prywatności Cloudflare). | https://developers.cloudflare.com/1.1.1.1/ |
| `shodan` | Shodan | Wzbogacanie | pobieranie | planowane | — | Nic nie jest wysyłane. | klucz API | 1 zapytanie/s, kredyty zapytań (sprawdzone 2026-11-22). | — | https://developer.shodan.io/api |
| `censys` | Censys | Wzbogacanie | pobieranie | planowane | — | Nic nie jest wysyłane. | klucz API | Plan darmowy: tylko wyszukiwanie pojedynczych hostów/certyfikatów, 1 równoczesne zapytanie (sprawdzone 2026-11-22). | — | https://docs.censys.com/reference/get-started |
| `abuseipdb` | AbuseIPDB | Threat intelligence | pobieranie | live | — | Do AbuseIPDB trafia adres IP. | klucz API (`ABUSEIPDB_KEY`) | Plan darmowy: check 1000/dzień (sprawdzone 2026-11-22); HYDRA ogranicza do 30/min. Limit HYDRA: 30/min. | Warunki AbuseIPDB (niezweryfikowane). | https://docs.abuseipdb.com/ |
| `misp` | MISP | Threat intelligence | wysyłka | format eksportu | misp | Wskaźniki kampanii (domena, URL bez parametrów-sekretów, IP, SHA-256), id kampanii, decyzja, bity, liczba incydentów, daty. Dystrybucja 0 (tylko organizacja), TLP:AMBER. | klucz API (`MISP_URL`, `MISP_KEY`) | Instancja własna, bez limitu dostawcy; HYDRA ogranicza do 10/min. Limit HYDRA: 10/min. | Oprogramowanie MISP: AGPL-3.0 (HYDRA nie zawiera kodu MISP, tylko format JSON). | https://www.misp-project.org/openapi/ |
| `opencti-taxii` | OpenCTI / TAXII 2.1 (STIX 2.1) | Threat intelligence | wysyłka | format eksportu | stix21 | Obiekty STIX: domeny, wzorce URL (bez zapytań), IP, SHA-256, kampania z datami. | brak | Brak publicznie podanego limitu. | STIX 2.1: standard OASIS; biblioteka stix2 (BSD-3-Clause). | https://docs.oasis-open.org/cti/stix/v2.1/stix-v2.1.html |
| `neo4j` | Neo4j (graf) | Bazy danych i SIEM | wysyłka | format eksportu | neo4j | Węzły incydentów (id), genów (rodzaj, wartość po redakcji, częstość) i kampanii; krawędzie z bitami. | brak | Brak publicznie podanego limitu. | Format Cypher; Neo4j Community GPL-3.0 (HYDRA nie zawiera kodu Neo4j). | https://neo4j.com/docs/cypher-manual/current/ |
| `elasticsearch` | Elasticsearch / OpenSearch (_bulk) | Bazy danych i SIEM | wysyłka | format eksportu | es_bulk, ndjson | Jeden dokument na wskaźnik (typ, wartość, kampania, decyzja, bity, liczba, daty). | klucz API (`ES_URL`, `ES_API_KEY`) | Domyślnie do 100 MB na żądanie (sprawdzone 2026-11-22); HYDRA ogranicza do 10/min. Limit HYDRA: 10/min. | Instancja własna. | https://www.elastic.co/docs/api/doc/elasticsearch/operation/operation-bulk |
| `splunk-hec` | Splunk HTTP Event Collector | Bazy danych i SIEM | wysyłka | format eksportu | siem_json | Jedno zdarzenie na wskaźnik (pola jak w eksporcie wskaźników). | klucz API (`SPLUNK_HEC_URL`, `SPLUNK_HEC_TOKEN`) | Instancja własna; HYDRA ogranicza do 10/min. Limit HYDRA: 10/min. | Instancja własna. | https://help.splunk.com/en/splunk-enterprise/get-started/get-data-in/9.4/get-data-with-http-event-collector/format-events-for-http-event-collector |
| `siem-generic` | SIEM (NDJSON / JSON) | Bazy danych i SIEM | wysyłka | format eksportu | ndjson, siem_json | Jeden rekord na wskaźnik. | brak | Brak publicznie podanego limitu. | Format otwarty. | https://github.com/ndjson/ndjson-spec |
| `sql-export` | SQL (SQLite / PostgreSQL) | Bazy danych i SIEM | wysyłka | format eksportu | sql | Tabele hydra_indicators i hydra_campaigns. | brak | Brak publicznie podanego limitu. | Format otwarty. | https://www.sqlite.org/lang.html |
| `csv-export` | CSV wskaźników | Bazy danych i SIEM | wysyłka | format eksportu | csv | Jeden wiersz na wskaźnik. | brak | Brak publicznie podanego limitu. | Format otwarty. | https://www.rfc-editor.org/rfc/rfc4180 |
| `webhook-generic` | Webhook (HMAC-SHA256) | Powiadomienia | wysyłka | live | webhook_json | Typ zdarzenia, identyfikatory i wskaźniki (bez treści). | HMAC | Brak publicznie podanego limitu. | Własny endpoint odbiorcy. | https://www.rfc-editor.org/rfc/rfc2104 |
| `slack` | Slack (Incoming Webhook) | Powiadomienia | wysyłka | format eksportu | webhook_json | Id kampanii, decyzja, liczba incydentów i do 5 domen. | klucz API (`SLACK_WEBHOOK_URL`) | Limit nie jest podany na stronie (niezweryfikowane); HYDRA ogranicza do 10/min. Limit HYDRA: 10/min. | Warunki Slack. | https://docs.slack.dev/messaging/sending-messages-using-incoming-webhooks |
| `teams` | Microsoft Teams | Powiadomienia | wysyłka | planowane | — | Nic nie jest wysyłane. | klucz API | Brak publicznie podanego limitu. | — | https://learn.microsoft.com/microsoftteams/platform/webhooks-and-connectors/what-are-webhooks-and-connectors |
| `email-smtp` | E-mail (SMTP) | Powiadomienia | wysyłka | planowane | — | Nic nie jest wysyłane. | login/hasło | Brak publicznie podanego limitu. | — | https://www.rfc-editor.org/rfc/rfc5321 |
| `xarf` | XARF v4 (zgłoszenia nadużyć) | Zgłaszanie i udostępnianie | wysyłka | format eksportu | xarf | Host, URL-e, IP, kontakty abuse z RDAP, id kampanii, daty. | brak | Brak publicznie podanego limitu. | XARF v4 (xarf.org), MIT. | https://github.com/xarf/xarf-spec |
| `evidence-package` | Podpisany pakiet dowodowy | Zgłaszanie i udostępnianie | wysyłka | format eksportu | evidence | Incydenty (id), geny po redakcji, wzbogacenia, powiązania, przypisania, kampanie, manifest i podpis. | brak | Brak publicznie podanego limitu. | Ed25519 (RFC 8032), biblioteka cryptography (Apache-2.0/BSD). | https://www.rfc-editor.org/rfc/rfc8032 |

Pakiety (presets):
- `cert-misp` — Pakiet CERT/MISP: `misp`, `opencti-taxii`, `cert-pl`, `xarf`, `evidence-package`. Wymiana z CERT: zdarzenie MISP, STIX 2.1 dla OpenCTI, lista ostrzeżeń CERT Polska, XARF i podpisany pakiet dowodowy.
- `siem` — Pakiet SIEM: `splunk-hec`, `elasticsearch`, `siem-generic`. Wskaźniki do Splunk HEC, Elasticsearch i dowolnego SIEM (NDJSON/JSON).
- `ads-meta` — Pakiet reklamowy: Meta Ad Library: `meta-adlibrary`, `crtsh`, `rdap`, `dns`, `cert-pl`. Biblioteka reklam Meta i wzbogacanie domen z reklam: crt.sh, RDAP, DNS, lista ostrzeżeń CERT Polska.
- `reputation` — Pakiet reputacji: `openphish`, `urlhaus`, `virustotal`, `urlscan`, `abuseipdb`. Reputacja wskaźników: OpenPhish (dopasowanie lokalne), URLhaus, VirusTotal, urlscan.io, AbuseIPDB.

<!-- 32 connectors; live=12, export-format=11, planned=9 -->
<!-- END CONNEoperatorR TABLE -->

Biblioteki reklam: tylko Meta udostępnia publiczne API (`live`). TikTok Commercial Content API wymaga zatwierdzenia
wniosku i obejmuje na razie dane z UE. Dla Google Ads Transparency Center, LinkedIn Ad Library i X Ads Repository
nie znaleziono dokumentacji API (stan 2026-11-22; strony LinkedIn i X odpowiadały 403/402 automatycznemu
pobraniu, więc to „nie znaleziono”, a nie potwierdzony brak). Szczegóły: internal notes, not published E-INTEG-1.

## 4a. Uwierzytelnianie API (logowanie, tokeny)

Od 2026-11-22 panel i API wymagają logowania (`HYDRA_AUTH=1`, szczegóły: [SECURITY_AUTH.md](SECURITY_AUTH.md)).
Konsola API w zakładce „Konsola” wysyła żądania w ramach bieżącej sesji (ciasteczko + nagłówek `X-CSRF-Token`) albo,
po wklejeniu tokenu, z nagłówkiem `Authorization: Bearer <token>`. Token osobisty tworzy się w menu użytkownika →
„Tokeny API” (pokazywany raz, przechowywany jako skrót sha256, można go unieważnić) lub poleceniem
`uv run hydra user token <użytkownik> --name automatyzacja --days 30`. Skrypty i automaty używają zmiennej
`HYDRA_API_TOKEN`:

```bash
curl -s -H "Authorization: Bearer $HYDRA_API_TOKEN" http://127.0.0.1:8000/api/v1/integrations
```

Zapis kluczy dostawców (`PUT /v1/integrations/{id}/secret`), połączenie, włączenie konektora, presety i konfiguracja
webhooków wymagają roli administratora; uruchomienie i test konektora — roli analityka. Wbudowany odbiornik testowy
`POST /v1/integrations/webhooks/sink` pozostaje publiczny (weryfikuje podpis HMAC).

## 5. Sekrety

- Plik na konektor: `~/.config/hydra/<id>.env` (linie `KLUCZ=WARTOŚĆ`), katalog 0700, plik 0600, zapis atomowy
  (plik tymczasowy 0600 + `os.replace`). `HYDRA_SECRETS_DIR` zmienia katalog (testy, Docker).
- Meta Ad Library używa istniejącego pliku `~/.config/hydra/meta.env` i zmiennej `META_AD_LIBRARY_TOKEN`
  (zgodność z `hydra.enrich.adlibrary`; starsza nazwa `HYDRA_META_TOKEN` też jest czytana przez moduł wzbogacania).
- Zmienna środowiskowa o tej samej nazwie ma pierwszeństwo przed plikiem.
- Wartość sekretu nigdy nie trafia do bazy stanu, repozytorium, logów, audytu, pamięci podręcznej ani odpowiedzi
  API. API zwraca tylko `configured`, źródło (`env` / `file`) i `last4` (ostatnie 4 znaki, tylko gdy wartość ma co
  najmniej 12 znaków; inaczej `null`). W podglądzie nagłówki z sekretem mają postać `•••• + last4`.
- Webhooki: konfiguracja endpointów w `~/.config/hydra/webhooks.json` (0600, bo adres Slack/Teams sam jest
  sekretem), sekret podpisu w `~/.config/hydra/webhook-<id>.env` (0600).
- Usunięcie: `DELETE /v1/integrations/{id}/secret?name=…` albo usunięcie pliku.

## 6. Bezpieczeństwo ruchu wychodzącego

Jedyna ścieżka HTTP integracji to `hydra/integrations/http.py`. Zanim jakikolwiek bajt opuści komputer:

1. `HYDRA_OFFLINE=1` → status `offline` (komunikat po polsku), nic nie jest wysyłane;
2. konektor wyłączony → `disabled` (przycisk Testuj działa bez włączenia);
3. `netguard.check_url(url, resolve=True)`: tylko http/https, każdy rozwiązany adres publiczny (loopback tylko dla
   odbiornika testowego) → inaczej `blocked`;
4. limit konektora (okno 60 s) → `rate_limited`;
5. limit czasu (domyślnie 20 s), limit rozmiaru odpowiedzi (5 MB), przekierowania nie są śledzone.

Audyt (łańcuch zdarzeń z haszami, `hydra/events.py`, przez jedyny Writer):

- `INTEGRATION_REQUEST` — każda próba wywołania: `{connector, method, host, path (bez query), status, ms,
  bytes_out, bytes_in, payload_sha256}`. Bez tokenów; sekrety są usuwane z komunikatów błędów.
- `INTEGRATION_DELIVERY` — każda próba dostawy webhooka: tylko host (bez ścieżki i query), nigdy sekret.
- Pobranie pliku eksportu jest audytowane jako zdarzenie eksportu.
- Pozostałe zdarzenia: `INTEGRATION_TEST`, `INTEGRATION_RUN`, `INTEGRATION_SECRET` (tylko nazwa sekretu),
  `INTEGRATION_ENABLE`, `INTEGRATION_CONNECT`, `INTEGRATION_PRESET`, `INTEGRATION_EXPORT`.
- crt.sh, RDAP, DNS i pobieranie Meta Ad Library przez harmonogram używają istniejącej ścieżki `hydra.enrich`
  (te same kontrole netguard); są audytowane per uruchomienie, nie per żądanie.
- Ograniczenia: TAXII push nie jest zaimplementowany (opencti-taxii = plik STIX). Pakiet XARF zawiera fragmenty
  dowodów (base64) z istniejącego eksportu abuse, a STIX i pakiet dowodowy zawierają zredagowane tytuły incydentów
  i opisy kampanii; jest to opisane przy każdym konektorze. Test Slacka wysyła widoczną wiadomość testową na kanał.

## 7. Webhooki: format i weryfikacja podpisu

Zdarzenia: `ping`, `delta` (zmiana wyników: nowa kampania, przypisanie), `job` (zadanie zakończone), `watch` (alert
HYDRA Watch — kandydat, nie potwierdzony członek kampanii), `review` (decyzja analityka), `export`.

Nagłówki: `X-Hydra-Event`, `X-Hydra-Delivery`, `X-Hydra-Timestamp` (sekundy unix), `X-Hydra-Signature:
sha256=<hex>`, gdzie hex = HMAC-SHA256(sekret, `"{timestamp}.{surowe ciało}"`). Ciało:
`{"id", "event", "created_at", "hydra_version", "data": {…}}` — tylko identyfikatory, liczby i wskaźniki.
Odbiorca porównuje podpis w czasie stałym i odrzuca żądania, gdy |teraz − timestamp| > 300 s.

Ponawianie: do 5 prób, odstęp wykładniczy od 2 s (+2, +4, +8, +16 s; `HYDRA_WEBHOOK_BACKOFF_S`), ponowienie przy
błędzie sieci, 429 i 5xx; inny kod 4xx → od razu `dead` (lista dead-letter, ręczne „Ponów”).

Python:

```python
import hashlib, hmac, time

def verify(secret: str, headers: dict, raw_body: bytes, tolerance_s: int = 300) -> bool:
    ts = headers.get("X-Hydra-Timestamp", "")
    sig = headers.get("X-Hydra-Signature", "")
    if not ts.isdigit or abs(time.time - int(ts)) > tolerance_s:
        return False
    mac = hmac.new(secret.encode, ts.encode + b"." + raw_body, hashlib.sha256).hexdigest
    return hmac.compare_digest("sha256=" + mac, sig)
```

JavaScript (Node.js):

```js
const crypto = require("node:crypto");

function verify(secret, headers, rawBody /* Buffer */, toleranceS = 300) {
  const ts = headers["x-hydra-timestamp"] ?? "";
  const sig = headers["x-hydra-signature"] ?? "";
  if (!/^\d+$/.test(ts) || Math.abs(Date.now / 1000 - Number(ts)) > toleranceS) return false;
  const mac = crypto.createHmac("sha256", secret).update(`${ts}.`).update(rawBody).digest("hex");
  const a = Buffer.from(`sha256=${mac}`), b = Buffer.from(sig);
  return a.length === b.length && crypto.timingSafeEqual(a, b);
}
```

openssl (ręczne sprawdzenie zapisanego ciała `body.json`):

```sh
TS=1763776800   # wartość X-Hydra-Timestamp
printf '%s.' "$TS" | cat - body.json | openssl dgst -sha256 -hmac "$SECRET" | sed 's/^.*= /sha256=/'
```

Wynik musi być identyczny z nagłówkiem `X-Hydra-Signature`. Ciało trzeba weryfikować w postaci surowej (bajty z
sieci), przed parsowaniem JSON.

## 8. Przepisy (recipes)

1. **Wymiana z CERT (offline).** Centrum integracji → Pakiet CERT/MISP → Pobierz pakiet dla kampanii: zdarzenie MISP
   (dystrybucja 0, TLP:AMBER), STIX 2.1 do importu w OpenCTI, zgłoszenia XARF v4 i podpisany pakiet dowodowy.
   Odbiorca sprawdza pakiet poleceniem `hydra verify-export <plik.zip>`.
2. **Wysyłka do własnego MISP (online).** Zapisz `MISP_URL` i `MISP_KEY`, obejrzyj Podgląd, ustaw `HYDRA_OFFLINE=0`,
   Testuj → Włącz → Uruchom. Wysyłka: `POST {MISP_URL}/events/add`, nagłówek `Authorization: <klucz>`.
3. **SIEM.** Pakiet SIEM: Splunk HEC (`POST {SPLUNK_HEC_URL}/services/collector/event`, `Authorization: Splunk
   <token>`, port domyślny 8088), Elasticsearch/OpenSearch (`POST {ES_URL}/_bulk`, NDJSON z końcowym `\n`,
   `Authorization: ApiKey <klucz>`) albo uniwersalny plik NDJSON dla dowolnego SIEM.
4. **Powiadomienia.** Zakładka Webhooki → Dodaj endpoint → Generuj sekret → wybierz zdarzenia → Wyślij ping.
   Najpierw na wbudowany odbiornik `http://127.0.0.1:<port>/v1/integrations/webhooks/sink` (działa offline), potem
   na własny serwer. Microsoft Teams: konektory Office 365 (w tym Incoming Webhooks oparte na konektorach) wycofano
   w maju 2026; użyj przepływu Workflows z wyzwalaczem „When a Teams webhook request is received”.
5. **Reklamy Meta.** Pakiet reklamowy: Meta Ad Library + crt.sh, RDAP, DNS i lista ostrzeżeń CERT Polska dla domen z
   reklam (§10).
6. **Reputacja wskaźników.** Pakiet reputacji: OpenPhish (dopasowanie lokalne, do OpenPhish nic nie trafia),
   URLhaus, VirusTotal, urlscan.io (tylko wyszukiwanie istniejących skanów), AbuseIPDB.

## 9. Prywatność: co opuszcza komputer

- Offline (domyślnie): nic.
- Eksport do pliku: nic; plik trafia do analityka, który sam decyduje o przekazaniu.
- Konektor `live` po włączeniu: tylko pola z kolumny „Dane wysyłane” (np. domena, URL bez parametrów-sekretów, IP,
  SHA-256, słowo kluczowe, page_id). Lista pól jest jawna w karcie i w Podglądzie (`fields_sent`, `redactions`).
- Nigdy: treści artefaktów (obrazy, HTML, nagrania, pliki), tekst OCR, nazwy plików, pliki organizatora, tytuły
  incydentów (poza wskazanymi eksportami plikowymi po redakcji), klucze i tokeny.
- Dane NASK nie są wysyłane do żadnej usługi zewnętrznej w demonstracji; każda wysyłka wymaga jawnej decyzji
  operatora (klucz, tryb online, Testuj, Włącz, Uruchom).
- Uwaga licencyjna: publiczne API VirusTotal i Google Safe Browsing v4 nie są przeznaczone do zastosowań
  komercyjnych (§4, E-INTEG-3); przed użyciem produkcyjnym potrzebna jest licencja komercyjna (VirusTotal Premium,
  Google Web Risk).

## 10. Token Meta Ad Library

1. Załóż konto Meta for Developers i utwórz aplikację (https://developers.facebook.com/).
2. Potwierdź tożsamość i lokalizację konta, jeśli Meta tego wymaga (strona https://www.facebook.com/ads/library/api/
   odpowiadała 403 przy sprawdzaniu 2026-11-22, więc wymóg jest niezweryfikowany).
3. W Graph API Explorer wygeneruj token dostępu dla aplikacji (endpoint `ads_archive`).
4. Zapisz token: Centrum integracji → Meta Ad Library API → Połącz (pole `META_AD_LIBRARY_TOKEN`), albo ręcznie w
   `~/.config/hydra/meta.env` (`META_AD_LIBRARY_TOKEN=…`, `chmod 600`), albo jako zmienną środowiskową.
5. Ustaw `HYDRA_OFFLINE=0` i `HYDRA_ENRICH=1`, uruchom API ponownie, kliknij Testuj, potem Włącz.
6. Zapytanie: `GET https://graph.facebook.com/<wersja>/ads_archive` z wymaganym `ad_reached_countries` (HYDRA: PL),
   `search_terms` (do 100 znaków) lub identyfikatorami stron (do 10). Reklamy, które nie dotarły do UE, są zwracane
   tylko, jeśli dotyczą polityki lub spraw społecznych. Przekroczenie limitu: błąd 613 (progi nie są publikowane).
7. Jeśli token wygaśnie (czas życia tokenów Meta nie był sprawdzany w tej sesji), Test zwraca błąd i token trzeba
   wygenerować ponownie.
