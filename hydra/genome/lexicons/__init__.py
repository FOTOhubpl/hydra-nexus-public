"""Polish scam lexicons (TXT / NAR families).

All patterns are written in the folded form produced by `hydra.genome.txt_norm.fold` (lowercase, no
diacritics, punctuation = space) and are compiled with `compile_terms`: a single space in a pattern matches
any run of whitespace, `\\b` word boundaries are added around each pattern. Stems use `\\w*`.

Origins (recorded so the jury can audit what is prior knowledge vs learned from the material):
- `material`: generic terms also confirmed in the NASK sample. Sample-specific terms (invented platform and
  product names, verbatim headline phrases) are not distributed with the repository: they are merged at import
  time from the local overlay `$HYDRA_DATA_DIR/nask/lexicons/terms.json` (see `lexicons/local.py`);
- `prior`: general knowledge of Polish scam ads (CERT Polska / NASK public warnings: fake investment
  platforms with impersonated celebrities, fake news portals, nutra cash-on-delivery, gambling apps);
- `public`: terms, schemes and brands from cited public sources (CERT Polska reports and posts, CSIRT KNF, KNF list),
  shipped as JSON with a source per entry (`lexicons/public.py`, docs/research/PL_SCAM_ECOSYSTEM.md); merged at
  import before the local overlay; `HYDRA_PL_LEXICON=0` disables them.
A lexicon hit is evidence for the scorer, never a link by itself.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from functools import lru_cache

from hydra.genome.lexicons.persons import FIRST_NAMES, PERSONS, Person  # noqa: F401


@dataclass(frozen=True)
class Term:
    id: str
    patterns: tuple[str, ...]
    weight: float = 1.0
    origin: str = "prior"
    source: str = ""  # source id (lexicons/pl_scam_schemes.json "sources") for origin="public"


def _rx(p: str) -> str:
    return r"\b" + p.replace(" ?", "\0").replace(" ", r"\s+").replace("\0", r"\s*") + r"\b"


@lru_cache(maxsize=64)
def compile_terms(terms: tuple[Term, ...]) -> tuple[tuple[Term, re.Pattern], ...]:
    return tuple((t, re.compile("|".join(_rx(p) for p in t.patterns))) for t in terms)


def T(id: str, *patterns: str, weight: float = 1.0, origin: str = "prior") -> Term:  # noqa: N802
    return Term(id, tuple(patterns), weight, origin)


# ------------------------------------------------------------------------------ schemes
# scheme -> indicative terms. Score = sum of weights of distinct terms hit (one hit per term).
SCHEMES: dict[str, tuple[Term, ...]] = {
    "investment": (
        T("platform_invest", r"platform\w* (inwestycyjn\w*|handlow\w*|tradingow\w*)", r"platform\w* ai",
          weight=2.0, origin="material"),
        T("deposit", r"depozyt\w*", r"wplat\w*", r"minimaln\w* (wplat\w*|depozyt\w*|kwot\w*)", weight=1.5,
          origin="material"),
        T("invest_verb", r"inwest\w*", weight=1.0),
        T("profit", r"zysk\w*", r"zarob\w*", r"dochod\w*", r"pasywn\w* dochod\w*", weight=1.0),
        T("crypto", r"kryptowalut\w*", r"bitcoin\w*", r"crypto\w*", r"trading\w*", r"gield\w*", weight=1.0),
        T("adviser", r"doradc\w*", r"konsultant\w*", r"opiekun\w* konta", r"menedzer\w* konta", weight=1.0,
          origin="material"),
        T("pension", r"emerytaln\w*", r"oszczedzani\w*", weight=0.7, origin="material"),
        T("algorithm", r"algorytm\w*", r"sztuczn\w* inteligencj\w*", weight=0.7, origin="material"),
    ),
    "health_supplement": (
        T("supplement", r"suplement\w*", r"preparat\w*", r"kapsul\w*", r"tabletk\w*", r"opakowa\w*",
          weight=1.5, origin="material"),
        T("disease", r"prostat\w*", r"potencj\w*", r"stawy", r"staw\w*", r"kregoslup\w*", r"przepuklin\w*",
          r"cukrzyc\w*", r"nadcisnieni\w*", r"odchudz\w*", r"schudn\w*", r"nadwag\w*", r"otylosc\w*",
          r"bol\w* plecow", r"leczeni\w*", r"chorob\w*", r"pasozyt\w*", r"wzrok\w*", r"sluch\w*",
          weight=1.0, origin="material"),
        T("pain_body", r"bol\w*", r"plec\w*", r"ledzw\w*", r"miesn\w*", r"kregow\w*", r"wozk\w* inwalidzk\w*",
          r"zapaleni\w*", r"dolegliwosc\w*", weight=1.0, origin="material"),
        T("state_refund", r"pokrywa \d+", r"refundac\w*", r"dofinansow\w*", r"program\w* panstwow\w*",
          weight=1.0, origin="material"),
        T("doctor", r"lekarz\w*", r"dr", r"n med", r"lek med", r"profesor\w*", r"kardiolog\w*", r"urolog\w*",
          weight=1.0, origin="material"),
        T("cod", r"za pobraniem", r"platnosc przy odbiorze", r"zamow\w*", r"kurier\w*", weight=1.0,
          origin="material"),
        T("discount", r"\d+ ?% (znizk\w*|taniej|rabat\w*)", r"znizk\w*", r"rabat\w*", r"promocj\w*", weight=0.7),
        T("body", r"sylwetk\w*", r"waga", r"kilogram\w*", r"\d+ kg", r"metabolizm\w*", weight=0.7,
          origin="material"),
    ),
    "gambling": (
        T("casino", r"kasyn\w*", r"casino", r"slot\w*", r"automat\w* do gier", r"ruletk\w*", weight=2.0),
        T("spins", r"darmow\w* spin\w*", r"free spins?", r"spin\w*", r"jackpot\w*", r"big win", r"wygraj\w*",
          r"wygran\w*", weight=1.5, origin="material"),
        T("bonus", r"bonus\w*", r"bonus powitaln\w*", weight=1.0, origin="material"),
        T("game", r"zagraj\w*", r"graj\w*", r"gr[ay] (online|w gre)", r"aplikacj\w* (do )?gr\w*", weight=1.0,
          origin="material"),
        T("bonanza", r"sweet bonanza", r"gates of olympus", r"aviator", r"plinko",
          weight=2.0, origin="material"),
        T("age18", r"18 ?\+", r"begambleaware\w*", r"gambling commission", weight=1.0, origin="material"),
    ),
    "fake_news_bait": (
        T("live", r"na zywo", r"pilne", r"breaking", r"z ostatniej chwili", weight=1.0, origin="material"),
        T("shock", r"szok\w*", r"wstrzasn\w*", r"skandal\w*", r"sensacj\w*", r"nikt sie (tego )?nie spodziewal\w*",
          weight=1.0, origin="material"),
        T("arrest", r"aresztowan\w*", r"zatrzyman\w*", r"w kajdankach", r"kajdank\w*", weight=1.5,
          origin="material"),
        T("attack", r"zamach\w*", r"strzal\w*", r"strzelal\w*", r"postrzelon\w*", r"atak\w* na", weight=1.5,
          origin="material"),
        T("deleted", r"usunieto", r"usuniet\w*", r"zakazan\w*", r"ocenzurowan\w*", r"zablokowa\w*",
          weight=1.5, origin="material"),
        T("interview", r"wywiad\w*", r"w studiu", r"transmisj\w*", r"po emisji", r"program\w* na zywo",
          weight=1.0, origin="material"),
        T("article", r"redakcj\w*", r"oprac", r"fot ", r"dziennikarz\w*", weight=0.7, origin="material"),
    ),
    # G12 (2026-11-21): funds-recovery scam ("we will recover your lost money"), our own Polish wording after
    # public CERT Polska / KNF warnings about fake law firms and recovery agents (prior knowledge, not sample text)
    "funds_recovery": (
        T("recover_promise", r"odzyskam\w* (twoje |pana |pani |swoje |wasze )?(srodki|pieniadze|oszczednosci)",
          r"odzyska\w* (twoje |pana |pani |swoje |utracon\w* )?(srodki|pieniadze|oszczednosci|inwestycj\w*)",
          r"odzyskani\w* (utraconych )?(srodkow|pieniedzy|oszczednosci|inwestycji)",
          r"odzyskiwani\w* (srodkow|pieniedzy)",
          weight=3.0),
        T("lost_money", r"utracon\w* (srodk\w*|pieniadz\w*|pieniedz\w*|oszczednosc\w*|inwestycj\w*)",
          r"stracil\w* (pieniadze|srodki|oszczednosci)", r"(padl\w*|padles|padlas) ofiar\w*",
          r"ofiar\w* oszust\w*", r"oszukan\w* przez", weight=1.5),
        T("refund_channel", r"zwrot (srodkow|pieniedzy)", r"chargeback", r"reklamacj\w* (transakcji|przelewu)",
          weight=1.0),
        T("legal_agent", r"kancelari\w*", r"prawnik\w*", r"adwokat\w*", r"radc\w* prawn\w*",
          r"agencj\w* (windykac|odzyskiw)\w*", weight=1.0),
        T("upfront_fee", r"oplat\w* (wstepn\w*|manipulacyjn\w*|administracyjn\w*)", r"podatek od zwrotu",
          r"oplat\w* za odzyskanie", weight=1.0),
    ),
    "giveaway": (
        T("free", r"za darmo", r"gratis\w*", r"bezplatn\w*", weight=1.0),
        T("prize", r"nagrod\w*", r"konkurs\w*", r"losowani\w*", r"rozdajemy", r"do wygrania", r"voucher\w*",
          r"bon\w* (o wartosci|na)", r"karta podarunkow\w*", weight=1.5),
        T("parcel", r"paczk\w* (czeka|do odebrania)", r"doplat\w*", r"oplat\w* (celn\w*|za dostawe)",
          weight=1.5),
        T("survey", r"ankiet\w*", r"odpowiedz na \d+ pytan\w*", weight=1.0),
    ),
}
SCHEME_MIN_SCORE = 2.5

# ------------------------------------------------------------------------------ motifs
MOTIFS: tuple[Term, ...] = (
    T("assassination_attempt", r"zamach\w*", r"strzal\w* w kierunku", r"postrzelon\w*", origin="material"),
    T("arrest", r"aresztowan\w*", r"wyprowadzon\w* (go |ja )?w kajdankach", r"zatrzymal\w* (go|ja) policj\w*",
      origin="material"),
    T("banned_interview", r"wywiad\w* (zakazan\w*|usuniet\w*)", r"nagrani\w* (wciaz|jeszcze) (jest )?dostepn\w*",
      r"zanim (to )?usuna", origin="material"),
    T("studio_scandal", r"opuscil\w* studio", r"w studiu", r"po emisji",
      r"podczas transmisji", origin="material"),
    T("government_program", r"program\w* (rzadow\w*|panstwow\w*)", r"rzad (wprowadzil|zatwierdzil)",
      r"dofinansowani\w*", r"refundacj\w*", r"pokrywa \d+", origin="material"),
    T("bank_angry", r"bank\w* (jest )?wsciekl\w*", r"banki (sa )?wsciekl\w*", r"banki nie chca", r"banki ukrywaj\w*"),
    T("celebrity_endorsement", r"(zarekomendowal\w*|poleca|polecil\w*|wspiera|promuje|promowal\w*) platform\w*",
      r"inicjator\w* platform\w*", r"zalozyciel\w*", origin="material"),
    T("authority_supervision", r"nadzor\w* knf", r"pod nadzorem", r"knf", r"komisj\w* nadzoru finansow\w*",
      r"zus pl", r"zus", r"nbp", r"bank\w* centraln\w*", origin="material"),
    T("journalist_test", r"przetestowal\w*",
      origin="material"),
    T("poll_side", r"ankiet\w*", origin="material"),
    T("registration_steps", r"jak (sie )?zarejestrowac", r"jak zaczac", r"krok \d", r"zarejestruj\w*",
      origin="material"),
    T("adviser_callback", r"doradc\w* (sie )?(skontaktuje|zadzwoni)", r"konsultant\w* (sie )?(skontaktuje|zadzwoni)",
      r"oddzwoni\w*", r"zadzwoni do (ciebie|pana|pani)", origin="material"),
    T("rags_to_riches", r"zamienil\w* \d+ ?zl na", r"z \d+ ?zl", r"milioner\w*", r"\d+ ?zl dziennie",
      r"\d+ ?zl (miesiecznie|tygodniowo)", origin="material"),
    T("doctors_shocked", r"lekarze w szoku", r"lekarze (sa )?(zdumieni|wsciekli)", r"koncern\w* farmaceutyczn\w*",
      origin="material"),
    T("scientifically_proven", r"naukowo udowodnion\w*", r"badani\w* klinicz\w*", r"udowodnion\w*",
      origin="material"),
    T("miracle_fast_result", r"w \d+ dni", r"bez diety", r"bez cwiczen", r"juz dzis wieczorem",
      origin="material"),
    T("fake_comments", r"lubie to", r"odpowiedz", r"\d+ (min|godz) temu", origin="material"),
    T("anti_migrant_bait", r"migrant\w*", r"imigrant\w*", r"uchodzc\w*",
      origin="material"),
    T("health_fear", r"zabija\w*",
      origin="material"),
    T("crypto_ai_bot", r"bot\w*", r"algorytm\w* ai", r"sztuczn\w* inteligencj\w*", origin="material"),
    T("pension_platform", r"emerytaln\w*", r"oszczedzani\w* emerytaln\w*", origin="material"),
)

# ------------------------------------------------------------------------------ persona
PERSONAS: tuple[Term, ...] = (
    T("doctor", r"lekarz\w*", r"dr", r"n med", r"lek med", r"doktor\w*", r"profesor\w*", r"prof",
      r"kardiolog\w*", r"urolog\w*", r"ortoped\w*", origin="material"),
    T("journalist", r"redakcj\w*", r"dziennikar\w*", r"reporter\w*", r"oprac", r"prezenter\w*",
      origin="material"),
    T("clergy", r"arcybiskup\w*", r"abp", r"biskup\w*", r"ksiedz\w*", r"ks", r"duchown\w*"),
    T("nurse", r"pielegniar\w*", origin="material"),
    T("chatbot_consultant", r"jestem (twoim |twoja )?(asystent\w*|konsultant\w*|doradc\w*)", r"czat\w*",
      r"chatbot\w*", origin="material"),
    T("expert", r"ekspert\w*", r"analityk\w*", r"specjalist\w*", origin="material"),
    T("celebrity", r"gwiazd\w*", r"celebryt\w*", r"pilkarz\w*", r"milioner\w*", r"biznesmen\w*",
      r"zalozyciel\w*", origin="material"),
    T("politician", r"premier\w*", r"minister\w*", r"posel\w*", r"prezydent\w*", r"prezes\w*", origin="material"),
    T("ordinary_testimonial", r"pani \w+ z", r"mam \d+ lat", r"jestem emeryt\w*", r"mama \w+", origin="prior"),
)

# ------------------------------------------------------------------------------ promise
PROMISES: tuple[Term, ...] = (
    T("money_multiplier", r"zamienil\w* \d[\d ]* ?zl na \d[\d ]*", r"\d+ ?x", r"podwoi\w*", r"potroi\w*",
      origin="material"),
    T("passive_income", r"\d[\d ]* ?zl (miesiecznie|dziennie|tygodniowo|na miesiac|w miesiac)",
      r"pasywn\w* dochod\w*", r"dodatkow\w* dochod\w*", r"zarabia\w*", origin="material"),
    T("fast_profit", r"w (kilka|ciagu kilku) dni", r"po \d+ dniach", r"juz po", r"otrzymac \d[\d ]* ?zl",
      origin="material"),
    T("guaranteed", r"gwarant\w*", r"bez ryzyka", r"100 ?% (bezpieczn\w*|skuteczn\w*)", origin="material"),
    T("weight_loss", r"schudn\w*", r"idealn\w* sylwetk\w*", r"smukl\w* sylwetk\w*", r"\d+ kg", origin="material"),
    T("cure", r"wylecz\w*", r"pozbyc sie", r"raz na zawsze", r"przywraca\w*", r"skuteczn\w*", origin="material"),
    T("bonus", r"bonus\w* (do )?\d", r"darmow\w* spin\w*", r"\d+ ?% bonus\w*", origin="material"),
    T("discount", r"\d+ ?% (znizk\w*|taniej)", r"\-\s?\d+ ?%", r"znizk\w* \d+ ?%", origin="material"),
    T("free_state_money", r"pokrywa \d+", r"program\w* (rzadow\w*|panstwow\w*)", r"dofinansowan\w*",
      origin="material"),
)

# ------------------------------------------------------------------------------ CTA
CTAS: tuple[Term, ...] = (
    T("learn_more", r"dowiedz sie wiecej", r"learn more", r"dowiedz sie", origin="material"),
    T("play", r"zagraj w gre", r"graj teraz", r"zagraj", r"play game", r"play now", origin="material"),
    T("install", r"pobierz aplikacje", r"zainstaluj", r"install", r"pobierz", origin="material"),
    T("register", r"zarejestruj sie", r"zarejestrowac", r"rejestracj\w*", r"zaloz konto", r"sign up",
      origin="material"),
    T("order", r"zamow\w*", r"kup teraz", r"zamawiam", r"order now", origin="material"),
    T("leave_phone", r"zostaw (swoj )?numer", r"podaj numer", r"numer telefonu", r"oddzwonimy", r"zadzwonimy",
      origin="material"),
    T("watch", r"zobacz (szybko|nagranie|wideo|teraz)", r"ogladaj", r"obejrzyj", r"kliknij aby posluchac",
      r"posluchaj", origin="material"),
    T("read", r"czytaj (wiecej|dalej)", r"czytac", r"przeczytaj", origin="material"),
    T("click", r"kliknij", r"klik", origin="material"),
    T("start_investing", r"zacznij (zarabiac|inwestowac)", r"rozpocznij", r"zacznij teraz", origin="material"),
)

# ------------------------------------------------------------------------------ urgency
URGENCY: tuple[Term, ...] = (
    T("countdown", r"\d\d \d\d \d\d", r"0\d [0-5]\d", r"oferta wygasa",
      r"pozostalo \d+ (min|godz|sekund)", r"do konca promocji", r"timer", origin="material"),
    T("limited_stock", r"zostal\w* (tylko )?\d+ (opakowan\w*|sztuk|szt|paczek)", r"tylko \d+ (opakowan\w*|sztuk)",
      r"ostatni\w* (opakowan\w*|sztuk\w*)", r"only \d+ left", origin="material"),
    T("limited_places", r"liczba pozostalych miejsc", r"ostatnie miejsca", r"pozostalo \d+ miejsc\w*",
      r"liczba miejsc", origin="material"),
    T("time_limited", r"tylko dzis", r"tylko dzisiaj", r"czas ucieka", r"nie zwlekaj", r"spiesz sie",
      r"ostatnia szansa", r"zobacz szybko", r"zanim (to )?usuna", origin="material"),
    T("today_only_price", r"cena promocyjn\w*", r"stara cena", r"nowa cena", origin="material"),
)

# ------------------------------------------------------------------------------ brands
# Impersonated media brands (folded): well-known Polish and international news brands.
MEDIA_BRANDS: tuple[Term, ...] = (
    T("tvn24", r"tvn ?24\w*", r"tvn24 go", r"fakty po faktach", r"kropka nad i", r"fakty tvn", origin="material"),
    T("gazeta_pl", r"gazeta pl", r"sport pl", r"plotek", r"next gazeta", origin="material"),
    T("polsat_news", r"polsat news", r"polsat", r"wydarzenia", r"gosc wydarzen", origin="material"),
    T("interia", r"interia\w*", origin="material"),
    T("onet", r"onet\w*", r"onet pl"),
    T("wp_pl", r"wirtualna polska", r"wp pl"),
    T("rmf", r"rmf fm", r"rmf24"),
    T("tvp", r"tvp info", r"tvp"),
    T("fakt", r"fakt pl", r"fakt24"),
    T("business_insider", r"business insider"),
    T("forbes", r"forbes"),
    T("rzeczpospolita", r"rzeczpospolit\w*", r"rp pl"),
    T("money_pl", r"money pl"),
    T("pap", r"pap", origin="material"),
    T("us_news", r"fox news", r"nbc news", r"cnn", r"bbc", origin="material"),
)
INSTITUTION_BRANDS: tuple[Term, ...] = (
    T("knf", r"knf", r"komisj\w* nadzoru finansow\w*", origin="material"),
    T("zus", r"zus", r"zus pl", origin="material"),
    T("nbp", r"nbp", r"narodow\w* bank\w* polski\w*", r"bank\w* centraln\w*", origin="material"),
    T("pekao", r"pekao", r"bank pekao", origin="material"),
    T("pko_bp", r"pko bp", r"pko bank polski", r"ipko"),
    T("mbank", r"mbank"),
    T("deutsche_bank", r"deutsche bank", origin="material"),
    T("inpost", r"inpost\w*", r"paczkomat\w*", origin="material"),
    T("orlen", r"orlen\w*"),
    T("gov_pl", r"gov pl", r"mobywatel", r"ministerstw\w* finansow"),
    T("app_store", r"app store", r"google play", origin="material"),
    T("blik", r"blik", origin="material"),
    T("prokuratura", r"prokuratur\w*", origin="material"),
)

# Invented platform / product brands are learned dynamically (`find_products`); names seen in the organiser
# sample come from the local overlay (`lexicons/local.py`). Only public brands ship in the repository.
KNOWN_PRODUCTS: tuple[Term, ...] = (
    T("sweet_bonanza", r"sweet bonanza", origin="material"),
)

# Context words that introduce a product / platform name ("platformy X Y", "aplikacji X", "preparat X").
PRODUCT_CONTEXT = re.compile(
    r"\b(platform\w*|aplikacj\w*|preparat\w*|suplement\w*|produkt\w*|system\w*|program\w*|projekt\w*|"
    r"kapsulk\w*|kurs\w*|portal\w*|serwis\w*)\b"
)

# Platform UI chrome (Meta / Google), used to read the advertiser name next to "Sponsorowane".
SPONSORED = re.compile(r"\b(sponsorowane|sponsored|reklama)\b")

# ------------------------------------------------------------------------------ funnel (modus operandi)
# Scam kill-chain stages (wording after the Meta "Online Operations Kill Chain" threat reports, MechScam
# arXiv:2611.06110 mechanism labels and PreScam arXiv:2605.12243 kill chain), observed per incident.
FUNNEL_STAGES = ("lure", "trust", "offer", "capture", "handoff", "payment")
FUNNEL_LURE_MOTIFS = frozenset({"assassination_attempt", "arrest", "banned_interview", "studio_scandal",
                                "celebrity_endorsement", "anti_migrant_bait", "doctors_shocked", "health_fear",
                                "rags_to_riches", "government_program"})
FUNNEL_TRUST_MOTIFS = frozenset({"authority_supervision", "journalist_test", "scientifically_proven",
                                 "fake_comments", "poll_side"})
FUNNEL_TRUST_PERSONAS = frozenset({"doctor", "journalist", "expert", "clergy", "nurse", "ordinary_testimonial"})
HANDOFF_TERMS: tuple[Term, ...] = (
    T("callback_promise", r"(doradc|konsultant|opiekun|menedzer|specjalist)\w* (sie )?(z (toba|panem|pania) )?"
      r"(skontaktuje|zadzwoni|oddzwoni)\w*", r"oddzwonimy", r"zadzwonimy", r"skontaktujemy sie",
      r"operator\w* (sie )?(skontaktuje|zadzwoni)", origin="material"),
    T("chatbot", r"jestem (twoim |twoja )?(asystent\w*|konsultant\w*|doradc\w*)", r"czat\w* z konsultant\w*",
      origin="material"),
    T("messenger_handoff", r"whatsapp", r"telegram", origin="prior"),
)
PAYMENT_TERMS: tuple[Term, ...] = (
    T("deposit", r"minimaln\w* (wplat\w*|depozyt\w*|kwot\w*)", r"wplac\w*", r"depozyt\w*", r"doladuj\w*",
      origin="material"),
    T("second_deposit", r"kolejn\w* wplat\w*", r"drug\w* wplat\w*", origin="material"),
    T("withdrawal", r"wyplat\w*", r"wyplacil\w*", r"wyplacic", origin="material"),
    T("cash_on_delivery", r"za pobraniem", r"platnosc przy odbiorze", r"zaplac\w* (kurierowi|przy odbiorze)",
      origin="material"),
    T("payment_method", r"blik", r"karta (platnicz|kredytow)\w*", r"przelew\w*", r"kryptowalut\w*",
      origin="material"),
)

# ------------------------------------------------------------------------------ public-source lexicons
# Short brand ids that count only when written in capitals (TXT find_brands has its own generic rule; NAR uses this).
UPPER_ONLY_BRANDS: frozenset[str] = frozenset({"zus", "knf", "nbp", "tvp", "pap", "us_news"})
# Scheme taxonomy from public sources: scheme id -> {"label_pl", "funnel", "channels", "payment", "sources"}.
SCHEME_INFO: dict[str, dict] = {}


def _merge_public(base: tuple[Term, ...], extra: list[dict]) -> tuple[Term, ...]:
    """Append public-source terms; a term id that already exists keeps its own patterns (no silent widening of
    material-confirmed terms) and the public term is added under `<id>_pl` instead."""
    out = list(base)
    have = {t.id for t in out}
    for e in extra:
        tid = e["id"] if e["id"] not in have else f"{e['id']}_pl"
        if tid in have or not e.get("patterns"):
            continue
        out.append(Term(tid, tuple(e["patterns"]), float(e.get("weight", 1.0)), "public", str(e.get("source", ""))))
        have.add(tid)
    return tuple(out)


def _apply_public() -> None:
    from hydra.genome.lexicons import public

    global UPPER_ONLY_BRANDS
    data = public.load("pl_scam_schemes")
    g = globals()
    by_group: dict[str, list[dict]] = {}
    by_scheme: dict[str, list[dict]] = {}
    for e in data.get("terms") or []:
        if e.get("group") == "SCHEMES" and e.get("scheme"):
            by_scheme.setdefault(e["scheme"], []).append(e)
        elif e.get("group") in _GROUPS:
            by_group.setdefault(e["group"], []).append(e)
    for name, extra in by_group.items():
        g[name] = _merge_public(g[name], extra)
    for scheme, extra in by_scheme.items():
        SCHEMES[scheme] = _merge_public(SCHEMES.get(scheme, ()), extra)
    SCHEME_INFO.update(public.schemes())
    upper = set(UPPER_ONLY_BRANDS)
    for b in public.brands().values():
        grp = b.get("group")
        if grp not in ("MEDIA_BRANDS", "INSTITUTION_BRANDS"):
            continue
        if b.get("upper_only"):
            upper.add(b["id"])
        if b.get("patterns"):
            src = (b.get("sources") or [""])[0]
            g[grp] = _merge_public(g[grp], [{"id": b["id"], "patterns": b["patterns"], "source": src}])
    UPPER_ONLY_BRANDS = frozenset(upper)


_GROUPS = ("MOTIFS", "PERSONAS", "PROMISES", "CTAS", "URGENCY", "MEDIA_BRANDS", "INSTITUTION_BRANDS",
           "KNOWN_PRODUCTS", "HANDOFF_TERMS", "PAYMENT_TERMS")


# ------------------------------------------------------------------------------ social-engineering techniques
# G12 (2026-11-21): named persuasion techniques after the public Cialdini-style taxonomy, in our own wording.
# A technique gene is an explanation fact: which lure mechanism the text uses. Sources are the existing NAR term
# hits (kind, term id; "*" = any term of that kind) plus the extra terms below. Not the COLING 2025 dataset.
SE_TECHNIQUES: dict[str, tuple[tuple[str, str], ...]] = {
    "urgency": (("urgency", "countdown"), ("urgency", "time_limited"), ("urgency", "today_only_price")),
    "scarcity": (("urgency", "limited_stock"), ("urgency", "limited_places")),
    "authority": (("motif", "authority_supervision"), ("motif", "scientifically_proven"),
                  ("motif", "government_program"), ("persona", "doctor"), ("persona", "expert"),
                  ("persona", "politician"), ("persona", "clergy"), ("persona", "journalist"), ("persona", "nurse"),
                  ("institution_brand", "*"), ("media_brand", "*")),
    "social_proof": (("motif", "fake_comments"), ("motif", "journalist_test"), ("persona", "ordinary_testimonial")),
    "reciprocity": (("promise", "bonus"), ("promise", "free_state_money")),
    "liking": (("motif", "celebrity_endorsement"), ("persona", "celebrity")),
    "fear": (("motif", "health_fear"), ("motif", "doctors_shocked"), ("motif", "bank_angry"), ("motif", "arrest")),
}
SE_TERMS: dict[str, tuple[Term, ...]] = {
    "urgency": (T("act_now", r"dzialaj (juz )?teraz", r"nie czekaj", r"natychmiast", r"jeszcze dzis"),),
    "scarcity": (T("limited_offer", r"ograniczon\w* (liczb\w*|ilosc\w*|pul\w*|czas\w*)", r"ostatnie (sztuki|wolne)",
                   r"tylko dla (pierwszych|wybranych)", r"wyczerpan\w*"),),
    "authority": (T("credentials", r"licencjonowan\w*", r"regulowan\w*", r"certyfikowan\w*", r"kancelari\w*",
                    r"prawnik\w*", r"adwokat\w*", r"oficjaln\w* (strona|program|partner)"),),
    "social_proof": (T("crowd", r"tysiace (polakow|osob|ludzi|uzytkownikow)", r"\d[\d ]* (osob|polakow|uzytkownikow|"
                       r"klientow) (juz )?(zarabia|skorzystal|dolaczyl)\w*", r"opini\w* (klientow|uzytkownikow)",
                       r"zadowolon\w* klient\w*"),),
    "reciprocity": (T("gift_first", r"w prezencie", r"gratis\w*", r"za darmo", r"bezplatn\w*", r"prezent\w*",
                      r"bonus powitaln\w*"),),
    "liking": (T("like_you", r"zwykl\w* (ludzi|polak\w*|emeryt\w*)", r"tak jak ty", r"jestem jedn\w* z was"),),
    "fear": (T("threat", r"stracisz", r"strac\w* wszystko", r"zablokowan\w* (konto|srodki)", r"ostrzezeni\w*",
               r"zagrozeni\w*", r"niebezpieczenstw\w*", r"grozi (ci|panu|pani)\w*"),),
}
SE_LABEL_PL = {
    "urgency": "presja czasu", "scarcity": "niedobór (ograniczona oferta)", "authority": "autorytet",
    "social_proof": "społeczny dowód słuszności", "reciprocity": "wzajemność (prezent, bonus)",
    "liking": "sympatia / znana osoba", "fear": "strach",
}


# ------------------------------------------------------------------------------ local overlay
def _merge(base: tuple[Term, ...], extra: list[dict] | None) -> tuple[Term, ...]:
    if not extra:
        return base
    out = list(base)
    for e in extra:
        pats = tuple(e["patterns"])
        i = next((k for k, t in enumerate(out) if t.id == e["id"]), None)
        if i is None:
            out.append(Term(e["id"], pats, float(e.get("weight", 1.0)), e.get("origin", "material")))
        else:
            t = out[i]
            out[i] = Term(t.id, t.patterns + tuple(x for x in pats if x not in t.patterns), t.weight, t.origin)
    return tuple(out)


def _apply_overlay() -> None:
    from hydra.genome.lexicons import local

    terms = local.load("terms") or {}
    g = globals()
    for name in _GROUPS:
        g[name] = _merge(g[name], terms.get(name))
    for scheme, extra in (terms.get("SCHEMES") or {}).items():
        SCHEMES[scheme] = _merge(SCHEMES.get(scheme, ()), extra)


_apply_public()
_apply_overlay()
