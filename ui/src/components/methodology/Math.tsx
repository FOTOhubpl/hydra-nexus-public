/**
 * Formulas of the methodology as native MathML (no new dependency; Chromium, Firefox and Safari render MathML Core).
 * Each formula carries an `aria-label` with a plain-text reading and a source line (file:function). The formulas
 * mirror hydra/entropy.py, hydra/synapse.py and hydra/assign.py; constants are the code's defaults.
 */
import type { HTMLAttributes, ReactNode } from "react";
import { cx } from "../../lib/cx";

type MathAttrs = HTMLAttributes<HTMLElement> & { display?: "block" | "inline"; mathvariant?: string; stretchy?: string; form?: string; lspace?: string; rspace?: string; width?: string };

declare module "react" {
  // MathML Core elements are not in @types/react; additive augmentation for TSX.
  // eslint-disable-next-line @typescript-eslint/no-namespace
  namespace JSX {
    interface IntrinsicElements {
      math: MathAttrs; mrow: MathAttrs; mi: MathAttrs; mo: MathAttrs; mn: MathAttrs; mtext: MathAttrs;
      mfrac: MathAttrs; msub: MathAttrs; msup: MathAttrs; msubsup: MathAttrs; mspace: MathAttrs; mstyle: MathAttrs;
    }
  }
}

/** Block formula on a soft card, with the code reference underneath. */
export function Formula({ label, source, children, className }: { label: string; source?: ReactNode; children: ReactNode; className?: string }) {
  return (
    <figure className={cx("methodology-formula my-3 overflow-x-auto overflow-y-hidden rounded-xl border border-line bg-raised/60 px-4 py-4 [&_math]:py-1", className)}>
      <math display="block" aria-label={label} className="text-[1.15rem] text-fg">{children}</math>
      {source && <figcaption className="mt-2 text-2xs text-subtle">{source}</figcaption>}
    </figure>
  );
}

/** Inline formula inside a sentence. */
export function M({ label, children }: { label: string; children: ReactNode }) {
  return <math aria-label={label} className="text-fg">{children}</math>;
}

const fn = (name: string) => <mi mathvariant="normal">{name}</mi>;

export const FUEst = () => (
  <Formula label="u daszek od v równa się df pozostałych plus A razy u zero rodzaju, przez N pozostałych plus A; A równa się 20" source={<><span className="font-mono">hydra/entropy.py</span> · <span className="font-mono">Rarity.u_estimate</span> · A = <span className="font-mono">PRIOR_STRENGTH = 20</span>, u₀ = <span className="font-mono">KIND_U0</span> (domyślnie 2⁻⁸)</>}>
    <mrow>
      <mi>û</mi><mo>(</mo><mi>v</mi><mo>)</mo><mo>=</mo>
      <mfrac>
        <mrow><msub><mi>df</mi><mtext>poz</mtext></msub><mo>(</mo><mi>v</mi><mo>)</mo><mo>+</mo><mi>A</mi><mo>·</mo><msub><mi>u</mi><mn>0</mn></msub><mo>(</mo><mi>k</mi><mo>)</mo></mrow>
        <mrow><msub><mi>N</mi><mtext>poz</mtext></msub><mo>+</mo><mi>A</mi></mrow>
      </mfrac>
      <mspace width="1.2em" />
      <mi>A</mi><mo>=</mo><mn>20</mn>
    </mrow>
  </Formula>
);

export const FBackground = () => (
  <Formula label="u równa się maksimum z u lokalnego oraz df pozostałych plus df tła plus A u zero, przez N pozostałych plus N tła plus A" source={<><span className="font-mono">Rarity.u_estimate</span> · tło: <span className="font-mono">hydra/priors/background_df.json.gz</span>, rodzaj ignorowany, gdy ma &lt; 200 jednostek tła (<span className="font-mono">MIN_KIND_UNITS</span>)</>}>
    <mrow>
      <mi>û</mi><mo>=</mo>{fn("max")}<mo>(</mo>
      <msub><mi>û</mi><mtext>lok</mtext></msub><mo>,</mo>
      <mfrac>
        <mrow><msub><mi>df</mi><mtext>poz</mtext></msub><mo>+</mo><msub><mi>p</mi><mtext>df</mtext></msub><mo>+</mo><mi>A</mi><mo>·</mo><msub><mi>u</mi><mn>0</mn></msub></mrow>
        <mrow><msub><mi>N</mi><mtext>poz</mtext></msub><mo>+</mo><msub><mi>p</mi><mi>n</mi></msub><mo>+</mo><mi>A</mi></mrow>
      </mfrac>
      <mo>)</mo>
    </mrow>
  </Formula>
);

export const FBits = () => (
  <Formula label="b od v równa się minimum z 14 i maksimum z 0 i logarytmu przy podstawie 2 z m przez u daszek, razy waga jakości; m równa się 0,5" source={<><span className="font-mono">Rarity.discrete_bits</span> (<span className="font-mono">DEFAULT_M = 0.5</span>, <span className="font-mono">DISCRETE_CAP = 14</span>) · waga jakości: <span className="font-mono">hydra/synapse.py</span> · <span className="font-mono">quality_weight</span></>}>
    <mrow>
      <mi>b</mi><mo>(</mo><mi>v</mi><mo>)</mo><mo>=</mo>
      {fn("min")}<mo>(</mo><mn>14</mn><mo>,</mo>{fn("max")}<mo>(</mo><mn>0</mn><mo>,</mo>
      <msub>{fn("log")}<mn>2</mn></msub><mfrac><mi>m</mi><mrow><mi>û</mi><mo>(</mo><mi>v</mi><mo>)</mo></mrow></mfrac>
      <mo>)</mo><mo>)</mo><mo>·</mo><mi>q</mi><mo>(</mo><mi>v</mi><mo>)</mo>
      <mspace width="1.2em" />
      <mi>m</mi><mo>=</mo><mn>0,5</mn>
    </mrow>
  </Formula>
);

export const FQuality = () => (
  <Formula label="q równa się 0, gdy jakość poniżej 0,25, w przeciwnym razie minimum z 1 i jakości przez 0,5" source={<><span className="font-mono">synapse.quality_weight</span> · <span className="font-mono">QUALITY_MIN = 0.25</span>, <span className="font-mono">QUALITY_FULL = 0.5</span>; decyduje słabsza z dwóch obserwacji</>}>
    <mrow>
      <mi>q</mi><mo>=</mo>{fn("min")}<mo>(</mo><mn>1</mn><mo>,</mo><mfrac><msub><mi>q</mi><mtext>min</mtext></msub><mn>0,5</mn></mfrac><mo>)</mo>
      <mspace width="1.2em" />
      <mi>q</mi><mo>=</mo><mn>0</mn><mspace width="0.4em" /><mtext>gdy</mtext><mspace width="0.4em" /><msub><mi>q</mi><mtext>min</mtext></msub><mo>&lt;</mo><mn>0,25</mn>
      <mspace width="1.2em" />
      <msub><mi>q</mi><mtext>min</mtext></msub><mo>=</mo>{fn("min")}<mo>(</mo><msub><mi>q</mi><mi>a</mi></msub><mo>,</mo><msub><mi>q</mi><mi>b</mi></msub><mo>)</mo>
    </mrow>
  </Formula>
);

export const FSmooth = () => (
  <Formula label="w równa się U plus 1 minus h, przez U plus 1 minus L; L równa się maksimum z 5 i sufitu z 0,2 n; U równa się maksimum z 10 i sufitu z 0,5 n" source={<><span className="font-mono">entropy.operator_seed_soft</span>, <span className="font-mono">Rarity._soft_operator_bits</span> · tryb domyślny <span className="font-mono">HYDRA_OPERATOR_UNITS=smooth</span> (SPEC_ERRATA E-SH-24)</>}>
    <mrow>
      <mi>w</mi><mo>=</mo><mfrac><mrow><mi>U</mi><mo>+</mo><mn>1</mn><mo>−</mo><mi>h</mi></mrow><mrow><mi>U</mi><mo>+</mo><mn>1</mn><mo>−</mo><mi>L</mi></mrow></mfrac>
      <mspace width="1.2em" />
      <mi>L</mi><mo>=</mo>{fn("max")}<mo>(</mo><mn>5</mn><mo>,</mo><mo>⌈</mo><mn>0,2</mn><mi>n</mi><mo>⌉</mo><mo>)</mo><mo>,</mo>
      <mspace width="0.6em" />
      <mi>U</mi><mo>=</mo>{fn("max")}<mo>(</mo><mn>10</mn><mo>,</mo><mo>⌈</mo><mn>0,5</mn><mi>n</mi><mo>⌉</mo><mo>)</mo>
    </mrow>
  </Formula>
);

export const FSmoothBits = () => (
  <Formula label="bity równa się w razy bity posiadaczy jako jednej jednostki plus 1 minus w razy bity posiadaczy jako incydentów" source={<><span className="font-mono">Rarity._soft_operator_bits</span> · dla L &lt; h ≤ U; h ≤ L: jedna jednostka, h &gt; U: 0 bitów</>}>
    <mrow>
      <mi>b</mi><mo>=</mo><mi>w</mi><mo>·</mo><msub><mi>b</mi><mtext>jedna jednostka</mtext></msub><mo>+</mo><mo>(</mo><mn>1</mn><mo>−</mo><mi>w</mi><mo>)</mo><mo>·</mo><msub><mi>b</mi><mtext>incydenty</mtext></msub>
    </mrow>
  </Formula>
);

export const FSupport = () => (
  <Formula label="S równa się C plus pół A, gdy C większe od zera; suma całkowita równa się C plus A plus sprzeczności" source={<><span className="font-mono">hydra/synapse.py</span> · <span className="font-mono">score_pair</span>: C = bity poziomu kampanii, A = bity zasobu (≤ 8, <span className="font-mono">ASSET_CAP</span>), x = sprzeczności (−4 b, <span className="font-mono">CONTRADICTION_BITS</span>)</>}>
    <mrow>
      <mi>S</mi><mo>=</mo><mi>C</mi><mo>+</mo><mn>0,5</mn><mo>·</mo><mi>A</mi><mspace width="0.5em" /><mtext>(gdy</mtext><mspace width="0.3em" /><mi>C</mi><mo>&gt;</mo><mn>0</mn><mtext>)</mtext>
      <mspace width="1.2em" />
      <msub><mi>T</mi><mtext>suma</mtext></msub><mo>=</mo><mi>C</mi><mo>+</mo><mi>A</mi><mo>+</mo><mi>x</mi>
    </mrow>
  </Formula>
);

export const FGroup = () => (
  <Formula label="bity grupy zależności równa się minimum z 14 i bitów najsilniejszego genu plus 0,25 razy suma pozostałych; rodzina do 24 bitów" source={<><span className="font-mono">synapse.score_pair</span> · <span className="font-mono">REST_WEIGHT = 0.25</span>, <span className="font-mono">GROUP_CAP = 14</span>, <span className="font-mono">FAMILY_CAP = 24</span>; rodziny inne niż INF/META łączą grupy jako max + 0,5 × druga</>}>
    <mrow>
      <msub><mi>G</mi><mi>g</mi></msub><mo>=</mo>{fn("min")}<mo>(</mo><mn>14</mn><mo>,</mo><msub><mi>b</mi><mtext>max</mtext></msub><mo>+</mo><mn>0,25</mn><mo>·</mo><mo>∑</mo><msub><mi>b</mi><mtext>pozostałe</mtext></msub><mo>)</mo>
      <mspace width="1.2em" />
      <msub><mi>F</mi><mi>f</mi></msub><mo>=</mo>{fn("min")}<mo>(</mo><mn>24</mn><mo>,</mo><mo>∑</mo><msub><mi>G</mi><mi>g</mi></msub><mo>)</mo>
    </mrow>
  </Formula>
);

export const FSizeGuard = () => (
  <Formula label="podział składowej, gdy jej rozmiar większy od maksimum z 60 i podłogi z n przez 3 oraz gęstość mniejsza od 0,3" source={<><span className="font-mono">hydra/nexus.py</span> · <span className="font-mono">cluster</span>, <span className="font-mono">split_component</span> · <span className="font-mono">MAX_SIZE_MIN = 60</span>, <span className="font-mono">SPLIT_MAX_DENSITY = 0.3</span></>}>
    <mrow>
      <mo>|</mo><mi>K</mi><mo>|</mo><mo>&gt;</mo>{fn("max")}<mo>(</mo><mn>60</mn><mo>,</mo><mo>⌊</mo><mfrac><mi>n</mi><mn>3</mn></mfrac><mo>⌋</mo><mo>)</mo>
      <mspace width="0.6em" /><mo>∧</mo><mspace width="0.6em" />
      <mtext>gęstość</mtext><mo>(</mo><mi>K</mi><mo>)</mo><mo>&lt;</mo><mn>0,3</mn>
      <mspace width="0.6em" /><mo>⇒</mo><mspace width="0.6em" /><mtext>podział Leiden</mtext>
    </mrow>
  </Formula>
);

export const FQueue = () => (
  <Formula label="p równa się 1 przez 1 plus 2 do potęgi minus dowód minus 10; oczekiwana zmiana równa się p razy potwierdzenie plus 1 minus p razy odrzucenie" source={<><span className="font-mono">hydra/queue_gain.py</span> · p nieskalibrowane; ranking = oczekiwana zmiana / zakładany wysiłek (1 para, +0,5 sprzeczność, 1,5 karta przypisania)</>}>
    <mrow>
      <mi>p</mi><mo>=</mo><mfrac><mn>1</mn><mrow><mn>1</mn><mo>+</mo><msup><mn>2</mn><mrow><mo>−</mo><mo>(</mo><mi>e</mi><mo>−</mo><mn>10</mn><mo>)</mo></mrow></msup></mrow></mfrac>
      <mspace width="1.2em" />
      <mi>E</mi><mo>[</mo><mi>Δ</mi><mo>]</mo><mo>=</mo><mi>p</mi><mo>·</mo><msub><mi>Δ</mi><mtext>potw.</mtext></msub><mo>+</mo><mo>(</mo><mn>1</mn><mo>−</mo><mi>p</mi><mo>)</mo><mo>·</mo><msub><mi>Δ</mi><mtext>odrz.</mtext></msub>
    </mrow>
  </Formula>
);
