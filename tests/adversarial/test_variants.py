"""Real synthetic pixel/text mutations plus explicitly synthetic embedding contract fixtures."""

import numpy as np
import pytest
from PIL import Image, ImageDraw, ImageEnhance

from hydra.genome.txt_entities import find_phones
from hydra.genome.vis import phash_hex, unit_hash_values
from hydra.synapse import score_pair
from tests.adversarial.helpers import corpus, gene, image_unit


def creative():
    # Original generated pixels. No downloaded artwork, fonts, organiser screenshots or learned model.
    rng = np.random.default_rng(37)
    image = Image.new("RGB", (480, 320), (225, 215, 205))
    draw = ImageDraw.Draw(image)
    for _ in range(80):
        x, y = rng.integers(0, 400), rng.integers(0, 260)
        color = tuple(int(c) for c in rng.integers(20, 200, 3))
        draw.rectangle((int(x), int(y), int(x + 50), int(y + 40)), fill=color)
    draw.rectangle((15, 20, 310, 75), fill=(15, 45, 130))
    draw.ellipse((320, 90, 460, 250), fill=(180, 50, 80))
    return image


@pytest.mark.parametrize("mutation", [
    "resize", "recolor", "crop",
])
def test_creative_mutation_links_given_high_embedding_similarity(mutation):
    original = creative()
    variant = {
        "resize": lambda: original.resize((960, 640), Image.Resampling.LANCZOS),
        "recolor": lambda: ImageEnhance.Color(original).enhance(0.1),
        "crop": lambda: original.crop((20, 14, 460, 306)).resize(original.size, Image.Resampling.LANCZOS),
    }[mutation]()
    rows, vectors = {}, {}
    for iid, image in (("a", original), ("b", variant)):
        rows[iid], refs = image_unit(iid, iid, phash_hex(image))
        # Every hash gene the VIS extractor emits for a unit (pdq, pdq_canon, pdq_dihedral, phash_tiles,
        # phash_multicrop), computed by the product function on the real pixels; phash is already in the unit.
        for kind, (value, quality, _note) in unit_hash_values(image).items():
            if kind != "phash":
                rows[iid].append(rows[iid][0].model_copy(update={"kind": kind, "value": value, "quality": quality}))
        vectors.update(refs)
    genomes, rarity = corpus(rows, vectors)
    pair = score_pair(genomes["a"], genomes["b"], rarity)
    assert pair.score.hypothesis == "same_campaign", (mutation, phash_hex(original), phash_hex(variant), pair)


def test_changed_copy_preserving_rare_phrases_and_contact_links():
    phrase = "Syntetyczny bursztynowy kompas orbitalny"
    second = "Lazurowa mapa hipotetycznej pracowni"
    texts = {"a": f"{phrase}. {second}. Zadzwoń +48 700 123 456. Oferta w poniedziałek.",
             "b": f"Nowa domena i inne warunki. {second}. {phrase}. Kontakt: +48 700-123-456."}
    rows = {}
    for iid, text in texts.items():
        phones = find_phones(text)
        assert len(phones) == 1 and phrase in text
        rows[iid] = [
            gene(iid, "phrase", phrase, "TXT:phrase:rare", "TXT"),
            gene(iid, "phrase", second, "TXT:phrase:rare", "TXT"),
            gene(iid, "entity_phone", phones[0].value, "TXT:operator:phone", "TXT"),
        ]
    genomes, rarity = corpus(rows)
    pair = score_pair(genomes["a"], genomes["b"], rarity)
    assert pair.score.hypothesis == "same_campaign", pair
