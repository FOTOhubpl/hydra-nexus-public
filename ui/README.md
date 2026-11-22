# HYDRA NEXUS UI

Prepared 2026-11-20, before the task start (disclosed in `docs/DISCLOSURE.md`). It contains no
task logic. Task screens and evidence views are written from Sat 21.11.2026 11:00.

Stack: React 19, Vite, Tailwind 4, TanStack Query, react-router, sigma.js/graphology (graph view),
Vitest + Testing Library.

```bash
pnpm install --frozen-lockfile
pnpm dev         # 127.0.0.1:5173, proxies /api/* -> 127.0.0.1:8000 (HYDRA_UI_BACKEND to override)
pnpm typecheck && pnpm test
pnpm build       # dist/ (gitignored)
```

The browser talks only to `/api/*` on its own origin, and Vite strips the prefix. The server
listens on loopback only, never on a public interface.

## Layout
| Path | Purpose |
|---|---|
| `src/api/client.ts`, `src/api/types.ts` | typed fetch wrapper + react-query hooks for the generic backend endpoints (health, import, jobs, artifacts, audit) |
| `src/pages/` | Overview, Graph, Compare (`detail/:a/:b`), Item, Settings, NotFound |
| `src/components/` | AppShell (floating sidebar, icon rail, phone drawer; Ctrl/Cmd+B), StatusBar (breadcrumb top bar), CommandPalette (Ctrl/Cmd+K), `dashboard/` (bento cards of "Pulpit"), Panel, SplitPane, DataTable, PropertyList, ImagePane, GraphCanvas, FileDropzone, JobProgress, StatTile, Badge, Button, EmptyState, ApiErrorState, ErrorBoundary |
| `src/lib/` | formatting, status mapping, graph helpers, local settings/session storage |

## Task screens (from 2026-11-21 11:00)
`src/api/task.ts` (types mirroring `hydra/contracts.py` + hooks for `hydra/api_task.py`),
`src/components/evidence/` (OBS/INF/HYP pill, family chip, bits waterfall, probability bar, gene tables),
`src/lib/evidence.ts` (Polish labels, decimal comma, waterfall steps), `src/pages/task/*` (one file per screen).
Artefact viewer: `src/api/artifacts.ts` (client for `hydra/api_artifacts.py`), `src/components/artifacts/` (gallery,
viewer, per-type previews, "Co wyciągnięto", side-by-side pair evidence, dashboard "Materiały"),
`src/lib/artifactEvidence.ts` (bbox layers, text highlight spans; pure, unit-tested).
Graph ("Graf powiązań", `/graph`): `src/api/graph.ts` (adapter over the level-of-detail API of `docs/ui/GRAPH_API.md`,
legacy fallback to `/v1/graph/explore`), `src/components/graph/constellation/` (lazy constellation explorer: campaign
loader with cancellation + LRU, worker layout, sigma host, palette, scrubber, mini-map, inspector); the previous
four-view explorer stays in `src/components/graph/` at `/graph?classic=1`. Load / FPS measurement:
`node ui/scripts/graph-perf.mjs --url http://127.0.0.1:<port>/graph --out <dir> --label <name> [--run-id <id>]`.
Monitoring (`/watch`): tab "Na żywo" = realtime engine (`src/api/watchRt.ts` client + `watchRtBus` fed by the
`watch.*` events of `src/api/stream.ts`, `src/components/watch/` source cards, virtualised feed, match panel, watchlist,
48 h timeline, `src/pages/task/WatchLive.tsx`); tab "Odkrywanie domen (nagrania)" = the classic CERT/CT ticker
(`src/pages/task/WatchClassic.tsx`, `?view=classic`). Playwright: `ui/e2e/watch.spec.ts` (replay "demo", @mutating).
Synthetic rows for local UI work only:
`HYDRA_STATE_DIR=/tmp/x/state uv run python -m tests.ui_api.synthetic` (refuses the real state dir).

## Rules for task work
- Add task screens as new pages or routes. Reuse these components rather than forking them.
- Show only real analysis results from the backend. No mocked data.
- UI strings are Polish. Code and comments are English.
