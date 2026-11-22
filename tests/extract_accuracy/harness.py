"""F01 extraction-accuracy harness: synthetic artefacts with known embedded facts, scored per extractor.

Everything here is SYNTHETIC: generated hosts, phones, accounts, wallets, tracker ids, chat logs, HAR files and
rendered screenshots, plus person and brand names from the public lexicons shipped in `hydra/genome/lexicons`
(the organiser-derived local overlay is switched off with HYDRA_LEXICON_DIR=none). No organiser data is read.

Flow (the product path, not the finder functions in isolation):
1. `make_corpus(seed, ...)` writes one incident folder per (document, carrier) with a ground-truth record:
   facts `(kind, value)` the extractor should observe, and decoy text (IP addresses, dates, NIP, invalid
   checksums, click ids, ...) that must not become an observation.
2. `run_case` plans the folder with the real adapter (`hydra.adapters.nask_folder.plan_incident`: MIME by bytes,
   roles), resolves it and runs the registered extractors through `hydra.genome.base.run_all`.
3. `observed_facts` maps observed genes of each scored extractor to `(kind, value)`; `score` counts TP/FP/FN per
   extractor x fact kind (and per carrier), with Wilson 95% intervals for precision and recall.

Value conventions follow the product's gene values (E.164 phones, salted `pii_hash` for accounts, `chain:address`
wallets, `telegram:@handle`, `<tracker>:<id>`, `<platform>:<id>` ad ids, `a>b>c` redirect chains), so a mismatch is
a real observation error, not a formatting difference. Accounts and wallets with a failed checksum are decoys:
the product deliberately keeps them at quality <= 0.4, so precision is reported for all observed genes and for
genes with quality >= 0.5.
"""

from __future__ import annotations

import hashlib
import io
import json
import math
import os
import random
import string
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

os.environ.setdefault("HYDRA_LEXICON_DIR", "none")  # public lexicons only (same as the unit tests)

# ------------------------------------------------------------------------------------------------ statistics


def wilson(k: int, n: int, z: float = 1.959964) -> tuple[float, float]:
    """Wilson score interval for k successes in n trials (95% by default); (nan, nan) for n == 0."""
    if n <= 0:
        return (math.nan, math.nan)
    p = k / n
    den = 1 + z * z / n
    centre = (p + z * z / (2 * n)) / den
    half = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / den
    return (max(0.0, centre - half), min(1.0, centre + half))


@dataclass
class Counts:
    tp: int = 0
    fp: int = 0
    fn: int = 0
    fp_hq: int = 0  # false positives with gene quality >= 0.5

    def add(self, other: Counts) -> None:
        self.tp += other.tp
        self.fp += other.fp
        self.fn += other.fn
        self.fp_hq += other.fp_hq

    @property
    def precision(self) -> float:
        return self.tp / (self.tp + self.fp) if self.tp + self.fp else math.nan

    @property
    def precision_hq(self) -> float:
        return self.tp / (self.tp + self.fp_hq) if self.tp + self.fp_hq else math.nan

    @property
    def recall(self) -> float:
        return self.tp / (self.tp + self.fn) if self.tp + self.fn else math.nan

    @property
    def f1(self) -> float:
        p, r = self.precision, self.recall
        if math.isnan(p) or math.isnan(r) or p + r == 0:
            return math.nan if (math.isnan(p) or math.isnan(r)) else 0.0
        return 2 * p * r / (p + r)


# ------------------------------------------------------------------------------------------------ synthetic ids

B58 = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"


def b58encode(b: bytes) -> str:
    n = int.from_bytes(b, "big")
    out = ""
    while n:
        n, r = divmod(n, 58)
        out = B58[r] + out
    return "1" * (len(b) - len(b.lstrip(b"\0"))) + out


def b58check(payload: bytes) -> str:
    chk = hashlib.sha256(hashlib.sha256(payload).digest()).digest()[:4]
    return b58encode(payload + chk)


_BECH = "qpzry9x8gf2tvdw0s3jn54khce6mua7l"


def _polymod(values: list[int]) -> int:
    gen = (0x3B6A57B2, 0x26508E6D, 0x1EA119FA, 0x3D4233DD, 0x2A1462B3)
    chk = 1
    for v in values:
        top = chk >> 25
        chk = (chk & 0x1FFFFFF) << 5 ^ v
        for i in range(5):
            chk ^= gen[i] if (top >> i) & 1 else 0
    return chk


def bech32_segwit_v0(hrp: str, prog: bytes) -> str:
    """BIP-173 P2WPKH address (witness version 0, bech32 constant 1), written from the public specification."""
    acc, bits, data = 0, 0, [0]
    for b in prog:
        acc = (acc << 8) | b
        bits += 8
        while bits >= 5:
            bits -= 5
            data.append((acc >> bits) & 31)
    if bits:
        data.append((acc << (5 - bits)) & 31)
    exp = [ord(c) >> 5 for c in hrp] + [0] + [ord(c) & 31 for c in hrp]
    pm = _polymod(exp + data + [0] * 6) ^ 1
    chk = [(pm >> 5 * (5 - i)) & 31 for i in range(6)]
    return hrp + "1" + "".join(_BECH[d] for d in data + chk)


# EIP-55 mixed-case test vectors published in the EIP-55 specification text.
EIP55_VECTORS = ("0x5aAeb6053F3E94C9b9A09f33669435E7Ef1BeAed", "0xfB6916095ca1df60bB79Ce92cE3Ea74c37c5d359",
                 "0xdbF03B407c01E7cD3CBea99509d93f8DDDC8C6FB", "0xD1220A0cf47c7B9Be7A2E6BA89F429762e7b9aDb")


def iban_check_digits(cc: str, bban: str) -> str:
    s = bban + cc + "00"
    n = int("".join(str(int(c, 36)) for c in s))
    return f"{98 - n % 97:02d}"


def pl_sort_code(bank3: str, rng: random.Random) -> str:
    """8-digit Polish sort code: 3-digit bank number, 4 branch digits, weighted check digit (3,9,7,1,3,9,7)."""
    s7 = bank3 + "".join(rng.choice(string.digits) for _ in range(4))
    total = sum(int(d) * w for d, w in zip(s7, (3, 9, 7, 1, 3, 9, 7), strict=True))
    return s7 + str((10 - total % 10) % 10)


PL_BANKS = ("102", "105", "109", "114", "116", "124", "160", "175", "187", "249")
FOREIGN_IBANS = ("DE89370400440532013000", "GB82WEST12345698765432", "FR1420041010050500013M02606",
                 "ES9121000418450200051332", "LT121000011101001000")


def pii(value: str) -> str:
    from hydra.genome.webparse import pii_hash

    return pii_hash(value)


def group4(s: str) -> str:
    return " ".join(s[i:i + 4] for i in range(0, len(s), 4))


# ------------------------------------------------------------------------------------------------ fact templates

WORDS = ("zysk", "kapital", "inwest", "bonus", "fundusz", "lokata", "krypto", "energia", "paczka", "dostawa",
         "rabat", "prezent", "nagroda", "portfel", "rynek", "zloto", "akcje", "emerytura")
TLDS = ("pl", "com", "online", "top", "info", "net", "site", "shop", "xyz", "eu")


@dataclass
class Doc:
    doc_id: str
    lines: list[str]
    facts: set[tuple[str, str]] = field(default_factory=set)       # txt_entities ground truth
    page_facts: set[tuple[str, str]] = field(default_factory=set)  # inf_dom ground truth (HTML carrier)
    decoys: list[str] = field(default_factory=list)
    scripts: list[str] = field(default_factory=list)                # tracker snippets for the HTML carrier
    sender_phone: str | None = None                                 # E.164 of the phone-number chat sender
    sender_phone_raw: str | None = None


class Gen:
    def __init__(self, seed: int):
        self.rng = random.Random(seed)
        self._hosts: set[str] = set()

    # -- primitives
    def host(self, sub: bool = False) -> str:
        while True:
            r = self.rng
            name = f"{r.choice(WORDS)}-{r.choice(WORDS)}{r.randint(2, 99)}"
            h = f"{name}.{r.choice(TLDS)}"
            if sub:
                h = f"{r.choice(('promo', 'oferta', 'go', 'app'))}.{h}"
            if h not in self._hosts:
                self._hosts.add(h)
                return h

    def pl_mobile(self) -> str:
        return self.rng.choice("5678") + "".join(self.rng.choice(string.digits) for _ in range(8))

    def intl(self) -> tuple[str, str]:
        import phonenumbers

        while True:
            region = self.rng.choice(("GB", "DE", "CZ", "UA", "LT", "NL"))
            ex = phonenumbers.example_number_for_type(region, phonenumbers.PhoneNumberType.MOBILE)
            nat = str(ex.national_number)
            nat = nat[:-4] + "".join(self.rng.choice(string.digits) for _ in range(4))
            num = phonenumbers.parse(f"+{ex.country_code}{nat}", None)
            if phonenumbers.is_valid_number(num):
                return (phonenumbers.format_number(num, phonenumbers.PhoneNumberFormat.INTERNATIONAL),
                        phonenumbers.format_number(num, phonenumbers.PhoneNumberFormat.E164))

    def pl_iban(self) -> str:
        bban = pl_sort_code(self.rng.choice(PL_BANKS), self.rng) + "".join(
            self.rng.choice(string.digits) for _ in range(16))
        return "PL" + iban_check_digits("PL", bban) + bban

    def handle(self) -> str:
        return self.rng.choice(("inwestor", "doradca", "kryptoinfo", "zysk", "bonusy")) + "_" + "".join(
            self.rng.choice(string.ascii_lowercase) for _ in range(5))

    def digits(self, n: int) -> str:
        return self.rng.choice("123456789") + "".join(self.rng.choice(string.digits) for _ in range(n - 1))

    def upper_alnum(self, n: int) -> str:
        return "".join(self.rng.choice(string.ascii_uppercase + string.digits) for _ in range(n))

    # -- fact lines (text, txt facts, page facts)
    def f_domain(self):
        h = self.host()
        return f"Wejdź na {h} i odbierz bonus", {("domain", h)}, set()

    def f_url(self):
        h = self.host(sub=self.rng.random() < 0.4)
        path = f"/{self.rng.choice(WORDS)}/{self.rng.randint(10, 999)}"
        u = f"https://{h}{path}"
        return f"Rejestracja: {u} (ograniczona liczba miejsc)", {("domain", h), ("url", u)}, set()

    def f_url_defanged(self):
        h = self.host()
        path = f"/{self.rng.choice(WORDS)}"
        dh = h.replace(".", "[.]")
        u = f"https://{h}{path}"
        if self.rng.random() < 0.5:
            return f"Złośliwy adres: hxxps://{dh}{path}", {("domain", h), ("url", u)}, set()
        return f"Zgłoszona domena {dh} (zneutralizowana)", {("domain", h)}, set()

    def f_phone_pl(self):
        d = self.pl_mobile()
        fmt = self.rng.choice((f"+48 {d[:3]} {d[3:6]} {d[6:]}", f"{d[:3]}-{d[3:6]}-{d[6:]}", f"tel. {d}",
                               f"(+48) {d[:3]} {d[3:6]} {d[6:]}", f"0048 {d[:3]} {d[3:6]} {d[6:]}",
                               f"{d[:3]} {d[3:6]} {d[6:]}"))
        return f"Zadzwoń teraz: {fmt}", {("phone", "+48" + d)}, {("phone", pii("+48" + d))}

    def f_phone_intl(self):
        shown, e164 = self.intl()
        return f"Konsultant zagraniczny {shown}", {("phone", e164)}, set()

    def f_iban(self):
        if self.rng.random() < 0.3:
            ib = self.rng.choice(FOREIGN_IBANS)
            return f"Przelew zagraniczny na IBAN {group4(ib)}", {("iban", pii(ib))}, {("iban", pii(ib))}
        ib = self.pl_iban()
        if self.rng.random() < 0.5:
            return f"Wpłać na konto {group4(ib)} tytułem: depozyt", {("iban", pii(ib))}, {("iban", pii(ib))}
        nrb = ib[2:]
        return (f"Numer rachunku: {nrb[:2]} {group4(nrb[2:])}", {("iban", pii(ib))}, {("iban", pii(ib))})

    def f_wallet(self):
        r = self.rng.random()
        if r < 0.25:
            a = b58check(bytes([self.rng.choice((0x00, 0x05))]) + self.rng.randbytes(20))
            v = f"btc:{a}"
            line = f"Wpłać BTC na portfel {a}"
        elif r < 0.45:
            a = bech32_segwit_v0("bc", self.rng.randbytes(20))
            v = f"btc:{a}"
            line = f"Adres bitcoin: {a}"
        elif r < 0.7:
            a = b58check(b"\x41" + self.rng.randbytes(20))
            v = f"trx:{a}"
            line = f"USDT TRC20 portfel: {a}"
        elif r < 0.85:
            a = self.rng.choice(EIP55_VECTORS)
            v = f"eth:{a.lower()}"
            line = f"Ethereum (ERC20) wallet {a}"
        else:
            a = "0x" + self.rng.randbytes(20).hex()
            v = f"eth:{a}"
            line = f"Wyślij ETH na adres portfela {a}"
        return line, {("wallet", v)}, {("wallet", v)}

    def f_email(self):
        h = self.host()
        user = f"{self.rng.choice(('kontakt', 'biuro', 'jan.nowak', 'support'))}{self.rng.randint(1, 99)}"
        e = f"{user}@{h}"
        if self.rng.random() < 0.3:
            return f"Pisz: {user}[at]{h.replace('.', '[.]')}", {("email", e)}, set()
        return f"Kontakt e-mail: {e}", {("email", e)}, {("email", pii(e))}

    def f_telegram(self):
        hd = self.handle()
        r = self.rng.random()
        if r < 0.4:
            line = f"Dołącz do grupy na Telegramie: @{hd}"
        elif r < 0.8:
            line = f"Kanał VIP: https://t.me/{hd}"
        else:
            line = f"Napisz: t.me/{hd} (odpowiadamy 24/7)"
        tg = ("telegram", f"telegram:@{hd}")
        facts = {tg} | ({("brand", "telegram")} if "Telegram" in line else set())  # 'telegram' is a lexicon brand
        if "t.me/" in line:  # a t.me link is also a URL written in the text
            facts |= {("domain", "t.me"), ("url", f"https://t.me/{hd}")}
        return line, facts, {tg}  # inf_dom scans the raw page for t.me links and @handles next to "Telegram"

    def f_price(self):
        r = self.rng.random()
        n = self.rng.choice((49, 99, 149, 249, 990, 1299, 4800))
        if r < 0.3:
            return f"Tylko {n} zł zamiast {n * 2} zł", {("price", f"{n} PLN"), ("price", f"{n * 2} PLN")}, set()
        if r < 0.5:
            return f"Cena promocyjna {n},99 zł", {("price", f"{n}.99 PLN")}, set()
        if r < 0.7:
            big = self.rng.choice((1500, 3000, 12500))
            return (f"Zarabiaj {big // 1000} {big % 1000:03d} zł miesięcznie", {("price", f"{big} PLN")}, set())
        if r < 0.85:
            return f"Depozyt startowy €{n}", {("price", f"{n} EUR")}, set()
        return f"Inwestycja od {n} USD", {("price", f"{n} USD")}, set()

    def f_person(self):
        from hydra.genome.lexicons import PERSONS

        pool = [p for p in PERSONS if p.origin in ("prior", "documented") and len(p.canonical.split()) == 2]
        p = self.rng.choice(pool)
        given, surname = p.canonical.split()
        if (surname.endswith(("ski", "cki")) and given[-1] not in "aeiouy" and self.rng.random() < 0.5):
            form = f"{given}a {surname[:-1]}iego"
            return f"Wywiad z udziałem: {form} ujawnia sekret", {("person", p.canonical)}, set()
        return f"{p.canonical} poleca tę platformę", {("person", p.canonical)}, set()

    def f_brand(self):
        from hydra.genome.lexicons import INSTITUTION_BRANDS, MEDIA_BRANDS

        ids = {t.id for t in MEDIA_BRANDS + INSTITUTION_BRANDS}
        lex = json.loads((Path(__file__).resolve().parents[2] / "hydra/genome/lexicons/pl_brands.json").read_text())
        pool = [b for b in lex["brands"] if b["id"] in ids and "." not in b["name"]]
        b = self.rng.choice(pool)
        return f"Jak informuje {b['name']}, program jest legalny", {("brand", b["id"])}, set()

    FACTS = ("f_domain", "f_url", "f_url_defanged", "f_phone_pl", "f_phone_intl", "f_iban", "f_wallet",
             "f_email", "f_telegram", "f_price", "f_person", "f_brand")

    # -- decoys: text that must not become an observation of the scored kinds
    def decoy(self) -> str:
        r = self.rng
        d = self.pl_mobile()
        bad_iban = self.pl_iban()
        bad_iban = bad_iban[:10] + str((int(bad_iban[10]) + 1) % 10) + bad_iban[11:]
        legacy = b58check(b"\x00" + r.randbytes(20))
        bad_btc = legacy[:-3] + ("2" if legacy[-3] != "2" else "3") + legacy[-2:]
        return r.choice((
            f"Serwer {r.randint(11, 220)}.{d[:3]}.{d[3:6]}.{d[6:9]} odpowiada wolno",
            f"Data: {r.randint(10, 28)}.0{r.randint(1, 9)}.2025, godz. {r.randint(10, 23)}:{r.randint(10, 59)}",
            f"Wersja aplikacji {r.randint(1, 9)}.{d[:3]}.{d[3:6]}.{d[6:]}",
            f"NIP {d[:3]}-{d[3:6]}-{d[6:8]}-{r.randint(10, 99)}",
            f"Identyfikator zgłoszenia {d}{r.randint(100, 999)}",
            f"Rabat -{r.randint(10, 70)}% tylko dziś",
            "Fot. Policja, oprac. red.",
            f"Kod pocztowy {r.randint(10, 99)}-{r.randint(100, 999)} Warszawa",
            f"Plik raport_{r.randint(1, 99)}.pdf w załączniku",
            f"Konto {group4(bad_iban)} (stary numer)",
            f"ref {bad_btc}",
            f"Rok {r.randint(2019, 2026)} był rekordowy, wzrost o {r.randint(5, 95)}%",
        ))

    def doc(self, i: int, n_facts: int = 5) -> Doc:
        names = self.rng.sample(self.FACTS, n_facts)
        d = Doc(f"d{i:03d}", [])
        for nm in names:
            line, facts, page = getattr(self, nm)()
            d.lines.append(line)
            d.facts |= facts
            d.page_facts |= page
        for _ in range(self.rng.randint(1, 2)):
            t = self.decoy()
            d.lines.insert(self.rng.randint(0, len(d.lines)), t)
            d.decoys.append(t)
        # tracker snippets for the HTML carrier (inf_dom)
        pix, aw, g4, gtm = self.digits(15), "AW-" + self.digits(10), "G-" + self.upper_alnum(10), "GTM-" + \
            self.upper_alnum(7)
        d.scripts = [f"fbq('init', '{pix}'); fbq('track', 'PageView');",
                     f"gtag('config', '{aw}'); gtag('config', '{g4}');",
                     f"(function(w,d,s,l,i){{}})(window,document,'script','dataLayer','{gtm}');"]
        d.page_facts |= {("tracker", f"fb_pixel:{pix}"), ("google_ads", f"google_ads:{aw}"),
                         ("tracker", f"ga4:{g4}"), ("tracker", f"gtm:{gtm}")}
        # chat sender phone
        sp = self.pl_mobile()
        d.sender_phone, d.sender_phone_raw = "+48" + sp, f"+48 {sp[:3]} {sp[3:6]} {sp[6:]}"
        return d


# ------------------------------------------------------------------------------------------------ carriers

TEXT_CARRIERS = ("txt", "html", "chat_wa_ios", "chat_wa_android", "chat_tg_json", "chat_json_list")
OCR_SIZES = (12, 16, 24, 36)
OCR_FORMATS = ("png", "jpg90", "jpg50", "jpg20")
PERSONA = "Doradca Marta"


def _font(size: int):
    from PIL import ImageFont

    cands = [os.environ.get("HYDRA_TEST_FONT", "")]
    try:
        import matplotlib

        cands.append(str(Path(matplotlib.__file__).parent / "mpl-data/fonts/ttf/DejaVuSans.ttf"))
    except ImportError:
        pass
    cands += ["/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf", "/System/Library/Fonts/Supplemental/Arial.ttf"]
    for c in cands:
        if c and Path(c).is_file():
            return ImageFont.truetype(c, size), Path(c).name
    return ImageFont.load_default(size), "pillow-default"


def render_png(lines: list[str], size: int, fmt: str) -> tuple[bytes, str, str]:
    from PIL import Image, ImageDraw

    font, font_name = _font(size)
    pad, gap = 24, int(size * 0.8)
    probe = ImageDraw.Draw(Image.new("RGB", (10, 10)))
    widths = [probe.textlength(ln, font=font) for ln in lines]
    w = int(max(widths) + 2 * pad)
    h = int(pad * 2 + len(lines) * (size + gap))
    im = Image.new("RGB", (max(w, 320), h), (250, 250, 247))
    dr = ImageDraw.Draw(im)
    for k, ln in enumerate(lines):
        dr.text((pad, pad + k * (size + gap)), ln, fill=(20, 20, 30), font=font)
    buf = io.BytesIO()
    if fmt == "png":
        im.save(buf, format="PNG")
        return buf.getvalue(), ".png", font_name
    im.save(buf, format="JPEG", quality=int(fmt[3:]))
    return buf.getvalue(), ".jpg", font_name


def chat_text(d: Doc, kind: str) -> tuple[str, str]:
    """(file name, content) of a chat export with the doc lines as messages; senders alternate between a named
    persona and a phone-number sender (whose number is a txt_entities fact)."""
    senders = [PERSONA, d.sender_phone_raw]
    if kind == "chat_wa_ios":  # starts with '[': must not be mistaken for JSON
        rows = [f"[12.03.2026, 10:{10 + i:02d}:05] {senders[i % 2]}: {ln}" for i, ln in enumerate(d.lines)]
        return "czat.txt", "\n".join(rows) + "\n"
    if kind == "chat_wa_android":
        rows = [f"12.03.2026, 10:{10 + i:02d} - {senders[i % 2]}: {ln}" for i, ln in enumerate(d.lines)]
        return "WhatsApp Chat z Doradca.txt", "\n".join(rows) + "\n"
    if kind == "chat_tg_json":
        msgs = [{"id": i + 1, "type": "message", "date": f"2026-03-12T10:{10 + i:02d}:05",
                 "from": senders[i % 2], "from_id": f"user{1000 + i % 2}", "text": ln} for i, ln in enumerate(d.lines)]
        return "result.json", json.dumps({"name": "Inwestycje VIP", "type": "private_group", "id": 4242,
                                          "messages": msgs}, ensure_ascii=False, indent=1)
    if kind == "chat_json_list":  # a JSON array export: also starts with '['
        msgs = [{"sender": senders[i % 2], "timestamp": f"2026-03-12T10:{10 + i:02d}:05", "text": ln}
                for i, ln in enumerate(d.lines)]
        return "messages.json", json.dumps(msgs, ensure_ascii=False)
    raise ValueError(kind)


def html_page(d: Doc) -> str:
    from html import escape

    body = "\n".join(f"<p>{escape(ln)}</p>" for ln in d.lines)
    scripts = "\n".join(f"<script>{s}</script>" for s in d.scripts)
    return (f"<!doctype html><html lang=\"pl\"><head><meta charset=\"utf-8\"><title>Oferta specjalna</title>"
            f"{scripts}</head><body><main><h1>Oferta specjalna</h1>{body}</main></body></html>")


@dataclass
class Case:
    case_id: str
    carrier: str
    folder: Path
    truth: dict[str, set[tuple[str, str]]]  # extractor -> facts
    meta: dict[str, Any] = field(default_factory=dict)


def text_case(root: Path, d: Doc, carrier: str) -> Case:
    folder = root / f"{d.doc_id}-{carrier}"
    folder.mkdir(parents=True, exist_ok=True)
    truth: dict[str, set[tuple[str, str]]] = {"txt_entities": {f for f in d.facts if f[0] != "brand"},
                                             "txt_lines": {f for f in d.facts if f[0] == "brand"}}
    if carrier == "txt":
        (folder / "notatka.txt").write_text("\n".join(d.lines) + "\n", "utf-8")
    elif carrier == "html":
        (folder / "strona.html").write_text(html_page(d), "utf-8")
        truth["inf_dom"] = set(d.page_facts)
    else:
        name, content = chat_text(d, carrier)
        (folder / name).write_text(content, "utf-8")
        truth["txt_entities"].add(("phone", d.sender_phone))
        platform = {"chat_wa_ios": "whatsapp", "chat_wa_android": "whatsapp", "chat_tg_json": "telegram",
                    "chat_json_list": "generic_json"}[carrier]
        truth["txt_chat"] = {("chat_platform", platform), ("chat_persona", PERSONA.casefold()),
                             ("chat_start_date", "2026-03-12")}
    return Case(f"{d.doc_id}-{carrier}", carrier, folder, truth)


def ocr_case(root: Path, d: Doc, size: int, fmt: str) -> Case:
    carrier = f"ocr_{size}px_{fmt}"
    folder = root / f"{d.doc_id}-{carrier}"
    folder.mkdir(parents=True, exist_ok=True)
    data, ext, font = render_png(d.lines, size, fmt)
    (folder / f"reklama{ext}").write_bytes(data)
    truth = {"txt_entities": {f for f in d.facts if f[0] != "brand"},
             "txt_lines": {f for f in d.facts if f[0] == "brand"}}
    return Case(f"{d.doc_id}-{carrier}", carrier, folder, truth, {"font": font})


def _har_entry(url: str, status: int, *, location: str | None = None, mime: str = "text/html", body: str = "",
               t: int = 0, rtype: str = "document") -> dict:
    hdrs = [{"name": "Content-Type", "value": mime}]
    if location:
        hdrs.append({"name": "Location", "value": location})
    return {"startedDateTime": f"2026-03-12T10:00:{t:02d}.000Z", "_resourceType": rtype,
            "request": {"method": "GET", "url": url, "headers": [], "queryString": []},
            "response": {"status": status, "headers": hdrs, "redirectURL": location or "",
                         "content": {"mimeType": mime, "size": len(body), "text": body}}, "timings": {}}


def har_case(root: Path, g: Gen, i: int) -> Case:
    """Ad click -> TDS -> landing, as a Chrome-style HAR. Variant: the second hop is an HTML meta refresh."""
    click, mid, land = g.host(sub=True), g.host(), g.host()
    cid, gclid = g.digits(11), g.upper_alnum(24)
    pix, aw, g4 = g.digits(16), "AW-" + g.digits(10), "G-" + g.upper_alnum(10)
    meta_refresh = i % 2 == 1
    u1 = f"https://{click}/c/{g.digits(6)}?gclid={gclid}&gad_campaignid={cid}"
    u2 = f"https://{mid}/r/{g.digits(5)}"
    u3 = f"https://{land}/oferta/"
    page = (f"<html><head><script>fbq('init', '{pix}');</script><script>gtag('config', '{aw}');"
            f"gtag('config', '{g4}');</script></head><body><h1>Oferta</h1></body></html>")
    entries = [_har_entry(u1, 302, location=u2, t=1)]
    if meta_refresh:
        entries.append(_har_entry(u2, 200, t=2, body=f'<html><head><meta http-equiv="refresh" content="0;url={u3}">'
                                                     "</head><body></body></html>"))
    else:
        entries.append(_har_entry(u2, 301, location=u3, t=2))
    entries += [
        _har_entry(u3, 200, t=3, body=page),
        _har_entry("https://connect.facebook.net/en_US/fbevents.js", 200, mime="application/javascript",
                   body="/* fbevents */", t=4, rtype="script"),
        _har_entry(f"https://www.facebook.com/tr/?id={pix}&ev=PageView", 200, mime="image/gif", t=5, rtype="image"),
        _har_entry(f"https://www.googletagmanager.com/gtag/js?id={g4}", 200, mime="application/javascript",
                   body="/* gtag */", t=6, rtype="script"),
    ]
    har = {"log": {"version": "1.2", "creator": {"name": "synthetic", "version": "1"}, "entries": entries}}
    variant = "meta_refresh" if meta_refresh else "3xx"
    folder = root / f"h{i:03d}-har_{variant}"
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "page.har").write_text(json.dumps(har), "utf-8")
    truth = {"inf_har": {("redirect_chain", f"{click}>{mid}>{land}"), ("tracker", f"fb_pixel:{pix}"),
                         ("tracker", f"ga4:{g4}"), ("google_ads", f"google_ads:{aw}")},
             # the HAR entry document is the ad-click URL: inf_url reports its host as landing_host
             "inf_url": {("ad_campaign_id", f"google_ads:{cid}"), ("landing_host", click)}}
    return Case(folder.name, f"har_{variant}", folder, truth)


def url_case(root: Path, g: Gen, i: int) -> Case:
    """'Only a URL' input (role URL). Variants: Google Ads campaign id; a generic utm_campaign without an
    ad-platform marker (decoy: no ad id); a bare landing URL."""
    h = g.host(sub=i % 3 == 0)
    cid = g.digits(11)
    v = i % 3
    if v == 0:
        u = f"https://{h}/oferta?utm_source=google&gad_campaignid={cid}&gclid={g.upper_alnum(20)}"
        truth = {("landing_host", h), ("ad_campaign_id", f"google_ads:{cid}")}
    elif v == 1:
        u = f"https://{h}/promo?utm_source=newsletter&utm_campaign={cid}"
        truth = {("landing_host", h)}
    else:
        u = f"https://{h}/"
        truth = {("landing_host", h)}
    folder = root / f"u{i:03d}-url"
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "link.txt").write_text(u + "\n", "utf-8")
    return Case(folder.name, "url", folder, {"inf_url": truth})


def make_corpus(root: Path, seed: int, n_docs: int, n_ocr_docs: int, n_har: int, n_url: int,
                ocr_sizes=OCR_SIZES, ocr_formats=OCR_FORMATS, carriers=TEXT_CARRIERS) -> tuple[list[Case], list[Doc]]:
    g = Gen(seed)
    docs = [g.doc(i) for i in range(n_docs)]
    cases: list[Case] = []
    for d in docs:
        for c in carriers:
            cases.append(text_case(root, d, c))
    for d in docs[:n_ocr_docs]:
        for s in ocr_sizes:
            for f in ocr_formats:
                cases.append(ocr_case(root, d, s, f))
    cases += [har_case(root, g, i) for i in range(n_har)]
    cases += [url_case(root, g, i) for i in range(n_url)]
    return cases, docs


# ------------------------------------------------------------------------------------------------ product run

SCORED = ("txt_entities", "txt_lines", "inf_dom", "inf_har", "inf_url", "txt_chat")
TXT_KIND = {"domain_in_text": "domain", "url_in_text": "url", "entity_phone": "phone", "entity_iban": "iban",
            "crypto_wallet": "wallet", "entity_email": "email", "entity_price": "price", "person_named": "person",
            }
DOM_KIND = {"entity_phone": "phone", "entity_iban": "iban", "crypto_wallet": "wallet", "entity_email": "email"}


def resolve(folder: Path, settings) -> Any:
    from hydra.adapters.nask_folder import plan_incident
    from hydra.contracts import Incident, IncidentArtifact
    from hydra.genome.base import ResolvedArtifact, ResolvedIncident

    planned, errors, _ = plan_incident(folder, settings.max_file_mb * 1024 * 1024, settings=settings)
    arts = []
    for pa in planned:
        data = pa.data if pa.data is not None else pa.path.read_bytes()
        p = pa.path
        if p is None:
            p = folder / ".derived" / pa.rel.replace("/", "_")
            p.parent.mkdir(exist_ok=True)
            p.write_bytes(data)
        meta = IncidentArtifact(artifact_id=hashlib.sha256(data).hexdigest(), role=pa.role,
                                path_in_incident=pa.rel, mime=pa.mime, size=pa.size, meta=pa.meta)
        arts.append(ResolvedArtifact(meta, p))
    inc = Incident(id=f"f01-{folder.name}", source="synthetic", source_ref=folder.name,
                   artifacts=[a.meta for a in arts])
    return ResolvedIncident(inc, arts), errors


def observed_facts(genes) -> dict[str, dict[tuple[str, str], float]]:
    """extractor -> {(kind, value): max quality} of observed genes of the scored kinds."""
    out: dict[str, dict[tuple[str, str], float]] = defaultdict(dict)
    for g in genes:
        if g.status != "observed" or g.value is None:
            continue
        ex = g.extractor.split("@")[0]
        k = None
        if ex == "txt_entities":
            k = TXT_KIND.get(g.kind)
            if g.kind == "messenger_handle" and g.value.startswith("telegram:@"):
                k = "telegram"
        elif ex == "txt_lines":
            k = "brand" if g.kind == "brand_mention" else None
        elif ex == "inf_dom":
            k = DOM_KIND.get(g.kind)
            if g.kind == "tracker_id":
                k = "google_ads" if g.value.startswith("google_ads:") else "tracker"
            elif g.kind == "messenger_handle" and g.value.startswith("telegram:@"):
                k = "telegram"
        elif ex == "inf_har":
            if g.kind == "tracker_id":
                k = "google_ads" if g.value.startswith("google_ads:") else "tracker"
            elif g.kind == "redirect_chain":
                k = "redirect_chain"
        elif ex == "inf_url":
            k = g.kind if g.kind in ("landing_host", "ad_campaign_id") else None
        elif ex == "txt_chat":
            k = g.kind if g.kind in ("chat_platform", "chat_persona", "chat_start_date") else None
        if k is None:
            continue
        key = (k, g.value)
        out[ex][key] = max(out[ex].get(key, 0.0), float(g.quality))
    return out


def run_case(case: Case, settings, ocr_pool=None) -> dict[str, Any]:
    import time

    from hydra.genome import base
    from hydra.genome.base import ExtractContext, run_all

    base.load_family_modules()
    inc, errors = resolve(case.folder, settings)
    models = {"ocr_pool": ocr_pool} if ocr_pool is not None else {}
    ctx = ExtractContext(settings=settings, work_dir=settings.cache_dir / "work" / case.case_id, models=models)
    t0 = time.perf_counter()
    res = run_all(inc, ctx, only=[n for n in SCORED if n in base.REGISTRY])
    secs = time.perf_counter() - t0
    failed = [f"{g.extractor}:{g.kind}:{g.evidence.note}" for g in res.genes if g.status == "failed"]
    obs = observed_facts(res.genes)
    roles = sorted({a.meta.role for a in inc.artifacts})
    return {"case": case, "observed": obs, "failed": failed, "seconds": secs, "roles": roles,
            "import_errors": errors}


@dataclass
class CaseScore:
    case_id: str
    carrier: str
    extractor: str
    kind: str
    counts: Counts
    missed: list[str]
    spurious: list[str]


def score_case(result: dict[str, Any]) -> list[CaseScore]:
    case: Case = result["case"]
    out = []
    for ex, truth in case.truth.items():
        obs = result["observed"].get(ex, {})
        kinds = {k for k, _ in truth} | {k for k, _ in obs if k in {k2 for k2, _ in truth} or ex != "txt_chat"}
        for kind in sorted(kinds):
            t = {v for k, v in truth if k == kind}
            o = {v: q for (k, v), q in obs.items() if k == kind}
            c = Counts(tp=len(t & o.keys()), fp=len(o.keys() - t), fn=len(t - o.keys()),
                       fp_hq=sum(1 for v, q in o.items() if v not in t and q >= 0.5))
            out.append(CaseScore(case.case_id, case.carrier, ex, kind, c, sorted(t - o.keys()),
                                 sorted(f"{v} (q={o[v]:.2f})" for v in o.keys() - t)))
    return out


def aggregate(scores: list[CaseScore], by: tuple[str, ...]) -> dict[tuple, Counts]:
    agg: dict[tuple, Counts] = defaultdict(Counts)
    for s in scores:
        agg[tuple(getattr(s, b) for b in by)].add(s.counts)
    return dict(agg)


def carrier_group(carrier: str) -> str:
    if carrier.startswith("ocr_"):
        return "ocr"
    if carrier.startswith("chat_"):
        return "chat"
    if carrier.startswith("har_"):
        return "har"
    return carrier
