/**
 * In-app Polish documentation for "Integracje & API" (Dokumentacja tab), exported as data so docs/INTEGRATIONS.md
 * and docs/API.md can mirror it. Every endpoint, env var and CLI command below was checked against the code on
 * 2026-11-22 (hydra/api*.py, hydra/cli*.py, hydra/config.py, hydra/scheduler.py, hydra/enrich/adlibrary.py,
 * hydra/evidence_sign.py, hydra/export_*.py, hydra/api_stream.py, justfile). Webhook and connector endpoints follow
 * the integrations contract (hydra/api_integrations.py, hydra/api_webhooks.py); the live lists (connectors, event
 * types) are rendered from the API when it is available.
 *
 * Inline code in `text` uses backticks; the renderer turns them into <code>.
 */

export type DocBlock =
  | { t: "p"; text: string }
  | { t: "h"; text: string; id?: string }
  | { t: "list"; items: string[]; ordered?: boolean }
  | { t: "code"; lang: string; code: string; label?: string }
  | { t: "table"; head: string[]; rows: string[][] }
  | { t: "note"; tone: "info" | "warn"; text: string }
  /** Placeholder rendered from live API data by the tab (API reference, connector guides, webhook event list). */
  | { t: "dynamic"; kind: "api-reference" | "connectors" | "webhook-events" };

export interface DocSection {
  id: string;
  title: string;
  summary: string;
  blocks: DocBlock[];
}

export const DEFAULT_BASE = "http://127.0.0.1:8000";

const HMAC_PY = `import hashlib
import hmac
import time

REPLAY_WINDOW_S = 300

def verify_hydra_webhook(secret: bytes, headers: dict[str, str], raw_body: bytes) -> bool:
    """True, gdy podpis X-Hydra-Signature zgadza się, a znacznik czasu mieści się w oknie 300 s."""
    ts = headers.get("X-Hydra-Timestamp", "")
    sig = headers.get("X-Hydra-Signature", "")
    if not ts.isdigit() or abs(time.time() - int(ts)) > REPLAY_WINDOW_S:
        return False
    mac = hmac.new(secret, ts.encode("ascii") + b"." + raw_body, hashlib.sha256)
    return hmac.compare_digest("sha256=" + mac.hexdigest(), sig)`;

const HMAC_JS = `import crypto from "node:crypto";

const REPLAY_WINDOW_S = 300;

/** rawBody: Buffer z surową treścią (np. express.raw({ type: "application/json" })), nie sparsowany JSON. */
export function verifyHydraWebhook(secret, headers, rawBody) {
  const ts = String(headers["x-hydra-timestamp"] ?? "");
  const sig = String(headers["x-hydra-signature"] ?? "");
  if (!/^\\d+$/.test(ts) || Math.abs(Date.now() / 1000 - Number(ts)) > REPLAY_WINDOW_S) return false;
  const expected = "sha256=" + crypto.createHmac("sha256", secret).update(ts + ".").update(rawBody).digest("hex");
  const a = Buffer.from(expected);
  const b = Buffer.from(sig);
  return a.length === b.length && crypto.timingSafeEqual(a, b);
}`;

const HMAC_SH = `# Podpisz treść tak jak HYDRA i porównaj z nagłówkiem (SECRET = sekret odbiorcy)
TS=$(date +%s)
BODY='{"id":"dlv-test","event":"ping","created_at":"2026-11-22T03:00:00Z","data":{}}'
SIG="sha256=$(printf '%s.%s' "$TS" "$BODY" | openssl dgst -sha256 -hmac "$SECRET" | sed 's/^.*= //')"
echo "$SIG"

# Wyślij podpisane zdarzenie do wbudowanego odbiornika testowego (pętla zwrotna)
curl -sS -H "Authorization: Bearer $HYDRA_API_TOKEN" -X POST "${DEFAULT_BASE}/v1/integrations/webhooks/sink?endpoint_id=$ENDPOINT_ID" \\
  -H 'Content-Type: application/json' -H 'X-Hydra-Event: ping' \\
  -H "X-Hydra-Timestamp: $TS" -H "X-Hydra-Signature: $SIG" \\
  --data-raw "$BODY"`;

export const DOC_SECTIONS: DocSection[] = [
  {
    id: "przeglad",
    title: "Przegląd i model bezpieczeństwa",
    summary: "API tylko na pętli zwrotnej, offline domyślnie, bez cichych obejść, łańcuch audytu.",
    blocks: [
      {
        t: "p",
        text: "HYDRA NEXUS udostępnia jedno lokalne API HTTP (FastAPI) i interfejs na tym samym porcie. `hydra serve` (lub `just serve`) nasłuchuje na `127.0.0.1:8000` (zmienne `HYDRA_API_HOST`, `HYDRA_API_PORT`). Przykłady w dokumentacji używają domyślnego adresu `http://127.0.0.1:8000`; referencja API i konsola podstawiają adres tej instancji. Ścieżki są dostępne bezpośrednio (`/v1/...`) i z prefiksem `/api` (`/api/v1/...`), którego używa interfejs; serwer zdejmuje prefiks.",
      },
      { t: "h", text: "Tylko lokalnie" },
      {
        t: "list",
        items: [
          "Adres nasłuchu musi być adresem pętli zwrotnej (`127.0.0.1`, `::1`, `localhost`); `0.0.0.0` jest dozwolone wyłącznie w kontenerze (`HYDRA_IN_CONTAINER=1`), gdzie port i tak publikuje się na `127.0.0.1` hosta.",
          "Strażnik żądań odrzuca (403) nagłówek `Host` spoza listy lokalnych nazw (ochrona przed DNS rebinding) oraz zapisy POST/PUT/PATCH/DELETE z obcym `Origin` lub `Sec-Fetch-Site: cross-site` (ochrona CSRF).",
          "Mimo logowania nie wystawiaj API w sieci — ani przez `0.0.0.0`, ani przez reverse proxy czy tunel. Jeśli dane mają trafić gdzie indziej, użyj eksportu pliku, konektora lub webhooka.",
        ],
      },
      { t: "h", text: "Logowanie, sesja i tokeny API", id: "logowanie" },
      {
        t: "list",
        items: [
          "Panel wymaga zalogowania. Przeglądarka dostaje ciasteczko sesji `hydra_session` (HttpOnly, SameSite=Strict) i ciasteczko `hydra_csrf`; każde żądanie POST/PUT/PATCH/DELETE wysyła nagłówek `X-CSRF-Token` równy wartości `hydra_csrf`.",
          "Skrypty i integracje używają osobistego tokenu API (menu użytkownika → Tokeny API): nagłówek `Authorization: Bearer <token>`. Token ma uprawnienia swojego właściciela, jest pokazywany tylko raz i można go unieważnić. Przykłady czytają go ze zmiennej `HYDRA_API_TOKEN`.",
          "Role: Administrator (także konta i tokeny innych), Analityk (praca z dowodami i decyzje), Obserwator (tylko odczyt). Uprawnienia sprawdza serwer.",
          "Pierwsze uruchomienie tworzy konto administratora z losowym hasłem zapisanym w pliku na serwerze (ścieżkę pokazuje ekran logowania: „Jak znaleźć dane administratora?”); hasło można też ustawić zmienną `HYDRA_ADMIN_PASSWORD`. Hasło tymczasowe trzeba zmienić przy pierwszym logowaniu (min. 12 znaków).",
        ],
      },
      {
        t: "code",
        lang: "sh",
        label: "Token API w skrypcie",
        code: `export HYDRA_API_TOKEN='hnx_…'   # z menu Tokeny API; nie zapisuj w repozytorium\ncurl -sS -H "Authorization: Bearer $HYDRA_API_TOKEN" ${DEFAULT_BASE}/v1/status`,
      },
      { t: "h", text: "Offline domyślnie, bez cichych obejść" },
      {
        t: "list",
        items: [
          "`HYDRA_OFFLINE=1` (domyślnie) to wyłącznik: proces blokuje połączenia gniazd poza pętlą zwrotną. Wygrywa z każdym innym ustawieniem.",
          "`HYDRA_ENRICH=1` włącza zapytania o publiczne metadane (RDAP, DNS, crt.sh, Meta Ad Library itd.); działa tylko razem z `HYDRA_OFFLINE=0`. `scripts/env.sh` ustawia `HYDRA_OFFLINE=0` automatycznie, gdy `HYDRA_ENRICH=1` lub `HYDRA_LLM=1`.",
          "Gdy operacja wymaga sieci, a jest offline, API zwraca jawny status (`offline`, 503 z polskim komunikatem) — nigdy nie przełącza się po cichu na inne źródło ani nie udaje wyniku.",
          "Eksporty plików (STIX, MISP, CSV, XARF, pakiet podpisany) działają offline: to tylko generowanie plików na tym komputerze.",
        ],
      },
      { t: "h", text: "Dziennik audytu z łańcuchem skrótów" },
      {
        t: "p",
        text: "Każde zdarzenie (przebieg, recenzja, eksport, żądanie integracji, doręczenie webhooka) trafia do tabeli `events` jako `seq, ts, type, payload, prev_hash, hash`, gdzie `hash = sha256(prev_hash + \"\\n\" + ts + \"\\n\" + type + \"\\n\" + payload)`, a `payload` to kanoniczny JSON (posortowane klucze). Zmiana dowolnego wpisu zrywa łańcuch od tego miejsca.",
      },
      {
        t: "code",
        lang: "sh",
        label: "Weryfikacja łańcucha",
        code: `curl -sS -H "Authorization: Bearer $HYDRA_API_TOKEN" ${DEFAULT_BASE}/v1/audit/verify        # {"ok": true, "events": N, "first_bad_seq": null}\nhydra verify                                         # także ponowne haszowanie oryginałów w CAS`,
      },
      {
        t: "note",
        tone: "info",
        text: "Wpisy audytu integracji nie zawierają sekretów, treści zapytań ani ścieżek URL odbiorców webhooków — tylko host, metodę, status, czasy, liczby bajtów i skrót SHA-256 treści.",
      },
    ],
  },
  {
    id: "api",
    title: "Referencja API",
    summary: "Wszystkie endpointy z lokalnego /openapi.json: parametry, treść, odpowiedź, przykład curl.",
    blocks: [
      {
        t: "p",
        text: "Referencja jest generowana na żywo z `GET /openapi.json` tej instancji, więc zawsze odpowiada uruchomionej wersji. Każdy endpoint ma kotwicę (link) i przycisk „Otwórz w konsoli”, który otwiera go w zakładce Konsola API z wypełnionym formularzem.",
      },
      { t: "dynamic", kind: "api-reference" },
    ],
  },
  {
    id: "formaty",
    title: "Formaty danych",
    summary: "STIX 2.1, MISP, CSV/NDJSON/Elasticsearch, Splunk HEC, SQL, Neo4j, XARF, pakiet podpisany Ed25519.",
    blocks: [
      {
        t: "p",
        text: "Wszystkie eksporty przechodzą przez redakcję (`hydra.redact`: tokeny, klucze, hasła w URL, nazwy użytkowników w ścieżkach → `[REDACTED]`). Oryginały w CAS nigdy nie są modyfikowane.",
      },
      { t: "h", text: "STIX 2.1 (bundle)" },
      {
        t: "p",
        text: "`GET /v1/campaigns/{campaign_id}/stix?run_id=` (jedna kampania) i `GET /v1/runs/{run_id}/stix?multi_only=true` (cały przebieg). Obiekty: `identity` (HYDRA NEXUS), `campaign` (z `x_hydra_group_id`, `x_hydra_run_id`), `indicator` (wzorce domeny, URL, SHA-256; `x_hydra_bits`, `x_hydra_doc_freq`), `infrastructure` (phishing), obserwable `domain-name`/`ipv4-addr`, `observed-data`, `relationship` (`indicates`, `uses`, `consists-of`, `related-to`). Identyfikatory UUIDv5 — ponowny eksport daje identyczne ID.",
      },
      {
        t: "code",
        lang: "json",
        label: "STIX — fragment",
        code: `{\n  "type": "bundle",\n  "id": "bundle--…",\n  "objects": [\n    {"type": "campaign", "spec_version": "2.1", "id": "campaign--…", "name": "…", "x_hydra_run_id": "analyse-…"},\n    {"type": "indicator", "spec_version": "2.1", "pattern_type": "stix",\n     "pattern": "[domain-name:value = 'przyklad-oszustwa.example']",\n     "indicator_types": ["malicious-activity"], "x_hydra_bits": 7.2},\n    {"type": "relationship", "relationship_type": "indicates", "source_ref": "indicator--…", "target_ref": "campaign--…"}\n  ]\n}`,
      },
      { t: "h", text: "MISP event JSON" },
      {
        t: "p",
        text: "Konektor MISP (format eksportu): `POST /v1/integrations/misp/preview` pokazuje paczkę na sucho, `GET /v1/integrations/misp/export?campaign_id=` pobiera plik. Pola: `Event.info`, `threat_level_id`, `analysis`, `distribution=0` (tylko Twoja organizacja), `Attribute[]` typu `domain`/`url`/`ip-dst`/`sha256` z kategorią `Network activity` lub `Payload delivery`, `to_ids`, `comment`; tag `tlp:amber`.",
      },
      {
        t: "code",
        lang: "json",
        label: "MISP — fragment",
        code: `{\n  "Event": {\n    "info": "HYDRA NEXUS: kampania …",\n    "threat_level_id": "2", "analysis": "1", "distribution": "0",\n    "Tag": [{"name": "tlp:amber"}],\n    "Attribute": [\n      {"type": "domain", "category": "Network activity", "value": "przyklad-oszustwa.example", "to_ids": true, "comment": "kampania …"},\n      {"type": "sha256", "category": "Payload delivery", "value": "9f86d0…", "to_ids": false}\n    ]\n  }\n}`,
      },
      { t: "h", text: "CSV, NDJSON, Elasticsearch bulk" },
      {
        t: "list",
        items: [
          "CSV wskaźników (konektor `csv`/eksport integracji): jeden wskaźnik na wiersz — typ, wartość, kampania, decyzja, bity, pierwsze/ostatnie wystąpienie, liczba.",
          "CSV kampanii: `GET /v1/campaigns/{id}/export?format=csv` — kolumny `run_id, campaign_id, incident_id, family, kind, value, status, provenance, quality, extractor`.",
          "NDJSON: jeden obiekt JSON na linię (ten sam zestaw pól co CSV).",
          "Elasticsearch `_bulk`: pary linii — akcja `{\"index\":{\"_index\":\"hydra-indicators\"}}` i dokument.",
        ],
      },
      {
        t: "code",
        lang: "ndjson",
        label: "Elasticsearch bulk — fragment",
        code: `{"index":{"_index":"hydra-indicators"}}\n{"type":"domain","value":"przyklad-oszustwa.example","campaign_id":"c-…","decision":"same_campaign","score_bits":7.2}`,
      },
      { t: "h", text: "Splunk HEC JSON" },
      {
        t: "code",
        lang: "json",
        label: "Splunk HTTP Event Collector",
        code: `{"time": 1795320000, "host": "hydra-nexus", "source": "hydra", "sourcetype": "hydra:indicator",\n "event": {"type": "url", "value": "https://przyklad-oszustwa.example/[REDACTED]", "campaign_id": "c-…"}}`,
      },
      { t: "h", text: "SQL (SQLite / PostgreSQL)" },
      {
        t: "p",
        text: "Plik SQL zgodny z SQLite i PostgreSQL: `CREATE TABLE` + `INSERT` dla tabel wskaźników i kampanii. Import: `sqlite3 hydra.db < hydra.sql` lub `psql -f hydra.sql`.",
      },
      { t: "h", text: "Neo4j (Cypher / CSV)" },
      {
        t: "p",
        text: "`GET /v1/export/neo4j?format=cypher|zip|nodes_csv|edges_csv&run_id=&min_p=&min_df=2` lub `hydra export-neo4j OUT [--run R] [--min-df 2]`. Węzły: `Incident`, `Campaign`, `CampaignGroup`, `Gene`. Relacje: `LINKED_TO {probability, bits, hypothesis, calibrated, model, top_reason}`, `HAS_GENE {family, provenance}`, `MEMBER_OF`, `IN_GROUP`. Szczegóły: docs/NEO4J.md.",
      },
      { t: "h", text: "XARF v4 — pakiet zgłoszeń nadużyć" },
      {
        t: "p",
        text: "`GET /v1/campaigns/{campaign_id}/abuse-package?run_id=` zwraca ZIP: `reports/<host>.xarf.json`, `notes/<host>.txt` (polska notka), `manifest.json`, `CZYTAJ_MNIE.txt`. Nic nie jest wysyłane automatycznie (`sent: false`); dane zgłaszającego uzupełnij zmiennymi `HYDRA_REPORTER_ORG`, `HYDRA_REPORTER_EMAIL`, `HYDRA_REPORTER_DOMAIN`.",
      },
      {
        t: "code",
        lang: "json",
        label: "XARF — fragment",
        code: `{\n  "xarf_version": "4.2.0", "report_id": "…", "timestamp": "…",\n  "reporter": {"org": "…", "contact": "…", "domain": "…"},\n  "source_identifier": "przyklad-oszustwa.example",\n  "category": "content", "type": "phishing", "evidence_source": "automated_analysis",\n  "url": "https://przyklad-oszustwa.example/", "tags": ["campaign:…", "incidents:3"],\n  "evidence": [{"content_type": "image/png", "description": "…", "payload": "<base64>", "hash": "sha256:…", "size": 123456}]\n}`,
      },
      { t: "h", text: "Pakiet dowodowy podpisany Ed25519" },
      {
        t: "p",
        text: "`GET /v1/export/dataset?run_id=` (lub `hydra export-dataset [RUN] OUT_DIR`) tworzy zbiór (Parquet/CSV/JSON) z `manifest.json`, `SIGNED_MANIFEST.json` i `SIGNED_MANIFEST.sig`. Podpisany manifest (`hydra.evidence.signed_manifest.v1`) zawiera `algorithm: Ed25519`, `run_id`, `product`, `audit_chain_head {seq, hash}`, `files[{path, sha256, size}]`, `public_key`, `key_fingerprint`. Klucz: `<state_dir>/keys/evidence_ed25519.pem` (0600), tworzony przy pierwszym użyciu.",
      },
      {
        t: "code",
        lang: "sh",
        label: "Odcisk klucza i weryfikacja",
        code: `curl -sS -H "Authorization: Bearer $HYDRA_API_TOKEN" ${DEFAULT_BASE}/v1/exports/signing-key\n# {"algorithm": "Ed25519", "fingerprint": "SHA256:…", "public_key": "…"}\n\nhydra verify-export hydra-dataset.zip --fingerprint SHA256:…\ncurl -sS -H "Authorization: Bearer $HYDRA_API_TOKEN" -X POST '${DEFAULT_BASE}/v1/exports/verify?fingerprint=SHA256:…' \\\n  -H 'Content-Type: application/zip' --data-binary @hydra-dataset.zip`,
      },
      {
        t: "note",
        tone: "info",
        text: "Podpis potwierdza integralność od chwili podpisania, nie prawdziwość materiału. Odcisk klucza porównaj innym kanałem (np. telefonicznie).",
      },
    ],
  },
  {
    id: "webhooki",
    title: "Webhooki",
    summary: "Katalog zdarzeń, nagłówki, podpis HMAC-SHA256, okno 300 s, ponowienia i dead-letter.",
    blocks: [
      {
        t: "p",
        text: "Webhook to żądanie `POST` z JSON-em, które HYDRA wysyła do skonfigurowanego odbiorcy (zakładka Webhooki lub `POST /v1/integrations/webhooks`), gdy wydarzy się coś istotnego. Treść zawiera tylko identyfikatory i wskaźniki — nigdy artefaktów, tekstu OCR ani nazw plików.",
      },
      { t: "h", text: "Katalog zdarzeń" },
      { t: "dynamic", kind: "webhook-events" },
      { t: "h", text: "Treść i nagłówki" },
      {
        t: "code",
        lang: "http",
        label: "Przykładowe doręczenie",
        code: `POST /hooks/hydra HTTP/1.1\nContent-Type: application/json\nX-Hydra-Event: delta\nX-Hydra-Delivery: dlv-…\nX-Hydra-Timestamp: 1795320000\nX-Hydra-Signature: sha256=5d2f…\n\n{"id": "dlv-…", "event": "delta", "created_at": "2026-11-22T03:00:00Z", "hydra_version": "…", "data": {…}}`,
      },
      {
        t: "table",
        head: ["Nagłówek", "Znaczenie"],
        rows: [
          ["`X-Hydra-Event`", "typ zdarzenia (np. `delta`, `watch`, `ping`)"],
          ["`X-Hydra-Delivery`", "identyfikator doręczenia; ten sam przy ponowieniach (deduplikacja po stronie odbiorcy)"],
          ["`X-Hydra-Timestamp`", "czas podpisania (sekundy Unix)"],
          ["`X-Hydra-Signature`", "`sha256=` + hex(HMAC-SHA256(sekret, \"<timestamp>.<surowa treść>\"))"],
        ],
      },
      { t: "h", text: "Weryfikacja podpisu" },
      {
        t: "list",
        ordered: true,
        items: [
          "Weź surową treść żądania (bajty, przed parsowaniem JSON).",
          "Odrzuć, jeśli |teraz − `X-Hydra-Timestamp`| > 300 s (ochrona przed odtworzeniem).",
          "Policz HMAC-SHA256 z sekretu odbiorcy nad `<timestamp>.<treść>` i porównaj w stałym czasie z `X-Hydra-Signature`.",
          "Zdeduplikuj po `X-Hydra-Delivery` (ponowienia niosą ten sam identyfikator).",
        ],
      },
      { t: "code", lang: "python", label: "Python", code: HMAC_PY },
      { t: "code", lang: "javascript", label: "JavaScript (Node.js, crypto.timingSafeEqual)", code: HMAC_JS },
      { t: "code", lang: "sh", label: "curl + openssl", code: HMAC_SH },
      { t: "h", text: "Ponowienia, backoff, dead-letter" },
      {
        t: "list",
        items: [
          "Do 5 prób. Ponowienie po błędzie sieci, 429 i 5xx z wykładniczym odstępem: +2 s, +4 s, +8 s, +16 s (podstawa odstępu: `HYDRA_WEBHOOK_BACKOFF_S`, domyślnie 2).",
          "Inne 4xx (np. 401 przy złym podpisie) kończą doręczenie od razu jako `dead`.",
          "Po wyczerpaniu prób doręczenie trafia na listę dead-letter (`status: dead`); ponów ręcznie `POST /v1/integrations/webhooks/deliveries/{delivery_id}/retry`.",
          "Historia: `GET /v1/integrations/webhooks/deliveries?endpoint_id=&limit=`.",
          "Odbiorca spoza pętli zwrotnej wymaga `HYDRA_OFFLINE=0`, włączonego odbiorcy i adresu publicznego (netguard odrzuca adresy prywatne, link-local, metadata); inaczej doręczenie ma status `blocked_offline` z polskim opisem — bez cichego obejścia.",
        ],
      },
      { t: "h", text: "Odbiornik testowy na pętli zwrotnej" },
      {
        t: "p",
        text: "Wbudowany odbiornik `POST /v1/integrations/webhooks/sink?endpoint_id=<id>` sprawdza podpis i zapisuje ostatnie 50 doręczeń; podgląd: `GET /v1/integrations/webhooks/sink`. Ustaw adres odbiorcy na `http://127.0.0.1:8000/v1/integrations/webhooks/sink?endpoint_id=<id>`, aby przetestować całość offline — dane nie opuszczają komputera.",
      },
      {
        t: "code",
        lang: "sh",
        label: "Test",
        code: `curl -sS -H "Authorization: Bearer $HYDRA_API_TOKEN" -X POST ${DEFAULT_BASE}/v1/integrations/webhooks/<endpoint_id>/test   # zdarzenie "ping"\ncurl -sS -H "Authorization: Bearer $HYDRA_API_TOKEN" ${DEFAULT_BASE}/v1/integrations/webhooks/sink                         # {"receipts": [{"event": "ping", "signature_ok": true, …}]}`,
      },
    ],
  },
  {
    id: "konektory",
    title: "Przewodniki konektorów",
    summary: "Konfiguracja, uwierzytelnianie, limity, licencje i dane wychodzące każdego konektora; token Meta Ad Library.",
    blocks: [
      {
        t: "p",
        text: "Poziomy: „Na żywo (live)” — klient zaimplementowany; testowany na fiksturach z dokumentacji, bez wywołań produkcyjnych; „Format eksportu” — HYDRA tworzy plik w formacie odbiorcy (pobranie / podgląd na sucho), bez wysyłki; „Planowane” — tylko opis, bez przycisków, API zwraca 409. Lista poniżej pochodzi z `GET /v1/integrations`.",
      },
      { t: "dynamic", kind: "connectors" },
      { t: "h", text: "Jak uzyskać token Meta Ad Library", id: "meta-token" },
      {
        t: "list",
        ordered: true,
        items: [
          "Załóż konto Meta for Developers (developers.facebook.com) i utwórz aplikację.",
          "Potwierdź tożsamość i lokalizację na facebook.com/ID — wymagane dla dostępu do Ad Library API.",
          "Zaakceptuj warunki Ad Library API (facebook.com/ads/library/api) i w Graph API Explorer wygeneruj token użytkownika dla swojej aplikacji.",
          "Token z Graph API Explorer jest krótkotrwały (zwykle ok. 1 godziny). Wymień go na token długoterminowy (ok. 60 dni) przez endpoint `oauth/access_token` z `grant_type=fb_exchange_token` (wymaga identyfikatora i sekretu aplikacji) i odnawiaj przed wygaśnięciem.",
          "Zapisz token w `~/.config/hydra/meta.env` jako linię `META_AD_LIBRARY_TOKEN=...` (plik 0600) albo w Centrum integracji (pole maskowane, zapis do tego samego pliku). Zmienna środowiskowa `META_AD_LIBRARY_TOKEN` (starsza nazwa `HYDRA_META_TOKEN`) ma pierwszeństwo przed plikiem.",
          "Uruchom w trybie online: `HYDRA_ENRICH=1` i `HYDRA_OFFLINE=0`. Bez tego zapytania są blokowane z jawnym komunikatem.",
        ],
      },
      {
        t: "code",
        lang: "sh",
        label: "Zapis tokenu i uruchomienie",
        code: `mkdir -p ~/.config/hydra && chmod 700 ~/.config/hydra\n( umask 077; printf 'META_AD_LIBRARY_TOKEN=%s\\n' "$TOKEN" > ~/.config/hydra/meta.env )\nHYDRA_ENRICH=1 HYDRA_OFFLINE=0 hydra serve`,
      },
      {
        t: "list",
        items: [
          "Endpoint: `https://graph.facebook.com/v21.0/ads_archive`, kraj PL. Limity są liczone przez Graph API na poziomie aplikacji; HYDRA odczekuje 2 s między zapytaniami i ponawia (1 s, 2 s, 4 s) przy 429, 5xx i kodach dławienia Graph API.",
          "Warunki: korzystanie z Ad Library API podlega warunkom Meta (Platform Terms i Ad Library API Terms); dane służą analizie przejrzystości reklam, nie do odsprzedaży.",
          "Do Meta trafiają tylko słowa kluczowe i identyfikatory stron z listy obserwowanych — nigdy materiały NASK ani artefakty.",
          "Bez tokenu: pobierz eksport ręcznie z facebook.com/ads/library (CSV/JSON) i wrzuć go do `inbox/` harmonogramu lub zaimportuj — import ręczny działa offline.",
        ],
      },
      {
        t: "note",
        tone: "info",
        text: "Komunikat aplikacji przy braku tokenu: „Meta Ad Library API jest niedostępne: brak tokenu (zmienna META_AD_LIBRARY_TOKEN, starsza nazwa HYDRA_META_TOKEN, albo plik ~/.config/hydra/meta.env z linią META_AD_LIBRARY_TOKEN=...).”",
      },
    ],
  },
  {
    id: "prywatnosc",
    title: "Prywatność",
    summary: "Co wychodzi z komputera (tylko wskaźniki), co nigdy, gdzie są klucze; dane NASK nie są wysyłane.",
    blocks: [
      { t: "h", text: "Co może wyjść (tylko po włączeniu konektora i w trybie online)" },
      {
        t: "p",
        text: "Wyłącznie wskaźniki: domeny, URL-e (po redakcji), adresy IP, skróty (SHA-256, pHash), identyfikatory kampanii, reklam i stron, ASN, odciski certyfikatów, słowa kluczowe z listy obserwowanych, liczniki i daty. Dokładny zestaw pól każdego konektora (`data_out`) jest w tabeli w sekcji Przewodniki konektorów i w podglądzie „na sucho” przed każdą wysyłką.",
      },
      { t: "h", text: "Co nigdy nie wychodzi" },
      {
        t: "list",
        items: [
          "Artefakty (zrzuty ekranu, wideo, HTML, HAR), tekst OCR i transkrypcje.",
          "Nazwy plików i ścieżki, pliki organizatora (NASK), dane z `data/nask`.",
          "Klucze, tokeny i sekrety webhooków (również w logach, audycie i komunikatach błędów).",
        ],
      },
      {
        t: "note",
        tone: "warn",
        text: "Dane NASK nigdy nie są wysyłane do usług zewnętrznych. Konektory pracują na wskaźnikach wyprowadzonych lokalnie; materiały źródłowe zostają w CAS na tym komputerze.",
      },
      { t: "h", text: "Przechowywanie kluczy" },
      {
        t: "list",
        items: [
          "Plik na konektor: `~/.config/hydra/<id_konektora>.env` (linie `NAZWA=wartość`), katalog 0700, plik 0600, zapis atomowy. Katalog można zmienić zmienną `HYDRA_SECRETS_DIR`.",
          "Meta Ad Library: `~/.config/hydra/meta.env` (zgodność wsteczna, `HYDRA_META_ENV_FILE`).",
          "Zmienna środowiskowa o tej samej nazwie ma pierwszeństwo przed plikiem.",
          "API zwraca tylko `{name, configured, source, last4}` (`last4` dla wartości ≥ 12 znaków). Klucze nigdy nie trafiają do bazy stanu, repozytorium git, localStorage przeglądarki ani logów.",
          "Konfiguracja webhooków: `~/.config/hydra/webhooks.json` (0600 — sam URL bywa sekretem), sekret odbiorcy w `~/.config/hydra/webhook-<id>.env`.",
        ],
      },
    ],
  },
  {
    id: "strumien",
    title: "Strumień SSE /v1/stream",
    summary: "Zdarzenia hello/delta/reset/job/watch, wznowienie Last-Event-ID, curl -N i EventSource.",
    blocks: [
      {
        t: "p",
        text: "`GET /v1/stream?run_id=&last_event_id=` to strumień Server-Sent Events (`text/event-stream`) zmaterializowanych zmian: nowe przypisania, recenzje, eksporty, postęp zadań i alerty Watch. Bez `run_id` strumień podąża za najnowszym przebiegiem. Pierwsza linia to `retry: 3000`; co 15 s serwer wysyła komentarz `: hb <unix>` (heartbeat).",
      },
      {
        t: "table",
        head: ["Zdarzenie", "id", "Dane"],
        rows: [
          ["`hello`", "—", "`run_id, latest_run, version, seq, status, resumed, replayed, heartbeat_s, coalesce_ms, max_message_bytes`"],
          ["`delta`", "ostatni `seq` audytu", "`seq, from_seq, n, kinds` (assign/review/export/batch), `kpis, counts, campaigns, groups, nodes, links, edges, removed_nodes, top_genes, exports` — kilka zmian w oknie 250 ms łączy się w jedną"],
          ["`reset`", "—", "`reason` (`new_run`, `gap`, `rebuilt`, `too_large`, `overflow`), `run_id, latest_run` — pobierz widok od nowa"],
          ["`job`", "—", "`id, kind, status, total, done, failed, stage, progress` (maks. 4/s na zadanie)"],
          ["`watch`", "—", "alert Watch + `note_pl`: kandydat (dopasowanie odcisku kampanii), nie potwierdzony członek"],
        ],
      },
      { t: "h", text: "Wznowienie" },
      {
        t: "p",
        text: "Po zerwaniu wyślij nagłówek `Last-Event-ID` (EventSource robi to sam) lub parametr `last_event_id`. Serwer odtworzy zapisane delty o wyższym `seq`; jeśli identyfikator jest starszy niż przechowywana historia, dostaniesz `reset` z `reason: gap`. Żądanie z obcym `Origin` dostaje 403.",
      },
      {
        t: "code",
        lang: "sh",
        label: "curl",
        code: `curl -N -H "Authorization: Bearer $HYDRA_API_TOKEN" -H 'Accept: text/event-stream' ${DEFAULT_BASE}/v1/stream\ncurl -N -H "Authorization: Bearer $HYDRA_API_TOKEN" -H 'Last-Event-ID: 1234' ${DEFAULT_BASE}/v1/stream     # wznowienie\ncurl -N -H "Authorization: Bearer $HYDRA_API_TOKEN" '${DEFAULT_BASE}/v1/watch/stream?since=0'              # tylko alerty Watch (event: watch)`,
      },
      {
        t: "code",
        lang: "javascript",
        label: "EventSource (przeglądarka, ten sam origin)",
        code: `const es = new EventSource("/api/v1/stream");\nfor (const type of ["hello", "delta", "reset", "job", "watch"]) {\n  es.addEventListener(type, (e) => console.log(type, e.lastEventId, JSON.parse(e.data)));\n}\n// es.close();`,
      },
      { t: "p", text: "Liczniki subskrybentów i kursora: `GET /v1/stream/stats`. Pełny opis pól: docs/ui/STREAM_API.md." },
    ],
  },
  {
    id: "cli",
    title: "Odpowiedniki CLI",
    summary: "Polecenia `hydra` i `just` odpowiadające operacjom API.",
    blocks: [
      { t: "p", text: "Punkt wejścia: `hydra` (Typer, `hydra.cli:app`). Przed uruchomieniem: `. scripts/env.sh` (offline domyślnie)." },
      {
        t: "table",
        head: ["Polecenie", "Odpowiednik API / opis"],
        rows: [
          ["`hydra serve [--reload]`", "uruchamia API + UI na 127.0.0.1:$HYDRA_API_PORT (`just serve`)"],
          ["`hydra version`", "wersja (por. `GET /health`)"],
          ["`hydra status`", "liczby artefaktów i zadań (por. `GET /v1/status`)"],
          ["`hydra import PATHS...`", "`POST /v1/import`, `POST /v1/import/paths` — import plików/folderów/ZIP bez serwera"],
          ["`hydra verify`", "`GET /v1/audit/verify` + ponowne haszowanie CAS; kod wyjścia 1 przy niezgodności"],
          ["`hydra analyse [PATHS...] [--corpus …] [--incremental] [--json]`", "pełny przebieg analizy (por. `POST /v1/analyse`)"],
          ["`hydra assign ITEM [--kind auto|url|screenshot|hint|file]`", "przypisanie do kampanii lub nowy wzorzec"],
          ["`hydra assign-batch FOLDER --out DIR`", "`POST /v1/assign-batch`"],
          ["`hydra export-dataset [RUN] OUT_DIR`", "`GET /v1/export/dataset` — podpisany zbiór Ed25519"],
          ["`hydra verify-export PACKAGE [--fingerprint SHA256:…] [--json]`", "`POST /v1/exports/verify`"],
          ["`hydra export-neo4j OUT [--run R] [--min-df 2]`", "`GET /v1/export/neo4j`"],
          ["`hydra scheduler status`", "`GET /v1/scheduler/status`"],
          ["`hydra scheduler watch -k SŁOWO -d DOMENA [--page-id ID]`", "`POST /v1/watchlist` (`--page-id` tylko z CLI)"],
          ["`hydra scheduler run [--force] [--loop]`", "`POST /v1/scheduler/run`"],
          ["`hydra corpora`", "`GET /v1/corpora`"],
        ],
      },
      {
        t: "p",
        text: "Przydatne przepisy `just`: `just serve`, `just ui` (Vite na 127.0.0.1:5173 z proxy `/api`), `just verify`, `just check`, `just demo`, `just e2e-ui`.",
      },
    ],
  },
  {
    id: "przepisy",
    title: "Przepisy",
    summary: "Gotowe scenariusze: alert o nowych reklamach, IOC do MISP, strumień do SIEM, weryfikacja pakietu.",
    blocks: [
      { t: "h", text: "Co godzinę sprawdzaj nowe reklamy i wyślij alert webhookiem", id: "przepis-reklamy" },
      {
        t: "list",
        ordered: true,
        items: [
          "Skonfiguruj token Meta Ad Library (sekcja Przewodniki konektorów).",
          "Dodaj słowa kluczowe do listy obserwowanych (API poniżej lub `hydra scheduler watch -k \"słowo\"`).",
          "Ustaw interwał zadania `adlibrary_pull` na 3600 s w `<state_dir>/scheduler/watchlist.json` (`\"intervals_s\": {\"adlibrary_pull\": 3600}`; domyślnie 21600 s).",
          "Dodaj odbiorcę webhooka subskrybującego zdarzenia `delta` (nowe przypisanie do kampanii) i `watch` (alert-kandydat) w zakładce Webhooki.",
          "Uruchom z harmonogramem w trybie online: `HYDRA_SCHEDULER=1 HYDRA_ENRICH=1 HYDRA_OFFLINE=0 hydra serve`. Nowe reklamy trafiają jako foldery do `inbox/`, kolejny cykl je analizuje i przypisuje, co wywołuje zdarzenie `delta`.",
        ],
      },
      {
        t: "code",
        lang: "sh",
        label: "Lista obserwowanych i webhook",
        code: `curl -sS -H "Authorization: Bearer $HYDRA_API_TOKEN" -X POST ${DEFAULT_BASE}/v1/watchlist -H 'Content-Type: application/json' \\\n  --data-raw '{"add": [{"kind": "keyword", "value": "inwestycja bonus"}]}'\n\ncurl -sS -H "Authorization: Bearer $HYDRA_API_TOKEN" -X POST ${DEFAULT_BASE}/v1/integrations/webhooks -H 'Content-Type: application/json' \\\n  --data-raw '{"name": "Alert SOC", "url": "https://soc.example/hooks/hydra", "events": ["delta", "watch"], "enabled": true, "secret": "<losowy sekret>"}'`,
      },
      { t: "h", text: "Wyślij IOC kampanii do MISP", id: "przepis-misp" },
      {
        t: "list",
        ordered: true,
        items: [
          "Podejrzyj paczkę na sucho: `POST /v1/integrations/misp/preview` z `{\"campaign_id\": \"…\"}` — zobaczysz pola, redakcje i skrót.",
          "Pobierz plik: `GET /v1/integrations/misp/export?campaign_id=…` i zaimportuj w MISP (Event → Import from → MISP JSON). Distribution 0, TLP:AMBER.",
          "Opcjonalna wysyłka bezpośrednio do instancji MISP: zapisz `MISP_URL` i `MISP_KEY` w Centrum integracji, ustaw `HYDRA_OFFLINE=0`, kliknij Testuj, potem Uruchom (`POST /v1/integrations/misp/run`). Każde żądanie jest audytowane (host, status, skrót treści).",
          "Alternatywnie STIX 2.1: `GET /v1/campaigns/{id}/stix` (import w MISP/OpenCTI).",
        ],
      },
      {
        t: "code",
        lang: "sh",
        label: "MISP",
        code: `curl -sS -H "Authorization: Bearer $HYDRA_API_TOKEN" -X POST ${DEFAULT_BASE}/v1/integrations/misp/preview -H 'Content-Type: application/json' \\\n  --data-raw '{"campaign_id": "<id>"}'\ncurl -sS -H "Authorization: Bearer $HYDRA_API_TOKEN" -o misp-event.json '${DEFAULT_BASE}/v1/integrations/misp/export?campaign_id=<id>'`,
      },
      { t: "h", text: "Strumień do SIEM", id: "przepis-siem" },
      {
        t: "p",
        text: "Najprościej: odbiorca webhooka po stronie SIEM (zdarzenia `delta`, `watch`). Dla Splunk HEC lub Elasticsearch pobierz gotowe paczki (`siem_json`, `es_bulk`) z konektorów lub czytaj `/v1/stream` lokalnym przekaźnikiem:",
      },
      {
        t: "code",
        lang: "sh",
        label: "Przekaźnik SSE → plik NDJSON (np. dla Filebeat/Splunk UF)",
        code: `curl -sN -H "Authorization: Bearer $HYDRA_API_TOKEN" ${DEFAULT_BASE}/v1/stream | sed -un 's/^data: //p' >> /var/log/hydra/stream.ndjson`,
      },
      { t: "h", text: "Zweryfikuj podpisany pakiet dowodowy", id: "przepis-weryfikacja" },
      {
        t: "code",
        lang: "sh",
        label: "Weryfikacja",
        code: `curl -sS -H "Authorization: Bearer $HYDRA_API_TOKEN" ${DEFAULT_BASE}/v1/exports/signing-key           # odcisk: porównaj innym kanałem\nhydra verify-export hydra-dataset-<run>.zip --fingerprint SHA256:<odcisk>\n# PASS: … | pliki/files N | run … | klucz/key SHA256:…\n# Integralność potwierdzona (nie prawdziwość materiału). / Integrity verified, not truth.`,
      },
    ],
  },
];

/** Plain text of a section for search (all blocks, inline code markers removed). */
export function sectionText(section: DocSection): string {
  const parts: string[] = [section.title, section.summary];
  for (const b of section.blocks) {
    if (b.t === "p" || b.t === "h" || b.t === "note") parts.push(b.text);
    else if (b.t === "list") parts.push(...b.items);
    else if (b.t === "code") parts.push(b.label ?? "", b.code);
    else if (b.t === "table") parts.push(...b.head, ...b.rows.flat());
  }
  return parts.join("\n").replace(/`/g, "");
}

/** Static webhook event catalogue (contract), used when GET /v1/integrations/webhooks is unavailable. */
export const WEBHOOK_EVENTS_FALLBACK: { id: string; label_pl: string; description_pl: string; example: unknown }[] = [
  { id: "ping", label_pl: "Test połączenia (ping)", description_pl: "Wysyłane wyłącznie przyciskiem „Testuj”.", example: { endpoint_id: "wh-3f9a1c2b7d", note: "ping" } },
  { id: "delta", label_pl: "Zmiana wyników (delta)", description_pl: "Zakończony przebieg analizy (nowe kampanie) albo zmiana przypisania incydentu; tylko identyfikatory i liczniki.", example: { source: "RUN_FINISHED", run_id: "run-20261122-0215", kind: "analyse", pairs: 1284, campaigns: 37 } },
  { id: "job", label_pl: "Zadanie zakończone", description_pl: "Zakończenie zadania w tle (import, analiza): status i liczniki, bez nazw plików.", example: { job_id: "job-7c1e", kind: "import", status: "done", total: 120, done: 118, failed: 2 } },
  { id: "watch", label_pl: "Alert HYDRA Watch", description_pl: "Dopasowanie odcisku kampanii — kandydat do weryfikacji, nie potwierdzony członek.", example: { alert_seq: 12, event_id: "ct-000913", host_defanged: "przyklad[.]pl", event_bits: 14.2, assignment: "candidate", candidate: true } },
  { id: "review", label_pl: "Decyzja analityka", description_pl: "Recenzja analityka (połączenie, rozłączenie, cofnięcie); bez notatek i nazwy analityka.", example: { review_id: "rv-51d2", action: "cannot_link", target_ids: ["inc-0042", "inc-0107"] } },
  { id: "export", label_pl: "Eksport danych", description_pl: "Wygenerowano eksport (STIX, CSV, Neo4j, pakiet zgłoszenia): rodzaj i liczniki.", example: { kind: "stix_run", run_id: "run-20261122-0215", counts: { campaigns: 37, indicators: 412 } } },
];
