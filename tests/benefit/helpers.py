"""Benefit tests (G3-FEATURES, 2026-11-22): synthetic incident folders through the real adapter and extractors.

Every incident is a folder of generated files (no organiser data, no downloaded artwork). It is planned by
`hydra.adapters.nask_folder.plan_incident` and run through `hydra.genome.base.run_all` with the extractors listed
in `EXTRACTORS`; genomes, rarity and pair scores come from the product (`synapse.build_genome`,
`entropy.build_rarity`, `synapse.score_pair`, `nexus.cluster`).

Two deliberate substitutions, both stated in each test that depends on them:
- OCR: the shared OCR pool is replaced by `NoTextOcr` (every image reads as "no text"). The generated creatives
  carry no text, so this only removes 2-20 s of model start-up per incident; it never adds evidence.
- Models: the test `settings` fixture has an empty models directory, so DINOv2, e5 and YuNet/SFace are
  `unavailable` (the product's missing-model path). Visual matching in these tests therefore uses only the
  pixel hashes (pHash/PDQ families) and the ORB/RootSIFT + MAGSAC geometry check, never an embedding.
"""

from __future__ import annotations

import hashlib
import os
from itertools import combinations
from pathlib import Path

import numpy as np
import pytest
from PIL import Image, ImageDraw

from hydra import vis_colour, vis_geom
from hydra.entropy import build_rarity
from hydra.genome import base
from hydra.genome.base import ExtractContext, run_all
from hydra.genome.ocr_pool import OcrResult
from hydra.nexus import cluster
from hydra.synapse import build_genome, score_pair
from tests.extract_accuracy.harness import resolve

EXTRACTORS = ("vis", "ident", "logo", "txt_lines", "txt_phrases", "txt_entities", "txt_embed", "inf_text_hosts",
              "inf_url", "inf_har", "inf_dom", "nar")


def real_models_dir() -> Path | None:
    """The installed offline weights (read-only use), or None. Tests needing them skip when absent."""
    p = Path(os.environ.get("HYDRA_MODELS_DIR") or Path.home() / "HydraNexus" / "models")
    return p if (p / "dinov2-small").is_dir() and (p / "e5-small").is_dir() else None


class NoTextOcr:
    """Stand-in for the shared OCR pool: every image yields zero lines (ok=True). Never invents text."""

    def run(self, items, crop_chrome: bool = True):
        return [OcrResult(sha256=sha or hashlib.sha256(Path(p).read_bytes()).hexdigest(), engine="none")
                for p, sha in items]


def creative(seed: int, size=(480, 320)) -> Image.Image:
    """Textured synthetic creative (many small shapes: enough corners for ORB)."""
    rng = np.random.default_rng(seed)
    im = Image.new("RGB", size, tuple(int(c) for c in rng.integers(150, 240, 3)))
    d = ImageDraw.Draw(im)
    w, h = size
    for _ in range(260):
        x, y = int(rng.integers(0, w - 20)), int(rng.integers(0, h - 20))
        s = int(rng.integers(6, 28))
        fill = tuple(int(c) for c in rng.integers(0, 255, 3))
        if rng.random() < 0.5:
            d.rectangle((x, y, x + s, y + s // 2 + 3), fill=fill)
        else:
            d.ellipse((x, y, x + s, y + s), fill=fill)
    return im


def crop(im: Image.Image, fx=(0.12, 0.0, 0.0, 0.12), scale=0.8) -> Image.Image:
    w, h = im.size
    c = im.crop((int(w * fx[0]), int(h * fx[1]), w - int(w * fx[2]), h - int(h * fx[3])))
    return c.resize((int(c.width * scale), int(c.height * scale)))


class World:
    """A set of synthetic incidents, extracted once, scored with the product scorer."""

    def __init__(self, settings, root: Path, monkeypatch, real_models: bool = False):
        if real_models:
            models = real_models_dir()
            if models is None:
                pytest.skip("DINOv2/e5 weights not installed (run scripts/fetch_models.py)")
            settings = settings.model_copy(update={"models_dir": models})
        self.settings = settings
        self.root = root
        self.genes: dict[str, list] = {}
        self.vectors: dict[str, tuple[str, np.ndarray]] = {}
        self.images: dict[str, Image.Image] = {}
        base.load_family_modules()
        self.ocr = NoTextOcr()
        vis_geom._MEMO.clear()
        vis_colour._MEMO.clear()
        vis_geom._SPENT[0] = 0
        monkeypatch.setattr(vis_geom, "_settings", lambda: settings)
        monkeypatch.setattr(vis_geom, "_locate", lambda artifact_id, settings=None: root)
        monkeypatch.setattr(vis_geom, "load_unit", lambda ref, settings=None, root=None: self.images.get(ref[0]))

    def add(self, iid: str, files: dict[str, object]) -> list:
        """files: name -> PIL image | str (text) | bytes. Returns the incident's genes."""
        folder = self.root / iid
        folder.mkdir(parents=True)
        for name, content in files.items():
            p = folder / name
            if isinstance(content, Image.Image):
                content.save(p)
                self.images[hashlib.sha256(p.read_bytes()).hexdigest()] = content.convert("RGB")
            elif isinstance(content, str):
                p.write_text(content, "utf-8")
            else:
                p.write_bytes(content)  # type: ignore[arg-type]
        inc, _errors = resolve(folder, self.settings)
        ctx = ExtractContext(settings=self.settings, work_dir=self.root / ".work" / iid,
                             models={"ocr_pool": self.ocr})
        res = run_all(inc, ctx, only=[n for n in EXTRACTORS if n in base.REGISTRY])
        genes = [g.model_copy(update={"incident_id": iid}) for g in res.genes]
        self.genes[iid] = genes
        for v in res.vectors:
            self.vectors[v.key] = (v.model, v.data)
        return genes

    def build(self):
        genomes = {iid: build_genome(iid, gs, self.vectors) for iid, gs in self.genes.items()}
        per_kind: dict = {}
        for iid, genome in genomes.items():
            for key, (matrix, _genes) in genome.vectors.items():
                per_kind.setdefault(key, {})[iid] = matrix
        return genomes, build_rarity(self.genes, per_kind)

    def pair(self, a: str, b: str):
        genomes, rarity = self.build()
        return score_pair(genomes[a], genomes[b], rarity)

    def clustering(self):
        genomes, rarity = self.build()
        pairs = {(a, b): score_pair(genomes[a], genomes[b], rarity) for a, b in combinations(sorted(genomes), 2)}
        return pairs, cluster(genomes, pairs, rarity)


def kinds(pe) -> set[str]:
    return {m.kind for m in pe.score.reasons}


def members_of(clustering, iid: str) -> set[str]:
    for c in clustering.campaigns:
        if iid in c.members:
            return set(c.members)
    return {iid}


# Background: unrelated generic material so that document frequencies are not computed on 2-4 incidents only.
BACKGROUND_TEXTS = [
    "Promocja tygodnia w sklepie internetowym. Sprawdź nowe produkty i darmową dostawę od 199 zł.",
    "Konkurs dla klientów: wygraj rower miejski. Regulamin dostępny na stronie organizatora.",
    "Nowy kurs językowy online. Zapisz się dziś i otrzymaj pierwszą lekcję bez opłat.",
    "Wyprzedaż końca sezonu: kurtki, buty i akcesoria taniej nawet o 40 procent.",
    "Poradnik ogrodnika: jak przygotować rośliny do zimy w pięciu krokach.",
    "Webinar o oszczędzaniu energii w domu jednorodzinnym, transmisja w czwartek.",
]


def add_background(world: World, n: int = 6, image_seed0: int = 900) -> None:
    for k in range(n):
        world.add(f"bg-{k}", {"opis.txt": BACKGROUND_TEXTS[k % len(BACKGROUND_TEXTS)],
                             "reklama.png": creative(image_seed0 + k)})
