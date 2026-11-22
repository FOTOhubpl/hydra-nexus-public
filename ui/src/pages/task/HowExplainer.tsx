/**
 * "Jak to działa" explainer (docs/ui/UI_LOG.md, 2026-11-22 UI-HOW): hero, animated pipeline flow with a clickable
 * stepper, bento stage cards, gene families and the three assignment decisions. The text is a static definition
 * taken from docs/METHODOLOGY.md; the only numbers that are not definitions come from GET /v1/system/flow (`b`).
 * Motion: CSS transforms / opacity only, paused off screen, none under prefers-reduced-motion. Glass surfaces fall
 * back to opaque under prefers-reduced-transparency (index.css `.how-*`).
 */
import { useCallback, useEffect, useRef, useState, type KeyboardEvent, type PointerEvent, type ReactNode } from "react";
import { Link } from "react-router";
import type { SystemFlow } from "../../api/visual";
import { DecisionBadge } from "../../components/evidence";
import { InfoTip, Tooltip } from "../../components/Tooltip";
import { cx } from "../../lib/cx";
import { DECISION_LABEL, FAMILY_VAR, HYPOTHESIS_LABEL } from "../../lib/evidence";
import { formatCount } from "../../lib/format";
import { useMediaQuery, useReducedMotion } from "../../lib/media";

type Boxes = NonNullable<SystemFlow["boxes"]>;

const sum = (o: Record<string, number> | undefined) => Object.values(o ?? {}).reduce((s, v) => s + v, 0);

/* ---------- icons (inline SVG, 24 px grid, currentColor) ---------- */

const ICON: Record<string, ReactNode> = {
  import: <><path d="M12 3v11" /><path d="m7.5 9.5 4.5 4.5 4.5-4.5" /><path d="M4 15v3a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2v-3" /></>,
  genome: <><path d="M7 3c0 6 10 6 10 12s-10 3-10 6" /><path d="M17 3c0 6-10 6-10 12" /><path d="M9 6.5h6M8 11h8M9 17.5h6" /></>,
  rarity: <><path d="M4 20h16" /><rect x="5" y="12" width="3" height="6" rx="1" /><rect x="10.5" y="8" width="3" height="10" rx="1" /><rect x="16" y="4" width="3" height="14" rx="1" /></>,
  pairs: <><circle cx="7" cy="8" r="3" /><circle cx="17" cy="16" r="3" /><path d="m9.4 10 5.2 4" /><path d="M14 5h5v5" /></>,
  campaigns: <><circle cx="12" cy="5" r="2.2" /><circle cx="5" cy="17" r="2.2" /><circle cx="19" cy="17" r="2.2" /><path d="M11 7 6 15M13 7l5 8M7.2 17h9.6" /></>,
  assign: <><path d="M4 12h7" /><path d="M11 12 19 5M11 12h8M11 12l8 7" /><circle cx="4" cy="12" r="1.6" /></>,
  evidence: <><path d="M5 4h9l5 5v11H5z" /><path d="M14 4v5h5" /><path d="m9 14 2 2 4-4" /></>,
};

function StageIcon({ name, className }: { name: string; className?: string }) {
  return (
    <svg viewBox="0 0 24 24" width={20} height={20} fill="none" stroke="currentColor" strokeWidth={1.7} strokeLinecap="round" strokeLinejoin="round" aria-hidden="true" className={className}>
      {ICON[name]}
    </svg>
  );
}

/* ---------- static stage definitions (docs/METHODOLOGY.md §2–§14) ---------- */

interface Stage {
  key: string;
  short: string;
  name: string;
  does: string;
  input: string;
  output: string;
  guards: string[];
  tip: ReactNode;
  to: string;
  span: string;
  live?: (b: Boxes) => string;
}

const STAGES: Stage[] = [
  {
    key: "import", short: "Import", name: "Bezpieczny import", to: "/analyse", span: "",
    does: "Przyjmuje folder, ZIP, URL, obraz, wideo, tekst lub log. Każdy plik dostaje status, rolę i skrót SHA-256; identyczne bajty są zapisywane raz.",
    input: "folder · ZIP · URL · obraz · wideo · tekst / log",
    output: "incydent i jego artefakty z rolą i statusem",
    guards: ["oryginały tylko do odczytu (magazyn SHA-256)", "nic nie jest wykonywane: HTML i SVG to dane", "ZIP: ochrona przed path traversal, symlinkami i bombami"],
    tip: <>Jednostką analizy jest <b>incydent</b>: jedna zgłoszona reklama ze wszystkim, co o niej zapisano (zrzut, wideo, strona docelowa, HAR, metadane). Typ pliku jest rozpoznawany po bajtach, nie po rozszerzeniu.</>,
    live: (b) => `${formatCount(b.input.incidents)} incydentów · ${formatCount(b.adapters.artifacts)} artefaktów`,
  },
  {
    key: "genome", short: "Genom", name: "Genom incydentu", to: "/incidents", span: "xl:col-span-2",
    does: "Ekstraktory sześciu rodzin zamieniają artefakty w geny: wartość, jakość, status i wskaźnik do dowodu. Opcjonalne wzbogacanie (RDAP, DNS, certyfikaty) dokłada fakty o hostach.",
    input: "artefakty incydentu",
    output: "geny z pochodzeniem OBS / INF / HYP",
    guards: ["ekstraktor, który nie zadziała, zapisuje „niedostępne”, nigdy nic", "każdy gen wskazuje plik (SHA-256) i miejsce: ramkę, fragment tekstu, kod czasu, wpis HAR", "wzbogacanie domyślnie z cache (replay), bez sieci"],
    tip: <><b>Gen</b> to jedna cecha: rodzina, rodzaj, wartość lub wektor, jakość 0–1, status (zaobserwowano / niedostępne / błąd / nie dotyczy) i pochodzenie. Wzbogacanie pyta tylko o hosty i adresy IP znalezione w materiale; żaden plik nie opuszcza maszyny.</>,
    live: (b) => `${formatCount(b.genes.total)} genów`,
  },
  {
    key: "rarity", short: "Bity", name: "Rzadkie geny w bitach", to: "/simulation", span: "",
    does: "Im rzadsza wartość w korpusie, tym więcej bitów dowodu daje, gdy dzielą ją dwa incydenty. Waga zgodności w stylu Fellegi–Sunter, ucięta do 0–14 bitów.",
    input: "geny i częstość wartości w korpusie",
    output: "bity dowodu na wartość genu",
    guards: ["bity to heurystyka, nie prawdopodobieństwo", "wartości pospolite (CDN, biblioteki, formułki) = 0 bitów", "platforma i zestaw (kit) = 0 bitów do kampanii, tylko do grup"],
    tip: <><span className="font-mono">bity(v) = clip(log2(m / u(v)), 0, 14)</span>, m = 0,5. u(v) liczone bez samej pary, z priorem na rodzaj genu (założenie, nie dopasowanie). Wartość ze stu incydentów to słaby dowód, wartość z dwóch to mocny.</>,
    live: (b) => `${formatCount(sum(b.rarity.distinct_values_by_level))} różnych wartości`,
  },
  {
    key: "pairs", short: "Pary", name: "Hipotezy par", to: "/pairs", span: "",
    does: "Indeksy kandydatów (dokładne wartości, bliskie hashe, najbliżsi sąsiedzi embeddingów) proponują pary; każda para dostaje bity na rodzinę i jedną hipotezę.",
    input: "pary kandydatów z indeksów",
    output: `${HYPOTHESIS_LABEL.same_campaign} · ${HYPOTHESIS_LABEL.shared_template} · ${HYPOTHESIS_LABEL.shared_asset} · ${HYPOTHESIS_LABEL.independent} · wstrzymane`,
    guards: ["brak obserwacji = nieznane, nie kara", "wspólny zasób (ta sama twarz, obraz) to nie ta sama kampania", "progi to ręcznie ustawione heurystyki rozwijane na próbce, nie wynik kalibracji"],
    tip: <>Progi domyślne w bitach: <b>10</b> wiąże kampanię, <b>6</b> poziomu zestawu daje wspólny szablon, <b>6</b> samych zasobów daje wspólny zasób, poniżej <b>4</b> niezależne; pomiędzy, przy mieszanych dowodach: wstrzymanie.</>,
    live: (b) => `${formatCount(b.scoring.pairs_scored)} ocenionych par`,
  },
  {
    key: "campaigns", short: "Kampanie", name: "Kampanie i grupy", to: "/campaigns", span: "",
    does: "Kampanie to spójne składowe par „ta sama kampania”, więc łańcuch wariantów zostaje razem. Grupy łączą kampanie o wspólnym zestawie lub szablonie.",
    input: "hipotezy par",
    output: "kampanie, grupy kampanii, rdzeń genomu",
    guards: ["strażnik rozmiaru: zbyt duża i rzadka składowa jest dzielona i zgłaszana", "łączymy artefakty, nie sprawców", "siła w bitach i najsłabsze ogniwo, bez procentów"],
    tip: <><b>Rdzeń genomu</b> to wartości wspólne dla co najmniej połowy członków (i co najmniej dwóch). Mosty i członkowie-przeguby pokazują, gdzie kampania jest krucha; test bez jednej rodziny pokazuje, czy trzyma się bez niej.</>,
    live: (b) => `${formatCount(b.clustering.campaigns)} kampanii · ${formatCount(b.clustering.groups)} grup`,
  },
  {
    key: "assign", short: "Przypisanie", name: "Przypisanie otwartego zbioru", to: "/analyse", span: "xl:col-span-2",
    does: "Nowy materiał jest oceniany względem kampanii przebiegu: przypisanie do znanej kampanii, nowa kampania (nowy wzorzec) albo wstrzymanie do decyzji analityka.",
    input: "nowy incydent i kampanie przebiegu",
    output: "decyzja z najlepszą parą, bitami i powodem",
    guards: ["konformal nigdy nie promuje: tylko potwierdza albo obniża", "bramka dowodowa: ≥ 10 bitów, bez sprzeczności, operator lub dwie rodziny", "most przez rozdzielenie analityka (cannot-link) = wstrzymanie"],
    tip: <>Predyktor konformalny (ε = 0,1) decyduje dopiero od 20 elementów kalibracji; poniżej decyduje reguła progowa. Jego gwarancja jest marginalna i zakłada wymienialność. Weryfikacja: przypisanie leave-one-out na incydentach przebiegu.</>,
    live: (b) => `${formatCount(sum(b.assignment.by_decision))} przypisań w przebiegu`,
  },
  {
    key: "evidence", short: "Dowody", name: "Pakiet dowodowy", to: "/graph", span: "md:col-span-2 xl:col-span-4",
    does: "Każde powiązanie ma wodospad bitów, geny ze wskaźnikami dowodu, sprzeczności i brakujące rodziny. Eksporty: zgłoszenie nadużycia (XARF), STIX 2.1, graf Neo4j, wzbogacony zbiór danych.",
    input: "kampanie, pary, przypisania i recenzje",
    output: "pliki do pobrania z odnośnikami do dowodów",
    guards: ["nic nie jest wysyłane: analityk pobiera i działa sam", "sekrety i dane osobowe redagowane w eksportach", "eksport czyta tylko cache wzbogaceń, bez sieci"],
    tip: <>Recenzje analityka (akceptuj / odrzuć / niepewne) tworzą nową rewizję przebiegu zamiast nadpisywać poprzednią; oryginały w magazynie pozostają bajt w bajt jako dowód.</>,
    live: (b) => `${formatCount(b.outputs.exports?.total ?? 0)} eksportów zapisanych`,
  },
];

/* ---------- gene families and extra modalities (static definitions) ---------- */

interface Fam { code: string; name: string; src: string; ex: string; why: string; color?: string }

const FAMS: Fam[] = [
  { code: "VIS", name: "obraz", src: "kreacje, zrzuty ekranu, klatki wideo (1 kl./s i cięcia scen)", ex: "PDQ w 8 wariantach obrotu i odbicia, pHash, embedding DINOv2, dopasowania logo", why: "Ta sama kreacja wraca przycięta, przebarwiona, odbita albo z nakładką." },
  { code: "TXT", name: "tekst", src: "OCR obrazów, tekst strony i platformy, wklejone logi i czaty", ex: "MinHash 5-gramów, embedding e5, encje: telefon, IBAN, e-mail, kody promocyjne", why: "Rzadka encja (jeden IBAN) to mocny dowód; formułka typu „kliknij teraz” nie." },
  { code: "INF", name: "infrastruktura", src: "URL-e, log HAR, zapisany HTML; opcjonalnie RDAP, DNS, certyfikaty", ex: "domena rejestrowalna, szkielet etykiet URL, trackery i piksele, przekierowania, pliki zestawu", why: "Operatorzy rotują domeny, ale zostawiają zestawy, trackery i hosting. Każdy gen ma warstwę: operator, platforma, kit, boilerplate." },
  { code: "ID", name: "tożsamość", src: "twarze w kreacjach i klatkach", ex: "detekcja YuNet i embedding SFace", why: "Wizerunek osób publicznych wraca w wielu kampaniach; sam w sobie to wspólny zasób, nie ta sama kampania." },
  { code: "NAR", name: "narracja", src: "geny tekstu", ex: "typ oszustwa (inwestycje, zdrowie, rozdawnictwo…), persona, obietnica, techniki: pilność, autorytet, dowód społeczny", why: "Grupuje podobne kampanie do widoku „podobne”; 0 bitów do kampanii." },
  { code: "META", name: "metadane", src: "metadane platformy, pliki .info.json", ex: "id reklamy i strony, reklamodawca, daty", why: "Dokładne id to mocny dowód; daty dają pierwsze i ostatnie wystąpienie." },
];

const MODS: Fam[] = [
  { code: "audio", name: "ścieżka dźwiękowa", src: "wideo i nagrania ekranu", ex: "odcisk ścieżki (aud_fp, w VIS) i transkrypcja mowy (aud_asr, w TXT)", why: "Sygnał ponownego użycia głosu lub muzyki; nie rozpoznaje mówcy i nie wykrywa syntezy mowy.", color: "var(--color-fam-vis)" },
  { code: "video", name: "wideo", src: "reklamy wideo, nagrania ekranu", ex: "klatki 1 kl./s i cięcia scen jako geny VIS; vid_avsync: zgodność dźwięku z obrazem", why: "Ta sama kreacja w innym montażu nadal dzieli klatki; zgodność A/V to sygnał, nie werdykt.", color: "var(--color-fam-vis)" },
  { code: "prov", name: "pochodzenie pliku", src: "obrazy i wideo", ex: "manifest C2PA, znaki wodne generatorów, metadane generatora", why: "Fakty o pliku, nie werdykt; 0 bitów do powiązań; brak znaku nie świadczy o autentyczności.", color: "var(--color-fam-meta)" },
  { code: "dfk", name: "sygnały manipulacji", src: "obraz, wideo, znak wodny audio", ex: "dfk_image, dfk_video (domyślnie wyłączone), dfk_audio_wm", why: "Warstwa etykiet: 0 bitów do powiązań. To sygnał podejrzenia, nie detekcja deepfake.", color: "var(--color-unknown)" },
];

/* ---------- decisions ---------- */

const DECISIONS: { value: "assign" | "new_campaign" | "abstain"; when: string; then: string }[] = [
  { value: "assign", when: "Najlepsza kampania ma parę „ta sama kampania” (≥ 10 bitów), a żadna rywalka nie jest bliżej niż 3 bity.", then: "Incydent dołącza do kampanii; widać najlepszą parę i jej wodospad bitów." },
  { value: "new_campaign", when: "Żadna znana kampania nie ma wystarczających dowodów.", then: "Nowy wzorzec. Gdy dowody zestawu wiążą go z grupą: nowa kampania, ten sam zestaw narzędzi." },
  { value: "abstain", when: "Dwie kampanie są zbyt blisko, dowody są w szarej strefie (4–10 bitów) lub sprzeczne, albo materiał nie ma genów poza skrótem pliku.", then: "Decyzję podejmuje analityk; system nie zgaduje." },
];

/* ---------- pieces ---------- */

/** Spotlight follows the pointer through CSS variables (no React render per move). */
function spot(e: PointerEvent<HTMLElement>) {
  const el = e.currentTarget, r = el.getBoundingClientRect();
  el.style.setProperty("--mx", `${e.clientX - r.left}px`);
  el.style.setProperty("--my", `${e.clientY - r.top}px`);
}

function Connector({ on, vertical }: { on: boolean; vertical: boolean }) {
  return (
    <span aria-hidden="true" data-on={on} className={cx("how-link", vertical ? "how-link-y" : "how-link-x")}>
      <span className="how-link-dash how-anim" />
      <span className="how-link-dot how-anim"><span /></span>
    </span>
  );
}

function Flow({ active, onPick, b }: { active: number; onPick: (i: number, focusCard?: boolean) => void; b?: Boxes }) {
  const wide = useMediaQuery("(min-width: 64rem)", true);
  const ref = useRef<HTMLDivElement>(null);
  const btns = useRef<(HTMLButtonElement | null)[]>([]);
  const [visible, setVisible] = useState(true);
  useEffect(() => {
    const el = ref.current;
    if (!el || typeof IntersectionObserver === "undefined") return;
    const io = new IntersectionObserver(([e]) => setVisible(!!e?.isIntersecting));
    io.observe(el);
    return () => io.disconnect();
  }, []);
  const key = (e: KeyboardEvent<HTMLButtonElement>, i: number) => {
    const next = e.key === "ArrowRight" || e.key === "ArrowDown" ? i + 1 : e.key === "ArrowLeft" || e.key === "ArrowUp" ? i - 1 : e.key === "Home" ? 0 : e.key === "End" ? STAGES.length - 1 : null;
    if (next == null) return;
    e.preventDefault();
    const n = (next + STAGES.length) % STAGES.length;
    onPick(n);
    btns.current[n]?.focus();
  };
  const s = STAGES[active]!;
  return (
    <section aria-labelledby="how-flow-h" className="how-glass rounded-[calc(var(--radius-card)+4px)] p-4 sm:p-6">
      <div className="mb-4 flex flex-wrap items-baseline gap-x-3 gap-y-1">
        <h2 id="how-flow-h" className="text-lg font-semibold tracking-tight">Przepływ potoku</h2>
        <p className="text-xs text-subtle">Wybierz etap (kliknięcie, strzałki) albo przewiń karty niżej; aktywny etap świeci w obu miejscach.</p>
      </div>
      <div ref={ref} className="how-flow" data-paused={!visible}>
        <ol aria-label="Etapy przepływu" className={cx(wide ? "flex items-center" : "flex flex-col")}>
          {STAGES.map((st, i) => (
            <li key={st.key} className={cx(wide ? "contents" : "flex flex-col")}>
              {i > 0 && <Connector on={i <= active} vertical={!wide} />}
              <button
                ref={(el) => { btns.current[i] = el; }}
                type="button"
                aria-current={i === active ? "step" : undefined}
                aria-controls={`how-stage-${st.key}`}
                tabIndex={i === active ? 0 : -1}
                onClick={() => onPick(i, true)}
                onPointerEnter={(e) => { if (e.pointerType === "mouse") onPick(i); }}
                onKeyDown={(e) => key(e, i)}
                className={cx("how-node group", wide ? "w-[clamp(5.75rem,8vw,7.75rem)] flex-none flex-col text-center" : "w-full flex-row py-2 text-left")}
                data-active={i === active}
                data-done={i < active}
              >
                <span className="how-node-icon"><StageIcon name={st.key} /></span>
                <span className="min-w-0">
                  <span className="block font-mono text-2xs text-subtle">{String(i + 1).padStart(2, "0")}</span>
                  <span className="block text-sm leading-tight font-semibold">{wide ? st.short : st.name}</span>
                </span>
              </button>
            </li>
          ))}
        </ol>
      </div>
      <div className="mt-5 grid gap-3 rounded-xl border border-line bg-raised/60 p-4 sm:grid-cols-[1fr_auto]" aria-live="polite">
        <div className="min-w-0">
          <div className="flex items-center gap-2 text-sm font-semibold"><span className="font-mono text-xs text-accent">{String(active + 1).padStart(2, "0")}</span>{s.name}</div>
          <p className="mt-1 text-sm text-muted">{s.does}</p>
        </div>
        {b && s.live && <div className="self-start rounded-full border border-line px-2.5 py-1 font-mono text-xs whitespace-nowrap text-fg">{s.live(b)}</div>}
      </div>
    </section>
  );
}

function StageCard({ st, i, active, onHover, b }: { st: Stage; i: number; active: boolean; onHover: () => void; b?: Boxes }) {
  return (
    <article
      id={`how-stage-${st.key}`}
      data-stage={i}
      data-active={active}
      tabIndex={-1}
      aria-labelledby={`how-stage-${st.key}-h`}
      onPointerMove={spot}
      onPointerEnter={(e) => { if (e.pointerType === "mouse") onHover(); }}
      className={cx("how-card flex flex-col gap-3 p-5 outline-none", st.span)}
    >
      <div className="flex items-start gap-3">
        <span className="how-node-icon shrink-0" data-active={active}><StageIcon name={st.key} /></span>
        <div className="min-w-0 flex-1">
          <div className="font-mono text-2xs text-subtle">Etap {i + 1} z {STAGES.length}</div>
          <h3 id={`how-stage-${st.key}-h`} className="text-base font-semibold tracking-tight">{st.name}</h3>
        </div>
        <InfoTip content={st.tip} label={`Więcej: ${st.name}`} side="left" />
      </div>
      <p className="text-sm text-muted">{st.does}</p>
      {st.key === "genome" && (
        <ul className="grid grid-cols-3 gap-1.5 sm:grid-cols-6" aria-label="Rodziny genów">
          {FAMS.map((f) => (
            <li key={f.code} className="rounded-lg border border-line bg-raised px-2 py-1.5">
              <span aria-hidden="true" className="mb-1 block h-1 rounded-full" style={{ backgroundColor: FAMILY_VAR[f.code] }} />
              <span className="block font-mono text-xs font-semibold">{f.code}</span>
              <span className="block truncate text-2xs text-muted">{f.name}</span>
            </li>
          ))}
        </ul>
      )}
      <dl className="grid gap-1.5 text-xs">
        <div className="flex gap-2"><dt className="w-14 shrink-0 text-subtle">wejście</dt><dd className="min-w-0 text-fg">{st.input}</dd></div>
        <div className="flex gap-2"><dt className="w-14 shrink-0 text-subtle">wyjście</dt><dd className="min-w-0 text-fg"><span aria-hidden="true" className="mr-1 text-accent">→</span>{st.output}</dd></div>
      </dl>
      <ul className="mt-auto flex flex-col items-start gap-1.5" aria-label="Zabezpieczenia">
        {st.guards.map((g) => (
          <li key={g} className="how-guard"><svg viewBox="0 0 16 16" width={12} height={12} aria-hidden="true" fill="none" stroke="currentColor" strokeWidth={1.8} strokeLinecap="round" strokeLinejoin="round"><path d="M8 1.8 2.8 4v3.6c0 3 2.2 5.6 5.2 6.6 3-1 5.2-3.6 5.2-6.6V4z" /><path d="m5.8 8 1.6 1.6L10.4 6.6" /></svg>{g}</li>
        ))}
      </ul>
      <div className="flex flex-wrap items-center gap-2 border-t border-line pt-3 text-xs">
        {b && st.live ? (
          <Tooltip content="Liczba z bieżącego przebiegu (GET /v1/system/flow)." focusable>
            <span className="font-mono text-muted">{st.live(b)}</span>
          </Tooltip>
        ) : <span className="text-subtle">bez przebiegu: same definicje</span>}
        <Link to={st.to} className="ml-auto rounded-md px-1.5 py-0.5 font-medium text-accent hover:bg-accent-soft">Zobacz w aplikacji <span aria-hidden="true">→</span></Link>
      </div>
    </article>
  );
}

function FamilyChips() {
  const [sel, setSel] = useState<string>("VIS");
  const all = [...FAMS, ...MODS];
  const cur = all.find((f) => f.code === sel) ?? FAMS[0]!;
  const chip = (f: Fam) => (
    <Tooltip key={f.code} side="top" content={<><b>{f.code} · {f.name}</b><br />{f.ex}</>}>
      <button
        type="button"
        aria-pressed={sel === f.code}
        onClick={() => setSel(f.code)}
        className="how-chip"
        style={{ ["--chip" as string]: f.color ?? FAMILY_VAR[f.code] ?? "var(--color-accent)" }}
      >
        <span aria-hidden="true" className="size-2.5 rounded-[3px]" style={{ backgroundColor: "var(--chip)" }} />
        <span className="font-mono text-xs font-semibold">{f.code}</span>
        <span className="text-xs text-muted">{f.name}</span>
      </button>
    </Tooltip>
  );
  return (
    <section aria-labelledby="how-fam-h" className="how-glass rounded-[calc(var(--radius-card)+4px)] p-4 sm:p-6">
      <div className="mb-3 flex flex-wrap items-center gap-2">
        <h2 id="how-fam-h" className="text-lg font-semibold tracking-tight">Sześć rodzin genów</h2>
        <InfoTip content="Rodzina mówi, skąd pochodzi cecha. Bity dowodu sumują się po rodzinach z grupowaniem zależności, więc dwa geny z jednego źródła nie liczą się podwójnie." label="Czym jest rodzina genów" />
      </div>
      <div className="flex flex-wrap gap-2" role="group" aria-label="Rodziny genów">{FAMS.map(chip)}</div>
      <div className="mt-4 mb-2 text-xs font-semibold text-subtle">Dodatkowe modalności (geny w powyższych rodzinach)</div>
      <div className="flex flex-wrap gap-2" role="group" aria-label="Dodatkowe modalności">{MODS.map(chip)}</div>
      <div className="mt-4 grid gap-3 rounded-xl border border-line bg-raised/60 p-4 md:grid-cols-3" aria-live="polite" data-testid="how-family-detail">
        <div className="md:col-span-3 flex items-center gap-2 text-sm font-semibold"><span aria-hidden="true" className="size-3 rounded-[3px]" style={{ backgroundColor: cur.color ?? FAMILY_VAR[cur.code] }} />{cur.code} · {cur.name}</div>
        <div className="min-w-0"><div className="text-2xs font-semibold text-subtle uppercase">źródło</div><p className="text-sm text-muted">{cur.src}</p></div>
        <div className="min-w-0"><div className="text-2xs font-semibold text-subtle uppercase">przykłady genów</div><p className="text-sm text-muted">{cur.ex}</p></div>
        <div className="min-w-0"><div className="text-2xs font-semibold text-subtle uppercase">dlaczego łączy</div><p className="text-sm text-muted">{cur.why}</p></div>
      </div>
    </section>
  );
}

function Decisions({ b }: { b?: Boxes }) {
  const by = b?.assignment.by_decision;
  return (
    <section aria-labelledby="how-dec-h" className="space-y-3">
      <div className="flex flex-wrap items-center gap-2">
        <h2 id="how-dec-h" className="text-lg font-semibold tracking-tight">Trzy możliwe decyzje dla nowego materiału</h2>
        <InfoTip content="Reguła progowa decyduje zawsze; predyktor konformalny, gdy ma co najmniej 20 elementów kalibracji, może ją tylko potwierdzić albo obniżyć do wstrzymania lub nowej kampanii, nigdy podnieść." label="Kto decyduje" />
      </div>
      <div className="grid gap-4 md:grid-cols-3">
        {DECISIONS.map((d) => (
          <article key={d.value} onPointerMove={spot} className="how-card flex flex-col gap-3 p-5" aria-label={DECISION_LABEL[d.value]}>
            <div className="flex flex-wrap items-center gap-2">
              <DecisionBadge value={d.value} />
              {by && <span className="ml-auto font-mono text-xs text-subtle" title="liczba przypisań z tą decyzją w bieżącym przebiegu (GET /v1/system/flow)">{formatCount(by[d.value] ?? 0)} w przebiegu</span>}
            </div>
            <div><div className="text-2xs font-semibold text-subtle uppercase">kiedy</div><p className="text-sm text-fg">{d.when}</p></div>
            <div><div className="text-2xs font-semibold text-subtle uppercase">co dalej</div><p className="text-sm text-muted">{d.then}</p></div>
          </article>
        ))}
      </div>
    </section>
  );
}

const PRINCIPLES: { t: string; d: string }[] = [
  { t: "Bity, nie procenty", d: "Bity to heurystyczna siła dowodu z rzadkości wartości. Nie są prawdopodobieństwem; żaden ekran nie pokazuje ich jako procentu." },
  { t: "Brak = nieznane", d: "Brakująca modalność, niedostępna strona albo nieudane zapytanie usuwa rodzinę z pary. Nigdy nie obniża wyniku." },
  { t: "Artefakty, nie sprawcy", d: "Powiązanie mówi, że materiały dzielą rzadkie cechy. Nie mówi, kto stoi za kampanią." },
  { t: "Offline i powtarzalnie", d: "Rdzeń analizy działa bez sieci i deterministycznie; wzbogacenia są odtwarzane z cache." },
];

/* ---------- the explainer ---------- */

export function HowExplainer({ b, runId }: { b?: Boxes; runId?: string | null }) {
  const [active, setActive] = useState(0);
  const reduced = useReducedMotion();
  const lock = useRef(0);
  const grid = useRef<HTMLDivElement>(null);

  // scroll sync: the card crossing the middle band of the viewport becomes the active stage
  useEffect(() => {
    const root = grid.current;
    if (!root || typeof IntersectionObserver === "undefined") return;
    const io = new IntersectionObserver((entries) => {
      if (Date.now() < lock.current) return;
      const hit = entries.filter((e) => e.isIntersecting).sort((x, y) => y.intersectionRatio - x.intersectionRatio)[0];
      if (hit) setActive(Number((hit.target as HTMLElement).dataset.stage ?? 0));
    }, { rootMargin: "-45% 0px -45% 0px", threshold: [0, 0.01] });
    root.querySelectorAll("[data-stage]").forEach((el) => io.observe(el));
    return () => io.disconnect();
  }, []);

  const pick = useCallback((i: number, toCard = false) => {
    setActive(i);
    if (!toCard) return;
    lock.current = Date.now() + 900;
    const el = document.getElementById(`how-stage-${STAGES[i]!.key}`);
    el?.scrollIntoView?.({ behavior: reduced ? "auto" : "smooth", block: "center" });
    el?.focus?.({ preventScroll: true });
  }, [reduced]);

  return (
    <div className="space-y-8" data-testid="how-explainer">
      <section aria-labelledby="how-hero-h" className="how-hero relative overflow-hidden rounded-[calc(var(--radius-card)+6px)] p-6 sm:p-10">
        <div className="relative max-w-3xl">
          <div className="mb-3 inline-flex items-center gap-2 rounded-full border border-line bg-panel/70 px-3 py-1 text-xs font-medium text-muted">
            <span aria-hidden="true" className="size-1.5 rounded-full bg-accent" />Jak to działa
          </div>
          <h2 id="how-hero-h" className="text-3xl font-semibold tracking-tight sm:text-4xl">Od zgłoszonej reklamy do pakietu dowodowego</h2>
          <p className="mt-3 text-base text-muted sm:text-lg">
            HYDRA importuje materiał, buduje genom każdego incydentu, waży rzadkie geny w bitach, stawia hipotezy dla par, składa kampanie i grupy,
            a nowy materiał przypisuje do znanej kampanii, oznacza jako nową kampanię albo wstrzymuje, i dla każdego kroku przygotowuje dowody.
          </p>
          <ol className="mt-5 flex flex-wrap items-center gap-x-1.5 gap-y-2 text-xs" aria-label="Kolejność etapów">
            {STAGES.map((s, i) => (
              <li key={s.key} className="flex items-center gap-1.5">
                <button type="button" onClick={() => pick(i, true)} className="rounded-full border border-line bg-panel/80 px-2.5 py-1 font-medium text-fg hover:border-accent hover:text-accent">{s.short}</button>
                {i < STAGES.length - 1 && <span aria-hidden="true" className="text-subtle">→</span>}
              </li>
            ))}
          </ol>
        </div>
        {b && (
          <dl className="relative mt-6 grid max-w-3xl grid-cols-2 gap-3 sm:grid-cols-4">
            {[
              ["incydenty", formatCount(b.input.incidents)],
              ["geny", formatCount(b.genes.total)],
              ["ocenione pary", formatCount(b.scoring.pairs_scored)],
              ["kampanie", formatCount(b.clustering.campaigns)],
            ].map(([l, v]) => (
              <div key={l} className="rounded-xl border border-line bg-panel/70 px-3 py-2">
                <dt className="text-2xs text-subtle">{l}</dt>
                <dd className="font-mono text-xl text-fg tabular-nums">{v}</dd>
              </div>
            ))}
            <div className="col-span-2 text-2xs text-subtle sm:col-span-4">Liczby z bieżącego przebiegu {runId ? <span className="font-mono">{runId}</span> : null} (GET /v1/system/flow).</div>
          </dl>
        )}
      </section>

      <ul className="grid gap-3 sm:grid-cols-2 xl:grid-cols-4" aria-label="Zasady">
        {PRINCIPLES.map((p) => (
          <li key={p.t} className="how-card p-4" onPointerMove={spot}>
            <div className="text-sm font-semibold">{p.t}</div>
            <p className="mt-1 text-xs text-muted">{p.d}</p>
          </li>
        ))}
      </ul>

      <Flow active={active} onPick={pick} b={b} />

      <section aria-labelledby="how-stages-h" className="space-y-3">
        <h2 id="how-stages-h" className="text-lg font-semibold tracking-tight">Etapy: co robi każdy krok</h2>
        <div ref={grid} className="grid gap-4 md:grid-cols-2 xl:grid-cols-4">
          {STAGES.map((st, i) => <StageCard key={st.key} st={st} i={i} active={i === active} onHover={() => setActive(i)} b={b} />)}
        </div>
      </section>

      <FamilyChips />
      <Decisions b={b} />
    </div>
  );
}
