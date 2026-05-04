"""Server settings, sourced from environment variables.

Kept deliberately minimal — every setting is overridable via env so
12-factor deploy is straightforward, with sensible defaults for local
dev. Settings are immutable after process start; reload requires
restart (matches how Docker / systemd unit files cycle).
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class Settings:
    # Storage
    database_url: str = "sqlite:///./.evalguard/server.db"
    # Auth — when empty, the server runs in ``open`` mode (dev only).
    # Production deployments must set ``EVALGUARD_API_KEY``.
    api_key: str = ""
    # Default tenancy — every ingest gets attributed to this org/project
    # if the request doesn't carry override headers (Phase 2.5 wiring).
    default_org_slug: str = "default"
    default_project_slug: str = "default"
    # CORS — comma-separated allowlist; ``*`` means open. Same default
    # as the FastAPI docs page so local UI dev "just works".
    cors_origins: tuple[str, ...] = ("*",)
    # Bind
    bind_host: str = "127.0.0.1"
    bind_port: int = 8787

    @property
    def is_open_mode(self) -> bool:
        """True iff no API key is configured. Open mode is loud:
        startup logs warn, ``GET /v1/health`` advertises it. Suitable
        for local dev only."""
        return not self.api_key

    @property
    def sqlite_path(self) -> Path | None:
        """If ``database_url`` is a sqlite URL, return the filesystem
        path; otherwise None (Postgres support lands later)."""
        url = self.database_url
        if url.startswith("sqlite:///"):
            return Path(url[len("sqlite:///"):])
        if url.startswith("sqlite://"):
            return Path(url[len("sqlite://"):])
        return None


def _split_csv(value: str) -> tuple[str, ...]:
    return tuple(s.strip() for s in value.split(",") if s.strip())


def load_settings() -> Settings:
    """Read settings from the environment.

    Honours the ``EVALGUARD_*`` prefix on every key so a single env
    block governs the whole deployment.
    """
    return Settings(
        database_url=os.environ.get("EVALGUARD_DATABASE_URL", Settings.database_url),
        api_key=os.environ.get("EVALGUARD_API_KEY", ""),
        default_org_slug=os.environ.get("EVALGUARD_DEFAULT_ORG", Settings.default_org_slug),
        default_project_slug=os.environ.get("EVALGUARD_DEFAULT_PROJECT", Settings.default_project_slug),
        cors_origins=_split_csv(os.environ.get("EVALGUARD_CORS_ORIGINS", "*")) or ("*",),
        bind_host=os.environ.get("EVALGUARD_HOST", Settings.bind_host),
        bind_port=int(os.environ.get("EVALGUARD_PORT", Settings.bind_port)),
    )
