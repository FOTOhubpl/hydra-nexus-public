"""Communication logs and documents given as text input: chat exports and PDF ads.

Chat formats (detected from the bytes, never from the file name alone):
- WhatsApp text export, Android (`21.11.2026, 14:05 - Jan: tekst`) and iOS (`[21.11.2026, 14:05:11] Jan: tekst`);
- Telegram Desktop JSON export (`result.json`: `messages[]` with `from`, `date`, `text` as a string or a list of
  entity parts);
- Messenger / Facebook JSON export (`participants[]`, `messages[]` with `sender_name`, `timestamp_ms`, `content`;
  Facebook's latin-1 mojibake of UTF-8 is repaired);
- generic logs: `2026-11-21 14:05 Jan: tekst`, `[2026-11-21T14:05] Jan: tekst`, or a JSON list of objects with
  sender/text/timestamp-like keys.

`parse_chat(path)` returns a `ChatLog` (platform, messages with ISO timestamps, senders) or None. The TXT text
collector (`hydra.genome.txt_sources.text_file_blocks`) uses it so phrases and entities come from message bodies,
not from timestamp/sender prefixes. The `txt_chat` extractor adds the log's own genes: persona (sender) names,
the platform, the first-message date and the URLs written in messages as INF URL genes (host, registrable
domain, path skeleton, look-alike checks) — exactly as for a URL given as input.

PDF (`pdf_text`): a standard-library reader for text-based PDFs: Flate-decoded content streams (bounded
decompression), `Tj`/`TJ`/`'`/`"` string operators, and `/URI` link annotations. Nothing is rendered or
executed (JavaScript and forms in PDFs are ignored); CID/Type0 font encodings without a ToUnicode map are not
decoded, which yields no text rather than wrong text.
"""

from __future__ import annotations

import json
import re
import zlib
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

from hydra.contracts import Family, Gene, GeneStatus
from hydra.genome.base import ExtractContext, ExtractResult, ResolvedIncident, register

MAX_CHAT_BYTES = 20_000_000
MAX_MESSAGES = 50_000

_DATE = r"(\d{1,2})[./-](\d{1,2})[./-](\d{2,4})"
_TIME = r"(\d{1,2}):(\d{2})(?::(\d{2}))?(?:\s?([AaPp]\.?[Mm]\.?))?"
_NAME = r"([^:\n]{1,60}?)"
WA_ANDROID = re.compile(rf"^\u200e?{_DATE},?\s{_TIME}\s[-–]\s{_NAME}:\s(.*)$")
WA_IOS = re.compile(rf"^\u200e?\[{_DATE},?\s{_TIME}\]\s{_NAME}:\s(.*)$")
GENERIC = re.compile(r"^\u200e?\[?(\d{4})-(\d{2})-(\d{2})[ T](\d{1,2}):(\d{2})(?::(\d{2}))?(?:\.\d+)?"
                     r"(?:Z|[+-]\d{2}:?\d{2})?\]?"
                     rf"\s*[-–|]?\s*{_NAME}:\s(.*)$")
# system lines of the exporters (no sender persona, no content)
SYSTEM_RE = re.compile(
    r"(end-to-end encrypted|szyfrowane end-to-end|<media omitted>|<pominięto multimedia>|<załączono:|"
    r"image omitted|video omitted|audio omitted|sticker omitted|obraz pominięty|wiadomość usunięta|"
    r"this message was deleted|messages and calls are|wiadomości i połączenia)", re.I)


@dataclass
class Message:
    ts: str | None
    sender: str | None
    text: str
    index: int


@dataclass
class ChatLog:
    platform: str
    messages: list[Message] = field(default_factory=list)
    title: str | None = None

    @property
    def senders(self) -> list[str]:
        return list(dict.fromkeys(m.sender for m in self.messages if m.sender))

    @property
    def span(self) -> tuple[str | None, str | None]:
        ts = sorted(m.ts for m in self.messages if m.ts)
        return (ts[0], ts[-1]) if ts else (None, None)


def _iso(y: int, mo: int, d: int, h: int, mi: int, s: int = 0) -> str | None:
    try:
        return datetime(y, mo, d, h, mi, s).isoformat(timespec="seconds")
    except ValueError:
        return None


def _wa_ts(m: re.Match, sep_hint: str) -> str | None:
    a, b, y, hh, mm, ss, ampm = m.group(1, 2, 3, 4, 5, 6, 7)
    a_i, b_i, y_i = int(a), int(b), int(y)
    if y_i < 100:
        y_i += 2000
    # '.' exports (pl, de, ru) are day-first; '/' exports are month-first unless the first number is > 12
    day, month = (a_i, b_i) if sep_hint == "." or a_i > 12 else (b_i, a_i)
    h = int(hh)
    if ampm:
        pm = ampm.lower().startswith("p")
        h = (h % 12) + (12 if pm else 0)
    return _iso(y_i, month, day, h, int(mm), int(ss or 0))


def _parse_lines(text: str) -> ChatLog | None:
    lines = text.splitlines()
    head = [ln for ln in lines[:40] if ln.strip()]
    best = None
    for name, rx in (("whatsapp_ios", WA_IOS), ("whatsapp", WA_ANDROID), ("generic_log", GENERIC)):
        hits = sum(1 for ln in head if rx.match(ln))
        if hits >= 2 and hits * 2 >= len(head) and (best is None or hits > best[1]):
            best = (name, hits, rx)
    if best is None:
        return None
    platform, _, rx = best
    log = ChatLog(platform="whatsapp" if platform.startswith("whatsapp") else platform)
    cur: Message | None = None
    for ln in lines:
        m = rx.match(ln)
        if m:
            if platform == "generic_log":
                y, mo, d, hh, mi, ss = m.group(1, 2, 3, 4, 5, 6)
                ts = _iso(int(y), int(mo), int(d), int(hh), int(mi), int(ss or 0))
                sender, body = m.group(7), m.group(8)
            else:
                sep = "." if "." in ln[:12] else "/"
                ts, sender, body = _wa_ts(m, sep), m.group(8), m.group(9)
            if SYSTEM_RE.search(body) and len(body) < 120:
                cur = None
                continue
            cur = Message(ts, sender.strip().strip("\u200e~ ") or None, body.strip(), len(log.messages))
            log.messages.append(cur)
            if len(log.messages) >= MAX_MESSAGES:
                break
        elif cur is not None and ln.strip():  # continuation of a multi-line message
            cur.text = f"{cur.text}\n{ln.strip()}"
    return log if log.messages else None


def _fix_mojibake(s: str) -> str:
    """Facebook exports write UTF-8 bytes as latin-1 code points ('Å\\x82' for 'ł')."""
    try:
        return s.encode("latin-1").decode("utf-8")
    except (UnicodeEncodeError, UnicodeDecodeError):
        return s


def _tg_text(t: object) -> str:
    if isinstance(t, str):
        return t
    if isinstance(t, list):
        return "".join(x if isinstance(x, str) else str(x.get("text", "")) if isinstance(x, dict) else "" for x in t)
    return ""


SENDER_KEYS = ("from", "sender", "sender_name", "author", "user", "name", "nadawca")
TEXT_KEYS = ("text", "content", "message", "body", "msg", "tresc", "treść")
TS_KEYS = ("date", "timestamp", "timestamp_ms", "time", "datetime", "created_at", "sent_at", "data")


def _ts_value(v: object) -> str | None:
    if isinstance(v, int | float) and v > 0:
        sec = v / 1000 if v > 1e11 else v
        try:
            return datetime.fromtimestamp(sec, UTC).replace(tzinfo=None).isoformat(timespec="seconds")
        except (OverflowError, OSError, ValueError):
            return None
    if isinstance(v, str) and re.match(r"^\d{4}-\d{2}-\d{2}", v):
        try:
            return datetime.fromisoformat(v.replace("Z", "+00:00")).replace(tzinfo=None).isoformat(timespec="seconds")
        except ValueError:
            return v[:19]
    return None


def _parse_json(obj: object) -> ChatLog | None:
    if isinstance(obj, dict) and isinstance(obj.get("messages"), list):
        msgs = obj["messages"]
        if "participants" in obj or any(isinstance(m, dict) and "sender_name" in m for m in msgs[:20]):
            log = ChatLog("messenger", title=_fix_mojibake(str(obj.get("title") or "")) or None)
            for m in msgs[:MAX_MESSAGES]:
                if not isinstance(m, dict):
                    continue
                text = _fix_mojibake(str(m.get("content") or ""))
                share = m.get("share") if isinstance(m.get("share"), dict) else {}
                if share.get("link"):
                    text = f"{text} {share['link']}".strip()
                if text:
                    log.messages.append(Message(_ts_value(m.get("timestamp_ms")),
                                                _fix_mojibake(str(m.get("sender_name") or "")) or None, text,
                                                len(log.messages)))
            log.messages.sort(key=lambda x: x.ts or "")
            return log if log.messages else None
        if any(isinstance(m, dict) and ("from" in m or m.get("type") in ("message", "service")) for m in msgs[:20]):
            log = ChatLog("telegram", title=str(obj.get("name") or "") or None)
            for m in msgs[:MAX_MESSAGES]:
                if not isinstance(m, dict) or m.get("type", "message") != "message":
                    continue
                text = _tg_text(m.get("text"))
                if text.strip():
                    log.messages.append(Message(_ts_value(m.get("date")), str(m.get("from") or "") or None,
                                                text.strip(), len(log.messages)))
            return log if log.messages else None
        obj = msgs
    if isinstance(obj, list) and obj and all(isinstance(m, dict) for m in obj[:20]):
        keys = {k.lower() for m in obj[:20] for k in m}
        sk = next((k for k in SENDER_KEYS if k in keys), None)
        tk = next((k for k in TEXT_KEYS if k in keys), None)
        if sk is None or tk is None:
            return None
        zk = next((k for k in TS_KEYS if k in keys), None)
        log = ChatLog("generic_json")
        for m in obj[:MAX_MESSAGES]:
            low = {k.lower(): v for k, v in m.items()}
            text = low.get(tk)
            if isinstance(text, str) and text.strip():
                log.messages.append(Message(_ts_value(low.get(zk)) if zk else None, str(low.get(sk) or "") or None,
                                            text.strip(), len(log.messages)))
        return log if len(log.messages) >= 2 else None
    return None


def parse_chat_text(raw: str, as_json: bool | None = None) -> ChatLog | None:
    s = raw.lstrip("\ufeff")
    if as_json or (as_json is None and s.lstrip()[:1] in ("{", "[")):
        try:
            return _parse_json(json.loads(s))
        except (json.JSONDecodeError, RecursionError, ValueError):
            pass  # '[21.11.2026, 14:05] ...' (an iOS export) sniffs as JSON: parse it as lines
    return _parse_lines(s)


def read_text_bytes(raw: bytes) -> str:
    """UTF-8 (with BOM / UTF-16 BOM), else cp1250 (Polish Windows exports): same text whatever the encoding."""
    if raw.startswith((b"\xff\xfe", b"\xfe\xff")):
        return raw.decode("utf-16", "replace")
    try:
        return raw.decode("utf-8-sig")
    except UnicodeDecodeError:
        pass
    try:
        return raw.decode("cp1250")
    except UnicodeDecodeError:
        return raw.decode("utf-8", "replace")


def parse_chat(path: Path, as_json: bool | None = None) -> ChatLog | None:
    with path.open("rb") as fh:
        raw = fh.read(MAX_CHAT_BYTES)
    if raw.startswith(b"%PDF"):
        return None
    return parse_chat_text(read_text_bytes(raw), as_json)


def looks_like_chat_json(head: bytes) -> bool:
    """Cheap test on the first bytes (adapter classification): Telegram / Messenger / generic chat JSON."""
    h = head.lower()
    return b'"messages"' in h and (b'"from"' in h or b'"sender_name"' in h or b'"participants"' in h) or (
        h.lstrip()[:1] == b"[" and (b'"sender' in h or b'"from"' in h or b'"author"' in h))


# ----------------------------------------------------------------------------------------------- PDF

MAX_PDF_BYTES = 50_000_000
MAX_STREAM_OUT = 20_000_000  # decompressed bytes per PDF (bomb guard)
_STREAM = re.compile(rb"<<(.{0,2000}?)>>\s*stream\r?\n", re.S)
_URI = re.compile(rb"/URI\s*\(((?:\\.|[^\\)]){1,2000})\)")
_TEXT_OP = re.compile(rb"\[((?:\\.|[^\]\\])*)\]\s*TJ|\(((?:\\.|[^\\)])*)\)\s*(?:Tj|'|\")|(T\*|Td|TD|ET|BT)")
_PSTR = re.compile(rb"\(((?:\\.|[^\\)])*)\)")
_ESC = {b"n": b"\n", b"r": b"\r", b"t": b"\t", b"b": b"\b", b"f": b"\f", b"(": b"(", b")": b")", b"\\": b"\\"}


def _unescape(s: bytes) -> str:
    out = bytearray()
    i = 0
    while i < len(s):
        c = s[i:i + 1]
        if c == b"\\" and i + 1 < len(s):
            n = s[i + 1:i + 2]
            if n in _ESC:
                out += _ESC[n]
                i += 2
                continue
            m = re.match(rb"[0-7]{1,3}", s[i + 1:i + 4])
            if m:
                out.append(int(m.group(0), 8) & 0xFF)
                i += 1 + len(m.group(0))
                continue
            i += 1
            continue
        out += c
        i += 1
    b = bytes(out)
    if b.startswith(b"\xfe\xff"):
        return b[2:].decode("utf-16-be", "replace")
    try:
        return b.decode("utf-8")
    except UnicodeDecodeError:
        return b.decode("cp1250", "replace")


def _streams(data: bytes) -> list[bytes]:
    out, budget = [], MAX_STREAM_OUT
    for m in _STREAM.finditer(data):
        start = m.end()
        end = data.find(b"endstream", start)
        if end < 0:
            break
        body = data[start:end]
        if b"/FlateDecode" in m.group(1):
            d = zlib.decompressobj()
            try:
                body = d.decompress(body, budget)
            except zlib.error:
                continue
        elif b"/Filter" in m.group(1):
            continue  # images and other encodings carry no text
        budget -= len(body)
        out.append(body)
        if budget <= 0:
            break
    return out


def pdf_text(path: Path) -> tuple[list[str], list[str]]:
    """(text lines, link URIs) of a text-based PDF; ([], []) for anything else."""
    with path.open("rb") as fh:
        data = fh.read(MAX_PDF_BYTES)
    if not data.startswith(b"%PDF"):
        return [], []
    uris = [_unescape(u) for u in _URI.findall(data)]
    lines: list[str] = []
    for body in [data, *_streams(data)]:
        cur: list[str] = []
        for m in _TEXT_OP.finditer(body):
            if m.group(1) is not None:
                cur.append("".join(_unescape(p) for p in _PSTR.findall(m.group(1))))
            elif m.group(2) is not None:
                cur.append(_unescape(m.group(2)))
            elif cur:
                line = "".join(cur).strip()
                if line:
                    lines.append(line)
                cur = []
        if cur and "".join(cur).strip():
            lines.append("".join(cur).strip())
    for b in _streams(data):
        uris += [_unescape(u) for u in _URI.findall(b)]
    return list(dict.fromkeys(ln for ln in lines if ln)), list(dict.fromkeys(u for u in uris if u))


# ------------------------------------------------------------------------------------------ extractor

URL_RE = re.compile(r"(?:https?://|www\.)[^\s<>\"'()\[\]{}]{3,300}", re.I)


@register("txt_chat", family=Family.TXT, version=1)
def txt_chat(inc: ResolvedIncident, ctx: ExtractContext) -> ExtractResult:
    """Genes of communication logs and PDF documents (TEXT role): sender personas (ID), platform and first-message
    date (weak, never linking alone), and the URLs written in messages / PDF links as INF URL genes."""
    from hydra.genome.har import url_genes
    from hydra.genome.webparse import GeneSink

    out = ExtractResult()
    iid = inc.incident.id
    sink = GeneSink(iid, "txt_chat@1")
    texts = [a for a in inc.by_role("text")]
    if not texts:
        out.genes.append(Gene(incident_id=iid, family=Family.TXT, kind="chat_platform",
                              status=GeneStatus.NOT_APPLICABLE, extractor="txt_chat@1"))
        return out
    for a in texts:
        aid = a.meta.artifact_id
        ev = {"artifact_id": aid}
        urls: list[str] = []
        if a.path.suffix.lower() == ".pdf" or (a.meta.mime or "") == "application/pdf":
            lines, uris = pdf_text(a.path)
            urls = uris + [u for ln in lines for u in URL_RE.findall(ln)]
            out.genes.append(Gene(incident_id=iid, family=Family.TXT, kind="document_type", value="pdf",
                                  extractor="txt_chat@1", quality=0.1, dependency_group="txt_none:doc",
                                  evidence={**ev, "note": f"{len(lines)} text lines, {len(uris)} links"}))
        else:
            log = parse_chat(a.path, as_json=(a.meta.mime or "").endswith("json") or None)
            if log is None:
                continue
            first, last = log.span
            note = f"{log.platform}: {len(log.messages)} messages, {len(log.senders)} senders, {first} – {last}"
            out.genes.append(Gene(incident_id=iid, family=Family.TXT, kind="chat_platform", value=log.platform,
                                  extractor="txt_chat@1", quality=0.1, dependency_group="txt_none:chat",
                                  evidence={**ev, "note": note}))
            counts: dict[str, int] = {}
            for m in log.messages:
                if m.sender:
                    counts[m.sender] = counts.get(m.sender, 0) + 1
            for name, n in sorted(counts.items(), key=lambda kv: -kv[1])[:20]:
                key = re.sub(r"\s+", " ", name.casefold()).strip()
                if len(key) < 2 or re.fullmatch(r"[+\d\s()-]+", key):
                    continue  # a phone number as sender name is an entity, found by txt_entities
                out.genes.append(Gene(incident_id=iid, family=Family.ID, kind="chat_persona", value=key[:120],
                                      extractor="txt_chat@1", quality=0.6, dependency_group="id_person:chat",
                                      evidence={**ev, "note": f"sender '{name}' ({n} messages)"}))
            if first:
                out.genes.append(Gene(incident_id=iid, family=Family.META, kind="chat_start_date", value=first[:10],
                                      extractor="txt_chat@1", quality=0.3, dependency_group="meta:chat_date",
                                      evidence={**ev, "note": f"first message {first}, last {last}"}))
            urls = [u for m in log.messages for u in URL_RE.findall(m.text)]
        for u in dict.fromkeys(urls):
            u = u.rstrip(".,;:!?")
            url_genes(sink, u if u.lower().startswith("http") else f"https://{u}", "chat", aid)
    out.genes.extend(sink.genes)
    if not out.genes:
        out.genes.append(Gene(incident_id=iid, family=Family.TXT, kind="chat_platform",
                              status=GeneStatus.NOT_APPLICABLE, extractor="txt_chat@1"))
    return out
