"""SQLite in WAL mode with exactly one writer thread (ADR-002).

Readers open their own connections (`connect(..., readonly=True)`); every write goes through
`Writer.submit(fn)`, which runs `fn(conn)` inside one transaction on the writer thread. A write
and its audit event are committed together (see hydra.events).
"""

from __future__ import annotations

import os
import queue
import sqlite3
import threading
from collections.abc import Callable
from concurrent.futures import Future
from pathlib import Path
from typing import Any, TypeVar

T = TypeVar("T")

# Ordered, append-only migrations. Never edit a shipped one — add a new version.
# v1 is the generic skeleton; task-specific tables arrive as v2+ from Sat 11:00.
MIGRATIONS: list[tuple[int, str]] = [
    (
        1,
        """
        CREATE TABLE artifacts (
            id          TEXT PRIMARY KEY,            -- sha256 of the original bytes
            mime        TEXT NOT NULL,
            size        INTEGER NOT NULL,
            source_ref  TEXT NOT NULL,               -- first path / ID it was imported from
            observed_at TEXT,                        -- UTC ISO-8601 from source metadata, if any
            meta        TEXT NOT NULL DEFAULT '{}',  -- JSON
            status      TEXT NOT NULL DEFAULT 'stored',
            created_at  TEXT NOT NULL
        );
        CREATE TABLE artifact_sources (
            artifact_id TEXT NOT NULL REFERENCES artifacts(id),
            source_ref  TEXT NOT NULL,
            job_id      TEXT NOT NULL,
            imported_at TEXT NOT NULL,
            PRIMARY KEY (artifact_id, source_ref)
        );
        CREATE TABLE jobs (
            id          TEXT PRIMARY KEY,
            kind        TEXT NOT NULL,
            status      TEXT NOT NULL,               -- queued | running | done | failed
            params      TEXT NOT NULL DEFAULT '{}',
            total       INTEGER NOT NULL DEFAULT 0,
            done        INTEGER NOT NULL DEFAULT 0,
            failed      INTEGER NOT NULL DEFAULT 0,
            error_code  TEXT,
            error       TEXT,
            created_at  TEXT NOT NULL,
            started_at  TEXT,
            finished_at TEXT
        );
        CREATE TABLE job_items (
            job_id      TEXT NOT NULL REFERENCES jobs(id),
            idx         INTEGER NOT NULL,
            source_ref  TEXT NOT NULL,
            artifact_id TEXT,
            status      TEXT NOT NULL,               -- stored | duplicate | quarantined | failed
            error_code  TEXT,
            detail      TEXT,
            PRIMARY KEY (job_id, idx)
        );
        CREATE TABLE events (
            seq       INTEGER PRIMARY KEY AUTOINCREMENT,
            ts        TEXT NOT NULL,
            type      TEXT NOT NULL,
            payload   TEXT NOT NULL,                 -- canonical JSON
            prev_hash TEXT NOT NULL,
            hash      TEXT NOT NULL UNIQUE
        );
        CREATE INDEX idx_job_items_job ON job_items(job_id, status);
        CREATE INDEX idx_artifacts_mime ON artifacts(mime);
        """,
    ),
    (
        2,
        # Task schema (contracts v2.0.0, hydra/contracts.py). Owned by the architect.
        """
        CREATE TABLE incidents (
            id          TEXT PRIMARY KEY,
            source      TEXT NOT NULL,
            source_ref  TEXT NOT NULL,
            title       TEXT,
            observed_at TEXT,
            label       TEXT,                        -- ground truth (eval only), never shown as a result
            meta        TEXT NOT NULL DEFAULT '{}',
            created_at  TEXT NOT NULL
        );
        CREATE TABLE incident_artifacts (
            incident_id      TEXT NOT NULL REFERENCES incidents(id),
            artifact_id      TEXT NOT NULL REFERENCES artifacts(id),
            role             TEXT NOT NULL,
            path_in_incident TEXT NOT NULL,
            meta             TEXT NOT NULL DEFAULT '{}',
            PRIMARY KEY (incident_id, artifact_id, path_in_incident)
        );
        CREATE TABLE genes (
            id               INTEGER PRIMARY KEY AUTOINCREMENT,
            incident_id      TEXT NOT NULL REFERENCES incidents(id),
            family           TEXT NOT NULL,
            kind             TEXT NOT NULL,
            value            TEXT,
            vector_ref       TEXT,
            quality          REAL NOT NULL DEFAULT 1.0,
            status           TEXT NOT NULL,
            provenance       TEXT NOT NULL,
            evidence         TEXT NOT NULL DEFAULT '{}',
            extractor        TEXT NOT NULL,
            dependency_group TEXT,
            created_at       TEXT NOT NULL
        );
        CREATE TABLE gene_vectors (
            key   TEXT PRIMARY KEY,                  -- incident|artifact|kind|idx
            model TEXT NOT NULL,
            dim   INTEGER NOT NULL,
            data  BLOB NOT NULL                      -- float32 little-endian, L2-normalised
        );
        CREATE TABLE enrichment (
            key             TEXT PRIMARY KEY,
            source          TEXT NOT NULL,
            query           TEXT NOT NULL,
            fetched_at      TEXT NOT NULL,
            status          TEXT NOT NULL,
            response_sha256 TEXT,
            cache_path      TEXT,
            summary         TEXT NOT NULL DEFAULT '{}'
        );
        CREATE TABLE pairs (
            a           TEXT NOT NULL REFERENCES incidents(id),
            b           TEXT NOT NULL REFERENCES incidents(id),
            run_id      TEXT NOT NULL,
            total_bits  REAL NOT NULL,
            probability REAL NOT NULL,
            calibrated  INTEGER NOT NULL DEFAULT 0,
            hypothesis  TEXT NOT NULL,
            model       TEXT NOT NULL,
            detail      TEXT NOT NULL,               -- PairScore JSON
            PRIMARY KEY (run_id, a, b),
            CHECK (a < b)
        );
        CREATE TABLE campaigns (
            id         TEXT NOT NULL,
            run_id     TEXT NOT NULL,
            label      TEXT NOT NULL,
            confidence REAL NOT NULL DEFAULT 0,
            status     TEXT NOT NULL DEFAULT 'active',
            detail     TEXT NOT NULL,                -- Campaign JSON
            PRIMARY KEY (run_id, id)
        );
        CREATE TABLE campaign_members (
            run_id      TEXT NOT NULL,
            campaign_id TEXT NOT NULL,
            incident_id TEXT NOT NULL REFERENCES incidents(id),
            PRIMARY KEY (run_id, campaign_id, incident_id)
        );
        CREATE TABLE assignments (
            incident_id TEXT NOT NULL REFERENCES incidents(id),
            run_id      TEXT NOT NULL,
            decision    TEXT NOT NULL,
            campaign_id TEXT,
            detail      TEXT NOT NULL,               -- Assignment JSON
            created_at  TEXT NOT NULL,
            PRIMARY KEY (run_id, incident_id)
        );
        CREATE TABLE reviews (
            id         TEXT PRIMARY KEY,
            action     TEXT NOT NULL,
            target     TEXT NOT NULL,
            analyst    TEXT NOT NULL,
            note       TEXT,
            undoes     TEXT,
            created_at TEXT NOT NULL
        );
        CREATE TABLE runs (
            id          TEXT PRIMARY KEY,
            kind        TEXT NOT NULL,               -- analyse | eval
            params      TEXT NOT NULL DEFAULT '{}',
            metrics     TEXT NOT NULL DEFAULT '{}',
            created_at  TEXT NOT NULL,
            finished_at TEXT
        );
        CREATE INDEX idx_genes_incident ON genes(incident_id);
        CREATE INDEX idx_genes_kind_value ON genes(kind, value);
        CREATE INDEX idx_genes_family ON genes(family, status);
        CREATE INDEX idx_incident_artifacts_artifact ON incident_artifacts(artifact_id);
        CREATE INDEX idx_pairs_prob ON pairs(run_id, probability);
        CREATE INDEX idx_members_incident ON campaign_members(run_id, incident_id)
        """,
    ),
    (
        3,
        # CR-CORE-1 (contracts 2.1.0): campaign groups per run.
        """
        CREATE TABLE campaign_groups (
            run_id     TEXT NOT NULL,
            id         TEXT NOT NULL,
            label      TEXT NOT NULL,
            confidence REAL NOT NULL DEFAULT 0,
            detail     TEXT NOT NULL,                -- CampaignGroup JSON
            PRIMARY KEY (run_id, id)
        )
        """,
    ),
    (
        4,
        # CR-PERF-2 (2026-11-21): link-level pair reads per run (graph LOD, analytics, links-only graph).
        """
        CREATE INDEX IF NOT EXISTS idx_pairs_run_hyp_bits ON pairs(run_id, hypothesis, total_bits)
        """,
    ),
    (
        5,
        # DATAFLOW (ADR-0009, 2026-11-21): materialised per-run aggregates, updated incrementally by the single
        # Writer in the transaction of the event that changes them (hydra/dataflow.py). Additive: derived data
        # only, rebuildable from the v2 tables at any time (`python -m hydra.dataflow rebuild`).
        """
        CREATE TABLE agg_state (
            run_id           TEXT PRIMARY KEY,
            status           TEXT NOT NULL,              -- ready | stale
            version          INTEGER NOT NULL DEFAULT 0, -- +1 per applied delta and per rebuild
            seq              INTEGER NOT NULL DEFAULT 0, -- events.seq of the last applied delta
            base_seq         INTEGER NOT NULL DEFAULT 0, -- events.seq when the base was (re)built
            base_finished_at TEXT,
            built_at         TEXT,
            updated_at       TEXT,
            meta             TEXT NOT NULL DEFAULT '{}'
        );
        CREATE TABLE agg_counts (
            run_id TEXT NOT NULL,
            dim    TEXT NOT NULL,                        -- corpus | source | day | role | gene | decision | ...
            key    TEXT NOT NULL,
            n      INTEGER NOT NULL,                     -- current total (base + new material)
            n_new  INTEGER NOT NULL,                     -- part of n contributed by material new since analysis
            PRIMARY KEY (run_id, dim, key)
        ) WITHOUT ROWID;
        CREATE TABLE agg_incidents (
            run_id      TEXT NOT NULL,
            incident_id TEXT NOT NULL,
            is_new      INTEGER NOT NULL,                -- 1 = attached after the analysis (assign run)
            campaign_id TEXT NOT NULL,                   -- map campaign (member / assigned campaign / solo:<id>)
            decision    TEXT,
            assign_run  TEXT,                            -- run of the current decision when it is new material
            seq         INTEGER NOT NULL DEFAULT 0,
            data        TEXT NOT NULL,                   -- JSON contribution to the counters
            PRIMARY KEY (run_id, incident_id)
        ) WITHOUT ROWID;
        CREATE INDEX idx_agg_incidents_campaign ON agg_incidents(run_id, campaign_id);
        CREATE INDEX idx_agg_incidents_new ON agg_incidents(run_id, is_new);
        CREATE TABLE agg_genes (
            run_id TEXT NOT NULL,
            gkey   TEXT NOT NULL,                        -- family, kind, dependency group, value (unit separated)
            level  TEXT NOT NULL,
            df     INTEGER NOT NULL,
            df_new INTEGER NOT NULL,
            PRIMARY KEY (run_id, gkey)
        ) WITHOUT ROWID;
        CREATE INDEX idx_agg_genes_df ON agg_genes(run_id, df);
        CREATE TABLE agg_pairs (
            run_id      TEXT NOT NULL,
            a           TEXT NOT NULL,
            b           TEXT NOT NULL,
            ca          TEXT NOT NULL,                   -- map campaigns of the two ends, ca <= cb
            cb          TEXT NOT NULL,
            bits        REAL NOT NULL,
            p           REAL,
            hypothesis  TEXT NOT NULL,
            level       TEXT NOT NULL,                   -- '' = no link level
            family      TEXT,
            family_bits TEXT NOT NULL DEFAULT '{}',
            review      TEXT,                            -- must_link | cannot_link | unsure
            is_new      INTEGER NOT NULL DEFAULT 0,
            src_run     TEXT NOT NULL,
            PRIMARY KEY (run_id, a, b)
        ) WITHOUT ROWID;
        CREATE INDEX idx_agg_pairs_link ON agg_pairs(run_id, ca, cb);
        CREATE TABLE agg_campaigns (
            run_id            TEXT NOT NULL,
            campaign_id       TEXT NOT NULL,
            label             TEXT,
            group_id          TEXT,
            solo              INTEGER NOT NULL,
            size              INTEGER NOT NULL,
            n_new             INTEGER NOT NULL,
            first_seen        TEXT,
            last_seen         TEXT,
            internal_edges    INTEGER NOT NULL,
            max_internal_bits REAL,
            family            TEXT,
            meta              TEXT NOT NULL DEFAULT '{}',
            PRIMARY KEY (run_id, campaign_id)
        ) WITHOUT ROWID;
        CREATE TABLE agg_links (
            run_id      TEXT NOT NULL,
            ca          TEXT NOT NULL,
            cb          TEXT NOT NULL,
            level       TEXT NOT NULL,
            pairs       INTEGER NOT NULL,
            max_bits    REAL NOT NULL,
            sum_bits    REAL NOT NULL,
            family_bits TEXT NOT NULL,
            n_new       INTEGER NOT NULL,
            PRIMARY KEY (run_id, ca, cb, level)
        ) WITHOUT ROWID;
        CREATE TABLE agg_deltas (
            id         INTEGER PRIMARY KEY AUTOINCREMENT, -- stream cursor
            run_id     TEXT NOT NULL,
            seq        INTEGER NOT NULL,                  -- events.seq (SSE id)
            version    INTEGER NOT NULL,
            kind       TEXT NOT NULL,                     -- assign | review | export | reset
            bytes      INTEGER NOT NULL,
            payload    TEXT NOT NULL,                     -- JSON delta message (docs/ui/STREAM_API.md)
            created_at TEXT NOT NULL
        );
        CREATE INDEX idx_agg_deltas_run ON agg_deltas(run_id, seq)
        """,
    ),
    (
        6,
        # WATCH-RT (2026-11-22, SPEC_ERRATA "Watch realtime"): realtime public-feed watch (hydra/watch/rt). Additive and
        # self-contained: own tables only, no foreign keys into v2 tables (campaign ids are plain text references).
        # Idempotent (IF NOT EXISTS): a DB that reached a later version first gets these tables from
        # hydra.watch.rt.store.ensure_schema (same statements, through the Writer).
        """
        CREATE TABLE IF NOT EXISTS watch_sources (
            id                 TEXT PRIMARY KEY,         -- cert-pl | urlhaus | openphish | crtsh | meta-adlibrary
            enabled            INTEGER NOT NULL DEFAULT 0,
            interval_s         INTEGER NOT NULL,
            state              TEXT NOT NULL DEFAULT 'idle',
            state_detail       TEXT,
            last_poll          TEXT,
            last_ok            TEXT,
            next_due           TEXT,
            etag               TEXT,
            last_modified      TEXT,
            cursor             TEXT NOT NULL DEFAULT '{}', -- JSON, source specific (CERT byte offset, crt.sh max id)
            consecutive_errors INTEGER NOT NULL DEFAULT 0,
            circuit_open_until TEXT,
            total_polls        INTEGER NOT NULL DEFAULT 0,
            total_items        INTEGER NOT NULL DEFAULT 0,
            last_latency_ms    INTEGER,
            last_error         TEXT,
            config             TEXT NOT NULL DEFAULT '{}',
            updated_at         TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS watch_items (
            id             INTEGER PRIMARY KEY AUTOINCREMENT,
            source         TEXT NOT NULL,
            external_id    TEXT NOT NULL,
            payload_sha256 TEXT NOT NULL,
            kind           TEXT NOT NULL,                -- url | domain | cert | ad
            value          TEXT NOT NULL,                -- public feed value (host / URL / cert names), redacted
            indicators     TEXT NOT NULL DEFAULT '[]',   -- JSON [[kind, value], ...] normalised
            first_seen     TEXT NOT NULL,
            last_seen      TEXT NOT NULL,
            seen_count     INTEGER NOT NULL DEFAULT 1,
            run_id         INTEGER,
            match_count    INTEGER NOT NULL DEFAULT 0,
            replay         INTEGER NOT NULL DEFAULT 0,
            UNIQUE (source, external_id)
        );
        CREATE INDEX IF NOT EXISTS idx_watch_items_seen ON watch_items(first_seen);
        CREATE INDEX IF NOT EXISTS idx_watch_items_source ON watch_items(source, id);
        CREATE TABLE IF NOT EXISTS watch_watchlist (
            id           INTEGER PRIMARY KEY AUTOINCREMENT,
            kind         TEXT NOT NULL,                  -- domain | host | url | ip | phone | iban | wallet | ...
            value        TEXT NOT NULL,                  -- normalised match key
            display      TEXT NOT NULL,                  -- what the UI shows (masked for IBAN hashes)
            origin       TEXT NOT NULL,                  -- campaign | manual | replay
            campaign_id  TEXT NOT NULL DEFAULT '',
            run_id       TEXT,
            label        TEXT,
            incident_ids TEXT NOT NULL DEFAULT '[]',
            df           INTEGER,                        -- incidents of the run carrying the value
            n_incidents  INTEGER,
            created_at   TEXT NOT NULL,
            UNIQUE (kind, value, origin, campaign_id)
        );
        CREATE INDEX IF NOT EXISTS idx_watch_watchlist_key ON watch_watchlist(kind, value);
        CREATE TABLE IF NOT EXISTS watch_matches (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            item_id         INTEGER NOT NULL,
            watch_id        INTEGER NOT NULL,
            source          TEXT NOT NULL,
            indicator_kind  TEXT NOT NULL,
            indicator_value TEXT NOT NULL,
            campaign_id     TEXT,
            run_id          TEXT,
            strength        TEXT NOT NULL,               -- strong | weak
            bits            REAL,
            df              INTEGER,
            status          TEXT NOT NULL DEFAULT 'new', -- new | acknowledged | dismissed
            evidence        TEXT NOT NULL DEFAULT '{}',
            assign          TEXT,
            replay          INTEGER NOT NULL DEFAULT 0,
            created_at      TEXT NOT NULL,
            updated_at      TEXT NOT NULL,
            UNIQUE (item_id, watch_id)
        );
        CREATE INDEX IF NOT EXISTS idx_watch_matches_status ON watch_matches(status, id);
        CREATE TABLE IF NOT EXISTS watch_runs (
            id           INTEGER PRIMARY KEY AUTOINCREMENT,
            source       TEXT NOT NULL,
            started_at   TEXT NOT NULL,
            duration_ms  INTEGER NOT NULL DEFAULT 0,
            fetched      INTEGER NOT NULL DEFAULT 0,
            new          INTEGER NOT NULL DEFAULT 0,
            matched      INTEGER NOT NULL DEFAULT 0,
            http_status  INTEGER,
            not_modified INTEGER NOT NULL DEFAULT 0,
            error        TEXT,
            replay       INTEGER NOT NULL DEFAULT 0
        );
        CREATE INDEX IF NOT EXISTS idx_watch_runs_started ON watch_runs(started_at);
        CREATE INDEX IF NOT EXISTS idx_watch_runs_source ON watch_runs(source, id)
        """,
    ),
    (
        # AUTH-LOGIN (SPEC_ERRATA E-AUTH-1): panel users, server-side sessions, personal access tokens. Additive and
        # independent of every other table (IF NOT EXISTS: hydra.auth.ensure_schema runs the same DDL, so the
        # tables exist even when another branch's migration number was applied first).
        7,
        """
        CREATE TABLE IF NOT EXISTS users (
            id                   INTEGER PRIMARY KEY AUTOINCREMENT,
            username             TEXT NOT NULL UNIQUE COLLATE NOCASE,
            password_hash        TEXT NOT NULL,           -- scrypt$N$r$p$salt$hash (hydra.auth)
            role                 TEXT NOT NULL CHECK (role IN ('admin','analyst','viewer')),
            created_at           TEXT NOT NULL,
            last_login           TEXT,
            failed_attempts      INTEGER NOT NULL DEFAULT 0,
            locked_until         TEXT,
            must_change_password INTEGER NOT NULL DEFAULT 0,
            disabled             INTEGER NOT NULL DEFAULT 0,
            password_changed_at  TEXT
        );
        CREATE TABLE IF NOT EXISTS auth_sessions (
            id_hash      TEXT PRIMARY KEY,                -- sha256 of the cookie value (the value is never stored)
            user_id      INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            csrf_token   TEXT NOT NULL,
            created_at   TEXT NOT NULL,
            last_seen    TEXT NOT NULL,
            expires_at   TEXT NOT NULL,                   -- absolute limit
            idle_s       INTEGER NOT NULL,
            remember     INTEGER NOT NULL DEFAULT 0
        );
        CREATE INDEX IF NOT EXISTS idx_auth_sessions_user ON auth_sessions(user_id);
        CREATE TABLE IF NOT EXISTS api_tokens (
            id           INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id      INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            name         TEXT NOT NULL,
            prefix       TEXT NOT NULL,                   -- first characters, shown in the UI
            token_hash   TEXT NOT NULL UNIQUE,            -- sha256 of the token (the token is shown once)
            created_at   TEXT NOT NULL,
            last_used_at TEXT,
            expires_at   TEXT,
            revoked_at   TEXT
        );
        CREATE INDEX IF NOT EXISTS idx_api_tokens_user ON api_tokens(user_id)
        """,
    ),
    (
        # META-COLLECT (2026-11-22, architect-approved, docs/integrations/META_COLLECTION.md): Meta Ad Library
        # observations, revisions, durable analysis outbox and results (hydra/meta). Additive and self-contained
        # (no foreign keys into v2 tables: incident / run / campaign ids are plain text references). Every row
        # carries `mode` (live | recorded | synthetic) so replays never overwrite live data. IF NOT EXISTS:
        # hydra.meta.store.ensure_schema runs the same DDL on databases that are already past version 8.
        8,
        """
        CREATE TABLE IF NOT EXISTS meta_state (
            key        TEXT PRIMARY KEY,              -- config | collector | diagnostics | budget
            value      TEXT NOT NULL DEFAULT '{}',    -- JSON, never a secret
            updated_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS meta_queries (
            id             TEXT PRIMARY KEY,          -- q-<sha12(kind|value)>
            kind           TEXT NOT NULL,             -- keyword | page_ids
            value          TEXT NOT NULL,             -- operator keyword (<= 100 chars) or <= 10 page ids
            label          TEXT,
            origin         TEXT NOT NULL DEFAULT 'manual', -- manual | scheduler | expansion
            enabled        INTEGER NOT NULL DEFAULT 1,
            config_version INTEGER NOT NULL DEFAULT 1,
            depth          INTEGER NOT NULL DEFAULT 0,  -- expansion depth (P1)
            parent_ad_id   TEXT,                        -- expansion provenance (P1)
            cursor         TEXT NOT NULL DEFAULT '{}',  -- JSON per mode: {after, page, date_min, digest}
            last_run       TEXT,
            last_ok        TEXT,
            last_status    TEXT,
            last_error     TEXT,
            created_at     TEXT NOT NULL,
            updated_at     TEXT NOT NULL,
            UNIQUE (kind, value)
        );
        CREATE TABLE IF NOT EXISTS meta_polls (
            id            INTEGER PRIMARY KEY AUTOINCREMENT,
            mode          TEXT NOT NULL,
            query_id      TEXT,
            trigger       TEXT NOT NULL,              -- schedule | poll_now | scheduler | diagnose | expansion
            started_at    TEXT NOT NULL,
            finished_at   TEXT,
            requests      INTEGER NOT NULL DEFAULT 0,
            pages         INTEGER NOT NULL DEFAULT 0,
            records       INTEGER NOT NULL DEFAULT 0,
            new_ads       INTEGER NOT NULL DEFAULT 0,
            new_revisions INTEGER NOT NULL DEFAULT 0,
            status        TEXT NOT NULL,              -- ok | partial | empty | deferred_budget | rate_limited | ...
            reason        TEXT,
            usage         TEXT,                       -- usage headers JSON (no secrets), NULL = not sent
            cursor_before TEXT,
            cursor_after  TEXT
        );
        CREATE INDEX IF NOT EXISTS idx_meta_polls_mode ON meta_polls(mode, id);
        CREATE TABLE IF NOT EXISTS meta_ads (
            mode              TEXT NOT NULL,          -- live | recorded | synthetic
            ad_id             TEXT NOT NULL,          -- source_ad_id (Ad Library id, always a string)
            page_id           TEXT,
            page_name         TEXT,
            first_observed_at TEXT NOT NULL,          -- HYDRA observation time, not the delivery date
            last_observed_at  TEXT NOT NULL,
            seen_count        INTEGER NOT NULL DEFAULT 1,
            current_rev_id    INTEGER,
            revisions         INTEGER NOT NULL DEFAULT 0,
            PRIMARY KEY (mode, ad_id)
        );
        CREATE INDEX IF NOT EXISTS idx_meta_ads_last ON meta_ads(mode, last_observed_at);
        CREATE TABLE IF NOT EXISTS meta_revisions (
            id                INTEGER PRIMARY KEY AUTOINCREMENT,
            mode              TEXT NOT NULL,
            ad_id             TEXT NOT NULL,
            rev               INTEGER NOT NULL,
            content_sha256    TEXT NOT NULL,          -- normalized_content_sha256 (no observation times)
            content           TEXT NOT NULL,          -- normalized, redacted JSON
            graph_api_version TEXT,
            countries         TEXT NOT NULL DEFAULT '[]',
            observed_at       TEXT NOT NULL,
            collection_status TEXT NOT NULL,          -- metadata_available | asset_* | budget_deferred | ...
            collection_reason TEXT,
            UNIQUE (mode, ad_id, content_sha256)
        );
        CREATE TABLE IF NOT EXISTS meta_ad_queries (
            mode           TEXT NOT NULL,
            ad_id          TEXT NOT NULL,
            query_id       TEXT NOT NULL,
            config_version INTEGER NOT NULL DEFAULT 1,
            first_at       TEXT NOT NULL,
            last_at        TEXT NOT NULL,
            hits           INTEGER NOT NULL DEFAULT 1,
            PRIMARY KEY (mode, ad_id, query_id)
        );
        CREATE TABLE IF NOT EXISTS meta_observations (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            poll_id     INTEGER NOT NULL,
            mode        TEXT NOT NULL,
            ad_id       TEXT NOT NULL,
            query_id    TEXT,
            revision_id INTEGER NOT NULL,
            page        INTEGER NOT NULL DEFAULT 0,
            observed_at TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_meta_obs_ad ON meta_observations(mode, ad_id);
        CREATE TABLE IF NOT EXISTS meta_artifacts (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            revision_id INTEGER NOT NULL,
            role        TEXT NOT NULL,                -- ad_creative | ad_video | landing_html | snapshot | ...
            slot        TEXT NOT NULL DEFAULT '',     -- source position (card index, url digest)
            status      TEXT NOT NULL,                -- asset_available | asset_unavailable | ...
            reason      TEXT,
            artifact_id TEXT,                         -- CAS sha256 when stored
            source      TEXT NOT NULL,                -- api_field | snapshot_page | analyst_upload | ...
            url_redacted TEXT,
            mime        TEXT,
            size        INTEGER,
            derived     INTEGER NOT NULL DEFAULT 0,
            fetched_at  TEXT,
            UNIQUE (revision_id, role, slot, source)
        );
        CREATE TABLE IF NOT EXISTS meta_outbox (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            idem_key    TEXT NOT NULL UNIQUE,         -- sha256(mode|ad|revision content|extract version|context)
            mode        TEXT NOT NULL,
            ad_id       TEXT NOT NULL,
            revision_id INTEGER NOT NULL,
            status      TEXT NOT NULL,                -- queued | running | done | partial | failed | deferred
            attempts    INTEGER NOT NULL DEFAULT 0,
            owner       TEXT,
            lease_until TEXT,
            not_before  TEXT,
            last_error  TEXT,
            analysis_id INTEGER,
            created_at  TEXT NOT NULL,
            updated_at  TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_meta_outbox_status ON meta_outbox(status, id);
        CREATE TABLE IF NOT EXISTS meta_analyses (
            id                INTEGER PRIMARY KEY AUTOINCREMENT,
            idem_key          TEXT NOT NULL UNIQUE,
            mode              TEXT NOT NULL,
            ad_id             TEXT NOT NULL,
            revision_id       INTEGER NOT NULL,
            incident_id       TEXT,
            base_run_id       TEXT,
            context_version   TEXT,
            assign_run_id     TEXT,
            status            TEXT NOT NULL,          -- done | partial | failed
            reason            TEXT,
            decision          TEXT,                   -- assign | new_campaign | abstain | NULL
            hydra_campaign_id TEXT,
            correlation       TEXT NOT NULL DEFAULT '{}',
            triage            TEXT NOT NULL DEFAULT '{}',
            manipulation      TEXT NOT NULL DEFAULT '[]',
            triage_priority   TEXT NOT NULL DEFAULT 'unknown',
            review_status     TEXT NOT NULL DEFAULT 'unreviewed',
            report_id         TEXT,
            created_at        TEXT NOT NULL,
            updated_at        TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_meta_analyses_ad ON meta_analyses(mode, ad_id);
        CREATE TABLE IF NOT EXISTS meta_reviews (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            analysis_id INTEGER NOT NULL,
            decision    TEXT NOT NULL,
            analyst     TEXT NOT NULL,
            note        TEXT,
            created_at  TEXT NOT NULL
        )
        """,
    ),
]


def _busy_ms() -> int:
    try:
        return max(0, int(os.environ.get("HYDRA_DB_BUSY_MS") or 5000))
    except ValueError:
        return 5000


CONNECTED_STATE_DIRS: set[Path] = set()  # state dirs opened by this process (hydra.vis_geom finds CAS blobs there)


def connect(path: Path, readonly: bool = False) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    CONNECTED_STATE_DIRS.add(path.parent)
    if readonly:
        conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True, check_same_thread=False)
    else:
        conn = sqlite3.connect(path, check_same_thread=False, isolation_level=None)
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    # HYDRA_DB_BUSY_MS (default 5000): lock wait; raise it when two batch jobs share one state dir (a run persist
    # holds the write lock for tens of seconds on corpora of thousands of incidents)
    conn.execute(f"PRAGMA busy_timeout={_busy_ms()}")
    return conn


def schema_version(conn: sqlite3.Connection) -> int:
    return conn.execute("PRAGMA user_version").fetchone()[0]


def migrate(conn: sqlite3.Connection) -> int:
    current = schema_version(conn)
    for version, sql in MIGRATIONS:
        if version > current:
            conn.execute("BEGIN IMMEDIATE")
            try:
                for statement in _split(sql):
                    conn.execute(statement)
                conn.execute(f"PRAGMA user_version={version}")
                conn.execute("COMMIT")
            except BaseException:
                if conn.in_transaction:
                    conn.execute("ROLLBACK")
                raise
            current = version
    return current


def _split(sql: str) -> list[str]:
    # executescript() would auto-commit; run statements one by one inside our transaction.
    return [s.strip() for s in sql.split(";") if s.strip()]


_STOP = object()


class Writer:
    """Single writer thread. `submit(fn)` → Future with fn's return value."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self._q: queue.Queue[Any] = queue.Queue()
        self._ready = threading.Event()
        self._init_error: BaseException | None = None
        # Called on the writer thread after every successful COMMIT (ADR-0009: wakes the live stream). Listeners
        # must be O(1) and never block: set an event, enqueue a token. Exceptions are swallowed.
        self._listeners: list[Callable[[], None]] = []
        self._thread = threading.Thread(target=self._run, name="hydra-db-writer", daemon=True)
        self._thread.start()
        self._ready.wait()
        if self._init_error is not None:
            self._thread.join(timeout=10)
            raise self._init_error

    def _run(self) -> None:
        try:
            conn = connect(self.path)
            migrate(conn)
        except BaseException as exc:  # noqa: BLE001 — re-raised in __init__
            self._init_error = exc
            self._ready.set()
            return
        self._ready.set()
        while True:
            item = self._q.get()
            if item is _STOP:
                conn.close()
                return
            fn, fut = item
            if not fut.set_running_or_notify_cancel():
                continue
            try:
                conn.execute("BEGIN IMMEDIATE")
                result = fn(conn)
                conn.execute("COMMIT")
                fut.set_result(result)
                for listener in self._listeners:
                    try:
                        listener()
                    except Exception:  # noqa: BLE001 — a listener must never take down the only writer
                        pass
            except BaseException as exc:  # noqa: BLE001 — surfaced through the future
                # fn may have committed, or SQLite may have rolled back already (e.g. disk full).
                # An unguarded ROLLBACK would raise and kill the only writer thread.
                try:
                    if conn.in_transaction:
                        conn.execute("ROLLBACK")
                except sqlite3.Error:
                    pass
                fut.set_exception(exc)

    def submit(self, fn: Callable[[sqlite3.Connection], T]) -> Future[T]:
        if threading.current_thread() is self._thread:
            # Waiting on our own queue from inside a transaction would deadlock forever.
            raise RuntimeError("Writer.submit called from the writer thread; use the conn you were given")
        if not self._thread.is_alive():
            raise RuntimeError("database writer thread is not running")
        fut: Future[T] = Future()
        self._q.put((fn, fut))
        return fut

    def add_listener(self, fn: Callable[[], None]) -> None:
        """Register a post-commit callback (see `_listeners`)."""
        self._listeners.append(fn)

    def remove_listener(self, fn: Callable[[], None]) -> None:
        if fn in self._listeners:
            self._listeners.remove(fn)

    def run(self, fn: Callable[[sqlite3.Connection], T], timeout: float | None = 60) -> T:
        return self.submit(fn).result(timeout=timeout)

    def close(self) -> None:
        self._q.put(_STOP)
        self._thread.join(timeout=10)
