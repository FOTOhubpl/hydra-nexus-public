"""Polish lexicon for HYDRA Watch name scoring (own lists; nothing copied from other tools).

Brands are the ones Polish scam domains impersonate most often (CERT Polska Warning List, 7-day sample of
2026-11-21, REALTIME_DISCOVERY.md §1: allegrolokalnie, allegro, olx, vinted, inpost, banks, couriers, state
bodies). `official` = registrable domains owned by the brand: a host under them is never scored as a lure.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Brand:
    name: str                 # display name
    tokens: tuple[str, ...]   # whole-token spellings (ASCII, lower case, no hyphen)
    category: str             # marketplace | courier | bank | payment | state | energy | telco | media
    official: tuple[str, ...] = ()


BRANDS: tuple[Brand, ...] = (
    Brand("Allegro", ("allegro",), "marketplace", ("allegro.pl", "allegro.com", "allegrolokalnie.pl")),
    Brand("Allegro Lokalnie", ("allegrolokalnie", "allegrolokal", "alegrolokalnie"), "marketplace",
          ("allegrolokalnie.pl",)),
    Brand("OLX", ("olx",), "marketplace", ("olx.pl", "olx.com", "olxgroup.com")),
    Brand("Vinted", ("vinted",), "marketplace", ("vinted.pl", "vinted.com")),
    Brand("Otomoto", ("otomoto",), "marketplace", ("otomoto.pl",)),
    Brand("Ceneo", ("ceneo",), "marketplace", ("ceneo.pl",)),
    Brand("InPost", ("inpost", "paczkomat", "paczkomaty"), "courier", ("inpost.pl", "inpost.eu", "paczkomaty.pl")),
    Brand("Poczta Polska", ("pocztapolska", "pocztex"), "courier", ("poczta-polska.pl", "pocztex.pl", "envelo.pl")),
    Brand("DHL", ("dhl",), "courier", ("dhl.com", "dhl.pl", "dhl.de")),
    Brand("DPD", ("dpd",), "courier", ("dpd.com", "dpd.pl")),
    Brand("GLS", ("gls",), "courier", ("gls-group.eu", "gls-poland.com")),
    Brand("FedEx", ("fedex",), "courier", ("fedex.com",)),
    Brand("UPS", ("ups",), "courier", ("ups.com",)),
    Brand("PKO BP", ("pko", "pkobp", "ipko"), "bank", ("pkobp.pl", "ipko.pl", "pko.pl")),
    Brand("mBank", ("mbank",), "bank", ("mbank.pl", "mbank.com")),
    Brand("ING", ("ing", "ingbank"), "bank", ("ing.pl", "ing.com", "ingbank.pl")),
    Brand("Santander", ("santander",), "bank", ("santander.pl", "santander.com")),
    Brand("Pekao", ("pekao", "pekaosa"), "bank", ("pekao.com.pl",)),
    Brand("Millennium", ("millennium", "bankmillennium"), "bank", ("bankmillennium.pl",)),
    Brand("Alior", ("alior", "aliorbank"), "bank", ("aliorbank.pl",)),
    Brand("Credit Agricole", ("creditagricole", "agricole"), "bank", ("credit-agricole.pl",)),
    Brand("BNP Paribas", ("bnpparibas", "bnp"), "bank", ("bnpparibas.pl",)),
    Brand("Revolut", ("revolut",), "bank", ("revolut.com",)),
    Brand("BLIK", ("blik",), "payment", ("blik.com",)),
    Brand("Przelewy24", ("przelewy24", "przelewy"), "payment", ("przelewy24.pl",)),
    Brand("PayU", ("payu",), "payment", ("payu.pl", "payu.com")),
    Brand("ZUS", ("zus",), "state", ("zus.pl",)),
    Brand("gov.pl", ("govpl",), "state", ("gov.pl",)),  # bare "gov" is global noise (us-gov-west-1)
    Brand("mObywatel", ("mobywatel",), "state", ("mobywatel.gov.pl", "gov.pl")),
    Brand("e-Urząd Skarbowy", ("podatki", "eurzad", "urzadskarbowy"), "state", ("podatki.gov.pl", "gov.pl")),
    Brand("NFZ", ("nfz",), "state", ("nfz.gov.pl", "gov.pl")),
    Brand("Policja", ("policja",), "state", ("policja.pl", "policja.gov.pl")),
    Brand("e-TOLL", ("etoll",), "state", ("etoll.gov.pl", "gov.pl")),
    Brand("ORLEN", ("orlen",), "energy", ("orlen.pl", "orlen.com")),
    Brand("PGE", ("pge",), "energy", ("pge.pl", "gkpge.pl")),
    Brand("Tauron", ("tauron",), "energy", ("tauron.pl",)),
    Brand("Enea", ("enea",), "energy", ("enea.pl",)),
    Brand("PGNiG", ("pgnig",), "energy", ("pgnig.pl",)),
    Brand("Play", ("playmobile",), "telco", ("play.pl",)),
    Brand("Netflix", ("netflix",), "media", ("netflix.com",)),
    Brand("TVN24", ("tvn24",), "media", ("tvn24.pl",)),
    Brand("Biedronka", ("biedronka",), "retail", ("biedronka.pl",)),
    Brand("Lidl", ("lidl",), "retail", ("lidl.pl", "lidl.com")),
    Brand("Aldi", ("aldi",), "retail", ("aldi.pl", "aldi.com")),
    Brand("Rossmann", ("rossmann",), "retail", ("rossmann.pl",)),
)

# Polish bait words (lure vocabulary). Matched as whole tokens or as a token prefix (stem).
BAIT_STEMS: tuple[str, ...] = (
    "oferta", "oferty", "okazj", "transakcj", "aukcj", "doplat", "zwrot", "paczk", "przesylk", "inwestycj",
    "inwestuj", "zysk", "zarob", "zarabia", "dochod", "wyplat", "platnos", "zaplat", "odbior", "dostaw",
    "weryfikacj", "potwierdz", "logowani", "zaloguj", "konto", "bezpiecz", "blokad", "odblok", "aktualizacj",
    "faktur", "rachun", "mandat", "nagrod", "wygran", "loteri", "bonus", "kupon", "promocj", "rabat",
    "bilet", "numerbiletu", "zamowieni", "kurier", "sledzen", "kryptowalut", "bitcoin", "gielda", "inwest",
    "kredyt", "pozyczk", "dotacj", "swiadczen", "zasilek", "refundacj", "podatek", "nadplat",
)
# Generic English credential words: weaker (also common on benign hosts).
GENERIC_STEMS: tuple[str, ...] = ("login", "verify", "secure", "account", "update", "support", "payment", "wallet")

# Tokens that imitate a public-suffix boundary when they appear left of the real suffix.
FAKE_SUFFIX_TOKENS: frozenset[str] = frozenset({"pl", "com", "net", "org", "gov", "eu", "info"})

# Abused-TLD priors in bits (initial priors, REALTIME_DISCOVERY.md §5.3; CERT Polska 7-day top suffixes on
# 2026-11-21 were com, shop, store, sbs, cyou, top, cfd, pro, digital). Not yet measured lifts: see WATCH.md.
TLD_PRIOR_BITS: dict[str, float] = {
    "sbs": 2.0, "cfd": 2.0, "cyou": 2.0, "lat": 2.0, "icu": 2.0, "bond": 2.0, "rest": 1.5, "buzz": 1.5,
    "top": 1.5, "xyz": 1.5, "click": 1.5, "shop": 1.5, "store": 1.0, "online": 1.0, "site": 1.0, "live": 1.0,
    "life": 1.0, "pro": 1.0, "digital": 1.0, "info": 1.0, "fun": 1.0, "space": 1.0, "website": 1.0,
    "quest": 1.5, "autos": 1.5, "mom": 1.5, "beauty": 1.0, "hair": 1.0, "skin": 1.0, "makeup": 1.0, "monster": 1.5,
    "today": 0.5, "world": 0.5, "app": 0.5, "dev": 0.5, "vip": 1.0, "win": 1.0, "help": 1.0, "support": 1.0,
}

# First host labels that carry no meaning (dropped before scoring).
NOISE_LABELS: frozenset[str] = frozenset({"www", "m", "mail", "webmail", "cpanel", "autodiscover", "webdisk"})
