"""Payment and contact operator identifiers (ADOPT_PLAN item 3): bank accounts, crypto wallets, messenger handles,
BLIK / phone-transfer hints and phone tiering.

Pure functions over text; the TXT extractor (`txt_entities`) and the INF page extractor (`inf_dom`) turn the
results into genes. Every finder returns `txt_entities.Entity` objects with character offsets for the evidence
pointer and an optional `tier` that overrides the kind's default tier.

Tiers (dependency_group "<family>:<tier>:<name>"):
- full account (IBAN / Polish NRB), crypto wallet, messenger handle: operator (one account = one operator);
- bank code: platform (a bank or fintech serves many unrelated customers: never a campaign link on its own);
- payment method hints (BLIK, phone transfer, crypto): kit (style of the scheme);
- phones: per `phone_profile` (docs/cleanroom/phone_disposable.md): hotlines and public receive-SMS numbers
  boilerplate, toll-free / premium / shared-cost / short codes platform, everything else operator.

Values: accounts are stored only as a salted hash (`webparse.pii_hash`) with a masked note, so a TXT match (OCR,
page text) and an INF match (saved page) of one account give the same value. Wallets and messenger handles are
public pseudonymous identifiers and stay readable (the analyst pivots on them).

Checksums are validated offline: IBAN mod-97 and the bank registry via `schwifty` (MIT), Polish sort-code check
digit, Base58Check (BTC legacy, TRX), Bech32 / Bech32m (BIP-173 / BIP-350), EIP-55 (ETH), Monero Base58 with a
Keccak-256 checksum. A value that fails validation is kept with quality <= 0.4 and the reason in the note.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

from hydra.genome import adv_f01
from hydra.genome.txt_entities import Entity

# Polish waterfall labels for the new kinds (KIND_PL lives in hydra/synapse.py: CR-OPGENES-1 asks to merge these).
KIND_PL_OPERATOR: dict[str, str] = {
    "entity_iban": "Ten sam numer konta",
    "bank_code": "Ten sam bank odbiorcy (kod banku, wspólny dla wielu klientów)",
    "crypto_wallet": "Ten sam portfel kryptowalut",
    "messenger_handle": "Ten sam kontakt w komunikatorze",
    "payment_method": "Ta sama metoda płatności",
    "text_template_key": "Ten sam szablon tekstu, inne identyfikatory",
}

# =================================================================================================== Keccak-256

_RC = [
    0x0000000000000001, 0x0000000000008082, 0x800000000000808A, 0x8000000080008000, 0x000000000000808B,
    0x0000000080000001, 0x8000000080008081, 0x8000000000008009, 0x000000000000008A, 0x0000000000000088,
    0x0000000080008009, 0x000000008000000A, 0x000000008000808B, 0x800000000000008B, 0x8000000000008089,
    0x8000000000008003, 0x8000000000008002, 0x8000000000000080, 0x000000000000800A, 0x800000008000000A,
    0x8000000080008081, 0x8000000000008080, 0x0000000080000001, 0x8000000080008008,
]
_ROT = [[0, 36, 3, 41, 18], [1, 44, 10, 45, 2], [62, 6, 43, 15, 61], [28, 55, 25, 21, 56], [27, 20, 39, 8, 14]]
_M64 = (1 << 64) - 1


def _rol(v: int, n: int) -> int:
    return ((v << n) | (v >> (64 - n))) & _M64 if n else v


def _keccak_f(a: list[list[int]]) -> None:
    for rc in _RC:
        c = [a[x][0] ^ a[x][1] ^ a[x][2] ^ a[x][3] ^ a[x][4] for x in range(5)]
        d = [c[(x - 1) % 5] ^ _rol(c[(x + 1) % 5], 1) for x in range(5)]
        for x in range(5):
            for y in range(5):
                a[x][y] ^= d[x]
        b = [[0] * 5 for _ in range(5)]
        for x in range(5):
            for y in range(5):
                b[y][(2 * x + 3 * y) % 5] = _rol(a[x][y], _ROT[x][y])
        for x in range(5):
            for y in range(5):
                a[x][y] = b[x][y] ^ ((~b[(x + 1) % 5][y]) & b[(x + 2) % 5][y])
        a[0][0] ^= rc


def keccak256(data: bytes) -> bytes:
    """Original Keccak-256 (padding 0x01, as used by Ethereum and Monero; not FIPS-202 SHA3-256)."""
    rate = 136
    msg = bytearray(data) + b"\x01"
    msg += b"\x00" * (-len(msg) % rate)
    msg[-1] |= 0x80
    a = [[0] * 5 for _ in range(5)]
    for off in range(0, len(msg), rate):
        block = msg[off: off + rate]
        for i in range(rate // 8):
            a[i % 5][i // 5] ^= int.from_bytes(block[8 * i: 8 * i + 8], "little")
        _keccak_f(a)
    out = b"".join(a[i % 5][i // 5].to_bytes(8, "little") for i in range(4))
    return out


# =================================================================================================== Base58 / Bech32

B58 = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"
_B58_IDX = {c: i for i, c in enumerate(B58)}


def b58decode(s: str) -> bytes | None:
    n = 0
    for c in s:
        i = _B58_IDX.get(c)
        if i is None:
            return None
        n = n * 58 + i
    body = n.to_bytes((n.bit_length() + 7) // 8, "big") if n else b""
    pad = len(s) - len(s.lstrip("1"))
    return b"\x00" * pad + body


def b58encode(b: bytes) -> str:
    n = int.from_bytes(b, "big")
    out = ""
    while n:
        n, r = divmod(n, 58)
        out = B58[r] + out
    pad = len(b) - len(b.lstrip(b"\x00"))
    return "1" * pad + out


def _sha256d(b: bytes) -> bytes:
    return hashlib.sha256(hashlib.sha256(b).digest()).digest()


def b58check_payload(s: str) -> bytes | None:
    """Payload (version byte + data) when the 4-byte double-SHA-256 checksum holds, else None."""
    raw = b58decode(s)
    if raw is None or len(raw) < 5:
        return None
    body, chk = raw[:-4], raw[-4:]
    return body if _sha256d(body)[:4] == chk else None


_BECH32 = "qpzry9x8gf2tvdw0s3jn54khce6mua7l"
_BECH32_CONST, _BECH32M_CONST = 1, 0x2BC830A3


def _bech32_polymod(values: list[int]) -> int:
    gen = (0x3B6A57B2, 0x26508E6D, 0x1EA119FA, 0x3D4233DD, 0x2A1462B3)
    chk = 1
    for v in values:
        top = chk >> 25
        chk = (chk & 0x1FFFFFF) << 5 ^ v
        for i in range(5):
            chk ^= gen[i] if (top >> i) & 1 else 0
    return chk


def bech32_check(addr: str) -> str | None:
    """'bech32' / 'bech32m' when a segwit address has a valid checksum matching its witness version, else None."""
    if addr.lower() != addr and addr.upper() != addr:
        return None
    a = addr.lower()
    pos = a.rfind("1")
    if pos < 1 or pos + 7 > len(a) or len(a) > 90:
        return None
    hrp, data = a[:pos], a[pos + 1:]
    try:
        vals = [_BECH32.index(c) for c in data]
    except ValueError:
        return None
    pm = _bech32_polymod([ord(c) >> 5 for c in hrp] + [0] + [ord(c) & 31 for c in hrp] + vals)
    if not vals:
        return None
    witver = vals[0]
    if pm == _BECH32_CONST and witver == 0:
        return "bech32"
    if pm == _BECH32M_CONST and 1 <= witver <= 16:
        return "bech32m"
    return None


# Monero Base58: 8-byte blocks -> 11 characters; a final short block of n bytes uses _XMR_SIZES[n] characters.
_XMR_SIZES = [0, 2, 3, 5, 6, 7, 9, 10, 11]


def xmr_b58decode(s: str) -> bytes | None:
    out = bytearray()
    for off in range(0, len(s), 11):
        chunk = s[off: off + 11]
        if len(chunk) not in _XMR_SIZES:
            return None
        nbytes = _XMR_SIZES.index(len(chunk))
        n = 0
        for c in chunk:
            i = _B58_IDX.get(c)
            if i is None:
                return None
            n = n * 58 + i
        if n >> (8 * nbytes):
            return None
        out += n.to_bytes(nbytes, "big")
    return bytes(out)


def xmr_b58encode(b: bytes) -> str:
    out = ""
    for off in range(0, len(b), 8):
        chunk = b[off: off + 8]
        n = int.from_bytes(chunk, "big")
        s = ""
        while n:
            n, r = divmod(n, 58)
            s = B58[r] + s
        out += s.rjust(_XMR_SIZES[len(chunk)], "1")
    return out


def eip55(addr40: str) -> str:
    """EIP-55 mixed-case checksum form of a 40-hex-digit address (without 0x)."""
    low = addr40.lower()
    h = keccak256(low.encode()).hex()
    return "".join(c.upper() if c.isalpha() and int(h[i], 16) >= 8 else c for i, c in enumerate(low))


# =================================================================================================== wallets


@dataclass(frozen=True)
class Wallet:
    chain: str          # btc | eth | trx | xmr
    address: str        # canonical form (ETH lower-case)
    valid: bool
    reason: str


def check_wallet(chain: str, addr: str) -> Wallet:
    if chain == "btc":
        if addr.lower().startswith("bc1"):
            enc = bech32_check(addr)
            return Wallet("btc", addr.lower(), enc is not None, enc or "bech32 checksum fails")
        p = b58check_payload(addr)
        ok = p is not None and len(p) == 21 and p[0] in (0x00, 0x05)
        return Wallet("btc", addr, ok, "base58check" if ok else "base58check checksum fails")
    if chain == "trx":
        p = b58check_payload(addr)
        ok = p is not None and len(p) == 21 and p[0] == 0x41
        return Wallet("trx", addr, ok, "base58check" if ok else "base58check checksum fails")
    if chain == "eth":
        h = addr[2:]
        if h.lower() == h or h.upper() == h:
            return Wallet("eth", "0x" + h.lower(), True, "no EIP-55 checksum (single case)")
        ok = eip55(h) == h
        return Wallet("eth", "0x" + h.lower(), ok, "EIP-55" if ok else "EIP-55 checksum fails")
    if chain == "xmr":
        raw = xmr_b58decode(addr)
        ok = raw is not None and len(raw) in (69, 77) and keccak256(raw[:-4])[:4] == raw[-4:]
        return Wallet("xmr", addr, ok, "keccak checksum" if ok else "Monero checksum fails")
    return Wallet(chain, addr, False, "unknown chain")


_WALLET_RX: list[tuple[str, re.Pattern[str]]] = [
    ("btc", re.compile(r"(?<![0-9A-Za-z])(bc1[02-9ac-hj-np-z]{11,71}|BC1[02-9AC-HJ-NP-Z]{11,71})(?![0-9A-Za-z])")),
    ("btc", re.compile(r"(?<![0-9A-Za-z])([13][1-9A-HJ-NP-Za-km-z]{25,34})(?![0-9A-Za-z])")),
    ("eth", re.compile(r"(?<![0-9A-Za-z])(0x[0-9a-fA-F]{40})(?![0-9A-Za-z])")),
    ("trx", re.compile(r"(?<![0-9A-Za-z])(T[1-9A-HJ-NP-Za-km-z]{33})(?![0-9A-Za-z])")),
    ("xmr", re.compile(r"(?<![0-9A-Za-z])([48][1-9A-HJ-NP-Za-km-z]{94}(?:[1-9A-HJ-NP-Za-km-z]{11})?)(?![0-9A-Za-z])")),
]
_USDT_CTX = re.compile(r"usdt|tether|trc-?20", re.I)
_CRYPTO_CTX = re.compile(r"bitcoin|btc|ethereum|eth\b|erc-?20|usdt|tether|trc-?20|tron|monero|xmr|krypto|crypto|"
                         r"portfel|wallet|adres\w* portfel", re.I)


def find_wallets(text: str, *, need_context: bool = False) -> list[Entity]:
    """Crypto wallet addresses. `need_context`: only near a crypto word (used for code bodies, where 0x + 40 hex
    is also a hash literal)."""
    out: list[Entity] = []
    taken: list[tuple[int, int]] = []
    for chain, rx in _WALLET_RX:
        for m in rx.finditer(text):
            s, e = m.span(1)
            if any(a <= s < b for a, b in taken):
                continue
            ctx = text[max(0, s - 80): e + 40]
            has_ctx = bool(_CRYPTO_CTX.search(ctx))
            if need_context and not has_ctx:
                continue
            w = check_wallet(chain, m.group(1))
            if chain == "btc" and not w.valid and not has_ctx:
                continue  # a random base58-looking token without checksum or context is not a wallet
            if chain in ("trx", "xmr") and not w.valid and not has_ctx:
                continue
            label = chain
            if chain == "trx" and _USDT_CTX.search(ctx):
                label = "trx (USDT-TRC20)"
            q = 1.0 if w.valid else 0.3
            if chain == "eth" and w.valid and "no EIP-55" in w.reason:
                q = 0.8 if has_ctx else 0.6
            note = f"{label}: {w.reason}"
            out.append(Entity("crypto_wallet", f"{w.chain}:{w.address}", s, e, q, note, tier="operator"))
            taken.append((s, e))
    return out


# =================================================================================================== bank accounts

_IBAN_RX = re.compile(r"(?<![0-9A-Za-z])([A-Z]{2}\d{2}(?:[ ]?[0-9A-Z]{4}){2,7}(?:[ ]?[0-9A-Z]{1,4})?)(?![0-9A-Za-z])")
_NRB_RX = re.compile(r"(?<![\d])(\d{2}(?:[ -]?\d{4}){6})(?![\d])")
_PL_SORT_W = (3, 9, 7, 1, 3, 9, 7)


def iban_mod97(iban: str) -> bool:
    s = iban[4:] + iban[:4]
    try:
        return int("".join(str(int(c, 36)) for c in s)) % 97 == 1
    except ValueError:
        return False


def pl_sort_code_ok(sort8: str) -> bool:
    """Check digit of a Polish bank sort code (numer rozliczeniowy, 8 digits)."""
    if len(sort8) != 8 or not sort8.isdigit():
        return False
    total = sum(int(d) * w for d, w in zip(sort8[:7], _PL_SORT_W, strict=True))
    return (10 - total % 10) % 10 == int(sort8[7])


@dataclass(frozen=True)
class Account:
    iban: str
    country: str
    bank_id: str | None     # institution part used for the platform-tier bank gene ("PL:109")
    bank_name: str | None
    valid: bool
    reason: str

    @property
    def masked(self) -> str:
        return f"{self.iban[:4]} {self.iban[4:8]} **** {self.iban[-4:]}"


def parse_account(iban: str) -> Account:
    iban = iban.replace(" ", "").replace("-", "").upper()
    cc = iban[:2]
    if not iban_mod97(iban):
        return Account(iban, cc, None, None, False, "suma kontrolna IBAN niepoprawna")
    try:
        from schwifty import IBAN
        from schwifty.exceptions import SchwiftyException

        try:
            obj = IBAN(iban)
        except SchwiftyException as exc:
            return Account(iban, cc, None, None, False, f"schwifty: {type(exc).__name__}")
        code = obj.bank_code or None
        name = obj.bank_name if code else None
    except ImportError:  # schwifty missing: mod-97 only, no registry
        code, name = None, None
    reason = "IBAN poprawny"
    bank_id = None
    if cc == "PL":
        sort8 = iban[4:12]
        if not pl_sort_code_ok(sort8):
            return Account(iban, cc, None, name, False, "cyfra kontrolna numeru rozliczeniowego niepoprawna")
        bank_id = f"PL:{sort8[:3]}"  # 3-digit bank number; branch digits are not a bank-level identity
    elif code:
        bank_id = f"{cc}:{code}"
    return Account(iban, cc, bank_id, name, True, reason)


def find_accounts(text: str) -> list[Entity]:
    """IBANs (any country) and Polish NRB (26 digits without the PL prefix). Returns `entity_iban` with the
    normalised IBAN as value (the caller hashes it) plus one platform-tier `bank_code` per valid account."""
    out: list[Entity] = []
    spans: list[tuple[int, int]] = []
    cands: list[tuple[str, int, int, bool]] = []
    adv = adv_f01.enabled()
    for m in (adv_f01.IBAN_RX if adv else _IBAN_RX).finditer(text):
        v = m.group(1).replace(" ", "")
        if 15 <= len(v) <= 34 and sum(c.isdigit() for c in v[4:]) >= 8:
            if adv:  # ADV-F01 (CR-F01-4): Unicode boundary, country length + mod-97 boundary, OCR repair
                v, used = adv_f01.refine_iban(m.group(1))
                cands.append((v, m.start(1), m.start(1) + used, False))
                continue
            cands.append((v, *m.span(1), False))
    for m in _NRB_RX.finditer(text):
        s = m.start(1)
        if any(a <= s < b for _, a, b, _ in cands):
            continue
        cands.append(("PL" + re.sub(r"[ -]", "", m.group(1)), *m.span(1), True))
    for v, s, e, nrb in cands:
        if any(a <= s < b for a, b in spans):
            continue
        acc = parse_account(v)
        note = f"{acc.masked} ({acc.reason}{', NRB bez prefiksu PL' if nrb else ''})"
        out.append(Entity("entity_iban", acc.iban, s, e, 1.0 if acc.valid else 0.4, note, tier="operator"))
        spans.append((s, e))
        if acc.valid and acc.bank_id:
            out.append(Entity("bank_code", acc.bank_id, s, e, 0.5, f"bank: {acc.bank_name or 'nieznany'}",
                              tier="platform"))
    return out


# =================================================================================================== messengers

_TG_RESERVED = {"share", "s", "iv", "addstickers", "addemoji", "addtheme", "proxy", "socks", "login", "setlanguage",
                "confirmphone", "bg", "boost", "contact", "addlist", "invoice", "m", "k", "a", "z"}
_TG_URL = re.compile(r"(?<![\w.-])(?:https?://)?(?:www\.)?(?:t|telegram)\.(?:me|dog)/(\+[\w-]{8,40}|joinchat/[\w-]{8,40}|"
                     r"c/\d{6,14}|[A-Za-z][A-Za-z0-9_]{3,31})(?![\w])", re.I)
_TG_SCHEME = re.compile(r"tg://resolve\?domain=([A-Za-z][A-Za-z0-9_]{3,31})", re.I)
_WA_URL = re.compile(r"(?<![\w.-])(?:https?://)?(?:wa\.me/|(?:api|web)\.whatsapp\.com/send/?\?(?:[^\s\"'<>]*?&)?phone=|"
                     r"whatsapp://send\?(?:[^\s\"'<>]*?&)?phone=)(?:%2B|\+)?(\d{7,15})", re.I)
_WA_GROUP = re.compile(r"(?:https?://)?chat\.whatsapp\.com/(?:invite/)?([A-Za-z0-9]{18,30})", re.I)
_SIGNAL = re.compile(r"(?:https?://)?signal\.me/#(?:p/(\+?\d{7,15})|eu/([A-Za-z0-9_-]{8,120}))", re.I)
_SIGNAL_GROUP = re.compile(r"(?:https?://)?signal\.group/#([A-Za-z0-9_-]{20,200})", re.I)
_VIBER = re.compile(r"viber://(?:chat|add|contact)\?number=(?:%2B|\+)?(\d{7,15})", re.I)
_AT_HANDLE = re.compile(r"(?<![\w@.])@([A-Za-z][A-Za-z0-9_]{4,31})(?![\w@.])")
_AT_STOP = {"media", "import", "keyframes", "supports", "charset", "namespace", "layer", "container", "property",
            "font", "page", "document", "viewport", "counter", "param", "returns", "return", "type", "throws",
            "example", "license", "author", "version", "context", "graph", "admin", "support", "telegram"}
_TG_CTX = re.compile(r"telegram|telegramie|\btg\b|\btelega", re.I)


def _e164(digits: str) -> str | None:
    p = phone_profile("+" + digits.lstrip("0"))
    return p.e164 if p else None


def find_messenger_handles(text: str) -> list[Entity]:
    """Telegram (t.me links, tg:// links, @handle next to the word Telegram), WhatsApp (wa.me, api/web send links,
    group invites), Signal (signal.me, signal.group), Viber (viber:// links). Phone-based handles also yield the
    phone as `entity_phone` (same dependency group 'contact', one bit budget)."""
    out: list[Entity] = []
    for m in _TG_URL.finditer(text):
        v = m.group(1)
        low = v.lower()
        if low.split("/")[0] in _TG_RESERVED:
            continue
        if low.startswith(("+", "joinchat/")):
            val, grp = "telegram_invite:" + v.split("/")[-1].lstrip("+"), "messenger"
        elif low.startswith("c/"):
            val, grp = "telegram_channel:" + v[2:], "messenger"
        else:
            val, grp = "telegram:@" + low, "messenger"
        out.append(Entity("messenger_handle", val, m.start(), m.end(), 1.0, "link Telegram", tier="operator",
                          group=grp))
    for m in _TG_SCHEME.finditer(text):
        out.append(Entity("messenger_handle", "telegram:@" + m.group(1).lower(), m.start(), m.end(), 1.0,
                          "link tg://", tier="operator", group="messenger"))
    for m in (adv_f01.AT_HANDLE4 if adv_f01.enabled() else _AT_HANDLE).finditer(text):
        ctx = text[max(0, m.start() - 40): m.end() + 40]
        if m.group(1).lower() in _AT_STOP or not _TG_CTX.search(ctx) or any(e.start <= m.start() < e.end for e in out):
            continue
        out.append(Entity("messenger_handle", "telegram:@" + m.group(1).lower(), m.start(), m.end(), 0.8,
                          "@nazwa obok słowa Telegram", tier="operator", group="messenger"))
    for rx, app in ((_WA_URL, "whatsapp"), (_VIBER, "viber")):
        for m in rx.finditer(text):
            e164 = _e164(m.group(1))
            if e164 is None:
                continue
            out.append(Entity("messenger_handle", f"{app}:{e164}", m.start(), m.end(), 1.0, f"link {app}",
                              tier="operator", group="contact"))
            prof = phone_profile(e164)
            if prof is not None:
                out.append(Entity("entity_phone", e164, m.start(), m.end(), 0.9, f"numer z linku {app}; "
                                  + prof.describe(), tier=prof.tier, group="contact"))
    for m in _WA_GROUP.finditer(text):
        out.append(Entity("messenger_handle", "whatsapp_group:" + m.group(1), m.start(), m.end(), 1.0,
                          "zaproszenie do grupy WhatsApp", tier="operator", group="messenger"))
    for m in _SIGNAL.finditer(text):
        if m.group(1):
            e164 = _e164(m.group(1).lstrip("+"))
            if e164:
                out.append(Entity("messenger_handle", f"signal:{e164}", m.start(), m.end(), 1.0, "link Signal",
                                  tier="operator", group="contact"))
        else:
            out.append(Entity("messenger_handle", "signal_user:" + m.group(2), m.start(), m.end(), 1.0,
                              "link Signal (nazwa użytkownika)", tier="operator", group="messenger"))
    for m in _SIGNAL_GROUP.finditer(text):
        out.append(Entity("messenger_handle", "signal_group:" + m.group(1)[:64], m.start(), m.end(), 1.0,
                          "zaproszenie do grupy Signal", tier="operator", group="messenger"))
    return out


# =================================================================================================== payment hints

_PAY_HINTS: list[tuple[str, re.Pattern[str]]] = [
    ("blik", re.compile(r"\bblik\w*", re.I)),
    ("phone_transfer", re.compile(r"przelew\w*\s+na\s+(?:numer\s+)?telefon|na\s+numer\s+telefonu\s+(?:przez|w)\s+blik",
                                  re.I)),
    ("crypto", re.compile(r"\b(?:bitcoin\w*|btc|usdt|tether|kryptowalut\w*|cryptocurrenc\w*)\b", re.I)),
    ("card_payment", re.compile(r"\b(?:numer\s+karty|kod\s+cvv|cvv2?|cvc)\b", re.I)),
    ("paysafecard", re.compile(r"\bpaysafe\s?card\b", re.I)),
    ("cash_courier", re.compile(r"\bkurier\w*\s+(?:odbierze|po\s+odbi[oó]r)\s+got[oó]wk", re.I)),
]


def find_payment_hints(text: str) -> list[Entity]:
    """Payment-method hints (kit tier: describes the scheme, not one operator). One entity per method and text."""
    out = []
    for name, rx in _PAY_HINTS:
        m = rx.search(text)
        if m:
            out.append(Entity("payment_method", name, m.start(), m.end(), 0.5, "wzmianka o metodzie płatności",
                              tier="kit", group="payment_method"))
    return out


# =================================================================================================== phones


@dataclass(frozen=True)
class PhoneProfile:
    e164: str
    ptype: str           # mobile | fixed | voip | toll_free | premium | shared_cost | uan | personal | short | ...
    tier: str            # operator | platform | boilerplate
    reason: str          # Polish, for the evidence note
    weight: float        # specificity 0..1 (7 -> 0, 11+ -> 1 significant digits incl. country code)
    valid: bool
    list_version: str

    def describe(self) -> str:
        return (f"typ {self.ptype}, poziom {self.tier}: {self.reason} (waga {self.weight:.2f},"
                f" listy {self.list_version})")


_PTYPE = {0: "fixed", 1: "mobile", 2: "fixed_or_mobile", 3: "toll_free", 4: "premium", 5: "shared_cost", 6: "voip",
          7: "personal", 8: "pager", 9: "uan", 10: "voicemail", 99: "unknown"}
PLATFORM_PTYPES = {"toll_free", "premium", "shared_cost", "uan"}
MIN_DIGITS, FULL_DIGITS = 7, 11  # specificity ramp (spec: followthemoney idea); counted on E.164 digits


@lru_cache(maxsize=1)
def phone_lists() -> dict:
    """HYDRA-owned lists (hydra/genome/assets/phone_lists.json) merged with the deployment overlay
    `<lexicon dir>/phone_lists.json` (hydra.genome.lexicons.local)."""
    base = json.loads((Path(__file__).parent / "assets" / "phone_lists.json").read_text("utf-8"))
    from hydra.genome.lexicons import local

    extra = local.load("phone_lists") or {}
    for key in ("brand_hotlines", "receive_sms"):
        base[key] = {**base.get(key, {}), **(extra.get(key) or {})}
    if extra.get("version"):
        base["version"] = f"{base['version']}+{extra['version']}"
    return base


def _warninglist_phone(e164: str):
    try:
        from hydra.enrich import warninglists
    except ImportError:
        return None
    fn = getattr(warninglists, "classify_phone", None)
    try:
        return fn(e164) if fn else warninglists.classify("entity_phone", e164)
    except Exception:  # noqa: BLE001 — a broken list must never break extraction
        return None


def specificity(digits: int) -> float:
    return round(max(0.0, min(1.0, (digits - MIN_DIGITS) / (FULL_DIGITS - MIN_DIGITS))), 2)


@lru_cache(maxsize=20_000)
def phone_profile(raw: str, region: str = "PL") -> PhoneProfile | None:
    """Normalise to E.164 and tier a phone number (docs/cleanroom/phone_disposable.md, behaviour 1-6, 8).
    Returns None when the string is not a phone number at all."""
    import phonenumbers
    from phonenumbers import shortnumberinfo

    lists = phone_lists()
    ver = str(lists.get("version", "?"))
    s = raw.strip().translate(str.maketrans("Oo", "00"))
    try:
        num = phonenumbers.parse(s, region)
    except phonenumbers.NumberParseException:
        return None
    nsn = phonenumbers.national_significant_number(num)
    e164 = f"+{num.country_code}{nsn}"
    digits = len(str(num.country_code)) + len(nsn)
    weight = specificity(digits)
    if shortnumberinfo.is_emergency_number(nsn, phonenumbers.region_code_for_country_code(num.country_code)):
        return PhoneProfile(e164, "emergency", "boilerplate", "numer alarmowy", 0.0, True, ver)
    if len(nsn) < 7:
        short = shortnumberinfo.is_valid_short_number(num)
        return PhoneProfile(e164, "short", "platform" if short and len(nsn) >= 4 else "boilerplate",
                            "numer skrócony (usługa wspólna)" if short else f"za krótki ({len(nsn)} cyfr)",
                            weight, short, ver)
    if e164 in lists.get("brand_hotlines", {}):
        return PhoneProfile(e164, "hotline", "boilerplate",
                            f"oficjalna infolinia marki {lists['brand_hotlines'][e164]}", 0.0, True, ver)
    if e164 in lists.get("receive_sms", {}):
        return PhoneProfile(e164, "receive_sms", "boilerplate", "publiczny numer do odbioru SMS", 0.0, True, ver)
    hit = _warninglist_phone(e164)
    if hit is not None and getattr(hit, "effect", None) in ("boilerplate", "label"):
        return PhoneProfile(e164, "reserved", "boilerplate", f"lista ostrzeżeń MISP {hit.list_name}", 0.0, True, ver)
    valid = phonenumbers.is_valid_number(num)
    ptype = _PTYPE.get(phonenumbers.number_type(num), "unknown")
    if len(set(nsn[-7:])) <= 2:
        return PhoneProfile(e164, ptype, "boilerplate", "numer zastępczy (powtarzające się cyfry)", 0.0, valid, ver)
    if ptype in PLATFORM_PTYPES:
        return PhoneProfile(e164, ptype, "platform", "numer usługowy (bezpłatny, premium lub wspólnych kosztów)",
                            weight, valid, ver)
    if ptype == "voip":
        return PhoneProfile(e164, ptype, "operator", "numer VoIP (tani do zmiany; CR-OPGENES-2: -2 bity)", weight,
                            valid, ver)
    if not valid:
        return PhoneProfile(e164, ptype, "operator", "numer nie przechodzi walidacji planu numeracji", weight * 0.5,
                            False, ver)
    return PhoneProfile(e164, ptype, "operator", "numer kontaktowy operatora", weight, True, ver)


_INTL = re.compile(r"(?<![\w+])(?:\+|00)(?!48)([1-9]\d{0,2})[\s.()-]{0,2}(\d[\d\s.()-]{5,16}\d)(?![\w])")


def find_intl_phones(text: str) -> list[Entity]:
    """Non-Polish numbers written with an explicit +CC / 00CC prefix (Polish ones: txt_entities.find_phones)."""
    import phonenumbers

    out = []
    for m in _INTL.finditer(text):
        raw = "+" + m.group(1) + re.sub(r"\D", "", m.group(2))
        try:
            num = phonenumbers.parse(raw, None)
        except phonenumbers.NumberParseException:
            continue
        if not phonenumbers.is_valid_number(num):
            continue
        prof = phone_profile(raw)
        if prof is None:
            continue
        out.append(Entity("entity_phone", prof.e164, m.start(), m.end(), 0.8, prof.describe(), tier=prof.tier,
                          group="contact"))
    return out


def tier_phone(e: Entity) -> Entity:
    """Attach the phone tier and reason to an `entity_phone` from txt_entities.find_phones."""
    prof = phone_profile(e.value)
    if prof is None:
        return e
    note = (e.note + "; " if e.note else "") + prof.describe()
    return Entity(e.kind, prof.e164, e.start, e.end, e.quality, note, tier=prof.tier, group="contact")


def find_operator_ids(text: str) -> list[Entity]:
    """All payment/contact operator identifiers of this module in one text (phones excluded: find_phones +
    tier_phone and find_intl_phones are applied by the caller), plus the toll-fraud chain ids of
    hydra/genome/tollfraud.py (app-store packages, Ad Library ids, premium SMS, carrier billing, cloud load
    balancers, certificate fingerprints), whose values equal the INF ones."""
    from hydra.genome.tollfraud import find_text_ids

    return (find_accounts(text) + find_wallets(text) + find_messenger_handles(text) + find_payment_hints(text)
            + find_text_ids(text))
