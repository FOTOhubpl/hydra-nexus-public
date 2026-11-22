"""Connector registry: one typed, frozen descriptor per external service HYDRA NEXUS can talk to or export for.

Implementation levels are honest:
- `live`: a client in `hydra.integrations.clients` (or an existing enrichment module) is implemented and tested
  against hand-written fixtures built from the provider's public documentation (no real calls in tests);
- `export-format`: HYDRA produces the provider's import format locally (download / dry-run preview); a push is
  offered only where a client exists (`live_impl` set);
- `planned`: descriptor only; the API answers 409 for connect/test/run/export/secret.

Provider facts (limits, licences, authentication) were checked against public pages on 2026-11-22 where the
docs agent could reach them; unverified points say so in `notes_pl`.
"""

from __future__ import annotations

from dataclasses import dataclass, field

CATEGORIES: dict[str, tuple[str, str]] = {
    "enrichment": ("Wzbogacanie", "Publiczne rejestry i reputacja wskaźników (domeny, URL, IP, skróty plików)."),
    "threat_intel": ("Threat intelligence", "Platformy wymiany wskaźników zagrożeń (MISP, OpenCTI/TAXII, STIX)."),
    "ad_transparency": ("Przejrzystość reklam", "Biblioteki reklam platform (Meta, Google, TikTok, LinkedIn, X)."),
    "outbound_sharing": ("Zgłaszanie i udostępnianie", "Pakiety zgłoszeń nadużyć i dowodów do przekazania dalej."),
    "database_sink": ("Bazy danych i SIEM", "Eksport do baz grafowych, SQL, Elasticsearch i systemów SIEM."),
    "notification": ("Powiadomienia", "Webhooki i komunikatory (Slack, Teams, e-mail)."),
}
DIRECTIONS = ("inbound", "outbound", "both")
AUTHS = ("none", "api_key", "oauth_token", "basic", "hmac")
LEVELS: dict[str, tuple[str, str]] = {
    "live": ("Na żywo (live)", "Klient zaimplementowany; testowany na fiksturach z dokumentacji, bez wywołań "
                               "produkcyjnych. Wymaga trybu online (HYDRA_OFFLINE=0) i jawnego włączenia."),
    "export-format": ("Format eksportu", "HYDRA tworzy plik w formacie importu dostawcy lokalnie (pobranie lub "
                                         "podgląd na sucho). Działa offline; wysyłka tylko tam, gdzie jest klient."),
    "planned": ("Planowane", "Tylko opis integracji. Brak klienta, brak przycisków połącz/testuj/uruchom."),
}
FORMATS = ("stix21", "misp", "csv", "ndjson", "es_bulk", "siem_json", "neo4j", "xarf", "evidence", "sql",
           "webhook_json")
DATA_FIELDS = ("domain", "url", "ip", "sha256", "phash", "campaign_id", "decision", "score_bits", "first_seen",
               "last_seen", "count", "keyword", "ad_id", "page_id", "asn", "cert_fingerprint")
# Never part of any data_out: listed so tests (and reviewers) can check the registry against them.
FORBIDDEN_FIELDS = ("artifact_bytes", "ocr_text", "file_name", "organiser_file", "secret", "token", "screenshot",
                    "html", "text", "title", "label")
# Connectors whose action needs no network at all (local snapshot lookup): run/test work offline.
LOCAL_ONLY = frozenset({"cert-pl"})

NEVER_DEFAULT = ("Nigdy: treści artefaktów (obrazy, HTML, nagrania), tekst OCR, nazwy plików, pliki organizatora, "
                 "tytuły incydentów, klucze i tokeny.")


@dataclass(frozen=True)
class Connector:
    id: str
    name: str
    category: str
    direction: str
    auth: str
    level: str
    description_pl: str
    docs_url: str
    secrets: tuple[str, ...] = ()
    rate_limit_pl: str = "Brak publicznie podanego limitu."
    rate_limit_per_min: float | None = None
    data_out: tuple[str, ...] = ()
    data_out_pl: str = "Nic nie jest wysyłane."
    never_sent_pl: str = NEVER_DEFAULT
    licence_pl: str = ""
    formats: tuple[str, ...] = ()
    setup_pl: tuple[str, ...] = field(default_factory=tuple)
    notes_pl: str = ""
    base_url: str | None = None
    live_impl: str | None = None


@dataclass(frozen=True)
class Preset:
    id: str
    name_pl: str
    description_pl: str
    connectors: tuple[str, ...]


_FIXTURE_NOTE = ("Testy klienta używają przykładowych odpowiedzi napisanych ręcznie na podstawie publicznej "
                 "dokumentacji (nie nagranych z prawdziwych wywołań).")

CONNECTORS: tuple[Connector, ...] = (
    # ------------------------------------------------------------------ ad transparency
    Connector(
        id="meta-adlibrary", name="Meta Ad Library API", category="ad_transparency", direction="inbound",
        auth="oauth_token", level="live", secrets=("META_AD_LIBRARY_TOKEN",),
        description_pl="Wyszukiwanie reklam w bibliotece reklam Meta (Facebook, Instagram) po słowach kluczowych "
                       "i identyfikatorach stron; zasila harmonogram (pull reklam do skrzynki importu).",
        docs_url="https://developers.facebook.com/docs/graph-api/reference/ads_archive/",
        rate_limit_pl="Limit Graph API nie jest podany liczbowo; przekroczenie zwraca błąd 613 (HYDRA ponawia z "
                      "wycofaniem 1/2/4 s).", rate_limit_per_min=30,
        data_out=("keyword", "page_id"), data_out_pl="Do Meta trafiają słowa kluczowe z listy obserwacji i "
                                                     "identyfikatory stron (page_id), kraj PL.",
        licence_pl="Warunki Meta Platform (niezweryfikowane). Reklamy, które nie dotarły do UE, są zwracane tylko "
                   "gdy dotyczą polityki/spraw społecznych (sprawdzone 2026-11-22).",
        setup_pl=("Załóż konto Meta for Developers i utwórz aplikację.",
                  "Potwierdź tożsamość i lokalizację (facebook.com/ID).",
                  "Zaakceptuj warunki Ad Library API i wygeneruj token w Graph API Explorer.",
                  "Wklej token jako META_AD_LIBRARY_TOKEN (plik ~/.config/hydra/meta.env, 0600).",
                  "Ustaw HYDRA_OFFLINE=0 i HYDRA_ENRICH=1, kliknij Testuj."),
        notes_pl="Wymóg potwierdzenia tożsamości: niezweryfikowane (strona 403 przy sprawdzaniu 2026-11-22). "
                 "Uruchomienie wywołuje istniejące zadanie harmonogramu `adlibrary_pull`. " + _FIXTURE_NOTE,
        base_url="https://graph.facebook.com", live_impl="hydra.integrations.clients.meta_adlibrary",
    ),
    Connector(
        id="google-ads-transparency", name="Google Ads Transparency Center", category="ad_transparency",
        direction="inbound", auth="none", level="planned",
        description_pl="Centrum przejrzystości reklam Google (wyszukiwarka, YouTube, sieć reklamowa).",
        docs_url="https://adstransparency.google.com/",
        licence_pl="Warunki Google. Zbiór BigQuery `bigquery-public-data.google_political_ads` obejmuje tylko "
                   "reklamy polityczne.",
        notes_pl="Brak publicznego API (nie znaleziono dokumentacji) — stan 2026-11-22: dostępna jest tylko strona "
                 "WWW i zbiór BigQuery z reklamami politycznymi (nazwa zbioru niezweryfikowana). HYDRA nie używa "
                 "nieudokumentowanych wewnętrznych endpointów. "
                 "Import ręczny: zrzut ekranu lub zapis strony reklamy przez import HYDRA.",
    ),
    Connector(
        id="tiktok-commercial-content", name="TikTok Commercial Content API", category="ad_transparency",
        direction="inbound", auth="oauth_token", level="planned",
        description_pl="Biblioteka reklam i treści komercyjnych TikTok.",
        docs_url="https://developers.tiktok.com/products/commercial-content-api",
        licence_pl="Warunki TikTok for Developers; na obecnym etapie dane tylko z krajów UE.",
        notes_pl="Dostęp tylko po złożeniu wniosku (konto deweloperskie + formularz, odpowiedź ok. 2 dni robocze; "
                 "sprawdzone 2026-11-22). Klient powstanie po uzyskaniu klucza.",
    ),
    Connector(
        id="linkedin-ad-library", name="LinkedIn Ad Library", category="ad_transparency", direction="inbound",
        auth="oauth_token", level="planned",
        description_pl="Biblioteka reklam LinkedIn (wymóg DSA).", docs_url="https://www.linkedin.com/ad-library/",
        notes_pl="Brak publicznego API (nie znaleziono dokumentacji) — stan 2026-11-22; tylko opis.",
    ),
    Connector(
        id="x-ads-repository", name="X Ads Repository", category="ad_transparency", direction="inbound",
        auth="oauth_token", level="planned",
        description_pl="Repozytorium reklam X (dawniej Twitter), wymóg DSA.",
        docs_url="https://ads.x.com/ads-repository",
        notes_pl="Brak publicznego API (nie znaleziono dokumentacji) — stan 2026-11-22; tylko opis.",
    ),
    # ------------------------------------------------------------------ enrichment / reputation
    Connector(
        id="urlscan", name="urlscan.io", category="enrichment", direction="inbound", auth="api_key",
        level="live", secrets=("URLSCAN_API_KEY",),
        description_pl="Wyszukiwanie istniejących skanów domeny w urlscan.io (GET /api/v1/search/?q=domain:X). "
                       "HYDRA nie zleca nowych skanów.",
        docs_url="https://docs.urlscan.io/pages/api-intro",
        rate_limit_pl="Limity zależą od planu konta (niezweryfikowane liczbowo); HYDRA ogranicza do 30/min.",
        rate_limit_per_min=30, data_out=("domain",),
        data_out_pl="Do urlscan.io trafia nazwa domeny (zapytanie wyszukiwania).",
        licence_pl="Warunki urlscan.io; wyniki wyszukiwania publicznych skanów.",
        setup_pl=("Załóż konto na urlscan.io i wygeneruj klucz API (Settings & API).",
                  "Wklej klucz jako URLSCAN_API_KEY.", "Ustaw HYDRA_OFFLINE=0, kliknij Testuj, potem Włącz."),
        notes_pl="Nagłówek API-Key (nazwa nagłówka niezweryfikowana na stronie wprowadzenia). " + _FIXTURE_NOTE, base_url="https://urlscan.io",
        live_impl="hydra.integrations.clients.urlscan",
    ),
    Connector(
        id="virustotal", name="VirusTotal", category="enrichment", direction="inbound", auth="api_key",
        level="live", secrets=("VT_API_KEY",),
        description_pl="Reputacja domen, adresów URL, IP i skrótów SHA-256 (API v3: /domains, /urls, "
                       "/ip_addresses, /files).",
        docs_url="https://docs.virustotal.com/reference/public-vs-premium-api",
        rate_limit_pl="Publiczne API: 4 zapytania/min, 500/dzień (sprawdzone 2026-11-22).", rate_limit_per_min=4,
        data_out=("domain", "url", "ip", "sha256"),
        data_out_pl="Do VirusTotal trafia domena, URL (jako identyfikator base64url), IP lub SHA-256 pliku. "
                    "Same pliki nigdy nie są wysyłane.",
        licence_pl="Publiczne API nie może być używane w produktach komercyjnych ani procesach biznesowych, które "
                   "nie wnoszą nowych plików (sprawdzone 2026-11-22); do zastosowań komercyjnych Premium API.",
        setup_pl=("Załóż konto VirusTotal i skopiuj klucz API z profilu.", "Wklej klucz jako VT_API_KEY.",
                  "Ustaw HYDRA_OFFLINE=0, kliknij Testuj, potem Włącz."),
        notes_pl="Nagłówek x-apikey. " + _FIXTURE_NOTE, base_url="https://www.virustotal.com",
        live_impl="hydra.integrations.clients.virustotal",
    ),
    Connector(
        id="urlhaus", name="URLhaus (abuse.ch)", category="threat_intel", direction="inbound", auth="api_key",
        level="live", secrets=("URLHAUS_AUTH_KEY",),
        description_pl="Sprawdzenie hosta lub URL w bazie złośliwych adresów URLhaus (POST /v1/host/, /v1/url/).",
        docs_url="https://urlhaus-api.abuse.ch/",
        rate_limit_pl="Limit na minutę nie jest podany; wyniki ograniczone do 100 URL na host (sprawdzone "
                      "2026-11-22). HYDRA ogranicza do 30/min.", rate_limit_per_min=30,
        data_out=("domain", "url", "ip"), data_out_pl="Do abuse.ch trafia host (domena/IP) albo URL.",
        licence_pl="Warunki abuse.ch (nie sprawdzone na stronie API).",
        setup_pl=("Zaloguj się na auth.abuse.ch i wygeneruj Auth-Key.", "Wklej klucz jako URLHAUS_AUTH_KEY.",
                  "Ustaw HYDRA_OFFLINE=0, kliknij Testuj, potem Włącz."),
        notes_pl="Nagłówek Auth-Key wymagany dla wszystkich wywołań (obowiązkowy od 2025-06-30, blog abuse.ch "
                 "2024-11-18). " + _FIXTURE_NOTE,
        base_url="https://urlhaus-api.abuse.ch", live_impl="hydra.integrations.clients.urlhaus",
    ),
    Connector(
        id="phishtank", name="PhishTank", category="threat_intel", direction="inbound", auth="api_key",
        level="planned", secrets=(),
        description_pl="Społecznościowa baza phishingu (Cisco Talos), pliki co godzinę.",
        docs_url="https://www.phishtank.com/developer_info.php",
        rate_limit_pl="Bez klucza tylko kilka pobrań dziennie (sprawdzone 2026-11-22).",
        licence_pl="Warunki Cisco Talos (nie przeczytane, niezweryfikowane).",
        notes_pl="Rejestracja nowych kluczy bywa wstrzymana (niezweryfikowane). Planowane: pobieranie pliku i "
                 "lokalne dopasowanie jak w OpenPhish.",
    ),
    Connector(
        id="openphish", name="OpenPhish (feed społecznościowy)", category="threat_intel", direction="inbound",
        auth="none", level="live",
        description_pl="Pobiera publiczny plik feed.txt i dopasowuje wskaźniki LOKALNIE: do OpenPhish nie trafia "
                       "nic o naszych domenach ani URL-ach.",
        docs_url="https://openphish.com/phishing_feeds.html",
        rate_limit_pl="Feed społecznościowy aktualizowany co 12 h (sprawdzone 2026-11-22); HYDRA pobiera najwyżej raz "
                      "na godzinę.", rate_limit_per_min=1,
        data_out=(), data_out_pl="Nic o wskaźnikach: tylko zwykłe pobranie publicznego pliku (GET feed.txt).",
        licence_pl="Wymaga akceptacji Terms of Use OpenPhish; użycie komercyjne nie jest wprost dozwolone (plany "
                   "komercyjne są płatne), niezweryfikowane.",
        setup_pl=("Klucz nie jest potrzebny.", "Ustaw HYDRA_OFFLINE=0, kliknij Testuj, potem Włącz."),
        notes_pl="Dopasowanie lokalne (dokładny URL lub host). " + _FIXTURE_NOTE,
        base_url="https://raw.githubusercontent.com", live_impl="hydra.integrations.clients.openphish",
    ),
    Connector(
        id="google-safe-browsing", name="Google Safe Browsing v4", category="threat_intel", direction="inbound",
        auth="api_key", level="live", secrets=("GOOGLE_SAFE_BROWSING_KEY",),
        description_pl="Sprawdzenie URL-i na listach zagrożeń Google (threatMatches:find).",
        docs_url="https://developers.google.com/safe-browsing/v4",
        rate_limit_pl="Limit nie jest podany na stronie (niezweryfikowane); HYDRA ogranicza do 60/min.",
        rate_limit_per_min=60, data_out=("url",),
        data_out_pl="Do Google trafiają pełne URL-e (z usuniętymi parametrami zawierającymi sekrety).",
        licence_pl="Safe Browsing API tylko do użytku niekomercyjnego; do zastosowań komercyjnych Web Risk API "
                   "(Google Cloud). Wersja v4 oznaczona jako przestarzała (sprawdzone 2026-11-22).",
        setup_pl=("Utwórz projekt w Google Cloud Console i włącz Safe Browsing API.",
                  "Utwórz klucz API i wklej go jako GOOGLE_SAFE_BROWSING_KEY.",
                  "Ustaw HYDRA_OFFLINE=0, kliknij Testuj, potem Włącz."),
        notes_pl="Klucz w parametrze key (usuwany z audytu i pamięci podręcznej). Web Risk: tylko opis. "
                 + _FIXTURE_NOTE,
        base_url="https://safebrowsing.googleapis.com", live_impl="hydra.integrations.clients.safebrowsing",
    ),
    Connector(
        id="cert-pl", name="CERT Polska — Lista ostrzeżeń", category="threat_intel", direction="inbound",
        auth="none", level="live",
        description_pl="Sprawdzenie hostów na liście ostrzeżeń przed niebezpiecznymi stronami CERT Polska "
                       "(lokalna migawka pliku domains.json, wyszukiwanie bez sieci).",
        docs_url="https://cert.pl/lista-ostrzezen/",
        rate_limit_pl="Plik aktualizowany co 5 minut (sprawdzone 2026-11-22); wyszukiwanie lokalne bez limitu.",
        data_out=(), data_out_pl="Nic: wyszukiwanie w lokalnej migawce. Odświeżenie migawki to zwykłe pobranie "
                                 "publicznego pliku (scripts).",
        licence_pl="Do dowolnego użytku, także komercyjnego, bez gwarancji; przypisanie nie może sugerować "
                   "poparcia NASK (sprawdzone 2026-11-22).",
        setup_pl=("Klucz nie jest potrzebny.", "Migawka: $HYDRA_DATA_DIR/external/cert/domains.json."),
        notes_pl="Działa offline (istniejący moduł hydra.enrich.cert_pl). Trafienie domeny nadrzędnej jest "
                 "słabsze niż trafienie hosta.",
        base_url="https://hole.cert.pl", live_impl="hydra.integrations.clients.local",
    ),
    Connector(
        id="crtsh", name="crt.sh (Certificate Transparency)", category="enrichment", direction="inbound",
        auth="none", level="live",
        description_pl="Certyfikaty TLS domeny z logów Certificate Transparency (wystawcy, nazwy SAN, daty).",
        docs_url="https://crt.sh/", rate_limit_pl="Bezpłatna usługa bez SLA (częste 502); HYDRA: 1 zapytanie/3 s.",
        rate_limit_per_min=20, data_out=("domain",), data_out_pl="Do crt.sh trafia nazwa domeny.",
        licence_pl="Dane CT są publiczne z założenia (RFC 6962); warunki usługi crt.sh niezweryfikowane.",
        setup_pl=("Klucz nie jest potrzebny.", "Ustaw HYDRA_OFFLINE=0 i HYDRA_ENRICH=1."),
        notes_pl="Istniejący moduł hydra.enrich.crtsh z pamięcią podręczną i odtwarzaniem offline.",
        base_url="https://crt.sh", live_impl="hydra.integrations.clients.local",
    ),
    Connector(
        id="rdap", name="RDAP (rdap.org)", category="enrichment", direction="inbound", auth="none", level="live",
        description_pl="Dane rejestracyjne domen i sieci IP (rejestrator, daty, status, serwery nazw).",
        docs_url="https://about.rdap.org/",
        rate_limit_pl="rdap.org: 10 zapytań / 10 s, nadmiar kończy się HTTP 429 (sprawdzone 2026-11-22).",
        rate_limit_per_min=60, data_out=("domain", "ip"), data_out_pl="Do rdap.org i rejestru trafia domena lub IP.",
        licence_pl="Dane RDAP są publiczne (ICANN), pola rejestrującego zanonimizowane (RODO).",
        setup_pl=("Klucz nie jest potrzebny.", "Ustaw HYDRA_OFFLINE=0 i HYDRA_ENRICH=1."),
        notes_pl="Istniejący moduł hydra.enrich.rdap z pamięcią podręczną i odtwarzaniem offline.",
        base_url="https://rdap.org", live_impl="hydra.integrations.clients.local",
    ),
    Connector(
        id="dns", name="DNS (publiczne resolwery)", category="enrichment", direction="inbound", auth="none",
        level="live", description_pl="Rekordy A, AAAA, CNAME, NS, MX, TXT przez publiczny resolwer (dig @1.1.1.1).",
        docs_url="https://developers.cloudflare.com/1.1.1.1/", rate_limit_pl="HYDRA: 20 zapytań/s.",
        rate_limit_per_min=600, data_out=("domain",), data_out_pl="Do resolwera DNS trafia nazwa domeny.",
        licence_pl="Publiczny resolwer 1.1.1.1 (polityka prywatności Cloudflare).",
        setup_pl=("Klucz nie jest potrzebny.", "Ustaw HYDRA_OFFLINE=0 i HYDRA_ENRICH=1."),
        notes_pl="Istniejący moduł hydra.enrich.dns (dig) z pamięcią podręczną i odtwarzaniem offline.",
        base_url=None, live_impl="hydra.integrations.clients.local",
    ),
    Connector(
        id="shodan", name="Shodan", category="enrichment", direction="inbound", auth="api_key", level="planned",
        description_pl="Usługi i banery hostów IP.", docs_url="https://developer.shodan.io/api",
        rate_limit_pl="1 zapytanie/s, kredyty zapytań (sprawdzone 2026-11-22).",
        notes_pl="Klucz w parametrze ?key=. Tylko opis.",
    ),
    Connector(
        id="censys", name="Censys", category="enrichment", direction="inbound", auth="api_key", level="planned",
        description_pl="Hosty i certyfikaty z internetowego skanowania.", docs_url="https://docs.censys.com/reference/get-started",
        rate_limit_pl="Plan darmowy: tylko wyszukiwanie pojedynczych hostów/certyfikatów, 1 równoczesne zapytanie "
                      "(sprawdzone 2026-11-22).",
        notes_pl="Platform API: Authorization: Bearer <PAT> (+ X-Organization-ID). Tylko opis.",
    ),
    Connector(
        id="abuseipdb", name="AbuseIPDB", category="threat_intel", direction="inbound", auth="api_key",
        level="live", secrets=("ABUSEIPDB_KEY",),
        description_pl="Raporty nadużyć dla adresów IP (GET /api/v2/check).", docs_url="https://docs.abuseipdb.com/",
        rate_limit_pl="Plan darmowy: check 1000/dzień (sprawdzone 2026-11-22); HYDRA ogranicza do 30/min.",
        rate_limit_per_min=30, data_out=("ip",), data_out_pl="Do AbuseIPDB trafia adres IP.",
        licence_pl="Warunki AbuseIPDB (niezweryfikowane).",
        setup_pl=("Załóż konto AbuseIPDB i utwórz klucz API.", "Wklej klucz jako ABUSEIPDB_KEY.",
                  "Ustaw HYDRA_OFFLINE=0, kliknij Testuj, potem Włącz."),
        notes_pl="Nagłówek Key. " + _FIXTURE_NOTE, base_url="https://api.abuseipdb.com",
        live_impl="hydra.integrations.clients.abuseipdb",
    ),
    # ------------------------------------------------------------------ threat-intel sharing
    Connector(
        id="misp", name="MISP", category="threat_intel", direction="outbound", auth="api_key",
        level="export-format", secrets=("MISP_URL", "MISP_KEY"), formats=("misp",),
        description_pl="Zdarzenie MISP (JSON) z domenami, URL-ami, IP i SHA-256 kampanii; plik do importu albo "
                       "opcjonalna wysyłka POST {MISP_URL}/events/add.",
        docs_url="https://www.misp-project.org/openapi/",
        rate_limit_pl="Instancja własna, bez limitu dostawcy; HYDRA ogranicza do 10/min.", rate_limit_per_min=10,
        data_out=("domain", "url", "ip", "sha256", "campaign_id", "decision", "score_bits", "first_seen",
                  "last_seen", "count"),
        data_out_pl="Wskaźniki kampanii (domena, URL bez parametrów-sekretów, IP, SHA-256), id kampanii, decyzja, "
                    "bity, liczba incydentów, daty. Dystrybucja 0 (tylko organizacja), TLP:AMBER.",
        licence_pl="Oprogramowanie MISP: AGPL-3.0 (HYDRA nie zawiera kodu MISP, tylko format JSON).",
        setup_pl=("Utwórz klucz API w MISP (Administration → Auth keys).",
                  "Zapisz MISP_URL (np. https://misp.example.org) i MISP_KEY.",
                  "Sprawdź podgląd (na sucho), potem Testuj i Uruchom (wysyłka)."),
        notes_pl="Nagłówek Authorization: <klucz>. Wysyłka opcjonalna; adres instancji przechowywany jako sekret.",
        live_impl="hydra.integrations.clients.push",
    ),
    Connector(
        id="opencti-taxii", name="OpenCTI / TAXII 2.1 (STIX 2.1)", category="threat_intel", direction="outbound",
        auth="none", level="export-format", formats=("stix21",),
        description_pl="Pakiet STIX 2.1 (kampania, wskaźniki, infrastruktura, obserwacje) do importu w OpenCTI lub "
                       "innym kliencie TAXII.", docs_url="https://docs.oasis-open.org/cti/stix/v2.1/stix-v2.1.html",
        data_out=("domain", "url", "ip", "sha256", "campaign_id", "score_bits", "first_seen", "last_seen"),
        data_out_pl="Obiekty STIX: domeny, wzorce URL (bez zapytań), IP, SHA-256, kampania z datami.",
        never_sent_pl="Nic nie jest wysyłane automatycznie (plik). Pakiet zawiera opis kampanii i tytuły incydentów "
                      "po redakcji (external_references); nigdy treści artefaktów, OCR, plików organizatora, kluczy.",
        licence_pl="STIX 2.1: standard OASIS; biblioteka stix2 (BSD-3-Clause).",
        setup_pl=("Pobierz plik STIX 2.1.", "W OpenCTI: Data → Import → plik JSON (konektor ImportFileStix)."),
        notes_pl="Wysyłka TAXII 2.1 (push) planowana; obecnie tylko plik.",
    ),
    # ------------------------------------------------------------------ database / SIEM sinks
    Connector(
        id="neo4j", name="Neo4j (graf)", category="database_sink", direction="outbound", auth="none",
        level="export-format", formats=("neo4j",),
        description_pl="Skrypt Cypher z grafem incydentów, genów i kampanii do załadowania w Neo4j.",
        docs_url="https://neo4j.com/docs/cypher-manual/current/",
        data_out=("domain", "url", "ip", "sha256", "phash", "campaign_id", "score_bits", "count"),
        data_out_pl="Węzły incydentów (id), genów (rodzaj, wartość po redakcji, częstość) i kampanii; krawędzie z "
                    "bitami.", licence_pl="Format Cypher; Neo4j Community GPL-3.0 (HYDRA nie zawiera kodu Neo4j).",
        setup_pl=("Pobierz plik .cypher.", "Uruchom w Neo4j Browser lub cypher-shell."),
        notes_pl="Istniejący eksport hydra.export_neo4j.",
    ),
    Connector(
        id="elasticsearch", name="Elasticsearch / OpenSearch (_bulk)", category="database_sink",
        direction="outbound", auth="api_key", level="export-format", secrets=("ES_URL", "ES_API_KEY"),
        formats=("es_bulk", "ndjson"),
        description_pl="Wskaźniki jako NDJSON dla API _bulk (linia akcji + dokument); plik albo opcjonalna wysyłka "
                       "POST {ES_URL}/_bulk.",
        docs_url="https://www.elastic.co/docs/api/doc/elasticsearch/operation/operation-bulk",
        rate_limit_pl="Domyślnie do 100 MB na żądanie (sprawdzone 2026-11-22); HYDRA ogranicza do 10/min.",
        rate_limit_per_min=10,
        data_out=("domain", "url", "ip", "sha256", "campaign_id", "decision", "score_bits", "first_seen",
                  "last_seen", "count"),
        data_out_pl="Jeden dokument na wskaźnik (typ, wartość, kampania, decyzja, bity, liczba, daty).",
        licence_pl="Instancja własna.",
        setup_pl=("Utwórz klucz API (Kibana → Stack Management → API keys) z prawem zapisu do indeksu "
                  "hydra-indicators.", "Zapisz ES_URL i ES_API_KEY (zakodowany id:klucz).",
                  "Sprawdź podgląd, potem Testuj i Uruchom."),
        notes_pl="Nagłówek Authorization: ApiKey <klucz>, Content-Type application/x-ndjson.",
        live_impl="hydra.integrations.clients.push",
    ),
    Connector(
        id="splunk-hec", name="Splunk HTTP Event Collector", category="database_sink", direction="outbound",
        auth="api_key", level="export-format", secrets=("SPLUNK_HEC_URL", "SPLUNK_HEC_TOKEN"),
        formats=("siem_json",),
        description_pl="Zdarzenia HEC (JSON, sourcetype hydra:indicator); plik albo opcjonalna wysyłka POST "
                       "{SPLUNK_HEC_URL}/services/collector/event.",
        docs_url="https://help.splunk.com/en/splunk-enterprise/get-started/get-data-in/9.4/get-data-with-http-event-"
                 "collector/format-events-for-http-event-collector",
        rate_limit_pl="Instancja własna; HYDRA ogranicza do 10/min.", rate_limit_per_min=10,
        data_out=("domain", "url", "ip", "sha256", "campaign_id", "decision", "score_bits", "first_seen",
                  "last_seen", "count"),
        data_out_pl="Jedno zdarzenie na wskaźnik (pola jak w eksporcie wskaźników).", licence_pl="Instancja własna.",
        setup_pl=("Splunk: Settings → Data inputs → HTTP Event Collector → New Token.",
                  "Zapisz SPLUNK_HEC_URL (np. https://splunk.example.org:8088) i SPLUNK_HEC_TOKEN.",
                  "Sprawdź podgląd, potem Testuj i Uruchom."),
        notes_pl="Endpoint /services/collector/event (port 8088), nagłówek Authorization: Splunk <token>, klucze "
                 "time/host/source/sourcetype/event, zdarzenia łączone w jednym żądaniu (sprawdzone 2026-11-22).",
        live_impl="hydra.integrations.clients.push",
    ),
    Connector(
        id="siem-generic", name="SIEM (NDJSON / JSON)", category="database_sink", direction="outbound", auth="none",
        level="export-format", formats=("ndjson", "siem_json"),
        description_pl="Uniwersalny plik NDJSON (jeden wskaźnik na linię) lub zdarzenia w stylu HEC dla dowolnego "
                       "SIEM (QRadar, Sentinel, Wazuh, Graylog).", docs_url="https://github.com/ndjson/ndjson-spec",
        data_out=("domain", "url", "ip", "sha256", "campaign_id", "decision", "score_bits", "first_seen",
                  "last_seen", "count"),
        data_out_pl="Jeden rekord na wskaźnik.", licence_pl="Format otwarty.",
        setup_pl=("Pobierz plik.", "Wczytaj go agentem plikowym SIEM."),
    ),
    Connector(
        id="sql-export", name="SQL (SQLite / PostgreSQL)", category="database_sink", direction="outbound",
        auth="none", level="export-format", formats=("sql",),
        description_pl="Skrypt SQL (CREATE TABLE + INSERT) z tabelami wskaźników i kampanii, zgodny z SQLite i "
                       "PostgreSQL.", docs_url="https://www.sqlite.org/lang.html",
        data_out=("domain", "url", "ip", "sha256", "campaign_id", "decision", "score_bits", "first_seen",
                  "last_seen", "count"),
        data_out_pl="Tabele hydra_indicators i hydra_campaigns.", licence_pl="Format otwarty.",
        setup_pl=("Pobierz plik .sql.", "sqlite3 baza.db < plik.sql  albo  psql -f plik.sql"),
    ),
    Connector(
        id="csv-export", name="CSV wskaźników", category="database_sink", direction="outbound", auth="none",
        level="export-format", formats=("csv",),
        description_pl="Tabela wskaźników (CSV, UTF-8, zabezpieczona przed formułami arkusza).",
        docs_url="https://www.rfc-editor.org/rfc/rfc4180",
        data_out=("domain", "url", "ip", "sha256", "campaign_id", "decision", "score_bits", "first_seen",
                  "last_seen", "count"),
        data_out_pl="Jeden wiersz na wskaźnik.", licence_pl="Format otwarty.", setup_pl=("Pobierz plik CSV.",),
    ),
    # ------------------------------------------------------------------ notifications
    Connector(
        id="webhook-generic", name="Webhook (HMAC-SHA256)", category="notification", direction="outbound",
        auth="hmac", level="live", formats=("webhook_json",),
        description_pl="Podpisane powiadomienia o zdarzeniach HYDRA (nowa kampania, zadanie, alert obserwacji, "
                       "przegląd, eksport). Konfiguracja w zakładce Webhooki.",
        docs_url="https://www.rfc-editor.org/rfc/rfc2104",
        data_out=("campaign_id", "decision", "count", "domain", "first_seen", "last_seen"),
        data_out_pl="Typ zdarzenia, identyfikatory i wskaźniki (bez treści).",
        licence_pl="Własny endpoint odbiorcy.",
        setup_pl=("Otwórz zakładkę Webhooki.", "Dodaj endpoint, wygeneruj sekret, wybierz zdarzenia.",
                  "Wyślij ping testowy."),
        notes_pl="Zarządzane przez /v1/integrations/webhooks (osobny router). Loopback działa offline.",
        live_impl="hydra.integrations.webhooks",
    ),
    Connector(
        id="slack", name="Slack (Incoming Webhook)", category="notification", direction="outbound", auth="api_key",
        level="export-format", secrets=("SLACK_WEBHOOK_URL",), formats=("webhook_json",),
        description_pl="Krótkie podsumowanie kampanii (id, liczby, najczęstsze domeny) na kanał Slack.",
        docs_url="https://docs.slack.dev/messaging/sending-messages-using-incoming-webhooks",
        rate_limit_pl="Limit nie jest podany na stronie (niezweryfikowane); HYDRA ogranicza do 10/min.",
        rate_limit_per_min=10, data_out=("campaign_id", "decision", "count", "domain"),
        data_out_pl="Id kampanii, decyzja, liczba incydentów i do 5 domen.", licence_pl="Warunki Slack.",
        setup_pl=("Slack: utwórz aplikację, włącz Incoming Webhooks, dodaj webhook do kanału.",
                  "Zapisz adres jako SLACK_WEBHOOK_URL (adres sam w sobie jest sekretem).",
                  "Sprawdź podgląd, potem Uruchom."),
        notes_pl="Wysyłka POST {\"text\": ...} (sprawdzone 2026-11-22). Slack unieważnia ujawnione adresy webhooków.",
        live_impl="hydra.integrations.clients.push",
    ),
    Connector(
        id="teams", name="Microsoft Teams", category="notification", direction="outbound", auth="api_key",
        level="planned", description_pl="Powiadomienia na kanał Teams (Workflows / Power Automate).",
        docs_url="https://learn.microsoft.com/microsoftteams/platform/webhooks-and-connectors/what-are-webhooks-and-connectors",
        notes_pl="Konektory Office 365 (w tym Incoming Webhooks oparte na konektorach) wycofane w maju 2026; obecnie "
                 "webhook przez aplikację Workflows (wyzwalacz 'When a Teams webhook request is received'), sprawdzone "
                 "2026-11-22. Tylko opis; do tego czasu użyj webhooka ogólnego z przepływem Workflows.",
    ),
    Connector(
        id="email-smtp", name="E-mail (SMTP)", category="notification", direction="outbound", auth="basic",
        level="planned", description_pl="Raporty e-mail przez własny serwer SMTP.",
        docs_url="https://www.rfc-editor.org/rfc/rfc5321", notes_pl="Tylko opis.",
    ),
    # ------------------------------------------------------------------ outbound sharing
    Connector(
        id="xarf", name="XARF v4 (zgłoszenia nadużyć)", category="outbound_sharing", direction="outbound",
        auth="none", level="export-format", formats=("xarf",),
        description_pl="Pakiet ZIP ze zgłoszeniami XARF v4 na każdy host kampanii i notami po polsku dla "
                       "rejestratorów i hostingodawców. Wysyłka wyłącznie ręczna.",
        docs_url="https://github.com/xarf/xarf-spec",
        data_out=("domain", "url", "ip", "campaign_id", "first_seen", "last_seen"),
        data_out_pl="Host, URL-e, IP, kontakty abuse z RDAP, id kampanii, daty.",
        never_sent_pl="Nic nie jest wysyłane automatycznie. Uwaga: zgłoszenie XARF zawiera wyciąg dowodu (fragment "
                      "HAR / zrzut ekranu) jako załącznik base64 — sprawdź pakiet przed ręczną wysyłką. Nigdy tekst "
                      "OCR, pliki organizatora ani klucze.",
        licence_pl="XARF v4 (xarf.org), MIT.",
        setup_pl=("Wybierz kampanię i pobierz pakiet.", "Uzupełnij HYDRA_REPORTER_ORG / HYDRA_REPORTER_EMAIL.",
                  "Wyślij ręcznie po weryfikacji."),
        notes_pl="Istniejący eksport hydra.export_abuse; wymaga campaign_id.",
    ),
    Connector(
        id="evidence-package", name="Podpisany pakiet dowodowy", category="outbound_sharing", direction="outbound",
        auth="none", level="export-format", formats=("evidence",),
        description_pl="ZIP zbioru danych analizy podpisany Ed25519 (manifest sha256); weryfikacja: "
                       "`hydra verify-export`.", docs_url="https://www.rfc-editor.org/rfc/rfc8032",
        data_out=("domain", "url", "ip", "sha256", "phash", "campaign_id", "decision", "score_bits", "first_seen",
                  "last_seen", "count"),
        data_out_pl="Incydenty (id), geny po redakcji, wzbogacenia, powiązania, przypisania, kampanie, manifest i "
                    "podpis.",
        never_sent_pl="Nic nie jest wysyłane automatycznie. Pakiet zawiera tytuły incydentów po redakcji; nigdy "
                      "bajty artefaktów, tekst OCR, pliki organizatora ani klucze. Materiał organizatora: nie do "
                      "publicznej dystrybucji.",
        licence_pl="Ed25519 (RFC 8032), biblioteka cryptography (Apache-2.0/BSD).",
        setup_pl=("Pobierz pakiet.", "Odbiorca weryfikuje: hydra verify-export <plik.zip>."),
        notes_pl="Istniejący eksport hydra.export_dataset + hydra.evidence_sign.",
    ),
)

PRESETS: tuple[Preset, ...] = (
    Preset("cert-misp", "Pakiet CERT/MISP",
           "Wymiana z CERT: zdarzenie MISP, STIX 2.1 dla OpenCTI, lista ostrzeżeń CERT Polska, XARF i podpisany "
           "pakiet dowodowy.", ("misp", "opencti-taxii", "cert-pl", "xarf", "evidence-package")),
    Preset("siem", "Pakiet SIEM", "Wskaźniki do Splunk HEC, Elasticsearch i dowolnego SIEM (NDJSON/JSON).",
           ("splunk-hec", "elasticsearch", "siem-generic")),
    Preset("ads-meta", "Pakiet reklamowy: Meta Ad Library",
           "Biblioteka reklam Meta i wzbogacanie domen z reklam: crt.sh, RDAP, DNS, lista ostrzeżeń CERT Polska.",
           ("meta-adlibrary", "crtsh", "rdap", "dns", "cert-pl")),
    Preset("reputation", "Pakiet reputacji",
           "Reputacja wskaźników: OpenPhish (dopasowanie lokalne), URLhaus, VirusTotal, urlscan.io, AbuseIPDB.",
           ("openphish", "urlhaus", "virustotal", "urlscan", "abuseipdb")),
)

_BY_ID = {c.id: c for c in CONNECTORS}
_PRESETS = {p.id: p for p in PRESETS}


def get(connector_id: str) -> Connector | None:
    return _BY_ID.get(connector_id)


def preset(preset_id: str) -> Preset | None:
    return _PRESETS.get(preset_id)


def can_push(c: Connector) -> bool:
    """Outbound connector with a push client (export-format + optional push, or live notification)."""
    return c.direction in ("outbound", "both") and c.live_impl == "hydra.integrations.clients.push"


def needs_network(c: Connector) -> bool:
    return c.level != "planned" and c.id not in LOCAL_ONLY and (c.level == "live" or can_push(c))
