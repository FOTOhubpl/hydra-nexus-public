"""Integrations & API (connector registry, secrets, outbound sharing, webhooks).

Everything here is off by default and works offline: inbound lookups and outbound pushes need HYDRA_OFFLINE=0 plus
an explicit per-connector opt-in; export formats are produced locally. Keys live only in `~/.config/hydra/*.env`
(0600) or environment variables (`hydra.integrations.secrets`), never in the state DB, logs, caches or git.
See docs/INTEGRATIONS.md and docs/API.md.
"""
