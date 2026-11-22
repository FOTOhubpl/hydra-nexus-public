/**
 * Polish display names for gene kinds and scheme ids (B9, audit f450a70). Display only: the raw code stays in the
 * data, in tooltips (`kod: …`) and in exports. Scheme labels mirror hydra/labels_pl.py SCHEME_LABELS_PL; unknown
 * codes fall back to the code itself.
 */

export const GENE_KIND_PL: Record<string, string> = {
  // TXT: text
  ad_headline: "nagłówek reklamy", page_title: "tytuł strony", headline: "nagłówek", lead: "lead artykułu",
  library_ad_text: "tekst w bibliotece reklam", ad_text: "tekst reklamy", cta: "wezwanie do działania", promise: "obietnica",
  product_name: "nazwa produktu", byline: "podpis autora", ocr_line: "tekst z obrazu (OCR)", phrase: "charakterystyczna fraza",
  text_template_key: "szablon tekstu", minhash_band: "podobieństwo tekstu (MinHash)", simhash: "odcisk tekstu (SimHash)",
  text_embedding: "podobieństwo znaczeniowe tekstu", e5: "podobieństwo znaczeniowe tekstu", scheme: "typ oszustwa",
  language: "język", claim: "twierdzenie", promo_code: "kod promocyjny", payment_method: "metoda płatności",
  entity_phone: "numer telefonu", entity_email: "adres e-mail", entity_iban: "numer konta (IBAN)", entity_price: "kwota / cena",
  entity_discount: "rabat", bank_code: "kod banku", crypto_wallet: "portfel kryptowalut", messenger_handle: "konto w komunikatorze",
  commenter_name: "autor komentarza", domain_in_text: "domena w tekście", url_in_text: "adres URL w tekście",
  carrier_billing_marker: "płatność przez operatora komórkowego", person_named: "osoba (z nazwiska)",
  persona_renamed: "persona pod inną nazwą", brand_target: "podszywana marka", institution: "podszywana instytucja",
  // INF: infrastructure
  domain: "domena", host: "host", registrable_domain: "domena", landing_host: "host strony docelowej", landing: "strona docelowa",
  url: "adres URL", url_skeleton: "szkielet adresu URL", url_label_skeleton: "szkielet adresu URL", redirect_chain: "łańcuch przekierowań",
  ip: "adres IP", asn: "ASN", as_org: "hosting (operator AS)", ns: "serwery NS", ns_set: "serwery NS", registrar: "rejestrator domeny",
  registration_month: "miesiąc rejestracji domeny", cert_san: "certyfikat (SAN)", cert_issuer: "wystawca certyfikatu",
  tls_cert_sha256: "certyfikat TLS", tracker: "identyfikator śledzący", tracker_id: "identyfikator śledzący / piksel",
  tds_param: "parametr TDS", favicon_mmh3: "favicon", dom: "struktura strony", dom_skeleton_hash: "szkielet DOM strony",
  tlsh_html_skeleton: "szkielet HTML (TLSH)", css_build_hash: "build CSS", js_build_hash: "build JS", form: "formularz",
  captcha_sitekey: "klucz CAPTCHA", chat_widget_id: "widżet czatu", exfil_telegram_bot: "bot Telegram (wyciek danych)",
  exfil_discord_webhook: "webhook Discord (wyciek danych)", app_package_id: "identyfikator aplikacji",
  asset_sha256: "identyczny zasób strony", asset_nameset_hash: "zestaw nazw zasobów strony", cloud_lb_host: "host chmurowy",
  cloud_lb_name: "nazwa w chmurze", operator_id: "identyfikator operatora", advertiser_page: "strona reklamodawcy",
  phone: "numer telefonu", kit: "zestaw phishingowy",
  // VIS: visual
  pdq: "odcisk obrazu (PDQ)", pdq_canon: "odcisk obrazu (PDQ, kanoniczny)", phash: "odcisk obrazu (pHash)",
  phash_tiles: "odcisk fragmentów obrazu", phash_multicrop: "odcisk wycinków obrazu", dinov2: "podobieństwo wizualne (DINOv2)",
  siglip2: "podobieństwo wizualne (SigLIP)", logo: "logo", logo_strip: "pasek logotypów", image_class: "klasa obrazu", face: "twarz",
  artifact_sha256: "identyczny plik", video_segment: "wspólny fragment wideo", frame: "klatka wideo",
  av_sync_suspicion: "spójność ruchu ust i dźwięku", audio_fp_sig: "odcisk dźwięku", audio_fp_band: "odcisk dźwięku (pasmo)",
  // scorer-internal
  cap_correction: "korekta limitu rodziny",
};

/** Mirror of hydra/labels_pl.py SCHEME_LABELS_PL. */
export const SCHEME_PL: Record<string, string> = {
  investment: "fałszywe inwestycje", crypto: "kryptowaluty", health_supplement: "cudowne leki i suplementy", gambling: "hazard",
  fake_news_bait: "fałszywy artykuł jako przynęta", funds_recovery: "odzyskiwanie utraconych środków", giveaway: "nagrody i loterie",
  phishing: "wyłudzenie danych (phishing)", loan_or_debt: "pożyczki i oddłużanie", loan: "pożyczki i oddłużanie", shopping: "zakupy",
  other: "inny", marketplace_phishing: "fałszywy odbiór płatności (OLX, Vinted)", parcel_fee: "dopłata do przesyłki",
  gov_refund: "zwrot podatku lub świadczenia", bank_impersonation: "podszycie pod bank", fake_shop: "fałszywy sklep",
  account_takeover: "przejęcie konta", fake_charity: "fałszywa zbiórka",
};

export function geneKindPl(kind: string | null | undefined): string {
  if (!kind) return "";
  return GENE_KIND_PL[kind] ?? kind;
}

export function schemePl(id: string | null | undefined): string {
  if (!id) return "";
  return SCHEME_PL[id] ?? id;
}

/** "investment, parcel_fee" or "investment|crypto" → Polish labels; separators kept. */
export function schemesPl(value: string | null | undefined): string {
  if (!value) return "";
  return value.split(/(\s*[,|/;]\s*)/).map((part) => SCHEME_PL[part] ?? part).join("");
}

// snake_case codes only (an underscore inside), so ordinary words in a Polish sentence are never replaced
const SNAKE_CODE = /(?<![\w:/.=-])([a-z][a-z0-9]*(?:_[a-z0-9]+)+)(?![\w:/.=-])/g;

/** Known snake_case gene kind or scheme codes inside a reason sentence shown in Polish. */
export function polishCodes(text: string): string {
  return text.replace(SNAKE_CODE, (code) => GENE_KIND_PL[code] ?? SCHEME_PL[code] ?? code);
}
