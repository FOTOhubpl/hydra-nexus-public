"""Runtime settings. Same env vars as scripts/env.sh (HYDRA_*), so shell and Python agree."""

from __future__ import annotations

import os
import platform
from functools import lru_cache
from pathlib import Path

from pydantic import SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

LOOPBACK_HOSTS = {"127.0.0.1", "::1", "localhost"}
CONTAINER_BIND_HOST = "0.0.0.0"  # noqa: S104 — only with HYDRA_IN_CONTAINER=1 (see Settings._loopback_only)


def in_container() -> bool:
    """HYDRA_IN_CONTAINER=1: set by the compose `gui` service (own network namespace, port published on 127.0.0.1)."""
    return os.environ.get("HYDRA_IN_CONTAINER", "").strip() == "1"


def platform_tag() -> str:
    """e.g. darwin-arm64, linux-x86_64 — matches HYDRA_PLATFORM in scripts/env.sh."""
    return f"{platform.system().lower()}-{platform.machine().lower()}"


def _default_workers() -> int:
    cpus = os.cpu_count() or 2
    # Leave headroom for the API process and the single DB writer.
    return max(1, min(cpus - 2, 56))


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="HYDRA_", extra="ignore")

    root: Path = Path.home() / "HydraNexus"
    data_dir: Path | None = None
    models_dir: Path | None = None
    cache_dir: Path | None = None
    state_dir: Path | None = None

    offline: bool = True
    api_host: str = "127.0.0.1"
    api_port: int = 8000

    max_file_mb: int = 200          # per input file
    max_upload_mb: int = 2048       # per multipart request (sum of files)
    max_zip_entries: int = 50_000
    max_zip_ratio: float = 200.0    # uncompressed/compressed — zip-bomb guard
    workers: int = _default_workers()
    work_image_max_side: int = 2048

    enrich: bool = False                 # HYDRA_ENRICH=1: live public-metadata lookups (ADR-007)
    enrich_cache: Path | None = None     # HYDRA_ENRICH_CACHE, default <cache_dir>/enrich (replay source)
    ui_dist: Path | None = None          # HYDRA_UI_DIST: built UI served at / by the API (single port)
    scheduler: bool = False              # HYDRA_SCHEDULER=1: run the watchlist/ad-library scheduler in the API
    scheduler_tick_s: float = 60.0

    # AUTH-LOGIN (docs/SECURITY_AUTH.md). HYDRA_AUTH=1 (default): every /v1/* and /api/* path needs a session cookie
    # or a personal access token. HYDRA_ADMIN_PASSWORD: password of the first `admin` (then no credentials file).
    auth: bool = True
    admin_password: SecretStr | None = None
    session_idle_h: float = 8.0        # HYDRA_SESSION_IDLE_H; "Zapamiętaj mnie" raises it to the absolute limit
    session_abs_h: float = 24.0        # HYDRA_SESSION_ABS_H: absolute session lifetime

    @field_validator("api_host")
    @classmethod
    def _loopback_only(cls, v: str) -> str:
        # Spec §14: the API never listens on a public interface; reach it through the SSH tunnel.
        # One exception: inside a container with its own network namespace (compose service `gui`, profile
        # gui-ports) the API binds 0.0.0.0 so Docker can forward the port it publishes on the host's 127.0.0.1
        # only. That needs HYDRA_IN_CONTAINER=1, set by that service and nowhere else; the Host/Origin guard
        # (hydra.api._LocalOnlyGuard) still rejects every non-local Host header.
        if v == CONTAINER_BIND_HOST and in_container():
            return v
        if v not in LOOPBACK_HOSTS:
            raise ValueError(f"HYDRA_API_HOST must be loopback (0.0.0.0 only with HYDRA_IN_CONTAINER=1), got {v!r}")
        return v

    @model_validator(mode="after")
    def _fill_dirs(self) -> Settings:
        self.data_dir = self.data_dir or self.root / "data"
        self.models_dir = self.models_dir or self.root / "models"
        self.cache_dir = self.cache_dir or self.root / "cache" / platform_tag()
        self.state_dir = self.state_dir or self.root / "state"
        self.enrich_cache = self.enrich_cache or self.cache_dir / "enrich"
        return self

    # Derived locations -------------------------------------------------------
    @property
    def db_path(self) -> Path:
        assert self.state_dir is not None
        return self.state_dir / "hydra.sqlite"

    @property
    def cas_dir(self) -> Path:
        assert self.state_dir is not None
        return self.state_dir / "cas"

    @property
    def work_dir(self) -> Path:
        """Derived working copies (normalised images, frames, audio). Rebuildable, never originals."""
        assert self.state_dir is not None
        return self.state_dir / "work"

    @property
    def admin_credentials_path(self) -> Path:
        """Written once (0600) when the first admin is generated; see hydra.auth.bootstrap_admin."""
        assert self.state_dir is not None
        return self.state_dir / "ADMIN_CREDENTIALS.txt"

    @property
    def tmp_dir(self) -> Path:
        assert self.state_dir is not None
        return self.state_dir / "tmp"

    def ensure_dirs(self) -> None:
        for d in (self.data_dir, self.models_dir, self.cache_dir, self.state_dir,
                  self.cas_dir, self.work_dir, self.tmp_dir):
            assert d is not None
            d.mkdir(parents=True, exist_ok=True)


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
