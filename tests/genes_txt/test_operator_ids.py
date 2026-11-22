"""Payment and contact operator genes (ADOPT_PLAN item 3): checksums, tiers, pair scoring. Synthetic inputs only."""

from __future__ import annotations

import hashlib

import pytest

from hydra.contracts import ArtifactRole, IncidentArtifact
from hydra.entropy import build_rarity
from hydra.genome import txt as _txt  # noqa: F401  (registers txt_entities)
from hydra.genome.base import ExtractContext, ResolvedArtifact, ResolvedIncident, run_all
from hydra.genome.operator_ids import (
    KIND_PL_OPERATOR,
    b58encode,
    check_wallet,
    eip55,
    find_accounts,
    find_messenger_handles,
    find_payment_hints,
    find_wallets,
    keccak256,
    phone_profile,
    pl_sort_code_ok,
    specificity,
    xmr_b58encode,
)
from hydra.synapse import build_genome, gene_tier, level, score_pair

# Public test vectors (BIP-173, BIP-350, EIP-55, Bitcoin genesis address) and a synthetic Polish IBAN.
BECH32 = "bc1qw508d6qejxtdg4y5r3zarvary0c5xw7kv8f3t4"
BECH32M = "bc1p5d7rjq7g6rdk2yhzks9smlaqtedr4dekq08ge8ztwac72sfr9rusxg3297"
LEGACY = "1A1zP1eP5QGefi2DMPTfTL5SLmv7DivfNa"
ETH = "0x5aAeb6053F3E94C9b9A09f33669435E7Ef1BeAed"
IBAN_PL = "PL61 1090 1014 0000 0712 1981 2874"


def _flip(s: str, i: int) -> str:
    alt = "2" if s[i] != "2" else "3"
    return s[:i] + alt + s[i + 1:]


# ------------------------------------------------------------------------------------------------ checksums


def test_keccak_and_eip55_vectors():
    assert keccak256(b"").hex() == "c5d2460186f7233c927e7db2dcc703c0e500b653ca82273b7bfad8045d85a470"
    assert eip55(ETH[2:].lower()) == ETH[2:]
    assert check_wallet("eth", ETH).valid
    assert not check_wallet("eth", ETH[:-1] + "D").valid  # case of one letter changed


@pytest.mark.parametrize("addr", [BECH32, BECH32M, LEGACY])
def test_btc_valid_and_one_char_change_gets_low_quality(addr):
    assert check_wallet("btc", addr).valid
    bad = _flip(addr, len(addr) - 3)
    assert not check_wallet("btc", bad).valid
    e = find_wallets(f"Wyślij BTC na portfel {bad}")
    assert e and e[0].quality <= 0.4  # ADOPT acceptance: quality 0.4 or less
    assert find_wallets(f"Portfel bitcoin: {addr}")[0].quality == 1.0


def test_trx_usdt_and_synthetic_xmr():
    payload = b"\x41" + hashlib.sha256(b"synthetic-trx").digest()[:20]
    trx = b58encode(payload + hashlib.sha256(hashlib.sha256(payload).digest()).digest()[:4])
    assert trx.startswith("T") and check_wallet("trx", trx).valid
    e = find_wallets(f"Wpłać USDT (TRC20) na {trx}")
    assert e[0].value == f"trx:{trx}" and "USDT-TRC20" in e[0].note and e[0].tier == "operator"
    body = b"\x12" + hashlib.sha256(b"spend").digest() + hashlib.sha256(b"view").digest()
    xmr = xmr_b58encode(body + keccak256(body)[:4])
    assert len(xmr) == 95 and xmr.startswith("4") and check_wallet("xmr", xmr).valid
    assert not check_wallet("xmr", _flip(xmr, 50)).valid


def test_random_base58_without_context_is_not_a_wallet():
    assert not find_wallets("token 1BvBMSEYstWetqTFn5Au4m4GFg7xJaNVN3 zzz")  # checksum fails, no crypto word
    assert not find_wallets(f"hash {ETH.lower()}", need_context=True)


def test_iban_bank_split_and_nrb():
    es = find_accounts(f"Przelew na konto {IBAN_PL} (tytuł: depozyt)")
    acc = [e for e in es if e.kind == "entity_iban"]
    bank = [e for e in es if e.kind == "bank_code"]
    assert acc[0].value == "PL61109010140000071219812874" and acc[0].quality == 1.0 and acc[0].tier == "operator"
    assert bank[0].value == "PL:109" and bank[0].tier == "platform"
    assert "**** 2874" in acc[0].note and "1981" not in acc[0].note
    nrb = find_accounts("nr rachunku 61 1090 1014 0000 0712 1981 2874")
    assert nrb[0].value == "PL61109010140000071219812874" and "NRB" in nrb[0].note
    bad = find_accounts("PL61 1090 1014 0000 0712 1981 2875")
    assert bad[0].quality <= 0.4 and not [e for e in bad if e.kind == "bank_code"]
    assert pl_sort_code_ok("10901014") and not pl_sort_code_ok("10901015")


def test_messenger_handles():
    t = ("Napisz: https://t.me/Zysk_Doradca_PL lub wa.me/48600123456, Telegram: @InwestorPL1, "
         "grupa https://chat.whatsapp.com/AbCdEfGhIjKlMnOpQr12, https://t.me/share/url?u=x, signal.me/#p/+48600123457")
    vals = {(e.kind, e.value, e.group) for e in find_messenger_handles(t)}
    assert ("messenger_handle", "telegram:@zysk_doradca_pl", "messenger") in vals
    assert ("messenger_handle", "telegram:@inwestorpl1", "messenger") in vals
    assert ("messenger_handle", "whatsapp:+48600123456", "contact") in vals
    assert ("entity_phone", "+48600123456", "contact") in vals
    assert ("messenger_handle", "whatsapp_group:AbCdEfGhIjKlMnOpQr12", "messenger") in vals
    assert ("messenger_handle", "signal:+48600123457", "contact") in vals
    assert not any(v.endswith("@share") for _, v, _ in vals)
    assert not find_messenger_handles("kontakt @nazwa_uzytkownika na forum")  # @handle without Telegram context
    assert not find_messenger_handles("https://sklep.at.me/kontakt")
    assert not find_messenger_handles(".telegram-icon{color:red} @media (max-width:600px){}")  # CSS at-rule


def test_payment_hints_are_kit_tier():
    es = find_payment_hints("Zapłać kodem BLIK albo przelew na telefon")
    assert {e.value for e in es} == {"blik", "phone_transfer"} and all(e.tier == "kit" for e in es)


# ------------------------------------------------------------------------------------------------ phone tiers


def test_phone_tiers_and_specificity():
    assert phone_profile("+48 600 123 456").tier == "operator"
    hot = phone_profile("800 302 302")
    assert hot.tier == "boilerplate" and "infolinia" in hot.reason
    assert phone_profile("+48 701 234 567").tier == "platform"  # premium
    assert phone_profile("801 999 888").tier == "platform"  # shared cost
    assert phone_profile("112").tier == "boilerplate"
    voip = phone_profile("39 123 45 67")
    assert voip.ptype == "voip" and voip.tier == "operator"
    assert phone_profile("600 111 111").tier == "boilerplate"  # placeholder digits
    assert specificity(6) == 0.0 and specificity(11) == 1.0 and 0 < specificity(9) < 1
    short = phone_profile("123456", region="PL")
    assert short is None or short.weight == 0.0 or short.tier != "operator"


# ------------------------------------------------------------------------------------------------ extractor + scoring


def _incident(tmp_path, iid: str, text: str) -> ResolvedIncident:
    from hydra.contracts import Incident

    p = tmp_path / f"{iid}.txt"
    p.write_text(text, "utf-8")
    data = p.read_bytes()
    meta = IncidentArtifact(artifact_id=hashlib.sha256(data).hexdigest(), role=ArtifactRole.TEXT,
                            path_in_incident=f"{iid}.txt", mime="text/plain", size=len(data))
    return ResolvedIncident(Incident(id=iid, source="test", source_ref=iid), [ResolvedArtifact(meta, p)])


@pytest.fixture
def ctx(settings, tmp_path):
    return ExtractContext(settings=settings, work_dir=tmp_path / "work")


@pytest.fixture
def settings(tmp_path):
    from hydra.config import Settings

    return Settings(root=tmp_path, state_dir=tmp_path / "state")


FILLER = ["Ogród i dom: porady sezonowe numer {k}.", "Przepis na zupę dnia {k}.", "Rozkład jazdy linii {k}."]


def _score(tmp_path, ctx, text_a: str, text_b: str):
    texts = {"a": text_a, "b": text_b}
    texts.update({f"f{k}": FILLER[k % 3].format(k=k) for k in range(10)})
    genes = {}
    for iid, t in texts.items():
        res = run_all(_incident(tmp_path, iid, t), ctx, only=["txt_entities"])
        genes[iid] = res.genes
    rarity = build_rarity(genes, {})
    gen = {i: build_genome(i, gs, {}) for i, gs in genes.items()}
    return score_pair(gen["a"], gen["b"], rarity), genes


def test_shared_full_iban_links_with_polish_line(tmp_path, ctx):
    pe, genes = _score(tmp_path, ctx, f"Sklep Alfa. Wpłata na konto {IBAN_PL}.",
                       f"Sklep Beta, zupełnie inny tekst. Numer rachunku: {IBAN_PL}")
    acc = next(g for g in genes["a"] if g.kind == "entity_iban")
    assert acc.value.startswith("h:") and "1981" not in (acc.evidence.note or "")
    assert gene_tier(acc) == "operator" and level(acc) == "campaign"
    bank = next(g for g in genes["a"] if g.kind == "bank_code")
    assert gene_tier(bank) == "platform" and level(bank) == "kit"
    assert pe.campaign_bits > 0
    line = next(r for r in pe.score.reasons if r.kind == "entity_iban")
    assert line.explanation_pl.startswith("Ten sam numer konta")


def test_shared_bank_code_only_does_not_link(tmp_path, ctx):
    # two different valid accounts at the same bank (synthetic account numbers, checksums computed)
    def pl_iban(acct16: str) -> str:
        bban = "10901014" + acct16
        chk = 98 - int(bban + "252100") % 97
        return f"PL{chk:02d}{bban}"

    a, b = pl_iban("0000071219812874"), pl_iban("0000071219819999")
    pe, genes = _score(tmp_path, ctx, f"Sklep Alfa, konto {a}", f"Sklep Beta, konto {b}")
    assert {g.value for g in genes["a"] if g.kind == "bank_code"} == {"PL:109"}
    assert pe.campaign_bits == 0 and pe.support == 0
    assert pe.score.hypothesis != "same_campaign"


def test_shared_hotline_gives_no_link_but_mobile_does(tmp_path, ctx):
    pe, _ = _score(tmp_path, ctx, "Infolinia banku: 800 302 302, oferta A", "Zadzwoń 800 302 302, oferta B")
    assert pe.campaign_bits == 0 and not [r for r in pe.score.reasons if r.kind == "entity_phone"]
    pe2, genes = _score(tmp_path, ctx, "Kontakt: tel. +48 600 123 456, oferta A", "Zadzwoń: +48 600 123 456 teraz")
    ph = next(g for g in genes["a"] if g.kind == "entity_phone")
    assert ph.dependency_group == "TXT:operator:contact" and "mobile" in ph.evidence.note
    assert pe2.campaign_bits > 0


def test_shared_wallet_and_telegram_link(tmp_path, ctx):
    pe, _ = _score(tmp_path, ctx, f"Inwestuj! Portfel BTC: {BECH32}", f"Wpłać bitcoin na {BECH32} i zarabiaj")
    assert pe.campaign_bits > 0 and any(r.kind == "crypto_wallet" for r in pe.score.reasons)
    pe2, _ = _score(tmp_path, ctx, "Dołącz: https://t.me/ZyskTeamFikcja", "Pisz na t.me/zyskteamfikcja już dziś")
    assert pe2.campaign_bits > 0 and any(r.kind == "messenger_handle" for r in pe2.score.reasons)


def test_polish_labels_cover_new_kinds():
    assert set(KIND_PL_OPERATOR) >= {"bank_code", "crypto_wallet", "messenger_handle", "payment_method"}


def _page(tmp_path, iid: str, body: str) -> ResolvedIncident:
    from hydra.contracts import Incident

    p = tmp_path / iid / "Strona.html"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(f"<html><head><title>{iid}</title></head><body>{body}</body></html>", "utf-8")
    data = p.read_bytes()
    meta = IncidentArtifact(artifact_id=hashlib.sha256(data).hexdigest(), role=ArtifactRole.LANDING_HTML,
                            path_in_incident="Strona.html", mime="text/html", size=len(data))
    return ResolvedIncident(Incident(id=iid, source="test", source_ref=iid), [ResolvedArtifact(meta, p)])


def test_two_shops_with_the_same_iban_on_saved_pages_link(tmp_path, ctx):
    from hydra.genome import dom as _dom  # noqa: F401  (registers inf_dom)
    from hydra.genome.webparse import _BUNDLES

    bodies = {"a": f"<p>Sklep Alfa, przelew na {IBAN_PL}</p>", "b": f"<p>Inny sklep Beta: konto {IBAN_PL}</p>"}
    bodies.update({f"f{k}": f"<p>{FILLER[k % 3].format(k=k)}</p>" for k in range(10)})
    genes = {}
    for iid, body in bodies.items():
        _BUNDLES.clear()
        genes[iid] = run_all(_page(tmp_path, iid, body), ctx, only=["inf_dom"]).genes
    acc = next(g for g in genes["a"] if g.kind == "entity_iban")
    assert acc.value.startswith("h:") and acc.dependency_group == "inf:operator:contact"
    assert next(g for g in genes["a"] if g.kind == "bank_code").dependency_group == "inf:platform:bank"
    rarity = build_rarity(genes, {})
    gen = {i: build_genome(i, gs, {}) for i, gs in genes.items()}
    pe = score_pair(gen["a"], gen["b"], rarity)
    assert pe.score.hypothesis == "same_campaign"
    assert any(r.explanation_pl.startswith("Ten sam numer konta") for r in pe.score.reasons)



def test_chat_export_sender_phone_is_an_entity(tmp_path, ctx):
    """CR-E2E-1: the sender of a WhatsApp/iOS export is not part of the message text; a phone-number sender still
    yields `entity_phone` (a named sender does not)."""
    chat = ("[01.10.2026, 10:00] +48 733 112 909: InPost: Twoja paczka czeka. Dopłata 1,99 zł\n"
            "[01.10.2026, 11:01] Ja: Co to za dopłata?\n"
            "[01.10.2026, 12:02] +48 733 112 909: Prosimy zapłacić w ciągu 24h\n")
    genes = run_all(_incident(tmp_path, "c1", chat), ctx, only=["txt_entities"]).genes
    phones = [g for g in genes if g.kind == "entity_phone" and g.status == "observed"]
    assert [g.value for g in phones] == ["+48733112909"]
