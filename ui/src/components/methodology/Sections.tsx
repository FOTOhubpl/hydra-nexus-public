/**
 * Static explainer content of /methodology. Source of truth: docs/METHODOLOGY.md (§1–§11a), docs/pitch/SLIDES.md
 * "Aneks: wzory a kod", hydra/entropy.py, hydra/synapse.py, hydra/nexus.py, hydra/assign.py, hydra/vis_geom.py,
 * hydra/queue_gain.py, docs/experiments/KONTRA_*.md. Definitions and code defaults only; no measured numbers.
 */
import { Link } from "react-router";
import { DecisionBadge, FamChip, HypothesisBadge } from "../evidence";
import { Code, MethodSection, Term } from "./Kit";
import { FBackground, FBits, FGroup, FQuality, FQueue, FSizeGuard, FSmooth, FSmoothBits, FSupport, FUEst } from "./Math";
import { RarityCalculator } from "./RarityCalculator";
import { smoothBounds, smoothWeight } from "./rarity";
import { plNumber } from "../../lib/evidence";

const STAGES = ["Bezpieczny import", "Genom incydentu", "Rzadkie geny w bitach", "Hipotezy par", "Kampanie i grupy", "Przypisanie otwartego zbioru", "Pakiet dowodowy"];

const T = {
  bit: "Bit dowodu: log₂ ilorazu wiarygodności zgodności wartości. Heurystyczna waga dowodu, nie prawdopodobieństwo.",
  gene: "Gen: jedna zaobserwowana cecha (rodzina, rodzaj, wartość lub wektor, jakość 0–1, status, pochodzenie i wskaźnik do pliku).",
  df: "Częstość dokumentowa: liczba incydentów (jednostek) z tą wartością, bez samej porównywanej pary.",
  tier: "Poziom genu (tier) ogranicza hipotezę, którą gen może wesprzeć: operator, platforma, kit, formułka, etykieta.",
  abstain: "Wstrzymanie: system nie zgaduje; przypadek trafia do kolejki analityka.",
};

export function Overview() {
  return (
    <MethodSection id="m-przeglad" n={1} title="Metoda w skrócie" lead={<>
      HYDRA rozkłada każdy <Term tip="Jedna zgłoszona reklama ze wszystkim, co o niej zapisano: zrzut lub wideo, strona docelowa, HAR, metadane, tekst, URL.">incydent</Term> na <Term tip={T.gene}>geny</Term>, waży wspólne wartości ich rzadkością w <Term tip={T.bit}>bitach</Term>, ocenia pary, grupuje je w kampanie i dla nowego materiału podejmuje jedną z trzech decyzji. Łączymy artefakty, nie sprawców. Brak obserwacji to „nieznane”, nigdy kara. Pełny, interaktywny przepływ: <Link to="/how" className="text-accent hover:underline">Jak to działa</Link>.
    </>} tech={<>
      <p>Moduły: <Code>hydra/adapters</Code>, <Code>hydra/genome</Code>, <Code>hydra/enrich</Code>, <Code>hydra/entropy</Code> (M2), <Code>hydra/retrieval</Code> (M3), <Code>hydra/synapse</Code> (M5), <Code>hydra/nexus</Code> (M6), <Code>hydra/assign</Code> (M9), <Code>hydra/pipeline</Code>; API <Code>hydra/api_task.py</Code>. Źródło: <Code>docs/METHODOLOGY.md</Code> §1–§2.</p>
      <p>Kandydaci par: unia indeksów <Code>exact:&lt;kind&gt;</Code>, <Code>near:&lt;kind&gt;</Code> (Hamming PDQ/pHash), <Code>knn:&lt;kind&gt;</Code> (dokładne top-k cosinusa, <Code>cosine_topk</Code>); do 60 incydentów oceniana jest każda para (<Code>all_pairs_below = 60</Code>).</p>
    </>}>
      <ol className="grid gap-2 sm:grid-cols-2 xl:grid-cols-4" aria-label="Siedem etapów">
        {STAGES.map((s, i) => (
          <li key={s} className="flex items-center gap-2 rounded-xl border border-line bg-raised/50 px-3 py-2">
            <span className="flex size-6 shrink-0 items-center justify-center rounded-full bg-accent-soft font-mono text-2xs font-semibold text-accent">{i + 1}</span>
            <span className="text-sm">{s}</span>
          </li>
        ))}
      </ol>
      <div className="flex flex-wrap gap-2" aria-label="Trzy decyzje">
        <DecisionBadge value="assign" /><DecisionBadge value="new_campaign" /><DecisionBadge value="abstain" />
      </div>
    </MethodSection>
  );
}

const FAMS: { f: string; what: string; ex: string; extr: string; tier: string }[] = [
  { f: "VIS", what: "kreacja, zrzuty, klatki wideo; w tej rodzinie także odcisk audio i synchronizacja AV", ex: "PDQ (8 wariantów dihedralnych), pHash, pHash kafelków, DINOv2, logo, odcisk audio", extr: "vis, logo, aud_fp, vid_avsync, dfk_image*, dfk_video*", tier: "zasób; reguły bliskich duplikatów 12 b; limit kampanii 8 b" },
  { f: "TXT", what: "OCR, tekst strony i platformy, czaty, transkrypcja mowy", ex: "linie OCR, MinHash 5-gramów, e5, telefon, IBAN, e-mail", extr: "txt_lines, txt_phrases, txt_entities, txt_embed, txt_chat, aud_asr", tier: "fraza (kampania); limit kampanii 8 b; tekst < 5 słów = 0 b" },
  { f: "INF", what: "URL, HAR, zapisany HTML, wzbogacanie (RDAP, DNS, crt.sh, ASN)", ex: "domena rejestrowalna, szkielet URL, tracker/piksel, łańcuch przekierowań, pliki kitu", extr: "inf_url, inf_har, inf_http, inf_dom, inf_kit, inf_kit_names, inf_enrich, inf_domain_pl, inf_text_hosts, inf_tollfraud, artifact_sha256", tier: "operator / platforma / kit / formułka; limit 24 b" },
  { f: "ID", what: "twarze w kreacjach i klatkach", ex: "YuNet + SFace (128-d); cos < 0,60 ignorowane", extr: "ident", tier: "persona / zasób; limit kampanii 6 b; sama twarz = wspólny zasób" },
  { f: "NAR", what: "geny wyliczone z tekstu", ex: "typ oszustwa, persona, obietnica, technika socjotechniczna", extr: "nar, nar_funnel", tier: "0 b do kampanii; sprzeczność typów −4 b" },
  { f: "META", what: ".info.json, metadane platformy, pochodzenie pliku", ex: "id reklamy/strony, reklamodawca, daty, C2PA, znaki wodne audio", extr: "meta, prov, dfk_audio_wm", tier: "operator dla dokładnych id; limit 24 b; DFK = etykieta 0 b" },
];

export function Genome() {
  return (
    <MethodSection id="m-genom" n={2} title="Genom incydentu" lead={<>
      Każdy plik incydentu przechodzi przez ekstraktory sześciu rodzin. Gen ma status (<i>zaobserwowano, niedostępne, błąd, nie dotyczy</i>), pochodzenie (OBS / INF / HYP) i wskaźnik do dowodu: SHA-256 pliku oraz, gdy istnieje, ramkę, zakres znaków, kod czasu, wpis HAR albo ścieżkę DOM. Ekstraktor, który nie zadziała, zapisuje „niedostępne”, nigdy nic.
    </>} tech={<>
      <p>Rejestr: <Code>hydra.genome.base.load_family_modules()</Code> + <Code>REGISTRY</Code>; CORE dokłada <Code>artifact_sha256</Code>. * = domyślnie wyłączone. Geny DFK (<Code>docs/genes/DFK.md</Code>) to poziom etykiety: 0 bitów łączenia.</p>
      <p>Poziom genu zapisany w <Code>dependency_group = "&lt;rodzina&gt;:&lt;poziom&gt;:&lt;nazwa&gt;"</Code>; <Code>synapse.level</Code>, <Code>gene_tier</Code>. Sekrety w materiale są redagowane (<Code>hydra/redact.py</Code>), dopasowanie działa przez solony skrót <Code>secret_fingerprint</Code>.</p>
    </>}>
      <div className="grid gap-3 md:grid-cols-2 xl:grid-cols-3">
        {FAMS.map((x) => (
          <article key={x.f} className="rounded-xl border border-line bg-raised/40 p-3">
            <FamChip family={x.f} withName />
            <p className="mt-1.5 text-xs text-muted">{x.what}</p>
            <dl className="mt-2 space-y-1 text-xs">
              <div><dt className="inline text-subtle">Przykładowe geny: </dt><dd className="inline">{x.ex}</dd></div>
              <div><dt className="inline text-subtle">Ekstraktory: </dt><dd className="inline font-mono text-2xs break-words">{x.extr}</dd></div>
              <div><dt className="inline text-subtle">Poziom: </dt><dd className="inline">{x.tier}</dd></div>
            </dl>
          </article>
        ))}
      </div>
    </MethodSection>
  );
}

const TIERS: [string, string, string][] = [
  ["operator", "ten sam kupujący, zespół lub konto: tracker/piksel, id konta reklamowego, UUID strony, hosty CRM", "pełne bity rzadkości; może wesprzeć „ta sama kampania”"],
  ["platforma", "ten sam produkt (cloaker, tracker, kreator stron) u wielu klientów", "0 b do kampanii (PLATFORM_KIT_CAP = 0); liczy się do grup"],
  ["kit", "skopiowany szablon lub zasoby podszycia: favicon, sprite reakcji, szkielet DOM", "0 b do kampanii; do grup; geny stylu ≤ 3 b"],
  ["formułka", "wspólne dla niepowiązanych stron: IP CDN, ASN, biblioteki, fonty", "0 b, tylko pokrycie"],
  ["etykieta", "przynależność do listy (CERT Polska), sygnały DFK", "0 b; atrybut, nigdy dowód łączenia"],
];

export function Rarity() {
  return (
    <MethodSection id="m-rzadkosc" n={3} title="Rzadkość i bity (HYDRA-RGE)" lead={<>
      Wspólna wartość jest tym mocniejszym dowodem, im jest rzadsza. Szablon lub darmowy hosting widziany w wielu incydentach waży mało; jeden IBAN widziany tylko w dwóch incydentach waży dużo. To waga zgodności w stylu Fellegi–Sunter z poprawką na częstość (jak w Splink), liczona w <Term tip={T.bit}>bitach</Term>. Bity <b>nie są prawdopodobieństwem</b>.
    </>} tech={<>
      <p><b>Para wyłączona</b>: <Term tip={T.df}>df</Term> liczy innych posiadaczy niż para. u₀(k) to założone priory per rodzaj (<Code>KIND_U0</Code>, np. tracker 2⁻¹⁴, domena 2⁻¹¹, szkielet URL 2⁻⁷, ASN 2⁻³); przy 10 incydentach dominuje prior, przy dużym korpusie liczności.</p>
      <p><b>Hub</b>: wartość u więcej niż max(5, ⌈f·N⌉) jednostek daje 0 b, f = 0,2 dla genów kitu i jednostek kampanii lub operatora, 0,5 dla pozostałych genów liczonych w jednostkach-incydentach (<Code>HUB_FRAC</Code>, <Code>HUB_FRAC_INCIDENTS</Code>). <b>Stop-geny</b> (CDN, fonty, biblioteki, placeholdery SDK, standardowe pola formularza) i tekst &lt; 5 słów: 0 b (<Code>is_stop</Code>, <Code>is_generic_text</Code>, <Code>is_placeholder</Code>).</p>
      <p><b>Wektory</b> (DINOv2, e5): tło = empiryczny rozkład cosinusów; u_pary = min(1, k·l·P_tło(sim ≥ s)), limit 10 b (<Code>Rarity.vector_bits</Code>, <Code>VECTOR_CAP</Code>). <b>Bliskie hashe</b>: PDQ ≤ 31/256, pHash ≤ 10/64, kafelki ≤ 6 (<Code>synapse.NEAR_HASH</Code>).</p>
      <p><b>Grupy zależności</b> (<Code>synapse.score_pair</Code>): jedna fotografia daje naraz PDQ, pHash i DINOv2, więc w grupie najsilniejszy gen liczy się w pełni, reszta × 0,25.</p>
      <p><b>Limity rodzin do kampanii</b> (<Code>FAMILY_CAMPAIGN_CAP</Code>): TXT 8, VIS 8, ID 6, NAR 0, INF i META 24; identyczny plik zwolniony. <b>Prior tła</b> domyślnie <Code>HYDRA_BACKGROUND_PRIOR=mixed</Code>; jednostronny z założenia: może tylko obniżyć bity wartości pospolitych w sieci.</p>
    </>}>
      <FUEst />
      <FBackground />
      <FBits />
      <FQuality />
      <FGroup />
      <div className="overflow-x-auto"><table className="w-full text-xs">
        <caption className="mb-1 text-left text-xs font-semibold text-fg">Poziomy genów (<Term tip={T.tier}>tier</Term>)</caption>
        <thead className="bg-raised text-2xs tracking-wider text-subtle"><tr><th className="px-2 py-1.5 text-left">poziom</th><th className="px-2 py-1.5 text-left">co znaczy wspólna wartość</th><th className="px-2 py-1.5 text-left">skutek</th></tr></thead>
        <tbody>{TIERS.map(([a, b, c]) => <tr key={a} className="border-t border-line"><td className="px-2 py-1 font-semibold">{a}</td><td className="px-2 py-1">{b}</td><td className="px-2 py-1">{c}</td></tr>)}</tbody>
      </table></div>
      <RarityCalculator />
    </MethodSection>
  );
}

export function Hypotheses() {
  const rows: [string, string][] = [
    ["same_campaign", "S ≥ 10 b (link) oraz dowód operatora INF/META ≥ 8 b albo co najmniej dwie rodziny po ≥ 8 b; identyczny plik i bliskie duplikaty (12 b) mogą łączyć same"],
    ["shared_asset", "bity zasobu (obrazy, twarze, logo) ≥ 6 b, a bity kampanii poza samym obrazem < 4 b"],
    ["shared_template", "bity poziomu kitu ≥ 6 b bez połączenia kampanii: link grupy kampanii"],
    ["abstain", "sprzeczność przy sumie ≥ 4 b albo suma ≥ 4 b z mieszanymi dowodami"],
    ["independent", "suma < 4 b (low)"],
  ];
  return (
    <MethodSection id="m-pary" n={4} title="Hipotezy par" lead={<>
      Każda para dostaje bity na rodzinę i dokładnie jedną hipotezę. Rozróżniamy wspólny zasób (ta sama twarz czy zdjęcie), wspólny szablon (ten sam kit lub agencja) i tę samą kampanię, bo dla analityka to różne rzeczy. Reguły sprawdzane są po kolei, pierwsza spełniona wygrywa.
    </>} tech={<>
      <p>Kolejność w <Code>synapse.score_pair</Code>: (1) sprzeczności → <Code>abstain</Code> gdy T ≥ low, inaczej <Code>independent</Code>; (2) S ≥ link ∧ (operator ≥ <Code>OPERATOR_STRONG_BITS = 8</Code> ∨ ≥ 2 rodziny ≥ <Code>STRONG_FAMILY_BITS = 8</Code>); (3) wspólny zasób; (4) kit ≥ 6; (5) T ≥ 4 → wstrzymanie; (6) niezależne. Progi: <Code>synapse.Thresholds</Code> link 10, kit 6, asset 6, low 4: domyślne heurystyki ustawione ręcznie w trakcie rozwoju na tych samych 10 incydentach próbki (in-sample, ograniczenie L2), a nie wynik niezależnej kalibracji; ewentualna kalibracja to osobny eksperyment z progami zamrożonymi przed testem.</p>
      <p><b>Sprzeczność</b>: różne wykryte typy oszustwa dają −4 b, gdy bity kampanii pary &lt; link i para nie jest prowadzona przez rzadki obraz (≥ 5 b); schemat „other” i schemat o jakości &lt; 0,5 nigdy nie są sprzecznością.</p>
      <p><b>Niepełna geometria</b> (CR-ASTRA-SW-01): weryfikacja ORB/MAGSAC ma budżet na proces (<Code>HYDRA_GEOM_BUDGET</Code>, domyślnie 100 000). Gdy budżet odmówi weryfikacji wymaganej dla pary, para nie dostaje bitów geometrii, ale dostaje notę 0 bitów <Code>geom_skipped</Code> („wynik niepełny”) i <Code>meta.geometry_incomplete</Code>, a hipoteza, która wyszłaby <Code>independent</Code>, jest zamieniana na <Code>abstain</Code> (wstrzymanie): brak weryfikacji nie jest dowodem niezależności. Inne hipotezy zostają bez zmian, weryfikacja wykonana bez dopasowania pozostaje zwykłym wynikiem negatywnym, a brak artefaktu to brak obserwacji. Osobno globalny licznik <Code>GET /v1/geometry/stats</Code> zwraca <Code>complete: false</Code>; to stan całego procesu, nie wynik konkretnej pary.</p>
      <p><Code>PairScore.probability</Code> to nieskalibrowana logistyka bitów (<Code>calibrated=false</Code>); UI pokazuje bity.</p>
    </>}>
      <FSupport />
      <ul className="space-y-2">{rows.map(([h, d]) => <li key={h} className="flex flex-col gap-1 sm:flex-row sm:items-start sm:gap-3"><span className="sm:w-56 sm:shrink-0"><HypothesisBadge value={h} /></span><span className="text-sm">{d}</span></li>)}</ul>
    </MethodSection>
  );
}

export function Grouping() {
  return (
    <MethodSection id="m-grupowanie" n={5} title="Grupowanie" lead={<>
      Kampanie to spójne składowe grafu par „ta sama kampania”, więc łańcuch wariantów, w którym pierwszy i ostatni nic bezpośrednio nie dzielą, zostaje razem. Grupy kampanii łączą kampanie o wspólnym zestawie narzędzi (kit) lub szablonie. Strażnik rozmiaru dzieli zbyt dużą i rzadką składową, bo przy progu jeden most potrafi skleić wszystko.
    </>} tech={<>
      <p><Code>hydra/nexus.py</Code> · <Code>cluster</Code>: składowe spójne na krawędziach <Code>same_campaign</Code> (must-link analityka z wagą 10⁶, cannot-link respektowane); <Code>split_component</Code>: Leiden (graspologic-native, MIT), awaryjnie Louvain z networkx, ważone bitami; podział zapisany w <Code>Campaign.meta.split</Code> i zgłaszany alertem.</p>
      <p><b>Grupy</b>: składowe nad kampaniami połączonymi parą <Code>shared_template</Code> lub bitami kitu ≥ 6 między członkami (<Code>is_linked</Code>); tylko geny tożsamości kitu (styl i NAR pokazane, nie łączą, E-SH-22); wartość kitu u &gt; 20 % korpusu = 0 b.</p>
      <p><b>Rdzeń genomu</b>: wartości u ≥ połowy członków i ≥ 2. <b>Wsparcie</b> (<Code>hydra/campaign_support.py</Code>): gęstość, mediana bitów linków, najsłabsze ogniwo (maksymalne drzewo rozpinające), <b>mosty</b> i członkowie-przeguby, stabilność bez jednej rodziny. <b>Raport spójności</b> (<Code>hydra/consistency.py</Code>, <Code>GET /v1/campaigns/{"{id}"}/consistency</Code>): domyślnie tylko raport; dzielenie za flagą <Code>HYDRA_ADV_GATE=1</Code>.</p>
    </>}>
      <FSizeGuard />
    </MethodSection>
  );
}

export function Assignment() {
  return (
    <MethodSection id="m-przypisanie" n={6} title="Przypisanie nowego materiału" lead={<>
      Nowy materiał jest oceniany w <Term tip="Zbiór otwarty: nowy incydent nie musi należeć do żadnej znanej kampanii.">zbiorze otwartym</Term>: albo trafia do znanej kampanii, albo jest nowym wzorcem, albo system się <Term tip={T.abstain}>wstrzymuje</Term>. Rzadkość jest przeliczana z nowym incydentem, a jego własne wartości nie czynią się pospolitymi.
    </>} tech={<>
      <p><Code>hydra/assign.py</Code> · <Code>campaign_supports</Code> (s_c = najlepsze wsparcie pary z członkiem, ze sprzecznościami) i <Code>_decide</Code>; <Code>MARGIN = 3</Code>, <Code>ABSTAIN_MIN = 6</Code>. Wejścia: plik, folder, ZIP, URL, zrzut z linkiem, podpowiedź (<Code>hydra.pipeline.analyse_input</Code>). Materiał bez genów poza skrótem pliku: wstrzymanie, nigdy „nowy wzorzec”.</p>
      <p><b>Konformal</b> (<Code>hydra/conformal.py</Code> · <Code>apply_conformal</Code>, <Code>eligible</Code>): ε = 0,1, decyduje dopiero od 20 elementów kalibracji (<Code>MIN_CALIBRATION</Code>); wtedy tylko potwierdza lub obniża, nigdy nie promuje. Na próbce (5 elementów) zawsze decyduje próg.</p>
      <p><b>Partia</b> (<Code>hydra/batch.py</Code>): pozycje „nowa kampania” są oceniane między sobą i grupowane; wspólny wzorzec dostaje <Code>new_pattern_id = N-&lt;sha8 członków&gt;</Code>. Decyzje assign/abstain nie są zmieniane. <b>Duplikaty</b>: ten sam zestaw artefaktów (SHA-256) → <Code>duplicate_of</Code>; ponowny plik używa istniejącego incydentu.</p>
    </>}>
      <ol className="space-y-2">
        <li className="flex flex-col gap-1 sm:flex-row sm:gap-3"><span className="sm:w-64 sm:shrink-0"><DecisionBadge value="assign" /></span><span>najlepsza para z kampanią to „ta sama kampania” i s₁ ≥ 10 b; ale gdy druga kampania też ma s₂ ≥ 10 b i s₁ − s₂ &lt; 3 b → wstrzymanie</span></li>
        <li className="flex flex-col gap-1 sm:flex-row sm:gap-3"><span className="sm:w-64 sm:shrink-0"><DecisionBadge value="abstain" /></span><span>szara strefa: hipoteza pary „wstrzymane”, bity kampanii ≥ 6, bez sprzeczności, z dowodem operatora albo dwiema rodzinami; także most przez rozdzielenie analityka (cannot-link)</span></li>
        <li className="flex flex-col gap-1 sm:flex-row sm:gap-3"><span className="sm:w-64 sm:shrink-0"><DecisionBadge value="new_campaign" /></span><span>w pozostałych przypadkach; gdy bity kitu ≥ 6 wiążą z grupą, <Code>group_id</Code> mówi „nowa kampania, ten sam zestaw narzędzi”</span></li>
      </ol>
    </MethodSection>
  );
}

function SmoothPlot() {
  const n = 100;
  const { L, U } = smoothBounds(n);
  const hs = Array.from({ length: 60 }, (_, i) => i + 1);
  const W = 320, H = 110, px = 24, py = 10;
  const x = (h: number) => px + ((h - 1) / 59) * (W - px - 6);
  const y = (w: number) => H - py - 12 - w * (H - 2 * py - 12);
  return (
    <svg viewBox={`0 0 ${W} ${H}`} className="h-auto w-full max-w-md" role="img" aria-label={`Waga w w zależności od liczby posiadaczy h przy n = ${n}: 1 do L = ${L}, liniowo do U = ${U}, potem 0`}>
      {[0, 1].map((w) => <g key={w}><line x1={px} x2={W - 6} y1={y(w)} y2={y(w)} stroke="var(--color-line)" /><text x={px - 4} y={y(w) + 3} textAnchor="end" fontSize={9} fill="var(--color-subtle)">{w}</text></g>)}
      <polyline fill="none" stroke="var(--color-for)" strokeWidth={2} points={hs.map((h) => `${x(h)},${y(smoothWeight(n, h))}`).join(" ")} />
      {[L, U].map((v, i) => <g key={v}><line x1={x(v)} x2={x(v)} y1={py} y2={H - py - 12} stroke="var(--color-subtle)" strokeDasharray="3 3" /><text x={x(v)} y={H - 2} textAnchor="middle" fontSize={9} fill="var(--color-muted)">{i ? "U" : "L"} = {v}</text></g>)}
    </svg>
  );
}

export function SmoothUnits() {
  const { L, U } = smoothBounds(100);
  return (
    <MethodSection id="m-operator" n={7} title="Jednostki operatora „smooth” (domyślne)" lead={<>
      Gdy kilka incydentów jednej nieznanej kampanii dzieli identyfikator operatora (tracker, telefon, konto), liczenie ich osobno sztucznie obniżałoby rzadkość. Dlatego takie incydenty tworzą jedną jednostkę. Zamiast twardego progu waga tej reguły maleje liniowo z liczbą posiadaczy h: przy n = 100 wynosi 1 do L = {L}, {plNumber(smoothWeight(100, 35), 2)} przy h = 35 i 0 powyżej U = {U}.
    </>} tech={<>
      <p><Code>entropy.operator_units_mode</Code> (domyślnie <Code>smooth</Code> od commita 1c12321, <Code>HYDRA_OPERATOR_UNITS=legacy</Code> przywraca twardy limit C1). Wartości u 2 ≤ h ≤ L (nie „rozproszone”) łączą się w jednostki; L &lt; h ≤ U: bez łączenia, bity mieszane wagą w; powyżej limitu huba: 0 b.</p>
      <p><b>Reguła rozproszenia</b>: wartość u 6..U incydentów, których posiadacze należą do ≥ 3 (<Code>SPREAD_MIN_CAMPAIGNS</Code>) różnych wieloincydentowych jednostek innych wartości operatora, jest wartością międzyoperatorską (SDK, wspólne konto platformy): bez jednostki, bez wagi. W przypisaniu identyfikatory kont liczą znane kampanie, nie ich członków (E-SH-15).</p>
      <p>Na <Code>sample</Code> i <Code>sample+syn</Code> tryb smooth nic nie zmienia (0 wartości miękkich i rozproszonych), więc nie deklarujemy zysku na danych rzeczywistych (SPEC_ERRATA E-SH-24).</p>
    </>}>
      <FSmooth />
      <FSmoothBits />
      <SmoothPlot />
    </MethodSection>
  );
}

export function Geometry() {
  return (
    <MethodSection id="m-geometria" n={8} title="Geometria i przycięcia" lead={<>
      Hashe percepcyjne tanio wykrywają prawie identyczne obrazy, ale kreacja przycięta o 6–12 % i przeskalowana im ucieka. Dla par wizualnie bliskich, których hashe nie złapały, HYDRA dopasowuje punkty kluczowe i sprawdza, czy jeden obraz jest przycięciem drugiego. Widok „Co się zmieniło” rysuje dopasowania między miniaturami.
    </>} tech={<>
      <p><Code>hydra/vis_geom.py</Code>: pary jednostek po bramkach klasy i stocku z DINOv2 cos ≥ 0,80 (<Code>GEOM_COS_MIN</Code>), bez dopasowania hashem; ≤ 4 na parę incydentów. ORB (2000 punktów, test Lowe 0,75) z próbą lustrzaną, homografia <Code>cv2.USAC_MAGSAC</Code> (5 px); przy &lt; 120 inlierach jedna próba RootSIFT.</p>
      <p><b>same_asset</b>: ≥ 120 inlierów i pokrycie otoczki ≥ 15 % obu obrazów. <b>crop_match</b>: same_asset, prawie-podobieństwo (stosunek wartości osobliwych ≤ 1,25, człon rzutowy ≤ 0,15, bez lustra), pokrycie ≥ 0,25 i zajętość siatki 8×8 ≥ 0,30 po obu stronach.</p>
      <p>Flagi: <Code>HYDRA_ADV_CROP</Code> domyślnie włączona (od 2026-11-22; przycięcie liczy się jak dopasowanie podokna w regule bliskiego duplikatu), <Code>HYDRA_ADV_GEOM=1</Code> (powód zasobu 4 b, nigdy bity kampanii) i <Code>HYDRA_ADV_HSV=1</Code> domyślnie wyłączone. Budżet <Code>HYDRA_GEOM_BUDGET</Code> = 100 000 weryfikacji na proces; pominięte są liczone, para z pominiętą weryfikacją dostaje notę „wynik niepełny” i zamiast <Code>independent</Code> wychodzi jako <Code>abstain</Code>, a <Code>/v1/geometry/stats</Code> zgłasza <b>niepełną analizę</b> procesu (<Code>complete: false</Code>). Wyniki zapamiętywane w <Code>&lt;state&gt;/work/vis_geom/</Code>; ORB i MAGSAC++ deterministyczne.</p>
    </>} />
  );
}

export function QueueKontra() {
  return (
    <MethodSection id="m-kolejka" n={9} title="Kolejka analityka i KONTRA" lead={<>
      Kolejka ustawia na początku te przypadki, których rozstrzygnięcie zmieni najwięcej decyzji o współprzynależności na jednostkę pracy analityka. KONTRA („Laboratorium hipotez”) pozwala sprawdzić, co stałoby się z decyzją, gdyby usunąć część dowodów. Oba narzędzia porządkują i wyjaśniają; nie zmieniają kampanii same z siebie.
    </>} tech={<>
      <p><Code>hydra/queue_gain.py</Code> (<Code>order=gain</Code>, domyślnie): kontrfaktycznie liczy zmienione decyzje: odrzucenie w kampanii = minimalne ważone cięcie (must-link nie do cięcia), potwierdzenie między kampaniami = |A|·|B|, wstrzymane przypisanie = 1 + |C|. Prawdopodobieństwo p jest nieskalibrowane. Starsza kolejność <Code>order=ev</Code> = niepewność × wpływ. Kolejność to heurystyka, nie zmierzony zysk; pomiary krzywych są syntetyczne (<Code>docs/eval/QUEUE_CURVE.md</Code>).</p>
      <p><b>KONTRA</b> (<Code>hydra/kontra.py</Code> · <Code>run_experiment</Code>): tryb cienia, nie zapisuje genów, kampanii ani przypisań. Scenariusze wyłącznie usuwające (grupa, rodzina, artefakt, wykluczenie kampanii, poszukiwanie zbioru zmiany), każdy to prawdziwe przeliczenie. Zbiór zmiany jest zachłanny (wystarczający, nie minimalny); <Code>scenario_stability_fraction</Code> to statystyka skończonego katalogu, nie pewność. Ograniczenia: <Code>docs/experiments/KONTRA_LIMITATIONS.md</Code>.</p>
    </>}>
      <FQueue />
      <p className="text-xs text-muted">Kolejka: <Link to="/queue" className="text-accent hover:underline">Kolejka analityka</Link>. KONTRA otwiera się z karty incydentu (<Code>/lab/&lt;incydent&gt;</Code>).</p>
    </MethodSection>
  );
}

const GLOSSARY: [string, string][] = [
  ["bit dowodu", "log₂ ilorazu wiarygodności zgodności wartości; heurystyczna waga, nie prawdopodobieństwo"],
  ["df", "częstość dokumentowa: ilu incydentów (jednostek) ma wartość, bez pary"],
  ["u₀", "założony prior per rodzaj genu: szansa, że niepowiązany incydent ma tę wartość"],
  ["hub", "wartość u zbyt wielu jednostek; 0 bitów"],
  ["grupa zależności", "geny z jednej obserwacji; liczy się najsilniejszy, reszta × 0,25"],
  ["poziom (tier)", "operator, platforma, kit, formułka, etykieta: ogranicza możliwą hipotezę"],
  ["wsparcie S", "bity poziomu kampanii pary (+ połowa bitów zasobu), porównywane z progiem 10 b"],
  ["most", "link, którego usunięcie dzieli kampanię ≥ 3 członków"],
  ["wstrzymanie", "brak decyzji automatycznej; przypadek do analityka"],
  ["nowy wzorzec", "nowy materiał niepasujący do znanej kampanii; w partii wspólny new_pattern_id"],
  ["konformal", "predyktor zbioru (ε = 0,1); od 20 elementów kalibracji tylko potwierdza lub obniża"],
  ["LOO", "leave-one-out: każdy incydent po kolei usuwany i przypisywany z powrotem"],
];

export function Limits() {
  return (
    <MethodSection id="m-ograniczenia" n={11} title="Ograniczenia i słownik pojęć" lead={<>
      HYDRA nie wskazuje sprawców i nie wykrywa deepfake'ów. Etykiety ewaluacji to hipoteza zespołu, progi ustalono na tych samych 10 incydentach próbki, a infrastruktura niesie większość decyzji kampanii. Lista ograniczeń z numerami i źródłami: <Code>docs/release/KNOWN_LIMITATIONS.md</Code>.
    </>} tech={<>
      <p>Wybrane pozycje: L1 etykiety zespołu (brak drugiego niezależnego recenzenta); L2 progi in-sample; L4 konformal nieaktywny poniżej 20 elementów; L5 brak wykresu niezawodności, każde prawdopodobieństwo ma <Code>calibrated=false</Code>; L9 przycięte kreacje i zrzuty stron docelowych bywają „nową kampanią”; L11 domeny za Cloudflare czynią IP i ASN nieinformacyjnymi; L12 KONTRA bez wykazanej przewagi; L33 brak atrybucji.</p>
    </>}>
      <dl className="grid gap-x-6 gap-y-2 sm:grid-cols-2" aria-label="Słownik pojęć">
        {GLOSSARY.map(([k, v]) => <div key={k} className="border-t border-line pt-2"><dt className="text-sm font-semibold">{k}</dt><dd className="text-xs text-muted">{v}</dd></div>)}
      </dl>
    </MethodSection>
  );
}
