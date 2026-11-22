/**
 * Inline key form, the same everywhere a connector needs a key (preset card, connector card, detail sheet):
 * "Wymaga klucza API" callout (what the key unlocks, where to get it, plan and limits, provider link, "Jak zdobyć
 * klucz" into the docs tab), masked inputs with show/hide and paste, "Zapisz i testuj" → POST /connect (store in the
 * 0600 file → test → enable) with the result inline, and "Usuń klucz". Typed values live only in this component's
 * state and are cleared before the request resolves; the request is a plain fetch (no query/mutation cache), the API
 * never echoes a value back (only "zapisany" + last 4 characters).
 */
import { useId, useState, type FormEvent } from "react";
import { Link } from "react-router";
import { connectConnector, deleteSecret, useInvalidateIntegrations, type ConnectResult, type Connector } from "../../../api/integrations";
import { isApiError } from "../../../api/client";
import { Button } from "../../../components/Button";
import { cx } from "../../../lib/cx";
import { DEFAULT_KEY_INFO, isAddressSecret, KEY_INFO, keyDocHref } from "./labels";

type Outcome = { kind: "ok" | "offline" | "error"; text: string };

export function outcomeOf(r: ConnectResult, offline: boolean): Outcome {
  const failed = r.steps.find((s) => !s.ok);
  const offlineStep = r.steps.some((s) => s.status === "offline" || (!s.ok && s.id === "test" && offline));
  if (r.ok) return { kind: "ok", text: "klucz zapisany, test udany, konektor włączony." };
  if (offlineStep) {
    return { kind: "offline", text: "test jest zablokowany w trybie offline (HYDRA_OFFLINE=1). Uruchom HYDRA z HYDRA_OFFLINE=0 (dla wzbogacania także HYDRA_ENRICH=1) i użyj „Zapisz i testuj” ponownie." };
  }
  return { kind: "error", text: failed?.message_pl?.trim() || "Połączenie nie powiodło się." };
}

const OUTCOME_STYLE: Record<Outcome["kind"], { glyph: string; label: string; cls: string }> = {
  ok: { glyph: "✓", label: "Połączono", cls: "border-ok/35 bg-ok/10 text-ok" },
  offline: { glyph: "⏸", label: "Klucz zapisany", cls: "border-warn/35 bg-warn/10 text-warn" },
  error: { glyph: "✕", label: "Błąd", cls: "border-err/35 bg-err/10 text-err" },
};

const canPaste = () => typeof navigator !== "undefined" && typeof navigator.clipboard?.readText === "function";

export function KeyCallout({ connector }: { connector: Connector }) {
  const info = KEY_INFO[connector.id] ?? DEFAULT_KEY_INFO;
  return (
    <div role="note" aria-label={`Wymaga klucza API: ${connector.name}`} className="space-y-1 rounded-lg border border-warn/35 bg-warn/5 px-3 py-2 text-xs" data-testid={`key-callout-${connector.id}`}>
      <div className="flex items-center gap-1.5 font-semibold text-fg"><span aria-hidden="true" className="text-warn">!</span> Wymaga klucza API</div>
      <p className="text-muted">
        Klucz odblokowuje {info.unlocks}. Skąd go wziąć: {info.where}. Koszt i warunki: {info.plan}.
        {connector.rate_limit_pl ? <> Limit: {connector.rate_limit_pl}</> : null}
      </p>
      <div className="flex flex-wrap gap-x-3 gap-y-1">
        {connector.docs_url && (
          <a href={connector.docs_url} target="_blank" rel="noreferrer noopener" className="font-medium text-accent hover:underline">
            Strona dostawcy<span className="sr-only"> (otwiera się w nowej karcie)</span>
          </a>
        )}
        <Link to={keyDocHref(connector.id)} className="font-medium text-accent hover:underline" data-testid={`key-doc-${connector.id}`}>Jak zdobyć klucz</Link>
      </div>
    </div>
  );
}

export function KeyForm({ connector, offline, idPrefix, onSaved, autoFocus = false, showCallout = true }: {
  connector: Connector;
  offline: boolean;
  idPrefix: string;
  onSaved?: (r: ConnectResult) => void;
  autoFocus?: boolean;
  showCallout?: boolean;
}) {
  const uid = useId();
  const [values, setValues] = useState<Record<string, string>>({});
  const [shown, setShown] = useState<Record<string, boolean>>({});
  const [busy, setBusy] = useState(false);
  const [outcome, setOutcome] = useState<Outcome | null>(null);
  const [removing, setRemoving] = useState<string | null>(null);
  const invalidate = useInvalidateIntegrations();
  const c = connector;
  const missing = c.secrets.filter((s) => !s.configured && !values[s.name]?.trim());
  const needsKey = c.secrets.some((s) => !s.configured);
  const typed = Object.values(values).some((v) => v.trim());

  const submit = async (e: FormEvent) => {
    e.preventDefault();
    const secrets: Record<string, string> = {};
    for (const [k, v] of Object.entries(values)) if (v.trim()) secrets[k] = v.trim();
    setValues({}); // the key leaves the UI state before the request completes
    setShown({});
    setBusy(true);
    setOutcome(null);
    try {
      const r = await connectConnector(c.id, secrets);
      setOutcome(outcomeOf(r, offline));
      onSaved?.(r);
    } catch (err) {
      setOutcome({ kind: "error", text: isApiError(err) ? err.message : "Nie udało się zapisać klucza." });
    } finally {
      setBusy(false);
      void invalidate();
    }
  };

  const remove = async (name: string) => {
    setRemoving(name);
    setOutcome(null);
    try {
      await deleteSecret(c.id, name);
    } catch (err) {
      setOutcome({ kind: "error", text: isApiError(err) ? err.message : "Nie udało się usunąć klucza." });
    } finally {
      setRemoving(null);
      void invalidate();
    }
  };

  const paste = async (name: string) => {
    try {
      const text = await navigator.clipboard.readText();
      if (text) setValues((v) => ({ ...v, [name]: text.trim() }));
    } catch {
      setOutcome({ kind: "error", text: "Przeglądarka nie pozwoliła odczytać schowka; wklej klucz ręcznie (Ctrl+V)." });
    }
  };

  const style = outcome ? OUTCOME_STYLE[outcome.kind] : null;
  return (
    <form onSubmit={submit} autoComplete="off" className="min-w-0 space-y-2" data-testid={`${idPrefix}-keyform`} aria-label={`Klucze: ${c.name}`}>
      {showCallout && needsKey && <KeyCallout connector={c} />}
      {c.secrets.length === 0 && <p className="text-xs text-muted">Ten konektor nie wymaga klucza; „Testuj i włącz” sprawdzi połączenie i go włączy.</p>}
      {c.secrets.map((s, i) => {
        const address = isAddressSecret(s.name);
        const inputId = `${uid}-${s.name}`;
        const visible = address || shown[s.name];
        return (
          <div key={s.name} className="min-w-0 space-y-1">
            <label htmlFor={inputId} className="flex min-w-0 flex-wrap items-center gap-x-2 text-xs font-medium text-fg">
              <span>{address ? "Adres instancji" : "Klucz"}</span>
              <span className="font-mono text-2xs text-subtle">{s.name}</span>
              {s.configured && (
                <span className="font-normal text-ok" data-testid={`${idPrefix}-saved-${s.name}`}>
                  ✓ zapisany{s.last4 ? ` (…${s.last4})` : ""}{s.source === "env" ? ", ze zmiennej środowiskowej" : ""}; puste pole zachowa obecny
                </span>
              )}
            </label>
            <div className="flex min-w-0 items-center gap-1.5">
              <input
                id={inputId}
                type={visible ? (address ? "url" : "text") : "password"}
                inputMode={address ? "url" : undefined}
                autoComplete="off"
                spellCheck={false}
                autoFocus={autoFocus && i === 0}
                data-1p-ignore=""
                data-lpignore="true"
                name={`secret-${s.name}`}
                placeholder={address ? "https://…" : s.configured ? "••••••••" : "wklej klucz"}
                value={values[s.name] ?? ""}
                onChange={(ev) => setValues((v) => ({ ...v, [s.name]: ev.target.value }))}
                aria-describedby={s.configured ? `${inputId}-hint` : undefined}
                data-testid={`${idPrefix}-input-${s.name}`}
                className="h-8 min-w-0 flex-1 rounded-[4px] border border-line bg-raised px-2 font-mono text-sm"
              />
              {!address && (
                <Button size="sm" onClick={() => setShown((v) => ({ ...v, [s.name]: !v[s.name] }))} aria-pressed={Boolean(shown[s.name])} aria-label={shown[s.name] ? `Ukryj ${s.name}` : `Pokaż ${s.name}`} data-testid={`${idPrefix}-toggle-${s.name}`}>
                  {shown[s.name] ? "Ukryj" : "Pokaż"}
                </Button>
              )}
              {canPaste() && (
                <Button size="sm" onClick={() => void paste(s.name)} aria-label={`Wklej ${s.name} ze schowka`}>Wklej</Button>
              )}
              {s.configured && s.source === "file" && (
                <Button size="sm" variant="danger" onClick={() => void remove(s.name)} disabled={removing === s.name} aria-label={`Usuń klucz ${s.name}`} data-testid={`${idPrefix}-remove-${s.name}`}>
                  {removing === s.name ? "Usuwanie…" : "Usuń klucz"}
                </Button>
              )}
            </div>
            {s.configured && <span id={`${inputId}-hint`} className="sr-only">Klucz jest zapisany; puste pole zachowa obecny.</span>}
          </div>
        );
      })}
      <div className="flex min-w-0 flex-wrap items-center gap-2">
        <Button type="submit" variant="primary" disabled={busy || missing.length > 0} aria-busy={busy} data-testid={`${idPrefix}-submit`}>
          {busy && <span aria-hidden="true" className="inline-block size-3 rounded-full border-2 border-current border-r-transparent motion-safe:animate-spin" />}
          {busy ? "Zapisywanie i test…" : c.secrets.length === 0 || (!needsKey && !typed) ? "Testuj i włącz" : "Zapisz i testuj"}
        </Button>
        {missing.length > 0 && !busy && <span className="text-2xs text-subtle">Uzupełnij: {missing.map((s) => s.name).join(", ")}</span>}
      </div>
      <div role="status" aria-live="polite" className="min-w-0" data-testid={`${idPrefix}-result`} data-kind={outcome?.kind}>
        {outcome && style && (
          <p className={cx("rounded-md border px-3 py-2 text-xs break-words", style.cls)}>
            <span aria-hidden="true" className="font-bold">{style.glyph} </span>
            <span className="font-semibold">{style.label}: </span>{outcome.text}
          </p>
        )}
      </div>
    </form>
  );
}
