import { useAppStatus } from "../api/task";

/** Audit A04: say plainly whether an analyst decision changes the grouping or is only an audited annotation. */
export function ReviewEffectNote() {
  const st = useAppStatus(15_000);
  if (st.data?.reviews_change_grouping) {
    return <p className="text-xs text-muted">Decyzja zmienia grupowanie (nowa rewizja, z możliwością cofnięcia) i trafia do dziennika audytu.</p>;
  }
  return (
    <p className="text-xs text-abstain-ink">
      Adnotacja (nie zmienia grupowania): decyzja trafia do dziennika audytu z łańcuchem SHA-256, ale kampanie, graf i eksport zmienią się dopiero, gdy rdzeń zastosuje recenzje.
    </p>
  );
}
